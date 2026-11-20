"""보장분석 PDFs: share → stored only → user runs it on coverage.html → result files. Store and pipeline faked."""

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

    async def create(self, user_id, filename, size, encrypted, storage_path):
        self.n += 1
        self.rows[self.n] = {"id": self.n, "user_id": user_id, "filename": filename, "size_bytes": size,
                             "encrypted": encrypted, "storage_path": storage_path, "status": "stored",
                             "message": None, "extract": None, "results": None, "created_at": "2026-11-20T00:00"}
        return self.rows[self.n]

    async def list_reports(self, user_id):
        out = []
        for r in self.rows.values():
            if r["user_id"] == user_id:
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
                "encrypted_with_birth_date": False,
                "docs": {"홍길동_보장분석_청구및제안_2026-11-20.html": b"<html>a</html>",
                         "홍길동_맞춤보장제안_2026-11-20.html": b"<html>b</html>"}}

    monkeypatch.setattr(app.cov_service, "run", fake_run)
    app._COV_ACTIVE.clear()
    return fake


def _share(raw: bytes) -> dict:
    return client.post("/api/ingest/screenshot", headers=AUTH,
                       files={"image": ("report.pdf", raw, "application/pdf")}).json()


def _list() -> list[dict]:
    return client.get("/api/coverage", headers=AUTH).json()["reports"]


def test_shared_encrypted_pdf_is_only_stored(monkeypatch, store) -> None:
    seen = _fake(monkeypatch, SCREEN)
    res = _share(_pdf("20000101"))
    assert res["status"] == "saved" and res["service"] == "coverage" and "암호" in res["message"]
    assert seen["gemini"] == [] and store.runs == []
    r = _list()[0]
    assert r["encrypted"] and r["status"] == "stored" and not r["has_extract"] and r["files"] == []


def test_run_needs_the_right_password_then_saves_results(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    monkeypatch.setattr(app.cov_service, "is_coverage_report", lambda raw: True)
    rid = _share(_pdf("20000101"))["report_id"]
    assert client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={}).json()["need_password"]
    bad = client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={"password": "0000"}).json()
    assert not bad["ok"] and "맞지" in bad["message"]
    ok = client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={"password": "20000101"}).json()
    assert ok["ok"]
    r = _list()[0]
    assert r["status"] == "done" and r["customer_name"] == "홍길동" and "제안 후 월 약 113만" in r["message"]
    assert r["has_extract"] and [f["index"] for f in r["files"]] == [0, 1]
    f = client.get(f"/api/coverage/{rid}/files/1", headers=AUTH)
    assert f.status_code == 200 and f.content == b"<html>b</html>" and f.headers["content-type"].startswith("text/html")


def test_rerun_reuses_the_extract_without_password(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    monkeypatch.setattr(app.cov_service, "is_coverage_report", lambda raw: True)
    rid = _share(_pdf("pw"))["report_id"]
    client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={"password": "pw"})
    again = client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={}).json()
    assert again["ok"] and store.runs[-1]["rep"] == {"customer": {"name": "홍길동"}} and store.runs[-1]["raw"] == b""


def test_encrypted_pdf_that_is_not_a_report_goes_to_medical(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    monkeypatch.setattr(app.cov_service, "is_coverage_report", lambda raw: False)
    handled = []

    async def fake_medical(user, raw, mime, chosen, filename):
        handled.append(raw)
        return {"status": "saved", "message": "🧾 의료비 3건 저장"}

    monkeypatch.setattr(app, "_save_medical_document", fake_medical)
    rid = _share(_pdf("pw"))["report_id"]
    assert client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={"password": "pw"}).json()["ok"]
    r = _list()[0]
    assert handled and r["status"] == "other" and "의료비" in r["message"] and store.runs == []


def test_delete_removes_row_and_files(monkeypatch, store) -> None:
    _fake(monkeypatch, SCREEN)
    rid = _share(_pdf("pw"))["report_id"]
    assert store.files
    assert client.delete(f"/api/coverage/{rid}", headers=AUTH).json()["ok"]
    assert _list() == [] and store.files == {}


def test_pdf_size_limits(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    big = _pdf() + b"%" * (16 * 1024 * 1024)
    monkeypatch.setattr(app.cov_service, "is_coverage_report", lambda raw: True)
    assert _share(big)["status"] == "saved"  # a 16 MB report is fine (File API)
    monkeypatch.setattr(app.cov_service, "is_coverage_report", lambda raw: False)
    assert "15MB" in _share(big)["message"]  # a 16 MB medical PDF is not (inline Gemini)


def test_running_report_shows_stage_and_contract_counts(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    rid = _share(_pdf("pw"))["report_id"]
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


def test_encrypted_nhis_pdf_waits_on_the_medical_page_and_is_cleaned_up(monkeypatch, store) -> None:
    import app
    _fake(monkeypatch, SCREEN)
    res = _share_named(_pdf("700101"), "20261120요양급여내역20261120013720.pdf")
    assert res["status"] == "saved" and "의료비 기록" in res["message"]
    assert _list() == []  # not on the 보장분석 page
    med = client.get("/api/coverage?kind=medical", headers=AUTH).json()["reports"]
    assert len(med) == 1 and med[0]["status"] == "stored"
    monkeypatch.setattr(app.cov_service, "is_coverage_report", lambda raw: False)

    async def fake_medical(user, raw, mime, chosen, filename):
        return {"status": "saved", "message": "🩺 건강보험 요양급여내역 · 진료 7건"}
    monkeypatch.setattr(app, "_save_medical_document", fake_medical)
    rid = med[0]["id"]
    assert client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={"password": "700101"}).json()["ok"]
    row = client.get("/api/coverage?kind=medical", headers=AUTH).json()["reports"][0]
    assert row["status"] == "other" and "진료 7건" in row["message"] and "보장분석 보고서가 아니라" not in row["message"]
    assert store.files == {}  # the health-data PDF is deleted once saved
    again = client.post(f"/api/coverage/{rid}/run", headers=AUTH, json={"password": "700101"}).json()
    assert not again["ok"] and "이미" in again["message"]
