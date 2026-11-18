"""Prescription / dispensing records (처방조제정보) per customer or patient.

Source documents: the 심평원 '내가 먹는 약' PDF (text layer + ruled table) and screenshots of
similar screens. PDFs are parsed deterministically with pdfplumber (MIT); only PDFs without a
usable table and screenshots go to Gemini. Rows go to Supabase table pick3_prescriptions
(docs/SUPABASE_MIGRATION_004_prescriptions.sql).
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import date

import httpx

try:
    from .saved_places import StorageError, _supabase_headers, gemini_model
except ImportError:
    from saved_places import StorageError, _supabase_headers, gemini_model

TABLE = "pick3_prescriptions"
TITLE = "처방조제정보"
KINDS = ("처방", "조제")  # 처방 = issued at a clinic (외래/입원), 조제 = dispensed at a pharmacy


# ---- parsing -----------------------------------------------------------------


def _join_lines(text: str | None, korean: bool) -> str:
    """Undo cell wrapping. Korean names wrap mid-word (join tight); Latin text wraps at spaces."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if korean:
        return "".join(lines)
    out = ""
    for ln in lines:
        if not out or out.endswith(("(", "/", "-")) or ln.startswith((")", "/")):
            out += ln
        else:
            out += " " + ln
    return out.strip()


def _num(text) -> str:
    """Dose fields stay as short numeric text ('1', '0.5', '2')."""
    s = re.sub(r"[^0-9.]", "", str(text or ""))
    return s[:10]


