"""'나의건강기록' app export (보건복지부·한국보건의료정보원) → sections → Pick3 records. Text layer only, no Gemini.

An export holds one or more parts, each in any order: 건강검진(일반) / 진료이력 / 투약이력 (and possibly other 검진).
Every part starts with the app title and a 12pt heading line; 진료·투약 repeat it on each page, 검진 does not,
and a part can start mid-page (the previous part's footer above it). So:
  - a heading is a whole line equal to a known heading, in a font of at least 11pt (table cells are 7.5-9.8pt);
  - a page without a heading continues the previous part;
  - a heading below the page's first table means the page is shared, and tables are split at the heading.
Columns are found by header names, not positions. 진료이력 has no amounts: those visits are stored with an
empty amount (B안) unless a matching paid record exists, which then just gets the 상병명.
"""

from __future__ import annotations

import io
import re
from datetime import date

import httpx
import pdfplumber

try:
    from .medical import MEDICAL_TABLE, _kind as _medical_kind, _sb
    from .saved_places import StorageError
except ImportError:
    from medical import MEDICAL_TABLE, _kind as _medical_kind, _sb
    from saved_places import StorageError

APP_TITLE = "나의건강기록"
HEADINGS = [  # exact heading line (spaces removed) -> section
    (re.compile(r"^(?:건강|암|구강|영유아)검진(?:\((.+)\))?$"), "checkup"),
    (re.compile(r"^진료이력$"), "visits"),
    (re.compile(r"^투약이력$"), "meds"),
]
LABELS = {"checkup": "🩺 건강검진", "visits": "🏥 진료이력", "meds": "💊 투약이력"}
MIN_HEADING_SIZE = 11.0


