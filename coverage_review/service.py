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

from . import gemini, pdf_io, pdf_render, plan, prompts, render
from .facts import build, load_rules


def browser_enabled() -> bool:
    """Same switch as link_render.browser_enabled (PICK3_BROWSER=on); the CLI always has a browser locally."""
    return os.getenv("PICK3_BROWSER", "off").strip().lower() in ("on", "1", "true", "yes")


def pdf_engine() -> str:
    """COVERAGE_PDF_ENGINE: reportlab (default, pure Python) | browser (Chromium, needs PICK3_BROWSER=on) | html.
    'browser' without a browser falls back to reportlab."""
    e = os.getenv("COVERAGE_PDF_ENGINE", "reportlab").strip().lower()
    if e == "html":
        return "html"
    return "browser" if e == "browser" and browser_enabled() else "reportlab"


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
    params = {"select": "provider,kind,amount,visit_date,patient,diagnosis_code,diagnosis_name,visit_type,care_days",
              "order": "visit_date", "limit": "5000",
              "patient": f"ilike.*{'*'.join(patient.split())}*" if " " in patient.strip() else f"eq.{patient.strip()}"}
    if user_id is not None:
        params["user_id"] = f"eq.{user_id}"
    r = httpx.get(f"{url}/rest/v1/pick3_medical_records", params=params, timeout=30, headers=h)
    if r.status_code == 400 and "diagnosis_code" in r.text:  # migration 008 not run yet
        params["select"] = "provider,kind,amount,visit_date,patient"
        r = httpx.get(f"{url}/rest/v1/pick3_medical_records", params=params, timeout=30, headers=h)
    r.raise_for_status()
    return r.json()


Progress = Callable[[dict], None]  # {"stage": 1-4, "label": str, "done": int|None, "total": int|None}


_COUNT = re.compile(r'"stated_contract_count"\s*:\s*(\d+)')
CHUNK = 4  # contracts per coverage call


def _thinking() -> str:
    # Copying tables needs no deep reasoning; minimal thinking removes ~45 s of silence before output.
    return os.getenv("COVERAGE_EXTRACT_THINKING", "minimal")


def _extract_once(uri: str, model: str, progress: Progress, label: str) -> dict:
    """One call for the whole report (the fallback). Streams contracts read / the report's own count."""
    seen = {"done": -1, "total": None}

    def on_text(t: str) -> None:
        m = _COUNT.search(t)
        total = int(m.group(1)) if m else None
        done = t.count('"insurer"')  # one per contract object (the key exists only there)
        if (done, total) != (seen["done"], seen["total"]):
            seen.update(done=done, total=total)
            progress({"stage": 1, "label": label, "done": done, "total": total})
    progress({"stage": 1, "label": label})
    return gemini.generate_json_stream(model, [{"text": prompts.EXTRACT_PROMPT}, gemini.pdf_part(uri)],
                                       prompts.EXTRACT_SCHEMA, on_text, feature="coverage_extract",
                                       thinking=_thinking())