def _iso_date(text) -> str | None:
    m = re.search(r"(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})", str(text or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _kind(text: str, provider: str) -> str:
    t = str(text or "")
    if "조제" in t or ("약국" in provider and "외래" not in t and "입원" not in t):
        return "조제"
    return "처방"


def is_prescription_pdf(raw: bytes) -> bool:
    """True for a 처방조제정보 PDF (checked on the first page's text layer)."""
    if raw[:5] != b"%PDF-":
        return False
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            first = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception:
        return False
    return TITLE in first.replace(" ", "")


def parse_prescription_pdf(raw: bytes) -> tuple[list[dict], list[int]]:
    """Rows from the ruled table of every page. Returns (rows, missing 순번 numbers)."""
    import pdfplumber

    rows: list[dict] = []
    seen_seq: list[int] = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            # Default x-tolerance (3) merges Latin words ('sodiumhydrate'); 2 keeps their spaces.
            for table in page.extract_tables(table_settings={"text_x_tolerance": 2}):
                for cells in table:
                    if not cells or len(cells) < 9:
                        continue
                    seq = str(cells[0] or "").strip()
                    if not seq.isdigit():
                        continue  # header row
                    provider = _join_lines(cells[2], korean=True)
                    visit = _iso_date(cells[1])
                    drug = _join_lines(cells[4], korean=True)
                    if not (provider and visit and drug):
                        continue
                    seen_seq.append(int(seq))
                    rows.append({
                        "visit_date": visit,
                        "provider": provider[:80],
                        "kind": _kind(_join_lines(cells[3], korean=True), provider),
                        "drug_name": drug[:160],
                        "ingredient": _join_lines(cells[5], korean=False)[:160],
                        "dose": _num(cells[6]),
                        "times_per_day": _num(cells[7]),
                        "days": _num(cells[8]),
                    })
    missing = []
    if seen_seq:
        have = set(seen_seq)
        missing = [n for n in range(1, max(seen_seq) + 1) if n not in have]
    return rows, missing


_PROMPT = (
    "이 화면(또는 PDF)은 처방·조제(투약) 내역입니다. JSON으로만 답하세요.\n"
    '형식: {"patient": "환자 이름", "records": [{"date": "YYYY-MM-DD", "provider": "병·의원 또는 약국 이름", '
    '"kind": "처방|조제", "drug_name": "약품명", "ingredient": "성분명", "dose": "1회 투약량", '
    '"times_per_day": "1일 투여횟수", "days": "총 투약일수"}]}\n'
    "- 병·의원(외래·입원)은 처방, 약국(처방조제)은 조제.\n"
    "- patient: 화면에 보이는 환자 이름(선택된 탭이 있으면 그 이름), 없으면 빈 문자열.\n"
    "- 잘려서 날짜·기관·약품명이 확실히 보이지 않는 행은 빼세요. 추측하지 마세요."
)


async def extract_with_gemini(raw: bytes, mime_type: str) -> tuple[str, list[dict]]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent"
    payload = {
        "contents": [{"parts": [
            {"text": _PROMPT},
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(raw).decode("ascii")}},
        ]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
        resp.raise_for_status()
        data = resp.json()
    text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
    parsed = json.loads(text)
    if isinstance(parsed, list):
        parsed = {"patient": "", "records": parsed}
    rows = []
    for r in parsed.get("records") or []:
        if not isinstance(r, dict):
            continue
        provider = str(r.get("provider") or "").strip()
        visit = _iso_date(r.get("date"))
        drug = str(r.get("drug_name") or "").strip()
        if not (provider and visit and drug):
            continue
        rows.append({
            "visit_date": visit, "provider": provider[:80],
            "kind": r.get("kind") if r.get("kind") in KINDS else _kind("", provider),
            "drug_name": drug[:160], "ingredient": str(r.get("ingredient") or "").strip()[:160],
            "dose": _num(r.get("dose")), "times_per_day": _num(r.get("times_per_day")), "days": _num(r.get("days")),
        })
    return str(parsed.get("patient") or "").strip()[:40], rows


def name_from_filename(filename: str | None) -> str:
    """'홍길동 약처방.pdf' -> '홍길동'. Only a leading 2-4 syllable Hangul word followed by a separator."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", os.path.basename(str(filename or "")))
    m = re.match(r"^([가-힣]{2,4})(?:[\s_\-()]|$)", stem.strip())
    if not m:
        return ""
    name = m.group(1)
    # Document words are not names.
    if name in ("처방", "조제", "처방조제", "진료", "의료비", "약처방", "영수증", "내역", "스캔", "문서"):
        return ""
    return name


# ---- storage -------------------------------------------------------------------


def _sb() -> tuple[str, dict]:
    sb = _supabase_headers()
    if not sb:
        raise StorageError("처방 기록은 Supabase 설정이 필요합니다 (SUPABASE_URL/KEY).")
    base_url, headers = sb
    return f"{base_url}/rest/v1/{TABLE}", headers


def _owner(user_id: int | None) -> str:
    return f"eq.{user_id}" if user_id is not None else "is.null"


async def _request(method: str, params: dict, json_body=None, prefer: str = "return=representation"):
    endpoint, headers = _sb()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(method, endpoint, headers={**headers, "Prefer": prefer},
                                        params=params, json=json_body)
    except Exception as exc:
        raise StorageError(f"Supabase 연결 실패: {type(exc).__name__}: {exc}") from exc
    if resp.status_code not in (200, 201):
        raise StorageError(f"Supabase {method} HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.text else []


async def save(rows: list[dict], user_id: int | None, patient: str, source: str) -> tuple[int, int]:
    """Insert; repeats (same patient/date/provider/drug) are ignored. Returns (inserted, duplicates)."""
    if not rows:
        return 0, 0
    body = [{**r, "patient": patient, "user_id": user_id, "source": source} for r in rows]
    # The same drug can appear twice in one document (e.g. two lines on one visit) - keep one.
    uniq = {(b["visit_date"], b["provider"], b["drug_name"]): b for b in body}
    inserted = await _request(
        "POST", {"on_conflict": "user_id,patient,visit_date,provider,drug_name"}, list(uniq.values()),
        prefer="return=representation,resolution=ignore-duplicates",
    )
    return len(inserted or []), len(rows) - len(inserted or [])


async def recent_patient(user_id: int | None) -> str:
    rows = await _request("GET", {"user_id": _owner(user_id), "patient": "neq.", "select": "patient",
                                   "order": "created_at.desc,id.desc", "limit": "1"})
    return (rows[0].get("patient") or "") if rows else ""


async def list_rows(user_id: int | None, patient: str = "", year: int | None = None) -> list[dict]:
    params = {
        "user_id": _owner(user_id),
        "select": "id,patient,visit_date,provider,kind,drug_name,ingredient,dose,times_per_day,days,source,created_at",
        "order": "visit_date.desc,id.asc",
        "limit": "5000",
    }
    if patient:
        params["patient"] = f"eq.{patient}"
    if year:
        params["and"] = f"(visit_date.gte.{year}-01-01,visit_date.lte.{year}-12-31)"
    return await _request("GET", params)


async def delete_one(record_id: int, user_id: int | None) -> bool:
    return bool(await _request("DELETE", {"id": f"eq.{record_id}", "user_id": _owner(user_id)}))


async def delete_patient(user_id: int | None, patient: str) -> int:
    return len(await _request("DELETE", {"user_id": _owner(user_id), "patient": f"eq.{patient}", "select": "id"}))


async def set_patient(ids: list[int], user_id: int | None, patient: str) -> list[dict]:
    clean = sorted({int(i) for i in ids})
    if not clean:
        return []
    try:
        return await _request("PATCH", {"id": f"in.({','.join(map(str, clean))})", "user_id": _owner(user_id)},
                              {"patient": patient.strip()[:40]})
    except StorageError as exc:
        if "23505" in str(exc) or "409" in str(exc):
            raise ValueError("바꾸려는 이름에 같은 날짜·기관·약품 기록이 이미 있습니다.") from exc
        raise
