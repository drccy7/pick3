"""Whole pipeline as one call, shared by the CLI and the Pick3 server (sync; the server runs it in a thread)."""

from __future__ import annotations

import io
import os
import re
import threading
from datetime import date
from typing import Callable

import httpx
import pdfplumber

from . import gemini, pdf_io, plan, prompts, render
from .facts import build, load_rules


def browser_enabled() -> bool:
    """Same switch as link_render.browser_enabled (PICK3_BROWSER=on); the CLI always has a browser locally."""
    return os.getenv("PICK3_BROWSER", "off").strip().lower() in ("on", "1", "true", "yes")

DEFAULT_EXTRACT_MODEL = "gemini-3.5-flash"
DEFAULT_ANALYZE_MODEL = "gemini-pro-latest"
_BROWSER = threading.Lock()  # one Chromium at a time on a small server

# Vocabulary every Korean insurance coverage report uses and medical bills / prescriptions don't.
_REPORT_TERMS = ("보장분석", "보장 분석", "가입담보", "가입금액", "담보명", "보험기간", "납입기간", "계약자",
                 "피보험자", "보험료", "보유계약", "보장내역", "만기")


def is_coverage_report(raw: bytes, pages: int = 12) -> bool:
    """Cheap text check on an unencrypted PDF (no Gemini). Image-only PDFs return False."""
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = " ".join((p.extract_text() or "") for p in pdf.pages[:pages])
    except Exception:
        return False
    text = re.sub(r"\s+", "", text)
    hits = {t for t in _REPORT_TERMS if t.replace(" ", "") in text}
    return len(hits) >= 4


def fetch_records(patient: str, user_id: int | None) -> list[dict]:
    """This account's 병의원 rows for the customer name (spaces ignored). Never crosses accounts."""
    from .store import _conf
    url, h = _conf()
    params = {"select": "provider,kind,amount,visit_date,patient", "order": "visit_date", "limit": "5000",
              "patient": f"ilike.*{'*'.join(patient.split())}*" if " " in patient.strip() else f"eq.{patient.strip()}"}
    if user_id is not None:
        params["user_id"] = f"eq.{user_id}"
    r = httpx.get(f"{url}/rest/v1/pick3_medical_records", params=params, timeout=30, headers=h)
    r.raise_for_status()
    return r.json()


Progress = Callable[[dict], None]  # {"stage": 1-4, "label": str, "done": int|None, "total": int|None}


def _extract_once(pdf_raw: bytes, model: str, progress: Progress, label: str) -> dict:
    """Streamed so the caller sees real progress: contracts read so far / contracts the report says it has."""
    progress({"stage": 1, "label": "보고서 올리는 중"})
    up = gemini.upload_pdf(pdf_raw)
    progress({"stage": 1, "label": label})
    seen = {"done": -1, "total": None}

    def on_text(t: str) -> None:
        m = re.search(r'"stated_contract_count"\s*:\s*(\d+)', t)
        total = int(m.group(1)) if m else None
        done = t.count('"insurer"')  # one per contract object (the key exists only there)
        if (done, total) != (seen["done"], seen["total"]):
            seen.update(done=done, total=total)
            progress({"stage": 1, "label": label, "done": done, "total": total})
    try:
        return gemini.generate_json_stream(model, [{"text": prompts.EXTRACT_PROMPT}, gemini.pdf_part(up["uri"])],
                                           prompts.EXTRACT_SCHEMA, on_text, feature="coverage_extract",
                                           thinking=os.getenv("COVERAGE_EXTRACT_THINKING", "minimal"))
    finally:
        gemini.delete_file(up["name"])


def extract(pdf_raw: bytes, model: str | None = None, progress: Progress = lambda p: None) -> dict:
    """Copying tables needs no deep reasoning: thinking is minimal (COVERAGE_EXTRACT_THINKING), which
    removes ~50 s of silent 'thinking' before the first contract appears."""
    model = model or os.getenv("COVERAGE_EXTRACT_MODEL", DEFAULT_EXTRACT_MODEL)
    return _extract_once(pdf_raw, model, progress, "보고서 읽는 중")