def _squash(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def is_mhr_pdf(raw: bytes) -> bool:
    if raw[:5] != b"%PDF-":
        return False
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            lines = (pdf.pages[0].extract_text() or "").splitlines() if pdf.pages else []
    except Exception:
        return False
    return any(_squash(ln) == APP_TITLE for ln in lines[:3])


def _heading(line: dict):
    text = _squash(line["text"])
    size = max((c.get("size", 0) for c in line.get("chars", [])), default=0)
    if size and size < MIN_HEADING_SIZE:
        return None
    for rx, key in HEADINGS:
        if rx.match(text):
            return key, text
    return None


def _split(pdf) -> list[dict]:
    out: list[dict] = []
    for no, page in enumerate(pdf.pages, 1):
        heads = [(ln["top"], *h) for ln in page.extract_text_lines(return_chars=True) if (h := _heading(ln))]
        first_table = min((t.bbox[1] for t in page.find_tables()), default=page.height * 0.15)
        if out and (not heads or heads[0][0] > first_table):
            out[-1]["pages"].append(no)  # no heading, or content above it: the previous part continues
        for top, key, text in heads:
            if out and out[-1]["section"] == key and out[-1]["heading"] == text:
                if no not in out[-1]["pages"]:
                    out[-1]["pages"].append(no)
                continue  # 진료·투약 repeat their heading on every page
            out.append({"section": key, "heading": text, "pages": [no], "start": (no, round(top))})
    for i, s in enumerate(out):  # where the next part starts on a shared last page
        nxt = out[i + 1] if i + 1 < len(out) else None
        s["end"] = nxt["start"] if nxt and nxt["start"][0] == s["pages"][-1] else None
    return out


def split_sections(raw: bytes) -> list[dict]:
    """[{"section", "heading", "pages": [1-based], "start": (page, top), "end": (page, top) | None}]."""
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        return _split(pdf)


def patient_name(raw: bytes) -> str:
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
    m = re.search(r"성\s*명\s*[:：]?\s*([가-힣]{2,10})", text)
    return m.group(1) if m else ""


def _tables(pdf, sec: dict) -> list[list[list]]:
    """The part's tables, cut at its own heading (first page) and at the next part's heading (last page)."""
    out = []
    for no in sec["pages"]:
        for t in pdf.pages[no - 1].find_tables():
            top = t.bbox[1]
            if sec["start"][0] == no and top < sec["start"][1]:
                continue
            if sec.get("end") and sec["end"][0] == no and top >= sec["end"][1]:
                continue
            out.append(t.extract())
    return out


def _header_map(row: list, names: dict[str, str]) -> dict[str, int]:
    cols = {}
    for i, c in enumerate(row):
        key = _squash(c)
        for name, field in names.items():
            if key == name:
                cols[field] = i
    return cols


def _iso(text) -> str | None:
    m = re.search(r"(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})", _squash(text))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _int(text) -> int | None:
    s = re.sub(r"[^\d]", "", str(text or ""))
    return int(s) if s else None


def _num(text) -> str:
    return re.sub(r"[^0-9.]", "", str(text or ""))[:10]


VISIT_COLS = {"진료일자": "date", "진료기관": "provider", "상병명": "dx", "방문일수": "visit_days",
              "투약(요양)일수": "care_days", "진료구분": "type"}
MED_COLS = {"조제일자": "date", "조제기관": "provider", "제품명": "drug", "성분명": "ingredient",
            "1회투약량": "dose", "1일투약횟수": "times", "총투약일수": "days"}


def _visit_type(text: str) -> str:
    t = _squash(text)
    if "입원" in t:
        return "입원"
    if "조제" in t or "약국" in t:
        return "약국"
    return "외래"


def parse_visits(pdf, sec: dict) -> list[dict]:
    """진료이력: a data row, then a row starting with 진료구분 (방문 외래 / 처방 조제 …) and the address."""
    rows, cols = [], {}
    for table in _tables(pdf, sec):
        for r in table:
            h = _header_map(r, VISIT_COLS)
            if "date" in h:
                cols = {**cols, **{k: v for k, v in h.items() if k != "type"}}
                continue
            if "type" in h:  # second header row: 진료구분 sits in the first column of the detail row
                continue
            if not cols:
                continue
            d = _iso(r[cols["date"]]) if cols.get("date") is not None else None
            if d:
                provider = _squash(r[cols["provider"]])[:80] if "provider" in cols else ""
                if not provider:
                    continue
                dx = re.sub(r"\s+", " ", str(r[cols["dx"]] or "")).strip() if "dx" in cols else ""
                rows.append({"visit_date": d, "provider": provider, "diagnosis_name": dx or None,
                             "visit_days": _int(r[cols["visit_days"]]) if "visit_days" in cols else None,
                             "care_days": _int(r[cols["care_days"]]) if "care_days" in cols else None,
                             "visit_type": "외래"})
            elif rows and r and r[0]:  # detail row of the last visit
                rows[-1]["visit_type"] = _visit_type(r[0])
    for v in rows:
        v["kind"] = "약국" if v["visit_type"] == "약국" else _medical_kind(None, v["provider"])
    return rows


def parse_meds(pdf, sec: dict) -> list[dict]:
    """투약이력: one row per drug; date and pharmacy are only on the first drug of each dispensing."""
    rows, cols, cur = [], {}, (None, "")
    for table in _tables(pdf, sec):
        for r in table:
            h = _header_map(r, MED_COLS)
            if "drug" in h:
                cols = h
                continue
            if not cols or "drug" not in cols:
                continue
            d = _iso(r[cols["date"]]) if cols.get("date") is not None else None
            p = _squash(r[cols["provider"]]) if cols.get("provider") is not None else ""
            if d:
                cur = (d, p or cur[1])
            drug = re.sub(r"\s+", " ", str(r[cols["drug"]] or "")).strip()
            if not (cur[0] and cur[1] and drug):
                continue
            rows.append({"visit_date": cur[0], "provider": cur[1][:80], "kind": "조제", "drug_name": drug[:160],
                         "ingredient": re.sub(r"\s+", " ", str(r[cols["ingredient"]] or "")).strip()[:160]
                         if "ingredient" in cols else "",
                         "dose": _num(r[cols["dose"]]) if "dose" in cols else "",
                         "times_per_day": _num(r[cols["times"]]) if "times" in cols else "",
                         "days": _num(r[cols["days"]]) if "days" in cols else ""})
    return rows


def read(raw: bytes) -> dict:
    """{"patient", "sections": [{section, heading, pages}], "visits": [...], "meds": [...]}."""
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        secs = _split(pdf)
        visits = [v for s in secs if s["section"] == "visits" for v in parse_visits(pdf, s)]
        meds = [m for s in secs if s["section"] == "meds" for m in parse_meds(pdf, s)]
    return {"patient": patient_name(raw), "visits": visits, "meds": meds,
            "sections": [{"section": s["section"], "heading": s["heading"], "pages": s["pages"]} for s in secs]}


def section_summary(sections: list[dict]) -> str:
    def pages(p):
        return f"{p[0]}쪽" if len(p) == 1 else f"{p[0]}~{p[-1]}쪽"
    return " · ".join(f"{LABELS.get(s['section'], s['heading'])} {pages(s['pages'])}" for s in sections)


def _norm(provider: str) -> str:
    return re.sub(r"[\s()\[\]·.\-]", "", provider or "")


async def save_visits(rows: list[dict], user_id, patient: str) -> dict:
    """Same date + provider as an existing record: fill its empty diagnosis/visit fields (codes from
    요양급여내역 are never overwritten). Otherwise insert the visit with no amount (source mhr)."""
    if not rows:
        return {"linked": 0, "inserted": 0}
    endpoint, headers = _sb()
    owner = f"eq.{user_id}" if user_id is not None else "is.null"
    dates = sorted(r["visit_date"] for r in rows)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(endpoint, headers=headers, params={
            "select": "id,provider,visit_date,diagnosis_name,visit_type,visit_days,care_days",
            "user_id": owner, "patient": f"eq.{patient}",
            "and": f"(visit_date.gte.{dates[0]},visit_date.lte.{dates[-1]})", "limit": "5000"})
        if resp.status_code != 200:
            raise StorageError(f"Supabase select HTTP {resp.status_code}: {resp.text[:200]}")
        existing: dict[tuple, list[dict]] = {}
        for e in resp.json():
            existing.setdefault((e["visit_date"], _norm(e["provider"])), []).append(e)
        linked, new = 0, []
        for r in rows:
            found = existing.get((r["visit_date"], _norm(r["provider"])))
            if not found:
                new.append({"user_id": user_id, "patient": patient, "provider": r["provider"], "kind": r["kind"],
                            "amount": None, "visit_date": r["visit_date"], "source": "mhr",
                            **{k: r[k] for k in ("diagnosis_name", "visit_type", "visit_days", "care_days")}})
                existing[(r["visit_date"], _norm(r["provider"]))] = [{"id": None}]  # same visit twice: once
                continue
            linked += 1
            for e in found:
                if e.get("id") is None:
                    continue
                fill = {k: r[k] for k in ("diagnosis_name", "visit_type", "visit_days", "care_days")
                        if r.get(k) is not None and e.get(k) is None}
                if fill:
                    p = await client.patch(endpoint, headers=headers, json=fill,
                                           params={"id": f"eq.{e['id']}", "user_id": owner})
                    if p.status_code >= 300:
                        raise StorageError(f"Supabase update HTTP {p.status_code}: {p.text[:200]}")
        inserted = 0
        if new:
            ins = await client.post(endpoint, json=new, headers={**headers, "Prefer": "return=representation"})
            if ins.status_code not in (200, 201):
                raise StorageError(f"Supabase insert HTTP {ins.status_code}: {ins.text[:200]}")
            inserted = len(ins.json() or [])
    return {"linked": linked, "inserted": inserted}
