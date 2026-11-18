"""Medical expense records (병원·약국 결제 내역) from screenshots or PDFs, for insurance claims.

Gemini reads the patient name and each payment row; rows are stored per Pick3 user in
Supabase table pick3_medical_records (docs/SUPABASE_MIGRATION_003_medical.sql).
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import date

import httpx

try:
    from .saved_places import StorageError, _supabase_headers, gemini_model
except ImportError:
    from saved_places import StorageError, _supabase_headers, gemini_model

MEDICAL_TABLE = "pick3_medical_records"
KINDS = ("병원", "약국", "안경", "기타")
MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # Gemini inline limit is ~20 MB per request


def sniff_mime(raw: bytes, declared: str | None) -> str:
    """Trust the bytes over the declared type (Shortcuts may send application/octet-stream)."""
    if raw[:5] == b"%PDF-":
        return "application/pdf"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1", b"ftyphevc"):
        return "image/heic"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return declared or "image/jpeg"


_PROMPT = (
    "이 화면(또는 PDF)은 병원·약국 등 의료비 결제 내역입니다. JSON으로만 답하세요.\n"
    '형식: {"patient": "환자 이름", "records": [{"provider": "기관 이름", '
    '"kind": "병원|약국|안경|기타", "amount": 정수(원), "date": "YYYY-MM-DD"}]}\n'
    "- patient: 화면에 보이는 환자/가족 이름. 이름 탭이 여러 개면 선택(강조)된 탭의 이름. 없으면 빈 문자열.\n"
    "- kind: 의원·병원·치과·한의원·내과 등은 병원, 약국은 약국, 안경점은 안경, 그 밖은 기타.\n"
    "- 화면 끝에서 잘려 기관·금액·날짜 중 하나라도 확실히 보이지 않는 행은 빼세요. 추측하지 마세요.\n"
    "- 결제 내역이 아니면 records를 빈 배열로 두세요."
)


async def extract_medical_records(raw: bytes, mime_type: str) -> dict:
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
    async with httpx.AsyncClient(timeout=60.0) as client:  # multi-page PDFs take longer than a screenshot
        resp = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
        resp.raise_for_status()
        data = resp.json()
    text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
    parsed = json.loads(text)
    if isinstance(parsed, list):
        parsed = {"patient": "", "records": parsed}
    return {
        "patient": str(parsed.get("patient") or "").strip(),
        "records": [r for r in (parsed.get("records") or []) if isinstance(r, dict)],
    }


def _parse_amount(value) -> int | None:
    digits = re.sub(r"[^0-9-]", "", str(value if value is not None else ""))
    try:
        amount = int(digits)
    except ValueError:
        return None
    return amount if amount > 0 else None


def _parse_date(value) -> str | None:
    m = re.search(r"(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})", str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _kind(value, provider: str) -> str:
    if value in KINDS:
        return value
    if "약국" in provider:
        return "약국"
    if "안경" in provider:
        return "안경"
    if re.search(r"의원|병원|치과|한의원|클리닉|내과|외과|소아과|피부과|안과|이비인후과", provider):
        return "병원"
    return "기타"


def normalize_records(extracted: dict, patient_override: str = "") -> tuple[str, list[dict], int]:
    """Clean Gemini rows. Returns (patient, rows, skipped) where skipped = rows missing data."""
    patient = (patient_override or extracted.get("patient") or "").strip()[:40]
    rows, skipped = [], 0
    for r in extracted.get("records", []):
        provider = str(r.get("provider") or "").strip()[:80]
        amount = _parse_amount(r.get("amount"))
        visit_date = _parse_date(r.get("date"))
        if not (provider and amount and visit_date):
            skipped += 1
            continue
        rows.append({"patient": patient, "provider": provider, "kind": _kind(r.get("kind"), provider),
                     "amount": amount, "visit_date": visit_date})
    return patient, rows, skipped


def _sb() -> tuple[str, dict]:
    sb = _supabase_headers()
    if not sb:
        raise StorageError("의료비 기록은 Supabase 설정이 필요합니다 (SUPABASE_URL/KEY).")
    base_url, headers = sb
    return f"{base_url}/rest/v1/{MEDICAL_TABLE}", headers


async def save_medical_records(rows: list[dict], user_id: int | None, source: str) -> tuple[int, int]:
    """Insert rows for one user; exact repeats (same patient/provider/date/amount) are ignored.

    Returns (inserted, duplicates).
    """
    if not rows:
        return 0, 0
    endpoint, headers = _sb()
    body = [{**r, "user_id": user_id, "source": source} for r in rows]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                endpoint,
                headers={**headers, "Prefer": "return=representation,resolution=ignore-duplicates"},
                params={"on_conflict": "user_id,patient,provider,visit_date,amount"},
                json=body,
            )
    except Exception as exc:
        raise StorageError(f"Supabase 연결 실패: {type(exc).__name__}: {exc}") from exc
    if resp.status_code not in (200, 201):
        raise StorageError(f"Supabase insert HTTP {resp.status_code}: {resp.text[:200]}")
    inserted = len(resp.json() or [])
    return inserted, len(rows) - inserted


async def list_medical_records(user_id: int | None, patient: str = "", year: int | None = None) -> list[dict]:
    endpoint, headers = _sb()
    params = {
        "user_id": f"eq.{user_id}" if user_id is not None else "is.null",
        "select": "id,patient,provider,kind,amount,visit_date,source,created_at",
        "order": "visit_date.desc,id.desc",
        "limit": "2000",
    }
    if patient:
        params["patient"] = f"eq.{patient}"
    if year:
        params["and"] = f"(visit_date.gte.{year}-01-01,visit_date.lte.{year}-12-31)"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(endpoint, headers=headers, params=params)
    except Exception as exc:
        raise StorageError(f"Supabase 연결 실패: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise StorageError(f"Supabase select HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def delete_medical_record(record_id: int, user_id: int | None) -> bool:
    endpoint, headers = _sb()
    owner = f"eq.{user_id}" if user_id is not None else "is.null"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.delete(
                endpoint,
                headers={**headers, "Prefer": "return=representation"},
                params={"id": f"eq.{record_id}", "user_id": owner},
            )
    except Exception as exc:
        raise StorageError(f"Supabase 연결 실패: {type(exc).__name__}: {exc}") from exc
    if resp.status_code != 200:
        raise StorageError(f"Supabase delete HTTP {resp.status_code}: {resp.text[:200]}")
    return bool(resp.json())


class DuplicateRecordError(ValueError):
    """The edit would make the row identical to another (same patient/provider/date/amount)."""


def clean_update(fields: dict) -> dict:
    """Validate a partial edit from the web table; raises ValueError with a Korean message."""
    out: dict = {}
    if "patient" in fields and fields["patient"] is not None:
        out["patient"] = str(fields["patient"]).strip()[:40]
    if "provider" in fields and fields["provider"] is not None:
        provider = str(fields["provider"]).strip()[:80]
        if not provider:
            raise ValueError("기관 이름을 입력해 주세요.")
        out["provider"] = provider
    if "kind" in fields and fields["kind"] is not None:
        if fields["kind"] not in KINDS:
            raise ValueError("구분은 병원·약국·안경·기타 중 하나입니다.")
        out["kind"] = fields["kind"]
    if "amount" in fields and fields["amount"] is not None:
        amount = _parse_amount(fields["amount"])
        if not amount:
            raise ValueError("금액은 0보다 큰 숫자여야 합니다.")
        out["amount"] = amount
    if "visit_date" in fields and fields["visit_date"] is not None:
        visit_date = _parse_date(fields["visit_date"])
        if not visit_date:
            raise ValueError("날짜 형식이 올바르지 않습니다.")
        out["visit_date"] = visit_date
    if not out:
        raise ValueError("바꿀 내용이 없습니다.")
    return out


async def update_medical_record(record_id: int, user_id: int | None, changes: dict) -> dict | None:
    """Apply a validated edit to one of the user's rows. None = not found (or not theirs)."""
    endpoint, headers = _sb()
    owner = f"eq.{user_id}" if user_id is not None else "is.null"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.patch(
                endpoint,
                headers={**headers, "Prefer": "return=representation"},
                params={"id": f"eq.{record_id}", "user_id": owner},
                json=changes,
            )
    except Exception as exc:
        raise StorageError(f"Supabase 연결 실패: {type(exc).__name__}: {exc}") from exc
    if resp.status_code == 409 or "23505" in resp.text:
        raise DuplicateRecordError("같은 환자·기관·날짜·금액의 기록이 이미 있습니다.")
    if resp.status_code != 200:
        raise StorageError(f"Supabase update HTTP {resp.status_code}: {resp.text[:200]}")
    rows = resp.json()
    return rows[0] if rows else None


def summarize(rows: list[dict]) -> list[dict]:
    """Totals per (patient, year, kind) for the web table footer."""
    totals: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        key = (r.get("patient") or "", str(r.get("visit_date") or "")[:4], r.get("kind") or "기타")
        t = totals.setdefault(key, {"patient": key[0], "year": key[1], "kind": key[2], "count": 0, "amount": 0})
        t["count"] += 1
        t["amount"] += int(r.get("amount") or 0)
    return sorted(totals.values(), key=lambda t: (t["patient"], t["year"], t["kind"]), reverse=False)
