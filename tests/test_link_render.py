"""Shared links rendered by a headless browser: allowlist, SSRF guards, and the ingest flow (browser faked)."""

from __future__ import annotations

import pytest

import link_render
from test_medical import AUTH, SCREEN, _fake, client  # noqa: F401
from test_medical import _env  # noqa: F401  (autouse Supabase/Gemini env)

LINK = "https://result.example-ins.kr/unclaimed/result?t=abc.def.ghi"


@pytest.fixture(autouse=True)
def _domains(monkeypatch: pytest.MonkeyPatch) -> None:
    import app
    monkeypatch.setenv("PICK3_RENDER_DOMAINS", "example-ins.kr, other.com")
    app._JOBS.clear()


def _post_link() -> tuple[dict, dict]:
    """Share LINK; the reply is immediate, the work runs as a background task. Returns (reply, job)."""
    res = client.post("/api/ingest/screenshot", headers=AUTH,
                      files={"image": ("text.txt", LINK.encode(), "text/plain")}).json()
    jobs = client.get("/api/jobs", headers=AUTH).json()["jobs"]
    return res, jobs[0]


def test_allowlist_matches_domain_and_subdomains_only() -> None:
    assert link_render.host_allowed("example-ins.kr")
    assert link_render.host_allowed("result.example-ins.kr")
    assert not link_render.host_allowed("example-ins.kr.evil.com")
    assert not link_render.host_allowed("notexample-ins.kr")
    assert link_render.allowed_domains() == ["example-ins.kr", "other.com"]


def test_find_link_in_shared_text() -> None:
    assert link_render.find_link(f"결과 보기 {LINK}\n") == LINK
    assert link_render.find_link("링크 없음") == ""


@pytest.mark.parametrize("url,reason", [
    ("http://example-ins.kr/x", "https"),
    ("https://unknown.com/x", "지원하지 않는"),
])
def test_check_url_refuses(url: str, reason: str) -> None:
    with pytest.raises(link_render.LinkError, match=reason):
        link_render.check_url(url)


def test_check_url_refuses_private_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(link_render.socket, "getaddrinfo",
                        lambda *a, **k: [(None, None, None, None, ("10.0.0.5", 443))])
    with pytest.raises(link_render.LinkError, match="열 수 없는"):
        link_render.check_url(LINK)


def test_ingest_link_renders_and_saves_medical_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN, rows=[{"patient": "최근이름"}])
    opened: list[str] = []

    async def fake_render(url: str):
        opened.append(url)
        return [("", b"%PDF-1.7 rendered page")], url, 900

    monkeypatch.setattr(link_render, "render", fake_render)
    res, job = _post_link()
    assert res["status"] == "accepted" and "읽는 중" in res["message"] and res["job_id"] == job["id"]
    assert opened == [LINK]
    assert job["status"] == "done" and job["message"].startswith("🔗 링크에서 읽음\n")
    assert job["site"] == "result.example-ins.kr"
    assert seen["posts"][0][0].endswith("/pick3_medical_records")


def test_ingest_link_on_unlisted_site_gets_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN)
    res = client.post("/api/ingest/screenshot", headers=AUTH,
                      files={"image": ("text.txt", b"https://unknown.com/r?t=1", "text/plain")}).json()
    assert res["status"] == "unsupported" and "unknown.com" in res["message"] and "전체 페이지" in res["message"]
    assert seen["gemini"] == []


def test_ingest_link_render_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, SCREEN)

    async def boom(url: str):
        raise link_render.LinkError("페이지에 내용이 없습니다(링크가 만료됐을 수 있음).")

    monkeypatch.setattr(link_render, "render", boom)
    res, job = _post_link()
    assert res["status"] == "accepted"
    assert job["status"] == "error" and "만료" in job["message"]


def test_downloaded_html_instead_of_link_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN)
    html = b"<!DOCTYPE html><html><head><title>_</title></head><body><div id=app></div></body></html>"
    res = client.post("/api/ingest/screenshot", headers=AUTH, files={"image": ("result", html, "text/html")}).json()
    assert res["status"] == "unsupported" and "HTML" in res["message"] and seen["gemini"] == []


def test_link_with_person_tabs_saves_each_tab_under_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, [SCREEN, SCREEN])

    async def fake_render(url: str):
        return [("홍길동", b"%PDF-1.7 tab one"), ("김예시", b"%PDF-1.7 tab two")], url, 900

    monkeypatch.setattr(link_render, "render", fake_render)
    res, job = _post_link()
    assert job["status"] == "done"
    patients = [{r["patient"] for r in kw["json"]} for url, kw in seen["posts"] if url.endswith("/pick3_medical_records")]
    assert patients == [{"홍길동"}, {"김예시"}]  # the tab name wins over the name read from the page
    assert "홍길동" in job["message"] and "김예시" in job["message"]
