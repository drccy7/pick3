"""Shared links rendered by a headless browser: allowlist, SSRF guards, and the ingest flow (browser faked)."""

from __future__ import annotations

import pytest

import link_render
from test_medical import AUTH, SCREEN, _fake, client  # noqa: F401
from test_medical import _env  # noqa: F401  (autouse Supabase/Gemini env)

LINK = "https://result.example-ins.kr/unclaimed/result?t=abc.def.ghi"


@pytest.fixture(autouse=True)
def _domains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PICK3_RENDER_DOMAINS", "example-ins.kr, other.com")


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

    async def fake_render(url: str) -> bytes:
        opened.append(url)
        return b"%PDF-1.7 rendered page"

    monkeypatch.setattr(link_render, "render_pdf", fake_render)
    res = client.post("/api/ingest/screenshot", headers=AUTH,
                      files={"image": ("text.txt", LINK.encode(), "text/plain")}).json()
    assert opened == [LINK]
    assert res["service"] == "medical" and res["message"].startswith("🧾 의료비 기록 (링크)\n")
    assert seen["posts"][0][0].endswith("/pick3_medical_records")


def test_ingest_link_on_unlisted_site_gets_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN)
    res = client.post("/api/ingest/screenshot", headers=AUTH,
                      files={"image": ("text.txt", b"https://unknown.com/r?t=1", "text/plain")}).json()
    assert res["status"] == "unsupported" and "unknown.com" in res["message"] and "전체 페이지" in res["message"]
    assert seen["gemini"] == []


def test_ingest_link_render_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, SCREEN)

    async def boom(url: str) -> bytes:
        raise link_render.LinkError("페이지에 내용이 없습니다(링크가 만료됐을 수 있음).")

    monkeypatch.setattr(link_render, "render_pdf", boom)
    res = client.post("/api/ingest/screenshot", headers=AUTH,
                      files={"image": ("text.txt", LINK.encode(), "text/plain")}).json()
    assert res["status"] == "error" and "만료" in res["message"]
