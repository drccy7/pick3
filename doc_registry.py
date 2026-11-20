"""Which kind of document is this PDF? One ordered registry, one pdfplumber pass, three tiers of evidence.

Tiers (strongest first; a stronger tier always wins over a weaker one, whatever the registry order):
  1. title   - a page-1 title line (first lines, or a large-font line) equals the form's title -> "high"
  2. header  - one page-1 line / table row holds a form's column names                      -> "medium"
  3. words   - vocabulary thresholds over the first pages                                    -> "low"
Within a tier the registry order decides: a container (나의건강기록, which also holds 건강검진 parts) comes
before the kinds it contains, and 보험증권 before 보장분석 (both use insurance vocabulary).
Adding a kind is one entry; a disabled entry (enabled=False) is a placeholder that is never matched.
Text layer only, no Gemini.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Callable

import pdfplumber

try:
    from . import health_checkup, mhr, nhis_claims, prescriptions
    from .coverage_review.service import REPORT_TERMS
except ImportError:
    import health_checkup
    import mhr
    import nhis_claims
    import prescriptions
    from coverage_review.service import REPORT_TERMS

HEAD_LINES = 5  # a title sits in the first lines of page 1 ...
TITLE_SIZE = mhr.MIN_HEADING_SIZE  # ... or is printed larger than table text
CHECKUP_PAGES = 4  # same reach as health_checkup.is_checkup_pdf
COVERAGE_PAGES, COVERAGE_TERMS = 12, 4  # same reach and threshold as coverage_review.service.is_coverage_report


def _squash(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


@dataclass
class Facts:
    """What every tier needs, read once: page-1 title lines and header rows, and the first pages' text."""
    head: list[str] = field(default_factory=list)
    rows: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)

    def text(self, n: int) -> str:
        return _squash(" ".join(self.pages[:n]))


def _facts(pdf) -> Facts:
    f = Facts()
    if not pdf.pages:
        return f
    first = pdf.pages[0]
    lines = first.extract_text_lines(return_chars=True)
    for i, ln in enumerate(lines):
        size = max((c.get("size", 0) for c in ln.get("chars", [])), default=0)
        if i < HEAD_LINES or size >= TITLE_SIZE:
            f.head.append(_squash(ln["text"]))
    f.rows = [_squash(ln["text"]) for ln in lines]
    for t in first.find_tables():  # a header cell may wrap: the joined row still holds every name
        f.rows += ["".join(_squash(c) for c in row) for row in t.extract()[:3]]
    f.pages = [(p.extract_text() or "") for p in pdf.pages[:max(CHECKUP_PAGES, COVERAGE_PAGES)]]
    return f


def _coverage_words(f: Facts) -> bool:
    t = f.text(COVERAGE_PAGES)
    return len({w for w in REPORT_TERMS if _squash(w) in t}) >= COVERAGE_TERMS


def _mhr_sections(pdf) -> list[dict]:
    return [{"section": s["section"], "heading": s["heading"], "pages": s["pages"]} for s in mhr.split_pdf(pdf)]


@dataclass(frozen=True)
class DocType:
    type: str
    label: str
    box: str  # what the app does with it: mhr | medical | checkup | coverage | "" (the user picks)
    title: str = ""  # regex, fullmatch against a whitespace-free title line
    headers: tuple[tuple[str, ...], ...] = ()  # column-name sets; any one set, all names in one row
    words: Callable[[Facts], bool] | None = None
    sections: Callable | None = None  # containers: (pdf) -> [{section, heading, pages}]
    enabled: bool = True

    def match(self, tier: str, f: Facts) -> bool:
        if tier == "title":
            return bool(self.title) and any(re.fullmatch(self.title, ln) for ln in f.head)
        if tier == "header":
            return any(all(h in row for h in hs) for hs in self.headers for row in f.rows)
        return self.words is not None and self.words(f)


REGISTRY: list[DocType] = [
    DocType("mhr", "📱 나의건강기록", "mhr", title=re.escape(mhr.APP_TITLE), sections=_mhr_sections),
    DocType("nhis", "🩺 건강보험 요양급여내역", "medical", title=re.escape(_squash(nhis_claims.TITLE)),
            headers=(("진료개시일", "입내원일수", "요양기관명", "공단부담금"),)),
    DocType("prescription", "💊 처방조제정보", "medical", title=re.escape(prescriptions.TITLE),
            headers=(("진료시작일", "약품명", "성분명"),)),
    DocType("checkup", "🩺 건강검진", "checkup", title=r".{0,8}건강검진결과(?:통보서|보고서|표|지)?",
            words=lambda f: health_checkup.is_checkup_text(f.text(CHECKUP_PAGES))),
    DocType("policy", "📄 보험증권", "", title=r".{0,12}보험(?:가입)?증권", enabled=False),  # before 보장분석
    DocType("coverage", "📋 보장분석", "coverage", title=r".{0,12}보장분석(?:보고서|결과|리포트|표)?",
            words=_coverage_words),
    DocType("receipt", "🧾 진료비 영수증", "medical", title=r"(?:외래|입원)?진료비(?:계산서[·ㆍ]?)?영수증", enabled=False),
    DocType("bill_detail", "🧾 진료비 세부산정내역서", "medical", title=r"진료비세부(?:산정)?내역서?", enabled=False),
]
TIERS = (("title", "high"), ("header", "medium"), ("words", "low"))


def _none(via: str) -> dict:
    return {"type": "", "box": "", "confidence": "none", "via": via, "also": [], "sections": []}


def decide(f: Facts, registry: list[DocType] | None = None) -> tuple[DocType | None, dict]:
    """The winning entry (strongest tier, then registry order) and the result without sections."""
    specs = [d for d in (registry or REGISTRY) if d.enabled]
    hits = {tier: [d for d in specs if d.match(tier, f)] for tier, _ in TIERS}
    for tier, conf in TIERS:
        if hits[tier]:
            win = hits[tier][0]
            also = [d.type for d in specs if d is not win and any(d in h for h in hits.values())]
            return win, {"type": win.type, "box": win.box, "confidence": conf, "via": tier, "also": also,
                         "sections": []}
    return None, _none("")


def classify(raw: bytes) -> dict:
    """{"type", "box", "confidence": high|medium|low|none, "via", "also": [types], "sections": [...]}.
    Encrypted or unreadable PDFs return confidence "none" (open them first)."""
    if raw[:5] != b"%PDF-":
        return _none("not_pdf")
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            win, out = decide(_facts(pdf))
            if win is not None and win.sections is not None:
                out["sections"] = win.sections(pdf)
    except Exception:
        return _none("unreadable")
    return out
