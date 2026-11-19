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


async def render_pdf(url: str, timeout_ms: int = 30000) -> bytes:
    """Render `url` like a phone browser and return the page as PDF bytes."""
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
                await page.wait_for_timeout(800)  # late renders after the last request
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                if len((text or "").strip()) < 30:
                    raise LinkError("페이지에 내용이 없습니다(링크가 만료됐을 수 있음).")
                pdf = await page.pdf(width="430px", print_background=False,
                                     margin={"top": "8mm", "bottom": "8mm", "left": "4mm", "right": "4mm"})
            finally:
                await browser.close()
    if len(pdf) > MAX_PDF_BYTES:
        raise LinkError("페이지가 너무 큽니다.")
    return pdf
