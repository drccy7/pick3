"""Document classification, phase 2: Gemini reads masked page text and splits the PDF into typed parts.

Runs in SHADOW mode next to doc_registry (rules): compare(raw) returns both results and agreement flags,
nothing acts on the AI answer yet. Catalog-driven: a kind is one CATALOG entry (key, name, description,
box, a few example signals); the prompt and the response enum are built from it.

Privacy: only page text is sent (first MAX_PAGES pages, PAGE_CHARS each), after mask(): resident numbers,
phones, e-mails, long digit runs, labeled names (성명: …) and addresses. Unlabeled names in free text
cannot be masked reliably - that is why only a short head of each page is sent. Image-only pages
(no text layer) are marked needs_image and never sent (no images in this phase).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass

import httpx
import pdfplumber

try:
    from . import doc_registry, gemini_usage
    from .coverage_review import gemini, pdf_io
except ImportError:
    import doc_registry
    import gemini_usage
    from coverage_review import gemini, pdf_io

MAX_PAGES = 30  # pages read and sent (a longer PDF: the rest is "not_sent")
PAGE_CHARS = 1500  # per page, after masking
MIN_TEXT = 20  # fewer non-space characters than this = an image-only page
FEATURE = "doc_ai"
UNKNOWN = "unknown"


def model() -> str:
    return os.getenv("PICK3_DOC_AI_MODEL", "").strip() or "gemini-3.5-flash"


def thinking() -> str:
    return os.getenv("PICK3_DOC_AI_THINKING", "").strip() or "low"


# ---- catalog ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Kind:
    key: str
    name: str
    desc: str
    box: str  # same boxes as doc_registry: mhr | medical | checkup | coverage | restaurant | "" (the user picks)
    signals: tuple[str, ...] = ()  # short title lines / header names, never personal data
    media: tuple[str, ...] = ("pdf",)  # pdf, image
    part_of: str = ""  # a part of a container kind (its segments make the document that container)
    section: str = ""  # the container's section key (doc_registry sections)
    container: bool = False  # never a segment type itself; derived from its parts


CATALOG: list[Kind] = [
    Kind("mhr", "나의건강기록", "건강보험공단 '나의건강기록' 앱에서 내보낸 PDF(아래 구간들을 담는 묶음)", "mhr",
         ("첫 쪽 제목 '나의건강기록'",), container=True),
    Kind("mhr_checkup", "나의건강기록 · 건강검진 구간", "나의건강기록 PDF 안의 검진 결과 구간", "mhr",
         ("구간 제목 '건강검진(일반)', '암검진(…)', '구강검진'",), part_of="mhr", section="checkup"),
    Kind("mhr_visits", "나의건강기록 · 진료이력 구간", "나의건강기록 PDF 안의 병원 진료 목록 구간", "mhr",
         ("구간 제목 '진료이력'",), part_of="mhr", section="visits"),
    Kind("mhr_meds", "나의건강기록 · 투약이력 구간", "나의건강기록 PDF 안의 약 처방·조제 목록 구간", "mhr",
         ("구간 제목 '투약이력'",), part_of="mhr", section="meds"),
    Kind("nhis", "건강보험 요양급여내역", "공단에서 발급한 연도별 진료 내역(요양기관, 진료일, 공단부담금)", "medical",
         ("제목 '건강보험 요양급여내역'", "머리글 '진료개시일 · 입내원일수 · 요양기관명 · 공단부담금'")),
    Kind("prescription", "처방조제정보", "약 처방·조제 내역(약품명, 성분명, 투약일수)", "medical",
         ("제목 '처방조제정보'", "머리글 '진료시작일 · 약품명 · 성분명'", "'내가 먹는 약' 화면"), media=("pdf", "image")),
    Kind("checkup", "건강검진 결과", "나의건강기록이 아닌 단독 검진 결과 통보서·보고서", "checkup",
         ("제목 '…건강검진결과통보서'", "'종합판정', '정상A/정상B/질환의심'")),
    Kind("policy", "보험증권", "보험 계약 한 건의 증권(계약 내용·담보·보험료)", "",
         ("제목 '…보험증권' / '보험가입증권'",)),
    Kind("coverage", "보장분석", "여러 보험 계약을 모아 담보별로 정리한 보장분석 보고서", "coverage",
         ("제목 '보장분석 보고서'", "'보유계약 현황', '담보명 · 가입금액'")),
    Kind("receipt", "진료비 영수증", "병원·약국에서 받은 진료비(계산서)·영수증", "medical",
         ("제목 '진료비 계산서·영수증'", "'환자부담총액', '급여 · 비급여'")),
    Kind("bill_detail", "진료비 세부산정내역서", "진료비 항목별 세부 내역", "medical",
         ("제목 '진료비 세부산정내역서'", "'코드 · 명칭 · 단가 · 횟수'")),
    Kind("medical_payment", "의료비 결제 내역", "카드·앱 화면의 병원·약국 결제 금액 목록", "medical", media=("image",)),
    Kind("restaurant", "맛집", "음식점·카페를 소개하는 게시물, 리뷰, 지도, 메뉴 화면", "restaurant", media=("image",)),
]
BY_KEY = {k.key: k for k in CATALOG}


def segment_types(media: str = "pdf") -> list[str]:
    return [k.key for k in CATALOG if media in k.media and not k.container] + [UNKNOWN]


# ---- masking ---------------------------------------------------------------------------------
_NAME_LABEL = (r"(?:성\s*명|이\s*름|환\s*자\s*명?|수\s*진\s*자|피\s*보\s*험\s*자|계\s*약\s*자|보\s*호\s*자|대\s*상\s*자"
               r"|의\s*사\s*명?|담\s*당\s*의|설\s*계\s*사\s*명?)")  # people named on the form, staff included
_NOT_NAMES = {"성별", "나이", "연령", "정보", "동일", "본인", "구분", "기준", "부담", "관계", "연락처", "피보험자", "계약자",
              "수익자", "보험료", "주민번호", "등록번호"}  # header words that follow a name label
_MASKS: list[tuple[re.Pattern, object]] = [
    # 주민·외국인등록번호, also partly shown (900101-1******, 900101-1)
    (re.compile(r"(?<![\d*])\d{6}\s*-\s*[1-8](?:[\d*]{6})?(?![\d*])"), "######-#######"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[메일]"),
    (re.compile(r"(?<![\d*])(?:\+82[-\s]?)?0\d{1,2}[-.)]\s?\d{3,4}[-.]\d{4}(?![\d*])"), "[전화]"),
    # any run of >= 10 digits (dashes or * allowed): card, account, phone without dashes, issue numbers.
    # Dates (8 digits) and comma amounts are kept.
    (re.compile(r"(?<![\d*])\d(?:-?[\d*]){9,}(?![\d*])"), "[번호]"),
    (re.compile(r"(주\s*소)\s*[:：]?\s*[^\n]+"), r"\1 [주소]"),
    (re.compile(rf"({_NAME_LABEL})(\s*[:：]\s*|\s+)([가-힣]{{2,4}})(?![가-힣])"),  # a separator is required
     lambda m: m.group(0) if m.group(3) in _NOT_NAMES else f"{m.group(1)}{m.group(2)}[이름]"),
]


def mask(text: str) -> str:
    for rx, rep in _MASKS:
        text = rx.sub(rep, text)
    return text


# ---- pages -----------------------------------------------------------------------------------
def page_texts(raw: bytes, max_pages: int = MAX_PAGES) -> tuple[int, list[str]]:
    """(total pages, raw text of the first max_pages pages)."""
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        return len(pdf.pages), [(p.extract_text() or "") for p in pdf.pages[:max_pages]]


def prepare(texts: list[str], page_chars: int = PAGE_CHARS) -> tuple[list[str], list[int]]:
    """Masked, capped text per page and the 1-based pages without a text layer."""
    out, images = [], []
    for no, t in enumerate(texts, 1):
        if len(re.sub(r"\s+", "", t)) < MIN_TEXT:
            images.append(no)
            out.append("")
        else:
            out.append(mask(t)[:page_chars])
    return out, images


# ---- prompt + schema ---------------------------------------------------------------------------
def schema(media: str = "pdf") -> dict:
    return {"type": "OBJECT", "properties": {"segments": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "type": {"type": "STRING", "enum": segment_types(media)},
            "page_from": {"type": "INTEGER"},
            "page_to": {"type": "INTEGER"},
            "start_hint": {"type": "STRING", "description": "구간이 시작하는 제목 줄(쪽 중간에서 시작할 때)"},
            "confidence": {"type": "NUMBER", "description": "0~1"},
            "evidence": {"type": "STRING", "description": "판단 근거 원문 30자 이내"},
        },
        "required": ["type", "page_from", "page_to", "confidence", "evidence"],
        "propertyOrdering": ["type", "page_from", "page_to", "start_hint", "confidence", "evidence"],
    }}}, "required": ["segments"]}


def prompt(media: str = "pdf") -> str:
    lines = []
    for key in segment_types(media)[:-1]:
        k = BY_KEY[key]
        sig = f" (예: {'; '.join(k.signals)})" if k.signals else ""
        lines.append(f"- {k.key}: {k.name} — {k.desc}{sig}")
    lines.append(f"- {UNKNOWN}: 목록에 없는 종류이거나 판단할 수 없음")
    return (
        "아래는 PDF 한 개의 쪽별 글자입니다(개인정보는 [이름]·[번호] 등으로 가려져 있습니다). "
        "각 쪽이 어떤 종류의 문서(또는 문서 속 구간)인지 판정해 구간(segments)으로 나누어 JSON으로 답하세요.\n"
        "종류:\n" + "\n".join(lines) + "\n규칙:\n"
        "1. 같은 종류가 이어지는 쪽은 한 구간으로 묶고, 종류가 바뀌는 곳에서 새 구간을 시작합니다.\n"
        "2. 한 쪽 안에서 종류가 바뀌면 두 구간이 그 쪽을 함께 가질 수 있고, start_hint 에 새 구간의 제목 줄을 그대로 적습니다.\n"
        "3. '글자 없음' 쪽은 앞뒤 문맥으로 판단하되 확신이 없으면 unknown 으로 둡니다.\n"
        "4. 모든 쪽이 어느 구간에든 들어가야 합니다. 쪽 번호는 1부터입니다.\n"
        "5. evidence 는 판단 근거가 된 짧은 원문(30자 이내)이며 개인정보는 적지 않습니다.\n"
        "6. confidence 는 0~1 (제목이 그대로 보이면 0.9 이상, 낱말로만 짐작하면 0.6 이하)."
    )


def pages_block(pages: list[str], total: int) -> str:
    parts = [f"=== 쪽 {no} ===\n{t if t else '(글자 없음: 이미지 쪽)'}" for no, t in enumerate(pages, 1)]
    if total > len(pages):
        parts.append(f"(이하 {total - len(pages)}쪽 생략)")
    return "\n\n".join(parts)


# ---- validation ------------------------------------------------------------------------------
def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def clean_segments(raw_segments, n_pages: int, media: str = "pdf") -> list[dict]:
    """Enforce the enum (anything else -> unknown), clamp pages to 1..n_pages, order by page, merge touching
    parts of the same type, and trim overlaps: neighbours may share only one boundary page."""
    allowed = set(segment_types(media))
    segs = []
    for s in raw_segments if isinstance(raw_segments, list) else []:
        if not isinstance(s, dict):
            continue
        a, b = _int(s.get("page_from")), _int(s.get("page_to"))
        if a is None and b is None or n_pages < 1:
            continue
        a, b = a if a is not None else b, b if b is not None else a
        a, b = sorted((min(max(a, 1), n_pages), min(max(b, 1), n_pages)))
        t = str(s.get("type") or "").strip()
        try:
            conf = min(max(float(s.get("confidence") or 0), 0.0), 1.0)
        except (TypeError, ValueError):
            conf = 0.0
        segs.append({"type": t if t in allowed else UNKNOWN, "page_from": a, "page_to": b,
                     "start_hint": str(s.get("start_hint") or "")[:60], "confidence": round(conf, 2),
                     "evidence": str(s.get("evidence") or "")[:80]})
    segs.sort(key=lambda s: (s["page_from"], s["page_to"]))
    out: list[dict] = []
    for s in segs:
        prev = out[-1] if out else None
        if prev and s["type"] == prev["type"] and s["page_from"] <= prev["page_to"] + 1:
            prev["page_to"] = max(prev["page_to"], s["page_to"])
            prev["confidence"] = min(prev["confidence"], s["confidence"])
            continue
        if prev and s["page_from"] < prev["page_to"]:
            s["page_from"] = prev["page_to"]  # share at most the boundary page
            if s["page_from"] > s["page_to"]:
                continue
        out.append(s)
    return out


def summarize(segs: list[dict], n_pages: int, images: list[int]) -> dict:
    """Document-level answer from the segments: a container if any part belongs to one, else the kind
    covering the most pages (earliest wins a tie)."""
    known = [s for s in segs if s["type"] != UNKNOWN]
    covered = {p for s in segs for p in range(s["page_from"], s["page_to"] + 1)}
    base = {"type": "", "box": "", "confidence": 0.0, "also": [], "sections": [],
            "uncovered": [p for p in range(1, n_pages + 1) if p not in covered and p not in images]}
    if not known:
        return base
    top_of = lambda s: BY_KEY[s["type"]].part_of or s["type"]  # noqa: E731
    pages: dict[str, int] = {}
    for s in known:
        pages[top_of(s)] = pages.get(top_of(s), 0) + s["page_to"] - s["page_from"] + 1
    containers = [t for t in pages if BY_KEY[t].container]
    top = containers[0] if containers else max(pages, key=lambda t: pages[t])  # dict order = page order
    mine = [s for s in known if top_of(s) == top]
    sections = [{"section": BY_KEY[s["type"]].section, "pages": list(range(s["page_from"], s["page_to"] + 1)),
                 "heading": s["start_hint"]} for s in mine if BY_KEY[s["type"]].section]
    return {**base, "type": top, "box": BY_KEY[top].box, "confidence": max(s["confidence"] for s in mine),
            "also": [t for t in pages if t != top], "sections": sections}


# ---- usage capture (per call, alongside the app's own USAGE_HOOK) -----------------------------
_CALLS: ContextVar[list | None] = ContextVar("doc_ai_calls", default=None)


def _install_hook() -> None:
    prev = gemini.USAGE_HOOK
    if getattr(prev, "_doc_ai", False):
        return

    def hook(feature, model_, data):
        if prev:
            prev(feature, model_, data)
        box = _CALLS.get()
        if box is not None:
            box.append(gemini_usage.row_from_response(feature, model_, data))

    hook._doc_ai = True
    gemini.USAGE_HOOK = hook


def _usage(rows: list) -> dict | None:
    rows = [r for r in rows if r]
    if not rows:
        return None
    tot = {k: sum(r.get(k, 0) for r in rows) for k in ("prompt_tokens", "output_tokens", "thoughts_tokens")}
    try:
        usd = [gemini_usage.cost_usd(r) for r in rows]
        tot["usd"] = None if None in usd else round(sum(usd), 6)
    except Exception:
        tot["usd"] = None
    tot["model"] = rows[-1].get("model", "")
    return tot


# ---- entry points ----------------------------------------------------------------------------
def _result(status: str, **kw) -> dict:
    return {"status": status, "type": "", "box": "", "confidence": 0.0, "also": [], "sections": [],
            "segments": [], "pages": 0, "sent_pages": 0, "needs_image": [], "uncovered": [], "usage": None,
            "model": "", **kw}


def classify_pages(raw: bytes, max_pages: int = MAX_PAGES, page_chars: int = PAGE_CHARS) -> dict:
    """One Gemini call over masked page text. status: ok | needs_image (no text layer at all) | not_pdf |
    encrypted | unreadable | error. Never raises."""
    if raw[:5] != b"%PDF-":
        return _result("not_pdf")
    if pdf_io.is_encrypted(raw):
        return _result("encrypted")
    try:
        total, texts = page_texts(raw, max_pages)
    except Exception:
        return _result("unreadable")
    pages, images = prepare(texts, page_chars)
    common = {"pages": total, "sent_pages": len(pages), "needs_image": images}
    if len(images) == len(pages):
        return _result("needs_image", **common)
    _install_hook()
    calls: list = []
    token = _CALLS.set(calls)
    try:
        answer = gemini.generate_json(model(), [{"text": prompt("pdf")}, {"text": pages_block(pages, total)}],
                                      schema("pdf"), temperature=0.0, feature=FEATURE, thinking=thinking())
    except Exception as exc:
        return _result("error", error=str(exc)[:200], model=model(), **common, usage=_usage(calls))
    finally:
        _CALLS.reset(token)
    segs = clean_segments((answer or {}).get("segments") if isinstance(answer, dict) else None, len(pages))
    return {**_result("ok", **common), **summarize(segs, len(pages), images), "segments": segs,
            "usage": _usage(calls), "model": model()}


def _page_sets(sections: list[dict], limit: int) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for s in sections:
        out.setdefault(s["section"], set()).update(p for p in s.get("pages", []) if p <= limit)
    return out


def agreement(rule: dict, ai: dict) -> dict:
    """Flags for the shadow log. status: agree | disagree | ai_only | rule_only | both_none | ai_failed."""
    r, a = rule.get("type") or "", ai.get("type") or ""
    flags = {"same_type": r == a, "same_box": (rule.get("box") or "") == (ai.get("box") or ""),
             "same_sections": None, "same_pages": None}
    if ai.get("status") != "ok":
        flags["status"] = "ai_failed"
        return flags
    if r and a and r == a and (rule.get("sections") or ai.get("sections")):
        rs, as_ = _page_sets(rule.get("sections") or [], ai.get("sent_pages") or 0), _page_sets(ai["sections"], 10**6)
        flags["same_sections"] = set(rs) == set(as_)
        flags["same_pages"] = rs == as_
    flags["status"] = ("both_none" if not r and not a else "ai_only" if not r else "rule_only" if not a
                       else "agree" if r == a and flags["same_pages"] is not False else "disagree")
    return flags


def compare(raw: bytes) -> dict:
    """{"rule": doc_registry.classify, "ai": classify_pages, "agreement": flags}. Never raises."""
    rule = doc_registry.classify(raw)
    ai = classify_pages(raw)
    return {"rule": rule, "ai": ai, "agreement": agreement(rule, ai)}


# ---- shadow log (table pick3_doc_class_log, see docs/PLAN_doc_classifier_phase2.md) ---------------
LOG_TABLE = "pick3_doc_class_log"


def shadow_enabled() -> bool:
    return os.getenv("PICK3_DOC_AI_SHADOW", "0").strip().lower() in ("1", "true", "on", "yes")


def log_row(rule: dict, ai: dict, flags: dict, raw: bytes = b"", user_id=None, doc_ref: str | None = None) -> dict:
    """One row per document: both answers without evidence quotes (they may hold personal text)."""
    u = ai.get("usage") or {}
    strip = lambda segs: [{k: s[k] for k in ("type", "page_from", "page_to", "confidence")} for s in segs]  # noqa: E731
    return {"user_id": user_id, "doc_ref": doc_ref, "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
            "pages": ai.get("pages") or 0,
            "rule_type": rule.get("type") or "", "rule_box": rule.get("box") or "",
            "rule_confidence": rule.get("confidence") or "", "rule_via": rule.get("via") or "",
            "rule_sections": [{"section": s["section"], "pages": s["pages"]} for s in rule.get("sections") or []],
            "ai_status": ai.get("status") or "", "ai_type": ai.get("type") or "", "ai_box": ai.get("box") or "",
            "ai_confidence": ai.get("confidence") or 0.0, "ai_segments": strip(ai.get("segments") or []),
            "ai_needs_image": ai.get("needs_image") or [], "agreement": flags.get("status") or "",
            "flags": {k: v for k, v in flags.items() if k != "status"},
            "model": u.get("model") or ai.get("model") or "", "prompt_tokens": u.get("prompt_tokens") or 0,
            "output_tokens": (u.get("output_tokens") or 0) + (u.get("thoughts_tokens") or 0), "usd": u.get("usd")}


def shadow(raw: bytes, rule: dict, user_id=None, doc_ref: str | None = None) -> dict | None:
    """Background task body: classify with Gemini, compare with the rule result already computed, log one
    row. Off unless PICK3_DOC_AI_SHADOW=1. Never raises; returns the row (None when off)."""
    if not shadow_enabled():
        return None
    try:
        ai = classify_pages(raw)
        row = log_row(rule, ai, agreement(rule, ai), raw, user_id, doc_ref)
        conf = gemini_usage._conf()
        if conf:
            httpx.post(f"{conf[0]}/rest/v1/{LOG_TABLE}", headers=conf[1], json=row, timeout=10)
        return row
    except Exception:
        return None
