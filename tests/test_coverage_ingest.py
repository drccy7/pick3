"""Shared PDFs: 보장분석 → stored, run on coverage.html; encrypted → 암호 문서함 (open, classify). Store faked."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from test_medical import AUTH, SCREEN, _fake, client  # noqa: F401
from test_medical import _env  # noqa: F401  (autouse Supabase/Gemini env)


def _pdf(password: str = "") -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    if password:
        w.encrypt(password)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


class FakeStore:
    def __init__(self):
        self.rows, self.files, self.n = {}, {}, 0

    async def put_file(self, path, data, content_type="application/pdf"):
        self.files[path] = data

    async def get_file(self, path):
        return self.files[path]

    async def delete_files(self, paths):
        for p in paths:
            self.files.pop(p, None)

    async def create(self, user_id, filename, size, encrypted, storage_path, kind="coverage"):
        self.n += 1
        self.rows[self.n] = {"id": self.n, "user_id": user_id, "filename": filename, "size_bytes": size,
                             "encrypted": encrypted, "storage_path": storage_path, "status": "stored",
                             "kind": kind, "suggested": None,
                             "message": None, "extract": None, "results": None, "created_at": "2026-11-20T00:00"}
        return self.rows[self.n]

    async def list_reports(self, user_id, kind="coverage"):
        out = []
        for r in self.rows.values():
            if r["user_id"] == user_id and r.get("kind", "coverage") == kind:
                x = {k: v for k, v in r.items() if k != "extract"}
                x["extract_ready"] = (r["extract"] or {}).get("customer")
                out.append(x)
        return out

    async def get(self, user_id, rid):
        r = self.rows.get(rid)
        return dict(r) if r and r["user_id"] == user_id else None

    async def update(self, user_id, rid, **fields):
        self.rows[rid].update(fields)

    async def delete(self, user_id, rid):
        self.rows.pop(rid, None)


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch):
    import app
    fake = FakeStore()
    for name in ("put_file", "get_file", "delete_files", "create", "list_reports", "get", "update", "delete"):
        monkeypatch.setattr(app.cov_store, name, getattr(fake, name))
    fake.runs = []

    def fake_run(raw, *, user_id, rep=None, progress=lambda s: None, **kw):
        progress({"stage": 1, "label": "보고서 읽는 중", "done": 3, "total": 14})
        fake.runs.append({"raw": raw, "rep": rep, "user_id": user_id})
        return {"customer": {"name": "홍길동", "birth_date": "1960-01-01"}, "records": 12,
                "facts": {"contracts": [1, 2, 3]}, "extract": {"customer": {"name": "홍길동"}},
                "analysis": {"claims": [1], "gaps": [1, 2]}, "plan": {"total_mid": 1_130_000}, "warnings": [],
                "docs": {"홍길동_보장분석_청구및제안_2026-11-20.html": b"<html>a</html>",
                         "홍길동_맞춤보장제안_2026-11-20.html": b"<html>b</html>"}}

    monkeypatch.setattr(app.cov_service, "run", fake_run)
    app._COV_ACTIVE.clear()
    return fake


def _share(raw: bytes) -> dict:
    return client.post("/api/ingest/screenshot", headers=AUTH,
                       files={"image": ("report.pdf", raw, "application/pdf")}).json()


def _as(monkeypatch, box: str, confidence: str = "high") -> None:
    """The document registry reads every PDF as `box` ("" = unknown)."""
    import app
    doc = {"type": box, "box": box, "confidence": confidence if box else "none", "via": "", "also": [], "sections": []}
    monkeypatch.setattr(app.doc_registry, "classify", lambda raw: dict(doc))


def _report(monkeypatch) -> int:
    """Share an unencrypted PDF that reads as a 보장분석 report; returns its id."""
    import app
    _as(monkeypatch, "coverage")
    return _share(_pdf())["report_id"]


def _list(kind: str = "coverage") -> list[dict]:
    return client.get(f"/api/coverage?kind={kind}", headers=AUTH).json()["reports"]


def test_shared_encrypted_pdf_waits_in_the_locked_box(monkeypatch, store) -> None:
    seen = _fake(monkeypatch, SCREEN)
    res = _share(_pdf("20000101"))
    assert res["status"] == "saved" and res["kind"] == "locked" and "확인할 문서" in res["message"]
    assert seen["gemini"] == [] and store.runs == [] and _list() == []
    r = _list("locked")[0]
    assert r["encrypted"] and r["status"] == "stored"
    assert client.get(f"/api/docs/{r['id']}/original", headers=AUTH).status_code == 409
    assert client.post(f"/api/coverage/{r['id']}/run", headers=AUTH, json={}).json()["ok"] is False


def test_open_once_then_classify_as_coverage_and_run(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    rid = _share(_pdf("20000101"))["report_id"]
    bad = client.post(f"/api/docs/{rid}/open", headers=AUTH, json={"password": "0000"}).json()
    assert not bad["ok"] and "맞지" in bad["message"]
    _as(monkeypatch, "coverage")
    ok = client.post(f"/api/docs/{rid}/open", headers=AUTH, json={"password": "20000101"}).json()
    assert ok == {"ok": True, "suggested": "coverage"}
    r = _list("locked")[0]
    assert not r["encrypted"] and r["suggested"] == "coverage"  # the opened copy is kept, no password again
    pdf = client.get(f"/api/docs/{rid}/original", headers=AUTH)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and not app.cov_pdf.is_encrypted(pdf.content)
    assert client.post(f"/api/docs/{rid}/classify", headers=AUTH, json={"kind": "coverage"}).json()["ok"]
    assert _list("locked") == [] and _list()[0]["id"] == rid
    assert client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={}).json()["ok"]
    r = _list()[0]
    assert r["status"] == "done" and r["customer_name"] == "홍길동" and "제안 후 월 약 113만" in r["message"]
    assert r["has_extract"] and [f["index"] for f in r["files"]] == [0, 1]
    f = client.get(f"/api/coverage/{rid}/files/1", headers=AUTH)
    assert f.status_code == 200 and f.content == b"<html>b</html>" and f.headers["content-type"].startswith("text/html")


def test_classify_as_medical_saves_records_and_drops_the_pdf(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    handled = []

    async def fake_medical(user, raw, mime, chosen, filename):
        handled.append(raw)
        return {"status": "saved" if len(handled) > 1 else "not_found",
                "message": "🩺 건강보험 요양급여내역 · 진료 7건" if len(handled) > 1 else "의료비 내역을 찾지 못했습니다."}

    monkeypatch.setattr(app, "_save_medical_document", fake_medical)
    rid = _share(_pdf("700101"))["report_id"]
    client.post(f"/api/docs/{rid}/open", headers=AUTH, json={"password": "700101"})
    assert client.post(f"/api/docs/{rid}/classify", headers=AUTH, json={"kind": "medical"}).json()["ok"] is False
    assert _list("locked")  # not readable as 의료비: stays, the user can pick another kind
    d = client.post(f"/api/docs/{rid}/classify", headers=AUTH, json={"kind": "medical"}).json()
    assert d["ok"] and "진료 7건" in d["message"] and not app.cov_pdf.is_encrypted(handled[-1])
    assert _list("locked") == [] and store.files == {}  # the health-data PDF is not kept


def test_classify_as_checkup_or_delete(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    _as(monkeypatch, "checkup")
    a = _share(_pdf("pw"))["report_id"]
    b = _share(_pdf("pw"))["report_id"]
    assert client.post(f"/api/docs/{a}/classify", headers=AUTH, json={"kind": "checkup"}).json()["ok"] is False
    for rid in (a, b):
        assert client.post(f"/api/docs/{rid}/open", headers=AUTH, json={"password": "pw"}).json()["suggested"] == "checkup"
    assert client.post(f"/api/docs/{a}/classify", headers=AUTH, json={"kind": "checkup"}).json()["ok"]
    assert client.post(f"/api/docs/{b}/classify", headers=AUTH, json={"kind": "delete"}).json()["ok"]
    assert [r["id"] for r in _list("checkup")] == [a] and _list("locked") == []
    assert list(store.files) == [store.rows[a]["storage_path"]]
    assert client.post(f"/api/docs/{a}/classify", headers=AUTH, json={"kind": "coverage"}).status_code == 404
    assert client.post(f"/api/coverage/{a}/run", headers=AUTH, json={}).json()["ok"] is False  # not a report


def test_unencrypted_checkup_goes_to_its_box(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    _as(monkeypatch, "checkup")
    res = _share(_pdf())
    assert res["status"] == "saved" and res["kind"] == "checkup" and "건강검진" in res["message"]
    assert len(_list("checkup")) == 1 and _list() == []
    assert client.get("/api/coverage?kind=medical", headers=AUTH).json() == {"reports": []}  # old 의료비 page


def test_checkup_text_detection() -> None:
    import health_checkup as H
    form = ("일반건강검진 결과통보서 종합판정 정상B 신장 체중 허리둘레 체질량지수 혈압 혈색소 공복혈당 "
            "총콜레스테롤 AST ALT 감마지티피 요단백 흉부방사선")
    bill = "진료비 세부산정내역 혈색소 공복혈당 총콜레스테롤 AST ALT 감마지티피 요단백 검사료"
    assert H.is_checkup_text(form) and not H.is_checkup_text(bill) and not H.is_checkup_pdf(b"not a pdf")


def test_rerun_reuses_the_extract_without_password(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    _as(monkeypatch, "coverage")
    rid = _report(monkeypatch)
    client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={})
    again = client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={}).json()
    assert again["ok"] and store.runs[-1]["rep"] == {"customer": {"name": "홍길동"}} and store.runs[-1]["raw"] == b""
    assert store.runs[0]["raw"].startswith(b"%PDF")


def test_pdf_is_printed_from_the_result_html_once_and_dropped_on_rerun(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    _as(monkeypatch, "coverage")
    printed = []
    monkeypatch.setattr(app.cov_service, "html_to_pdf", lambda h: printed.append(h) or b"%PDF-" + h.encode())
    rid = _report(monkeypatch)
    client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={})
    assert [f["pdf"] for f in _list()[0]["files"]] == [False, False]
    for _ in range(2):
        r = client.get(f"/api/coverage/{rid}/pdf/1", headers=AUTH)
        assert r.status_code == 200 and r.content == b"%PDF-<html>b</html>"
        assert r.headers["content-type"] == "application/pdf" and ".pdf" in r.headers["content-disposition"]
    assert printed == ["<html>b</html>"]  # second send reuses the stored PDF
    assert [f["pdf"] for f in _list()[0]["files"]] == [False, True]
    pdf_path = store.rows[rid]["results"][1]["pdf_path"]
    client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={})  # new HTML → old PDF no longer matches
    assert pdf_path not in store.files and [f["pdf"] for f in _list()[0]["files"]] == [False, False]
    monkeypatch.setenv("COVERAGE_PDF", "off")
    assert client.get(f"/api/coverage/{rid}/pdf/0", headers=AUTH).status_code == 503


def test_old_runs_show_only_html_results(monkeypatch, store) -> None:
    _fake(monkeypatch, SCREEN)
    _report(monkeypatch)
    uid = store.rows[1]["user_id"]
    store.rows.clear()
    store.rows[9] = {"id": 9, "user_id": uid, "filename": "a.pdf", "size_bytes": 1, "encrypted": False,
                     "storage_path": "x/a.pdf", "status": "done", "message": "", "extract": None,
                     "created_at": "2026-11-20T00:00",
                     "results": [{"name": "a.html", "path": "x/result_0.html"}, {"name": "b.html", "path": "x/result_1.html"},
                                 {"name": "a.pdf", "path": "x/result_2.pdf"}, {"name": "b.pdf", "path": "x/result_3.pdf"}]}
    assert [(f["index"], f["name"]) for f in _list()[0]["files"]] == [(0, "a.html"), (1, "b.html")]
    assert client.get("/api/coverage/9/pdf/2", headers=AUTH).status_code == 404


def test_delete_removes_row_and_files(monkeypatch, store) -> None:
    _fake(monkeypatch, SCREEN)
    rid = _report(monkeypatch)
    assert store.files
    assert client.delete(f"/api/coverage/{rid}", headers=AUTH).json()["ok"]
    assert _list() == [] and store.files == {}


def test_pdf_size_limits(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    big = _pdf() + b"%" * (16 * 1024 * 1024)
    _as(monkeypatch, "coverage")
    assert _share(big)["status"] == "saved"  # a 16 MB report is fine (File API)
    _as(monkeypatch, "")
    assert "15MB" in _share(big)["message"]  # a 16 MB medical PDF is not (inline Gemini)


def test_running_report_shows_stage_and_contract_counts(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    rid = _report(monkeypatch)
    store.rows[rid]["status"] = "running"
    app._cov_progress(rid, {"stage": 1, "label": "보고서 읽는 중", "done": 6, "total": 14})
    p = _list()[0]["progress"]
    assert p["stage"] == 1 and (p["done"], p["total"]) == (6, 14) and p["elapsed"] >= 0
    app._cov_progress(rid, {"stage": 3, "label": "분석·제안 작성 중"})
    r = _list()[0]
    assert r["progress"]["stage"] == 3 and r["progress"]["done"] is None and r["message"] == "분석·제안 작성 중"
    app._COV_ACTIVE.clear()
    assert _list()[0]["status"] == "error"  # running but not in this process -> interrupted


def test_extract_reports_contracts_read_while_streaming(monkeypatch) -> None:
    from coverage_review import gemini, service
    seen = []
    monkeypatch.setattr(gemini, "upload_pdf", lambda raw: {"uri": "u", "name": "n"})
    monkeypatch.setattr(gemini, "delete_file", lambda name: None)

    def fake_stream(model, parts, schema, on_text, temperature=0.0, feature="", thinking=None):
        text = '{"report": {"issuer": "x", "stated_contract_count": 2}, "contracts": [{"no": 1, "insurer": "a"}'
        on_text(text)
        text += ', {"no": 2, "insurer": "b"}]}'
        on_text(text)
        return {"ok": True}

    monkeypatch.setattr(gemini, "generate_json_stream", fake_stream)
    monkeypatch.setenv("COVERAGE_EXTRACT_MODE", "single")
    assert service.extract(b"%PDF", progress=seen.append) == {"ok": True, "_read": {"mode": "single"}}
    counts = [(p.get("done"), p.get("total")) for p in seen if p.get("done") is not None]
    assert counts == [(1, 2), (2, 2)] and seen[0]["label"] == "보고서 올리는 중"



def _parallel_fakes(monkeypatch, drop_contract=None):
    """Overview returns 6 contracts (count streamed first); range calls return coverages per contract."""
    from coverage_review import gemini, prompts
    calls = []
    monkeypatch.setattr(gemini, "upload_pdf", lambda raw: {"uri": "u", "name": "n"})
    monkeypatch.setattr(gemini, "delete_file", lambda name: None)

    def fake_stream(model, parts, schema, on_text, temperature=0.0, feature="", thinking=None):
        prompt = parts[0]["text"]
        calls.append(prompt[:20])
        if prompt == prompts.OVERVIEW_PROMPT:
            on_text('{"report": {"issuer": "x", "stated_contract_count": 6}')
            return {"report": {"issuer": "x", "stated_contract_count": 6}, "customer": {"name": "홍길동"},
                    "contracts": [{"no": i, "insurer": f"보험{i}"} for i in range(1, 7)], "summary": [], "risks": []}
        import re
        first, last = map(int, re.search(r"(\d+)번부터 (\d+)번까지", prompt).groups())
        return {"contracts": [{"no": i, "insurer": f"보험{i}", "coverages": [{"name": f"담보{i}", "unit": "만원",
                                                                              "amount": i, "category": "기타"}]}
                              for i in range(first, last + 1) if i != drop_contract]}
    monkeypatch.setattr(gemini, "generate_json_stream", fake_stream)
    return calls


def test_parallel_read_merges_ranges_by_contract_number(monkeypatch) -> None:
    from coverage_review import service
    monkeypatch.delenv("COVERAGE_EXTRACT_MODE", raising=False)
    calls = _parallel_fakes(monkeypatch)
    seen = []
    rep = service.extract(b"%PDF", progress=seen.append)
    assert rep["_read"] == {"mode": "parallel", "calls": 3}  # overview + contracts 1-4 and 5-6
    assert [c["coverages"][0]["name"] for c in rep["contracts"]] == [f"담보{i}" for i in range(1, 7)]
    assert max(p.get("done") or 0 for p in seen) == 6 and len(calls) == 3


def test_parallel_read_falls_back_when_a_contract_is_missing(monkeypatch) -> None:
    from coverage_review import gemini, service
    monkeypatch.delenv("COVERAGE_EXTRACT_MODE", raising=False)
    calls = _parallel_fakes(monkeypatch, drop_contract=5)
    whole = {"report": {}, "contracts": [{"no": 1, "coverages": []}]}
    orig = gemini.generate_json_stream

    def stream(model, parts, schema, on_text, temperature=0.0, feature="", thinking=None):
        if parts[0]["text"].startswith(service.prompts.EXTRACT_PROMPT[:30]) and parts[0]["text"] == service.prompts.EXTRACT_PROMPT:
            return dict(whole)
        return orig(model, parts, schema, on_text, temperature, feature, thinking)
    monkeypatch.setattr(gemini, "generate_json_stream", stream)
    seen = []
    rep = service.extract(b"%PDF", progress=seen.append)
    assert rep["_read"] == {"mode": "single"} and any("다시 읽는" in p["label"] for p in seen)


def _share_named(raw: bytes, name: str) -> dict:
    return client.post("/api/ingest/screenshot", headers=AUTH,
                       files={"image": (name, raw, "application/pdf")}).json()


def test_contract_status_api_and_rerun_uses_it(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    rid = _report(monkeypatch)
    store.rows[rid]["extract"] = {"customer": {"name": "홍길동"}, "contracts": [
        {"no": 1, "insurer": "가생명", "product": "종신", "start": "2005-11", "pay_cycle": "월납", "premium": 100000,
         "paid_count": 240, "type": "종신·정기", "coverages": []},
        {"no": 2, "insurer": "나손보", "product": "가정종합", "start": "2023-07", "pay_cycle": "월납", "premium": 100000,
         "paid_count": 40, "type": "화재·가정", "coverages": []}]}
    d = client.get(f"/api/coverage/{rid}/contracts", headers=AUTH).json()
    assert [c["estimate"] for c in d["contracts"]] == ["paid_up", "paying"] and d["statuses"]["stopped"].startswith("납입 안 함")
    r = client.post(f"/api/coverage/{rid}/overrides", headers=AUTH, json={"overrides": {"2": "stopped", "1": "auto", "x": "paying"}})
    assert r.json()["overrides"] == {"2": "stopped"}
    assert client.get(f"/api/coverage/{rid}/contracts", headers=AUTH).json()["contracts"][1]["status"] == "stopped"
    seen = {}
    orig = app.cov_service.run

    def spy(raw, **kw):
        seen.update(kw)
        return orig(raw, **kw)
    monkeypatch.setattr(app.cov_service, "run", spy)
    assert client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={}).json()["ok"]
    assert seen["overrides"] == {"2": "stopped"} and seen["rep"]["contracts"][1]["no"] == 2


def _fake_mhr(monkeypatch, app):
    read = {"patient": "홍길동", "sections": [{"section": "visits", "heading": "진료이력", "pages": [1]},
                                            {"section": "meds", "heading": "투약이력", "pages": [2]},
                                            {"section": "checkup", "heading": "건강검진(일반)", "pages": [3, 4]}],
            "visits": [{"visit_date": "2026-03-06"}] * 3, "meds": [{"visit_date": "2026-03-06"}] * 2}
    saved = {}
    monkeypatch.setattr(app.mhr, "is_mhr_pdf", lambda raw: True)
    _as(monkeypatch, "mhr")
    monkeypatch.setattr(app.mhr, "read", lambda raw: read)

    async def visits(rows, uid, patient):
        saved["visits"] = (len(rows), patient)
        return {"linked": 1, "inserted": 2}

    async def meds(rows, uid, patient, source):
        saved["meds"] = (len(rows), patient, source)
        return 2, 0
    monkeypatch.setattr(app.mhr, "save_visits", visits)
    monkeypatch.setattr(app.prescriptions, "save", meds)
    return saved


def _pdf_pages(n: int) -> bytes:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_shared_mhr_export_is_kept_and_read_into_records(monkeypatch, store) -> None:
    import app
    from pypdf import PdfReader
    _fake(monkeypatch, SCREEN)
    saved = _fake_mhr(monkeypatch, app)
    res = _share(_pdf_pages(4))
    assert res["status"] == "saved" and res["kind"] == "mhr"
    assert "진료 3건" in res["message"] and "새로 저장 2건" in res["message"] and "건강검진" in res["message"]
    assert saved == {"visits": (3, "홍길동"), "meds": (2, "홍길동", "mhr")}
    (row,) = _list("mhr")
    assert row["status"] == "done" and row["customer_name"] == "홍길동" and store.files  # original kept
    (chk,) = _list("checkup")  # 검진 pages 3-4 saved on their own in the 🩺 box, named after the person
    assert chk["customer_name"] == "홍길동" and len(PdfReader(io.BytesIO(store.files[store.rows[chk["id"]]["storage_path"]])).pages) == 2
    assert "🩺 건강검진 폴더" in res["message"]
    assert client.post(f"/api/docs/{row['id']}/process", headers=AUTH).json()["ok"]  # read again
    assert [r["id"] for r in _list("checkup")] == [chk["id"]]  # replaced, not duplicated


def test_analyze_reruns_the_persons_report_or_writes_a_health_analysis(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    _fake_mhr(monkeypatch, app)
    mid = _share(_pdf_pages(4))["report_id"]
    seen = {}

    def fake_health(name, records, meds, **kw):
        seen["health"] = (name, len(records), len(meds), kw.get("checkup_docs"))
        return {"facts": {"records": {"count": 2}, "diagnoses": [1], "meds": [1, 2]}, "analysis": {"needs": [1]},
                "warnings": [], "docs": {"홍길동_건강보험분석_2026-11-21.html": b"<html>h</html>"}}

    async def meds(uid, name):
        return [{"drug_name": "x"}]
    monkeypatch.setattr(app.cov_health, "run", fake_health)
    monkeypatch.setattr(app.cov_service, "fetch_records", lambda name, uid: [{"a": 1}, {"b": 2}])
    monkeypatch.setattr(app.prescriptions, "list_rows", meds)
    d = client.post(f"/api/docs/{mid}/analyze", headers=AUTH).json()  # no 보장분석 report for 홍길동
    assert d["ok"] and d["mode"] == "health" and "건강·보험 분석" in d["message"]
    assert seen["health"] == ("홍길동", 2, 1, 1)
    (h,) = _list("health")
    assert h["status"] == "done" and "권할 보장 1개" in h["message"] and [f["name"] for f in h["files"]] == ["홍길동_건강보험분석_2026-11-21.html"]
    assert client.post(f"/api/health/{h['id']}/run", headers=AUTH).json()["ok"]  # 다시 작성
    assert len(_list("health")) == 1
    rid = _report(monkeypatch)  # now a 보장분석 report for 홍길동 exists
    store.rows[rid]["extract"] = {"customer": {"name": "홍길동"}}
    store.rows[rid]["customer_name"] = "홍길동"
    d = client.post(f"/api/docs/{mid}/analyze", headers=AUTH).json()
    assert d["ok"] and d["mode"] == "coverage" and d["report_id"] == rid and store.runs


def test_locked_mhr_is_suggested_and_processed_on_confirm(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    saved = _fake_mhr(monkeypatch, app)
    rid = _share(_pdf("pw"))["report_id"]
    assert client.post(f"/api/docs/{rid}/open", headers=AUTH, json={"password": "pw"}).json()["suggested"] == "mhr"
    d = client.post(f"/api/docs/{rid}/classify", headers=AUTH, json={"kind": "mhr"}).json()
    assert d["ok"] and "투약 2건" in d["message"] and saved["meds"][0] == 2
    assert _list("locked") == [] and len(_list("mhr")) == 1


def _encrypt(raw: bytes, password: str) -> bytes:
    from pypdf import PdfReader
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(raw)))
    w.encrypt(password)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_sharing_and_opening_use_the_same_registry(monkeypatch, store) -> None:
    import app
    pytest.importorskip("reportlab")
    from test_doc_registry import CHECKUP_WORDS, COVERAGE_WORDS, pdf
    _fake(monkeypatch, SCREEN)
    routed = []

    async def fake_medical(user, raw, mime, chosen, filename):
        routed.append(raw)
        return {"status": "saved", "service": "medical", "message": "ok"}
    monkeypatch.setattr(app, "_save_medical_document", fake_medical)
    samples = {"checkup": pdf(CHECKUP_WORDS), "coverage": pdf(COVERAGE_WORDS * 2),
               "medical": pdf([("건강보험 요양급여내역", 13), "발급기간 2026.01.01~2026.06.30"])}
    for box, raw in samples.items():
        assert app._guess_kind(raw) == box
        res = _share(raw)  # shared as is
        assert (res.get("kind") or "medical") == box, box
        rid = _share(_encrypt(raw, "pw"))["report_id"]  # the same file behind a password
        assert client.post(f"/api/docs/{rid}/open", headers=AUTH, json={"password": "pw"}).json()["suggested"] == box
    assert len(routed) == 1 and len(_list("checkup")) == 1 and len(_list()) == 1


def test_unknown_pdf_without_medical_rows_waits_opened_in_the_box(monkeypatch, store) -> None:
    seen = _fake(monkeypatch, {"patient": "", "records": []})
    _as(monkeypatch, "")
    res = _share(_pdf())
    assert res["status"] == "saved" and res["kind"] == "locked" and "종류를 골라" in res["message"]
    assert len(seen["gemini"]) == 2  # payments, then prescriptions: both found nothing
    (r,) = _list("locked")
    assert not r["encrypted"] and r["status"] == "opened" and r["suggested"] is None
    assert client.post(f"/api/docs/{r['id']}/classify", headers=AUTH, json={"kind": "checkup"}).json()["ok"]


def test_weak_guess_is_confirmed_by_the_user(monkeypatch, store) -> None:
    seen = _fake(monkeypatch, SCREEN)
    _as(monkeypatch, "medical", "low")
    res = _share(_pdf())
    assert res["kind"] == "locked" and "확인하고 저장" in res["message"] and seen["gemini"] == []
    (r,) = _list("locked")
    assert r["status"] == "opened" and r["suggested"] == "medical"


def test_open_all_with_one_password_then_save_all_as_guessed(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    _as(monkeypatch, "checkup")
    ids = [_share(_pdf(pw))["report_id"] for pw in ("700101", "700101", "other")]
    assert client.post("/api/docs/open-all", headers=AUTH, json={"password": " "}).json()["ok"] is False
    assert client.post("/api/docs/open-all", headers=AUTH, json={"password": "0000"}).json() == \
        {"ok": True, "opened": 0, "failed": 3}  # wrong password: nothing changes
    assert all(r["encrypted"] for r in _list("locked"))
    assert client.post("/api/docs/open-all", headers=AUTH, json={"password": "700101"}).json() == \
        {"ok": True, "opened": 2, "failed": 1}
    d = client.post("/api/docs/classify-all", headers=AUTH).json()
    assert d["ok"] and (d["saved"], d["failed"], len(d["messages"])) == (2, 0, 2)
    assert sorted(r["id"] for r in _list("checkup")) == ids[:2]
    assert [r["id"] for r in _list("locked")] == [ids[2]]  # still locked: left alone


def test_save_all_medical_stops_at_the_daily_limit(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    _as(monkeypatch, "medical")
    handled = []

    async def fake_medical(user, raw, mime, chosen, filename):
        handled.append(filename)
        return {"status": "saved", "service": "medical", "message": "🩺 진료 3건"}
    monkeypatch.setattr(app, "_save_medical_document", fake_medical)
    for _ in range(3):
        _share(_pdf("pw"))
    assert client.post("/api/docs/open-all", headers=AUTH, json={"password": "pw"}).json()["opened"] == 3
    monkeypatch.setattr(app, "_daily_limits", lambda user: (0, 300))
    d = client.post("/api/docs/classify-all", headers=AUTH).json()
    assert (d["saved"], d["failed"], len(d["messages"])) == (0, 1, 1) and "한도" in d["messages"][0]
    assert handled == [] and len(_list("locked")) == 3
    monkeypatch.setattr(app, "_daily_limits", lambda user: (30, 300))
    d = client.post("/api/docs/classify-all", headers=AUTH).json()
    assert (d["saved"], d["failed"]) == (3, 0) and len(handled) == 3
    assert _list("locked") == [] and store.files == {}
