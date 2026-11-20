"""국민건강보험공단 '건강보험 요양급여내역' PDF → per-visit rows with 진단명 (no Gemini; national standard form).

Each visit is printed as two lines (plus a 순번 line between them):
  2026.03.06 1 연세본내과의원 031-831-8910 13,240 0            <- 진료개시일 입내원일수 기관 연락처 공단부담금 (100/100미만)
  외래 1 기타및상세불명의원발성고혈압 I109 5,600 0             <- 구분 요양일수 [상병명 상병코드] 본인부담금 (100/100미만)
Pharmacy lines have no 상병. The form says 상병명 can include 의증/배제상병 - treat as "possible", not confirmed.
Records are matched to existing rows by (date, provider ignoring spaces/brackets): those get the diagnosis
attached; the rest are inserted. The 주민등록번호 on the form is never kept.
"""

from __future__ import annotations

import io
import re
from datetime import date

import httpx
import pdfplumber

try:
    from .medical import MEDICAL_TABLE, _sb, _kind
    from .saved_places import StorageError
except ImportError:
    from medical import MEDICAL_TABLE, _sb, _kind
    from saved_places import StorageError

TITLE = "건강보험 요양급여내역"
_A = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\s+(\d+)\s+(.+?)\s+(?:(\d{2,4}-\d{3,4}-\d{4})\s+)?([\d,]+)\s+([\d,]+)$")
_C = re.compile(r"^(외래|입원|약국)\s+(\d+)\s+(?:(.+?)\s+([A-Z]\d{2,5}))?\s*([\d,]+)\s+([\d,]+)$")


def _text(raw: bytes) -> str:
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def is_nhis_pdf(raw: bytes) -> bool:
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            first = pdf.pages[0].extract_text() or "" if pdf.pages else ""
    except Exception:
        return False
    return TITLE.replace(" ", "") in first.replace(" ", "")


def _int(s: str) -> int:
    return int(s.replace(",", "") or 0)


def parse(raw: bytes) -> dict:
    """{"patient", "period", "rows": [...], "unparsed": n}. Rows carry diagnosis fields; amount = 본인부담 합계."""
    text = _text(raw)
    m = re.search(r"성명\s+(\S+)", text)
    patient = m.group(1) if m else ""
    m = re.search(r"발급기간\s+(\d{4}\.\d{2}\.\d{2})\s*~\s*(\d{4}\.\d{2}\.\d{2})", text)
    period = (m.group(1), m.group(2)) if m else None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    rows, pending, carry, unparsed = [], None, "", 0
    for ln in lines:
        a = _A.match(ln)
        if a:
            if pending:
                unparsed += 1  # previous visit never got its second line
            pending, carry = a, ""
            continue
        if pending is None or re.fullmatch(r"\d+", ln):
            continue  # headers, page footers, 순번
        c = _C.match((carry + " " + ln).strip() if carry else ln)
        if not c:
            carry = (carry + " " + ln).strip() if ln.startswith(("외래", "입원", "약국")) or carry else ""
            continue  # a long 상병명 may wrap: join with the next line
        y, mo, d, vdays, provider, _phone, nhis, nhis2 = pending.groups()
        vtype, cdays, dname, dcode, own, own2 = c.groups()
        rows.append({"visit_date": date(int(y), int(mo), int(d)).isoformat(), "provider": provider.strip()[:80],
                     "visit_type": vtype, "visit_days": int(vdays), "care_days": int(cdays),
                     "diagnosis_name": (dname or "").strip() or None, "diagnosis_code": dcode,
                     "nhis_paid": _int(nhis) + _int(nhis2), "amount": _int(own) + _int(own2),
                     "kind": "약국" if vtype == "약국" else _kind(None, provider)})
        pending, carry = None, ""
    if pending:
        unparsed += 1
    # The form's own summary line: 총건수 외래 입원 약국 기관수 총진료비 공단부담금 본인부담금
    m = re.search(r"^(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)$", text, re.M)
    check = None
    if m:
        total, own = int(m.group(1)), _int(m.group(8))
        got_own = sum(r["amount"] for r in rows)
        check = {"stated_count": total, "stated_own": own, "ok": total == len(rows) and own == got_own}
    return {"patient": patient, "period": period, "rows": rows, "unparsed": unparsed, "check": check}


def _norm(provider: str) -> str:
    return re.sub(r"[\s()\[\]·.\-]", "", provider or "")


_FIELDS = ("diagnosis_code", "diagnosis_name", "visit_type", "visit_days", "care_days", "nhis_paid")