def analyze(facts: dict, model: str | None = None) -> dict:
    import json
    model = model or os.getenv("COVERAGE_ANALYZE_MODEL", DEFAULT_ANALYZE_MODEL)
    return gemini.generate_json(model, [{"text": prompts.ANALYZE_PROMPT},
                                        {"text": "facts = " + json.dumps(facts, ensure_ascii=False, default=str)}],
                                prompts.ANALYZE_SCHEMA, temperature=0.2, feature="coverage_analyze")


def check_extract(rep: dict) -> list[str]:
    w = []
    n = len(rep.get("contracts", []))
    stated = rep.get("report", {}).get("stated_contract_count")
    if stated and stated != n:
        w.append(f"계약 수 불일치: 보고서 {stated}건, 추출 {n}건")
    for c in rep.get("contracts", []):
        if not c.get("coverages") and c.get("type") != "자동차":
            w.append(f"계약 {c.get('no')} 담보 없음(보고서에 비어 있을 수 있음)")
    return w


def run(pdf_raw: bytes, *, user_id: int | None, today: date | None = None, patient: str | None = None,
        records: list[dict] | None = None, rep: dict | None = None, analysis: dict | None = None,
        encrypt: bool = True, progress: Progress = lambda p: None) -> dict:
    """Decrypted report PDF → {customer, facts, analysis, plan, warnings, docs: {filename: pdf bytes}}.

    `rep` / `analysis` let the CLI reuse cached steps. Records default to this user's rows for the
    report's customer name (no cross-account matching).
    """
    today = today or date.today()
    rules = load_rules()
    if rep is None:
        rep = extract(pdf_raw, progress=progress)
    warnings = check_extract(rep)
    name = (patient or rep.get("customer", {}).get("name") or "").strip()
    if records is None:
        records = fetch_records(name, user_id) if name else []
    progress({"stage": 2, "label": f"병의원 기록과 대조 중 (계약 {len(rep.get('contracts', []))}건 · 기록 {len(records)}건)"})
    facts = build(rep, records, today, rules)
    if not facts["premium"]["matches_stated"]:
        warnings.append(f"월 보험료 불일치: 보고서 {facts['premium']['stated_monthly']:,}원, "
                        f"추출 합계 {facts['premium']['monthly_sum']:,}원")
    if not records:
        warnings.append(f"'{name}' 이름의 병의원 기록이 없어 보고서만으로 분석함")
    raw_analysis = analysis
    if raw_analysis is None:
        progress({"stage": 3, "label": "분석·제안 작성 중"})
        raw_analysis = analyze(facts)
    clean, w2 = plan.sanitize(dict(raw_analysis), facts)
    warnings += w2
    pp = plan.premium_plan(facts, clean, rules)
    progress({"stage": 4, "label": "문서 만드는 중"})
    who = facts["customer"]["name"] or "고객"
    pages = {f"{who}_보장분석_청구및제안_{today}.pdf": render.html("analysis", facts, clean, pp, warnings),
             f"{who}_맞춤보장제안_{today}.pdf": render.html("proposal", facts, clean, pp, warnings)}
    pw = (facts["customer"].get("birth_date") or "").replace("-", "")
    if browser_enabled():
        with _BROWSER:
            docs = render.to_pdf(pages)
        if encrypt and pw:
            docs = {k: pdf_io.encrypt(v, pw) for k, v in docs.items()}
    else:  # no server browser: deliver the same documents as HTML (phone: 공유 → 프린트 → PDF)
        encrypt = False
        docs = {k[:-4] + ".html": v.encode("utf-8") for k, v in pages.items()}
    return {"customer": facts["customer"], "records": len(records), "extract": rep, "facts": facts,
            "analysis": raw_analysis, "plan": pp, "warnings": warnings, "docs": docs, "html": pages,
            "encrypted_with_birth_date": bool(encrypt and pw)}
