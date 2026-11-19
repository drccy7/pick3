"""Open a shared link in a headless browser and print it to PDF (for pages filled in by JavaScript).

Some pages (e.g. a result page opened from a KakaoTalk 알림톡 button) are an empty HTML shell; the
data arrives later through JavaScript. A plain HTTP fetch sees nothing, so we render the page with
Playwright/Chromium (Apache-2.0) the way a phone browser would and hand the PDF to the existing
medical-document pipeline.

Safety: only https links whose host is in PICK3_RENDER_DOMAINS are opened (no open proxy / SSRF),
and the host must resolve to public IPs only. One page at a time; images/fonts/media are not loaded
to keep memory low (Render 512 MB); the browser is started per link and closed right after.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit

MAX_PDF_BYTES = 15 * 1024 * 1024
_LOCK = asyncio.Semaphore(1)
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) CriOS/131.0 Mobile/15E148 Safari/604.1")


class LinkError(ValueError):
    """User-facing reason a link cannot be rendered."""


def allowed_domains() -> list[str]:
    raw = os.getenv("PICK3_RENDER_DOMAINS", "")
    return [d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip()]


def host_allowed(host: str, domains: list[str] | None = None) -> bool:
    host = (host or "").lower().rstrip(".")
    domains = allowed_domains() if domains is None else domains
    return any(host == d or host.endswith("." + d) for d in domains)


def find_link(text: str) -> str:
    """The first http(s) URL in shared text ("" if none)."""
    m = re.search(r"https?://[^\s<>\"']+", text or "")
    return m.group(0) if m else ""


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    ips = {ipaddress.ip_address(i[4][0]) for i in infos}
    return bool(ips) and all(ip.is_global for ip in ips)


def check_url(url: str) -> str:
    """Return the URL if it may be opened, else raise LinkError."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise LinkError("https 링크만 열 수 있습니다.")
    if not host_allowed(parts.hostname):
        raise LinkError(f"아직 지원하지 않는 사이트입니다({parts.hostname}).")
    if not _public_host(parts.hostname):
        raise LinkError("열 수 없는 주소입니다.")
    return url


async def render(url: str, timeout_ms: int = 30000) -> tuple[list[tuple[str, bytes]], str, int]:
    """Render `url` like a phone browser. Returns ([(person_tab_name or "", pdf)], final_url, text_chars)."""
    check_url(url)
    from playwright.async_api import async_playwright  # heavy import, only when used

    async with _LOCK:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                context = await browser.new_context(
                    user_agent=_MOBILE_UA, viewport={"width": 430, "height": 932},
                    device_scale_factor=1, is_mobile=True, has_touch=True, locale="ko-KR")
                page = await context.new_page()

                async def _route(route, request) -> None:
                    if request.resource_type in ("image", "media", "font"):
                        return await route.abort()
                    if request.is_navigation_request() and request.frame == page.main_frame:
                        # Redirects must stay on allowed sites too.
                        if not host_allowed(urlsplit(request.url).hostname or ""):
                            return await route.abort()
                    return await route.continue_()

                await page.route("**/*", _route)
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                text = await _settle(page)
                if len(text.strip()) < 30:
                    raise LinkError("페이지에 내용이 없습니다(링크가 만료됐을 수 있음).")
                pages: list[tuple[str, bytes]] = []
                tabs = await page.evaluate(_FIND_NAME_TABS)
                for name in tabs:  # one PDF per person tab (가족·고객 탭); [] -> just the page as is
                    await page.evaluate(_CLICK_NAME_TAB, name)
                    await page.wait_for_load_state("networkidle")
                    await _settle(page)
                    pages.append((name, await _pdf(page)))
                if not pages:
                    pages.append(("", await _pdf(page)))
                final_url = page.url
            finally:
                await browser.close()
    if sum(len(p) for _, p in pages) > MAX_PDF_BYTES:
        raise LinkError("페이지가 너무 큽니다.")
    return pages, final_url, len(text)


# A group of >= 2 sibling buttons (or role=tab) whose labels are all 2-4 syllable Hangul names is
# treated as person tabs. The page itself only shows the selected person; the link URL does not
# carry that choice, so we visit every tab.
_FIND_NAME_TABS = r"""() => {
  const isName = t => /^[가-힣]{2,4}$/.test(t);
  const groups = new Map();
  for (const b of document.querySelectorAll('button, [role=tab]')) {
    const p = b.parentElement;
    if (!p || groups.has(p)) continue;
    const kids = [...p.children].filter(k => k.matches('button, [role=tab]'));
    const names = kids.map(k => (k.innerText || '').trim());
    if (kids.length >= 2 && names.every(isName) && new Set(names).size === names.length) groups.set(p, names);
  }
  return groups.size === 1 ? [...groups.values()][0] : [];
}"""

_CLICK_NAME_TAB = r"""(name) => {
  for (const b of document.querySelectorAll('button, [role=tab]')) {
    if ((b.innerText || '').trim() === name) { if (!b.disabled) b.click(); return true; }
  }
  return false;
}"""


async def _pdf(page) -> bytes:
    return await page.pdf(width="430px", print_background=False,
                          margin={"top": "8mm", "bottom": "8mm", "left": "4mm", "right": "4mm"})


async def _settle(page, rounds: int = 12, pause_ms: int = 700) -> str:
    """Scroll to the bottom (lazy lists) until the visible text stops growing; return that text."""
    last = -1
    text = ""
    for _ in range(rounds):
        await page.evaluate("() => window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
        await page.wait_for_timeout(pause_ms)
        text = await page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        if len(text) == last:
            break
        last = len(text)
    await page.evaluate("() => window.scrollTo(0, 0)")
    return text


async def render_pdf(url: str, timeout_ms: int = 30000) -> bytes:
    pages, _final, _chars = await render(url, timeout_ms)
    return pages[0][1]