async def save(parsed: dict, user_id, patient: str) -> dict:
    """Attach diagnoses to matching rows (same date + provider) and insert the rest. Returns counts."""
    endpoint, headers = _sb()
    rows = parsed["rows"]
    if not rows:
        return {"linked": 0, "inserted": 0, "skipped": 0}
    dates = sorted(r["visit_date"] for r in rows)
    owner = f"eq.{user_id}" if user_id is not None else "is.null"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(endpoint, headers=headers, params={
            "select": "id,provider,visit_date,amount", "user_id": owner, "patient": f"eq.{patient}",
            "and": f"(visit_date.gte.{dates[0]},visit_date.lte.{dates[-1]})", "limit": "5000"})
        if resp.status_code != 200:
            raise StorageError(f"Supabase select HTTP {resp.status_code}: {resp.text[:200]}")
        existing: dict[tuple, list[int]] = {}
        no_amount: set[int] = set()
        for e in resp.json():
            existing.setdefault((e["visit_date"], _norm(e["provider"])), []).append(e["id"])
            if e.get("amount") is None:
                no_amount.add(e["id"])
        linked, new, skipped = 0, [], 0
        for r in rows:
            ids = existing.get((r["visit_date"], _norm(r["provider"])))
            fields = {k: r[k] for k in _FIELDS}
            if ids:
                p = await client.patch(endpoint, headers=headers, json=fields,
                                       params={"id": f"in.({','.join(map(str, ids))})", "user_id": owner})
                if p.status_code >= 300:
                    raise StorageError(f"Supabase update HTTP {p.status_code}: {p.text[:200]}")
                empty = [i for i in ids if i in no_amount]
                if empty and r["amount"] > 0:  # a visit known only from 나의건강기록: now it has its 본인부담
                    p = await client.patch(endpoint, headers=headers, json={"amount": r["amount"]},
                                           params={"id": f"in.({','.join(map(str, empty))})", "user_id": owner})
                    if p.status_code >= 300:
                        raise StorageError(f"Supabase update HTTP {p.status_code}: {p.text[:200]}")
                linked += 1
            elif r["amount"] > 0:
                new.append({"user_id": user_id, "patient": patient, "provider": r["provider"], "kind": r["kind"],
                            "amount": r["amount"], "visit_date": r["visit_date"], "source": "nhis", **fields})
            else:
                skipped += 1  # 본인부담 0 (e.g. fully covered): nothing to record as an expense
        inserted = 0
        if new:
            ins = await client.post(endpoint, json=new, params={"on_conflict": "user_id,patient,provider,visit_date,amount"},
                                    headers={**headers, "Prefer": "return=representation,resolution=ignore-duplicates"})
            if ins.status_code not in (200, 201):
                raise StorageError(f"Supabase insert HTTP {ins.status_code}: {ins.text[:200]}")
            inserted = len(ins.json() or [])
    return {"linked": linked, "inserted": inserted, "skipped": skipped}


def summary_message(parsed: dict, patient: str, counts: dict) -> str:
    rows = parsed["rows"]
    diag = {}
    for r in rows:
        if r["diagnosis_code"]:
            diag.setdefault((r["diagnosis_code"], r["diagnosis_name"]), 0)
            diag[(r["diagnosis_code"], r["diagnosis_name"])] += 1
    inpatient = sum(1 for r in rows if r["visit_type"] == "입원")
    p = parsed.get("period")
    lines = [f"🩺 건강보험 요양급여내역 · {patient or '이름 없음'} 님" + (f" ({p[0]}~{p[1]})" if p else ""),
             f"진료 {len(rows)}건" + (f" (입원 {inpatient}건)" if inpatient else "") +
             f" · 진단명 연결 {counts['linked']}건 · 새로 저장 {counts['inserted']}건"]
    if diag:
        top = sorted(diag.items(), key=lambda x: -x[1])[:4]
        lines.append("진단명: " + ", ".join(f"{n} {c}({k}회)" for (c, n), k in top))
    chk = parsed.get("check")
    if chk and not chk["ok"]:
        lines.append(f"⚠ 서식 합계({chk['stated_count']}건 · 본인부담 {chk['stated_own']:,}원)와 읽은 값이 다릅니다. 일부 줄을 못 읽었을 수 있습니다.")
    if parsed.get("unparsed"):
        lines.append(f"⚠ 읽지 못한 줄 {parsed['unparsed']}건")
    lines.append("※ 상병명은 의증·배제상병이 포함될 수 있어 확진 진단이 아닐 수 있습니다.")
    return "\n".join(lines)