def _extract_parallel(uri: str, model: str, progress: Progress) -> dict | None:
    """Overview (customer, summary, risks, contract list) and coverages for contracts 1-4, 5-8, ... run at
    the same time: the coverage calls start as soon as the overview stream shows the report's contract
    count. Returns None when the parts don't add up (caller then reads the report in one call)."""
    import concurrent.futures as cf
    lock = threading.Lock()
    state = {"total": None, "started": False, "done": {}}
    pool = cf.ThreadPoolExecutor(max_workers=8)
    chunk_futures: list[cf.Future] = []

    def report() -> None:
        with lock:
            done = sum(state["done"].values())
            total = state["total"]
        progress({"stage": 1, "label": "보고서 나눠 읽는 중", "done": min(done, total or done), "total": total})

    def chunk(first: int, last: int) -> dict:
        key = (first, last)

        def on_text(t: str) -> None:
            n = max(0, t.count('"coverages"') - 1)  # a contract counts once the next one has started
            with lock:
                changed = n > state["done"].get(key, 0)
                if changed:
                    state["done"][key] = n
            if changed:
                report()
        res = gemini.generate_json_stream(model, [{"text": prompts.chunk_prompt(first, last)}, gemini.pdf_part(uri)],
                                          prompts.chunk_schema(), on_text, feature="coverage_extract",
                                          thinking=_thinking())
        with lock:
            state["done"][key] = len(res.get("contracts") or [])
        report()
        return res

    def start_chunks(total: int) -> None:
        with lock:
            if state["started"]:
                return
            state["started"], state["total"] = True, total
        for first in range(1, total + 1, CHUNK):
            chunk_futures.append(pool.submit(chunk, first, min(first + CHUNK - 1, total)))
        report()

    def on_overview(t: str) -> None:
        m = _COUNT.search(t)
        if m and int(m.group(1)) > 0:
            start_chunks(int(m.group(1)))

    progress({"stage": 1, "label": "보고서 나눠 읽는 중"})
    try:
        over = pool.submit(gemini.generate_json_stream, model,
                           [{"text": prompts.OVERVIEW_PROMPT}, gemini.pdf_part(uri)], prompts.overview_schema(),
                           on_overview, 0.0, "coverage_extract", _thinking()).result()
        contracts = over.get("contracts") or []
        if not contracts:
            return None
        start_chunks(len(contracts))  # the report had no stated count: start now
        covs: dict[int, list] = {}
        for f in chunk_futures:
            for c in f.result().get("contracts") or []:
                covs.setdefault(int(c.get("no", 0)), c.get("coverages") or [])
    except Exception:
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    nos = [int(c.get("no", 0)) for c in contracts]
    stated = (over.get("report") or {}).get("stated_contract_count")
    if (stated and stated != len(contracts)) or len(set(nos)) != len(nos) or any(n not in covs for n in nos):
        return None  # numbering disagreed between calls or a range came back short
    for c in contracts:
        c["coverages"] = covs[int(c["no"])]
    over["_read"] = {"mode": "parallel", "calls": 1 + len(chunk_futures)}
    return over


def extract(pdf_raw: bytes, model: str | None = None, progress: Progress = lambda p: None) -> dict:
    """Upload once; read in parallel (COVERAGE_EXTRACT_MODE=parallel, default) and fall back to one call."""
    model = model or os.getenv("COVERAGE_EXTRACT_MODEL", DEFAULT_EXTRACT_MODEL)
    progress({"stage": 1, "label": "보고서 올리는 중"})
    up = gemini.upload_pdf(pdf_raw)
    try:
        if os.getenv("COVERAGE_EXTRACT_MODE", "parallel") == "parallel":
            rep = _extract_parallel(up["uri"], model, progress)
            if rep is not None:
                return rep
            progress({"stage": 1, "label": "나눠 읽은 결과가 맞지 않아 한 번에 다시 읽는 중"})
        rep = _extract_once(up["uri"], model, progress, "보고서 읽는 중")
        rep["_read"] = {"mode": "single"}
        return rep
    finally:
        gemini.delete_file(up["name"])


def analyze(facts: dict, model: str | None = None) -> dict:
    import json
    model = model or os.getenv("COVERAGE_ANALYZE_MODEL", DEFAULT_ANALYZE_MODEL)
    return gemini.generate_json(model, [{"text": prompts.ANALYZE_PROMPT},
                                        {"text": "facts = " + json.dumps(facts, ensure_ascii=False, default=str)}],
                                prompts.ANALYZE_SCHEMA, temperature=0.2, feature="coverage_analyze",
                                # low thinking: same claims/gaps/optimization as high on the test report, 37 s -> 15 s
                                thinking=os.getenv("COVERAGE_ANALYZE_THINKING", "low"))


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
    # HTML is the default view on the phone; PDFs of the same run are kept for sending to the customer.
    engine = pdf_engine()
    html_docs = {k[:-4] + ".html": v.encode("utf-8") for k, v in pages.items()}
    pdfs: dict[str, bytes] = {}
    if engine == "reportlab":
        names = list(pages)
        pdfs = {names[0]: pdf_render.analysis_pdf(facts, clean, pp, warnings),
                names[1]: pdf_render.proposal_pdf(facts, clean, pp, warnings)}
    elif engine == "browser":
        with _BROWSER:
            pdfs = render.to_pdf(pages)
    if encrypt and pw:
        pdfs = {k: pdf_io.encrypt(v, pw) for k, v in pdfs.items()}
    encrypt = bool(pdfs) and encrypt
    docs = {**html_docs, **pdfs}
    return {"customer": facts["customer"], "records": len(records), "extract": rep, "facts": facts,
            "analysis": raw_analysis, "plan": pp, "warnings": warnings, "docs": docs, "html": pages,
            "encrypted_with_birth_date": bool(encrypt and pw)}
