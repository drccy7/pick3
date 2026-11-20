"""HTML (Jinja2) → PDF (Playwright Chromium, already used by Pick3 link rendering)."""

from __future__ import annotations

import base64
import pathlib

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .facts import premium_range

TPL = pathlib.Path(__file__).with_name("templates")


def won(v) -> str:
    return "-" if v is None else f"{int(v):,}"


def man(v) -> str:
    """원 → '1억 3,500만' / '500만' / '없음' (coverage amounts)."""
    if not v:
        return "없음"
    m = int(round(v / 10_000))
    eok, rest = divmod(m, 10_000)
    if eok and rest:
        return f"{eok}억 {rest:,}만"
    return f"{eok}억" if eok else f"{rest:,}만"


def manwon(v) -> str:
    """원 → '5.2만' / '117만' (premiums)."""
    if v is None:
        return "-"
    x = v / 10_000
    if abs(x) >= 100 or float(x).is_integer():
        return f"{round(x):,}만"
    return f"{x:.1f}".rstrip("0").rstrip(".") + "만"


def topcov(covs: list[dict], n: int = 4) -> str:
    items = sorted((c for c in covs if c.get("amount")), key=lambda c: -c["amount"])[:n]
    s = " · ".join(f"{c['name']} {man(c['amount'])}" for c in items)
    more = len(covs) - len(items)
    return s + (f" 외 {more}건" if more > 0 else "")


def env() -> Environment:
    e = Environment(loader=FileSystemLoader(TPL), autoescape=select_autoescape(["html", "j2"]))
    e.filters.update(won=won, man=man, manwon=manwon, topcov=topcov)
    return e


def lookups(facts: dict, analysis: dict, plan: dict) -> tuple[dict, dict]:
    """(products by key, premium range by product key) shared by the HTML and ReportLab renderers."""
    prod = {p["key"]: p for p in facts["products"]}
    adds = {a["key"]: a for a in plan["adds"]}
    for g in analysis["gaps"]:  # gaps whose premium range is missing still render
        adds.setdefault(g["product_key"], premium_range(g["product_key"], facts.get("premium_table_age")) or {})
    return prod, adds


def html(kind: str, facts: dict, analysis: dict, plan: dict, warnings: list[str]) -> str:
    prod, adds = lookups(facts, analysis, plan)
    css = (TPL / "base.css").read_text(encoding="utf-8")
    return env().get_template(f"{kind}.html.j2").render(
        f=facts, a=analysis, plan=plan, warnings=warnings, prod=prod, adds=adds, css=css)


_FONT = TPL.parent / "fonts" / "NotoSansKR-subset.woff2"  # SIL OFL 1.1 (fonts/OFL.txt); servers lack Hangul fonts
_FONT_CSS: str | None = None


def _font_css() -> str:
    global _FONT_CSS
    if _FONT_CSS is None:
        data = base64.b64encode(_FONT.read_bytes()).decode("ascii") if _FONT.exists() else ""
        _FONT_CSS = ('<style>@font-face{font-family:"Pick3 KR";font-weight:100 900;'
                     f'src:url(data:font/woff2;base64,{data}) format("woff2");}}</style>') if data else ""
    return _FONT_CSS


def to_pdf(pages: dict[str, str]) -> dict[str, bytes]:
    """{name: html} → {name: pdf bytes} with one browser. The Hangul font is embedded, no network needed."""
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        for name, doc in pages.items():
            pg.set_content(doc.replace("<head>", "<head>" + _font_css(), 1), wait_until="load", timeout=60000)
            pg.evaluate("document.fonts.ready.then(() => true)")
            out[name] = pg.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        b.close()
    return out
