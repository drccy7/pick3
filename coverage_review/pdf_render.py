"""Browser-free PDFs (ReportLab Platypus, BSD) with the same content as the Jinja templates.

The server (512 MB) runs without Chromium, so this is the default engine. Mirrors
templates/analysis.html.j2 and templates/proposal.html.j2 section by section; keep them in step.
Hangul comes from static Noto Sans KR instances (fonts/build_fonts.py, SIL OFL 1.1).
"""

from __future__ import annotations

import io
import pathlib
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (CondPageBreak, Flowable, KeepInFrame, PageBreak, Paragraph, SimpleDocTemplate, Table,
                                TableStyle)

from .render import lookups, man, manwon, topcov, won

FONT_DIR = pathlib.Path(__file__).with_name("fonts")
FONT, FONT_B = "NotoSansKR", "NotoSansKR-Bold"

NAVY = colors.HexColor("#12305c")
INK = colors.HexColor("#1d2330")
MUTED = colors.HexColor("#5b6475")
LINE = colors.HexColor("#c9d3e3")
HEAD_BG = colors.HexColor("#e6ecf6")
BOX_BG = colors.HexColor("#f3f6fb")
WARN_BG, WARN_LINE = colors.HexColor("#fff6ec"), colors.HexColor("#f0c99a")
ORANGE, ORANGE_LINE = colors.HexColor("#b3540f"), colors.HexColor("#f0a35e")
RED, GREEN = colors.HexColor("#b33a12"), colors.HexColor("#1d6b3a")
FILL = colors.HexColor("#9aa6bb")
TAGS = {"hi": ("#ffe1d6", "#b33a12"), "mid": ("#fff0c2", "#8a6200"), "ok": ("#dcf2e3", "#1d6b3a")}

_glyphs: set[int] = set()


def _fonts() -> None:
    if _glyphs:
        return
    reg = TTFont(FONT, str(FONT_DIR / "NotoSansKR-Regular.ttf"))
    pdfmetrics.registerFont(reg)
    pdfmetrics.registerFont(TTFont(FONT_B, str(FONT_DIR / "NotoSansKR-Bold.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_B, italic=FONT, boldItalic=FONT_B)
    _glyphs.update(reg.face.charToGlyph)


def e(v) -> str:
    """Text → Paragraph markup: escaped, None → '', characters the font lacks → '·' (no silent tofu)."""
    if v is None:
        return ""
    s = str(v)
    if any(ord(ch) not in _glyphs for ch in s if ch not in "\n\t"):
        s = "".join(ch if ord(ch) in _glyphs or ch in "\n\t" else ("□" if ch in "☐☑☒" else "·") for ch in s)
    return escape(s)


def _style(name: str, size: float = 8.5, lead: float | None = None, color=INK, bold: bool = False, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=FONT_B if bold else FONT, fontSize=size, leading=lead or size * 1.42,
                          textColor=color, splitLongWords=1, bulletFontName=FONT, **kw)


class S:
    """Paragraph styles, built after the fonts are registered."""

    def __init__(self, base: float = 8.5) -> None:
        self.td = _style("td", base)
        self.tdn = _style("tdn", base, alignment=TA_RIGHT)
        self.th = _style("th", base, color=NAVY, bold=True)
        self.thn = _style("thn", base, color=NAVY, bold=True, alignment=TA_RIGHT)
        self.small = _style("small", base - 0.3, color=MUTED)
        self.body = _style("body", 9.3, 13.4)
        self.meta = _style("meta", 8.4, 12.2, color=MUTED)
        self.li = _style("li", 9.3, 13.4, leftIndent=4.5 * mm, bulletIndent=1.2 * mm, spaceBefore=0.5 * mm)
        self.li_small = _style("lis", 8.4, 12, leftIndent=4.5 * mm, bulletIndent=1.2 * mm, color=MUTED)
        self.h1 = _style("h1", 17, 22, color=NAVY, bold=True, spaceAfter=2 * mm)
        self.h2 = _style("h2", 12, 15, color=NAVY, bold=True)
        self.h3 = _style("h3", 10.2, 13, color=NAVY, bold=True, spaceBefore=3.5 * mm, spaceAfter=1.5 * mm)


def P(markup: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(markup, style)


def tag(text, kind: str) -> str:
    bg, fg = TAGS[kind]
    return f'<font backColor="{bg}" color="{fg}"><b>&nbsp;{e(text)}&nbsp;</b></font>'


def grid(rows: list[list], widths: list[float], *, header: bool = True, pad=(1.3 * mm, 1.7 * mm),
         extra: list | None = None) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    st = [("GRID", (0, 0), (-1, -1), 0.6, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("TOPPADDING", (0, 0), (-1, -1), pad[0]), ("BOTTOMPADDING", (0, 0), (-1, -1), pad[0] + 0.4),
          ("LEFTPADDING", (0, 0), (-1, -1), pad[1]), ("RIGHTPADDING", (0, 0), (-1, -1), pad[1])]
    if header:
        st.append(("BACKGROUND", (0, 0), (-1, 0), HEAD_BG))
    t.setStyle(TableStyle(st + (extra or [])))
    t.spaceBefore, t.spaceAfter = 1.5 * mm, 2.5 * mm
    return t


def box(content: list, width: float, bg=BOX_BG, border=LINE, radius: float = 3 * mm, pad=(2.5 * mm, 4 * mm),
        line: float = 0.75, height: float | None = None) -> Table:
    t = Table([[content]], colWidths=[width], rowHeights=[height] if height else None)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("BOX", (0, 0), (-1, -1), line, border),
                           ("ROUNDEDCORNERS", [radius] * 4), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), pad[0]), ("BOTTOMPADDING", (0, 0), (-1, -1), pad[0] + 1),
                           ("LEFTPADDING", (0, 0), (-1, -1), pad[1]), ("RIGHTPADDING", (0, 0), (-1, -1), pad[1])]))
    t.spaceBefore = t.spaceAfter = 2.5 * mm
    return t


class Rule(Flowable):
    """h2 of the analysis: navy title with a 2 px rule under it."""

    def __init__(self, para: Paragraph, before: float = 6 * mm) -> None:
        super().__init__()
        self.para, self.spaceBefore, self.spaceAfter = para, before, 2.5 * mm

    def wrap(self, aw, ah):
        self.width = aw
        _, h = self.para.wrap(aw, ah)
        self.ph = h
        self.height = h + 1.2 * mm + 1.5
        return aw, self.height

    def draw(self):
        self.para.drawOn(self.canv, 0, self.height - self.ph)
        self.canv.setStrokeColor(NAVY)
        self.canv.setLineWidth(1.5)
        self.canv.line(0, 0.75, self.width, 0.75)


class NumHead(Flowable):
    """Proposal heading: number in a navy circle + title."""

    def __init__(self, num: int, text: str, size: float = 10.5) -> None:
        super().__init__()
        self.num, self.text, self.size = num, text, size
        self.spaceBefore, self.spaceAfter = 3.2 * mm, 1.6 * mm

    def wrap(self, aw, ah):
        self.width, self.height = aw, 6 * mm
        return aw, self.height

    def draw(self):
        c, r = self.canv, 3 * mm
        c.setFillColor(NAVY)
        c.circle(r, r, r, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont(FONT_B, 9)
        c.drawCentredString(r, r - 3.2, str(self.num))
        c.setFillColor(INK)
        c.setFont(FONT_B, self.size)
        c.drawString(2 * r + 2 * mm, r - self.size * 0.36, self.text)


def _page_no(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont(FONT, 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawCentredString(A4[0] / 2, 7 * mm, f"- {doc.page} -")
    canvas.restoreState()


def _doc(buf, title: str, top: float, side: float) -> SimpleDocTemplate:
    return SimpleDocTemplate(buf, pagesize=A4, leftMargin=side, rightMargin=side, topMargin=top, bottomMargin=top,
                             title=title, author="Pick3", subject=title, creator="Pick3 coverage_review")


# ───────────────────────────── analysis (설계사용 분석서) ─────────────────────────────

def analysis_pdf(facts: dict, analysis: dict, plan: dict, warnings: list[str]) -> bytes:
    _fonts()
    s = S()
    f, a, p, c = facts, analysis, plan, facts["customer"]
    prod, _ = lookups(facts, analysis, plan)
    W = A4[0] - 26 * mm
    out: list = []

    def h2(text: str, page_break: bool = False) -> None:
        if page_break:
            out.append(PageBreak())
        else:  # headings never end a page; tables after them may split (keepWithNext would move them whole)
            out.append(CondPageBreak(35 * mm))
        out.append(Rule(P(e(text), s.h2), before=0 if page_break else 6 * mm))

    def h3(text: str) -> None:
        out.append(CondPageBreak(30 * mm))
        out.append(P(e(text), s.h3))

    out.append(P(e(f"{c['name']} 님 보험 보장 · 병의원 기록 대조 분석"), s.h1))
    who = f"{e(c['name'])}({e(c.get('sex'))}"
    if c.get("birth_date"):
        who += (f", {e(c['birth_date'])}, 만 {e(c.get('age'))}세, 보험나이 {e(c.get('insurance_age'))}세 → "
                f"{e(c.get('next_insurance_age_date'))}부터 {e(c.get('insurance_age_after'))}세")
    who += ")"
    rep, rec = f["report"], f["records"]
    meta = (f"작성 {e(f['today'])} · 설계사 내부 검토용 · 대상: {who}<br/>"
            f"자료 ① 보장분석 보고서({e(rep.get('issuer'))}"
            + (f", 분석일 {e(rep['analysis_date'])}" if rep.get("analysis_date") else "")
            + f") ② 병의원·약국 이용 기록 {rec['count']}건({e(rec.get('first'))} ~ {e(rec.get('last'))}, "
              f"합계 {won(rec.get('amount'))}원)")
    if c.get("age_note"):
        meta += f'<br/><font color="#b33a12"><b>※ {e(c["age_note"])}</b></font>'
    out.append(P(meta, s.meta))

    pts = [P(e(k), s.li) for k in a.get("key_points", [])]
    li = f"<b>보험료</b>: 보고서상 월 {won(p['stated'] or p['base'])}원"
    if p["paid_up"]:
        li += f" 중 납입하지 않는 계약 {won(p['paid_up'])}원 제외 시 {won(p['paying_now'])}원"
    li += (f". 정리 후 {won(p['after_opt'])}원 + 보강 약 {won(p['add_mid'])}원 = <b>종합 약 {won(p['total_mid'])}원</b>"
           f"(범위 {won(p['total_min'])}~{won(p['total_max'])}).")
    for x in pts:
        x.bulletText = "•"
    last = P(li, s.li)
    last.bulletText = "•"
    out.append(box([P("<b>핵심 요약</b>", s.body)] + pts + [last], W))
    if warnings:
        ws = []
        for w in warnings:
            x = P(e(w), s.li_small)
            x.bulletText = "•"
            ws.append(x)
        out.append(box([P("<b>자동 점검</b>", s.small)] + ws, W, WARN_BG, WARN_LINE))

    # 1. 보유 계약
    h2(f"1. 보유 계약 ({len(f['contracts'])}건, 월납 합계 {won(p['base'])}원)")
    rows = [[P("No", s.th), P("보험사 · 상품", s.th), P("기간", s.th), P("1회 보험료", s.thn), P("주요 담보", s.th)]]
    for k in f["contracts"]:
        period = f"{e(k.get('start') or '')}~{e(k.get('end') or '종신')}"
        if k.get("status_label"):
            period += f'<br/><font color="#b33a12"><b>{e(k.get("status_label"))}</b></font>'
        prem = won(k.get("premium")) + ("" if k.get("pay_cycle") == "월납" else f"<br/>({e(k.get('pay_cycle'))})")
        rows.append([P(e(k.get("no")), s.td), P(f"{e(k.get('insurer'))}<br/>{e(k.get('product'))}", s.td),
                     P(period, s.td), P(prem, s.tdn), P(e(topcov(k.get("coverages") or [])), s.td)])
    out.append(grid(rows, [9 * mm, 38 * mm, 28 * mm, 19 * mm, W - 94 * mm]))
    if rep.get("caveats"):
        out.append(P(e("보고서 유의사항: " + " / ".join(rep["caveats"])), s.small))

    # 2. 병의원 이용 기록 요약
    h2(f"2. 병의원 이용 기록 요약 ({rec['count']}건)")
    by_year = " · ".join(f"{e(y)}년 {v['count']}건 {won(v['amount'])}원" for y, v in rec.get("by_year", {}).items())
    by_type = " · ".join(f"{e(t)} {v['count']}건 {won(v['amount'])}원" for t, v in rec.get("by_type", {}).items())
    wi, wo = rec["with_indemnity"], rec["without_indemnity"]
    ind = (f"실손 적용 기간: {wi['count']}건 {won(wi['amount'])}원 / 실손 없는 기간: {wo['count']}건 "
           f"{won(wo['amount'])}원")
    ind += "".join(f"<br/>실손 계약 {e(ip.get('contract'))}: {e(ip.get('start'))}~{e(ip.get('end') or '')}"
                   for ip in f.get("indemnity_periods", []))
    recur = "<br/>".join(
        f"[{e(r['id'])}] {e(r['provider'])}: {e(r['from'])}~{e(r['to'])} 중 {e(r['months'])}개월 방문, "
        f"중앙값 {won(r.get('median_amount'))}원"
        + (f", 같은 날 약국 {r['same_day_pharmacy_visits']}회(중앙값 {won(r.get('pharmacy_median'))}원)"
           if r.get("same_day_pharmacy_visits") else "")
        for r in f.get("recurring", [])) or "없음"
    clus = "<br/>".join(
        f"[{e(k['id'])}] {e(k['provider'])} {e(k['first'])}~{e(k['last'])} {e(k['visits'])}회 {won(k.get('total'))}원"
        + (' <font color="#5b6475" size="8">(청구기한 경과)</font>' if k.get("expired") else "")
        for k in f.get("clusters", []))
    rows = [[P("연도별", s.td), P(by_year, s.td)], [P("기관 종류별", s.td), P(by_type, s.td)],
            [P("실손 기간 전후", s.td), P(ind, s.td)], [P("정기 진료", s.td), P(recur, s.td)],
            [P("큰 진료 묶음", s.td), P(clus, s.td)]]
    if f.get("diagnoses"):  # from the 건강보험 요양급여내역 (may include 의증/배제상병)
        dx = "<br/>".join(
            f"[{e(d['id'])}] {e(d.get('name'))}({e(d['code'])}) {e(d['visits'])}회 {e(d['first'])}~{e(d['last'])}"
            + (f" · 입원 {e(d['inpatient_visits'])}" if d.get("inpatient_visits") else "")
            for d in f["diagnoses"][:8])
        dx += f'<br/><font color="#5b6475" size="8">{e(f.get("diagnosis_note") or "")}</font>'
        rows.insert(4, [P("진단명<br/><font size='8'>요양급여내역</font>", s.td), P(dx, s.td)])
    out.append(grid(rows, [0.22 * W, 0.78 * W], header=False))

    # 3. 청구할 만한 내용
    cr = f["claims_rules"]
    h2("3. 청구할 만한 내용", page_break=True)
    out.append(box([P(f"청구 기한은 진료일로부터 <b>{e(cr.get('deadline_years'))}년</b>. {e(f['today'])} 기준 "
                      f"<b>{e(cr.get('expired_before'))} 이전 진료는 기한 경과</b>.", s.body)], W, WARN_BG, WARN_LINE))
    rows = [[P(h, s.th) for h in ("순위", "진료", "청구 대상", "확인할 것", "근거")]]
    for x in a.get("claims", []):
        kind = "hi" if x["rank"] == 1 else ("mid" if x["rank"] <= 3 else "ok")
        rows.append([P(tag(x["rank"], kind), s.td), P(e(x.get("title")), s.td), P(e(x.get("insurance")), s.td),
                     P(e(x.get("check")), s.td), P(e(", ".join(x.get("evidence", []))), s.small)])
    out.append(grid(rows, [11 * mm, 0.22 * W, 0.24 * W, 0.30 * W, W - 11 * mm - 0.76 * W]))
    ie = f["indemnity_estimate"]
    out.append(P(f"실손 추정({e(ie.get('id'))}): 기한 내·실손 기간 {e(ie.get('days'))}일 진료 {won(ie.get('billed'))}원 → "
                 f"<b>약 {won(ie.get('estimate'))}원</b> ({e('·'.join(ie.get('excluded_types', [])))} "
                 f"{won(ie.get('excluded_total'))}원 제외, 급여·비급여 구분 없는 개략치).", s.body))
    if ie.get("generations"):
        out.append(P("실손 세대(계약 시작월 기준): " + " / ".join(
            f"{e(g['contract'])} <b>{e(g['generation'])}</b> — {e(g['days'])}일 {won(g['billed'])}원 → 약 "
            f"{won(g['estimate'])}원 ({e(g.get('note', ''))})" for g in ie["generations"]), s.small))

    # 4. 보장 부족 → 신규 가입 제안
    h2("4. 보장 부족 → 신규 가입 제안")
    out.append(box([P(f"제안 시점: 상령일 <b>{e(c.get('next_insurance_age_date'))}</b> 이전(보험나이 "
                      f"{e(c.get('insurance_age_after'))}세 되기 전, {e(c.get('days_to_change'))}일 남음) 체결이 보험료상 유리.",
                      s.body)], W))
    rows = [[P(h, s.th) for h in ("순위", "제안 담보", "근거: 병의원 기록", "근거: 보고서", "상담 포인트")]]
    for g in a.get("gaps", []):
        pr = prod[g["product_key"]]
        rows.append([P(tag(g["rank"], "hi" if g["rank"] <= 3 else "mid"), s.td),
                     P(f"<b>{e(pr['label'])}</b><br/><font color='#5b6475' size='8'>현재 {e(man(pr.get('current_amount')))}"
                       f" → {e(pr.get('proposal'))}</font>", s.td),
                     P(e(g.get("record_basis")), s.td), P(e(g.get("report_basis")), s.td),
                     P(e(g.get("talking_point")), s.td)])
    out.append(grid(rows, [11 * mm, 0.21 * W, 0.24 * W, 0.24 * W, W - 11 * mm - 0.69 * W]))

    # 5. 보험료
    h2("5. 보험료 최적화 + 추가 권유 = 종합 보험료 (전후 비교)", page_break=True)
    out.append(box([P(f"추가 보험료는 <b>설정표(rules.json)의 시장 참고 추정 범위</b>(보험나이 {e(p.get('age_for_table'))}세 구간)다. "
                      "실제 견적으로 바꿔 넣는다. 감액 절감액은 특약별 보험료 확인 전이라 합계에 넣지 않았다.", s.body)],
                   W, WARN_BG, WARN_LINE))
    if f["premium"].get("paid_up_likely"):
        h3("5-1. 지금 납입하지 않는 계약")
        rows = [[P("계약", s.th), P("월 보험료", s.thn), P("근거", s.th)]]
        for x in f["premium"]["paid_up_likely"]:
            rows.append([P(e(x.get("label")), s.td), P(won(x.get("monthly")), s.tdn),
                         P(f"{e(x.get('basis'))}" + (f" → {e(x.get('month'))} 완료 추정" if x.get("month") else ""), s.td)])
        rows.append([P("<b>실제 납입 중</b>", s.td), P(f"<b>{won(p['paying_now'])}</b>", s.tdn),
                     P(f"{won(p['base'])} − {won(p['paid_up'])} (증권·자동이체로 확인)", s.td)])
        out.append(grid(rows, [0.45 * W, 22 * mm, W - 0.45 * W - 22 * mm]))
    h3("5-2. 최적화 — 중복·과잉 정리")
    rows = [[P("구분", s.th), P("계약", s.th), P("월 보험료", s.thn), P("이유", s.th), P("잃는 보장 · 주의", s.th)]]
    for x in p["removals"]:
        rows.append([P(tag("정리", "ok"), s.td), P(f"{e(x['no'])}. {e(x['label'])}", s.td),
                     P(f"−{won(x['monthly'])}", s.tdn), P(e(x.get("reason")), s.td), P(e(x.get("caution")), s.td)])
    for x in p["reductions"]:
        rows.append([P(tag("감액", "mid"), s.td), P(f"{e(x['no'])}. {e(x['label'])}", s.td),
                     P("견적 필요", s.tdn), P(e(x.get("reason")), s.td), P(e(x.get("caution")), s.td)])
    out.append(grid(rows, [13 * mm, 0.27 * W, 20 * mm, 0.25 * W, W - 33 * mm - 0.52 * W]))
    keep = " · ".join(f"{e(o.get('contract_no'))}({e(o.get('reason'))})"
                      for o in a.get("optimization", []) if o.get("action") == "유지")
    out.append(P(f"유지: {keep}", s.small))
    h3("5-3. 추가 권유 보험료 (참고 추정)")
    rows = [[P("순위", s.th), P("담보", s.th), P("월 보험료 추정", s.thn), P("비고", s.th)]]
    for x in p["adds"]:
        rows.append([P(e(x["rank"]), s.td), P(f"{e(x.get('label'))} ({e(x.get('proposal'))})", s.td),
                     P(f"{won(x.get('min'))} ~ {won(x.get('max'))}", s.tdn), P(e(x.get("note")), s.td)])
    rows.append([P("<b>합계</b>", s.td), "", P(f"<b>{won(p['add_min'])} ~ {won(p['add_max'])}</b>", s.tdn),
                 P(f"중간값 {won(p['add_mid'])}", s.td)])
    out.append(grid(rows, [11 * mm, 0.42 * W, 34 * mm, W - 45 * mm - 0.42 * W], extra=[("SPAN", (0, -1), (1, -1))]))
    h3("5-4. 종합 보험료 전후 비교 (월)")
    nm = "".join(f" · {e(x.get('id'))} {e(x.get('cycle'))} {won(x.get('premium'))}원 별도"
                 for x in f["premium"].get("non_monthly", []))
    rows = [[P("단계", s.th), P("월 보험료", s.thn), P("설명", s.th)],
            [P("① 보고서상 현재", s.td), P(won(p["stated"] or p["base"]), s.tdn), P("월납 합계" + nm, s.td)],
            [P("② 실제 납입 중", s.td), P(won(p["paying_now"]), s.tdn), P("납입하지 않는 계약 제외", s.td)],
            [P("③ 최적화 후", s.td), P(won(p["after_opt"]), s.tdn),
             P(f"② − 정리 {won(p['removed'])}" + (" (감액 절감은 견적 후 추가)" if p["reductions"] else ""), s.td)],
            [P("④ 추가 권유", s.td), P(f"+ {won(p['add_mid'])}", s.tdn),
             P(f"범위 {won(p['add_min'])} ~ {won(p['add_max'])}", s.td)],
            [P("<b>⑤ 종합(제안 후)</b>", s.td), P(f"<b>{won(p['total_mid'])}</b>", s.tdn),
             P(f"<b>범위 {won(p['total_min'])} ~ {won(p['total_max'])}</b>", s.td)]]
    out.append(grid(rows, [0.25 * W, 28 * mm, W - 0.25 * W - 28 * mm]))
    if p.get("paid_up_estimated"):
        out.append(P(f"※ 납입완료 추정이 틀리면 ⑤는 약 {won(p['total_if_not_paid_up'])}원.", s.small))
    h3("5-5. 보장 전후 비교")
    rows = [[P("담보", s.th), P("현재", s.thn), P("제안", s.thn), P("보고서 권장 · 판정", s.th)]]
    for g in a.get("gaps", []):
        pr = prod[g["product_key"]]
        judged = "<br/>".join(f"{e(r.get('item'))} 권장 {e(man(r.get('recommended')))} / 가입 {e(man(r.get('current')))} "
                              f"({e(r.get('status'))})" for r in pr.get("report_rows", []))
        rows.append([P(e(pr["label"]), s.td), P(e(man(pr.get("current_amount"))), s.tdn),
                     P(e(pr.get("proposal")), s.tdn), P(judged, s.td)])
    out.append(grid(rows, [0.28 * W, 0.12 * W, 0.17 * W, 0.43 * W]))

    # 6, 7
    h2("6. 주의사항")
    items = [e(x) for x in a.get("cautions", [])]
    items.append(f"기존 계약 해지·감액을 동반하면 비교안내 의무 이행·기록. 보호 계약"
                 f"({e(', '.join(str(x) for x in f.get('protected_contracts', [])))})은 해지 권유하지 않음.")
    lis = []
    for x in items:
        para = P(x, s.li)
        para.bulletText = "•"
        lis.append(para)
    out.append(box(lis, W, WARN_BG, WARN_LINE))
    h2("7. 분석의 한계")
    out.append(P("병의원 기록에는 기관·날짜·금액만 있고 진단명과 급여·비급여 구분이 없어 청구 가능 여부와 금액은 진료 흐름에 근거한 "
                 "추정이다. 보고서는 현재 유지 계약만 보여 주므로 과거 계약은 알 수 없다. 보험금 지급 여부는 약관·증권으로 최종 "
                 "확인해야 하며 본 문서는 상담 참고용이다. 위험도는 통계 예측이며 의학적 진단이 아니다. 분석·문장 일부는 "
                 "AI(Gemini)가 작성했고 숫자는 코드로 계산했다.", s.small))

    buf = io.BytesIO()
    _doc(buf, f"{c['name']} 님 보장·진료기록 대조 분석", 14 * mm, 13 * mm).build(
        out, onFirstPage=_page_no, onLaterPages=_page_no)
    return buf.getvalue()


# ───────────────────────────── proposal (고객용 1쪽 + 설계사 확인 1쪽) ─────────────────────────────

def _fit(flowables: list, w: float, h: float) -> float:
    """Natural height of a flowable list (incl. spaceBefore/After) at width w."""
    total = 0.0
    for fl in flowables:
        total += fl.wrap(w, h)[1] + fl.getSpaceBefore() + fl.getSpaceAfter()
    return total


def _stage(label: str, value: str, detail: str, kind: str, w: float) -> Table:
    vcol = {"": NAVY, "down": GREEN, "up": ORANGE, "total": colors.white}[kind]
    lcol = colors.HexColor("#d5def0") if kind == "total" else MUTED
    bg = NAVY if kind == "total" else colors.HexColor("#f6f8fc")
    rows = [[P(e(label), _style("stl", 8, 10.5, color=lcol, alignment=TA_CENTER))],
            [P(e(value), _style("stv", 12.5, 16, color=vcol, bold=True, alignment=TA_CENTER))],
            [P(detail, _style("std", 7.6, 10, color=lcol, alignment=TA_CENTER))]]
    t = Table(rows, colWidths=[w])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("BOX", (0, 0), (-1, -1), 0.75, NAVY if kind == "total" else LINE),
                           ("ROUNDEDCORNERS", [2.5 * mm] * 4), ("TOPPADDING", (0, 0), (-1, -1), 0.4 * mm),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0.4 * mm), ("TOPPADDING", (0, 0), (-1, 0), 2 * mm),
                           ("BOTTOMPADDING", (0, -1), (-1, -1), 2 * mm), ("LEFTPADDING", (0, 0), (-1, -1), 1.5 * mm),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 1.5 * mm)]))
    return t


def proposal_pdf(facts: dict, analysis: dict, plan: dict, warnings: list[str] | None = None) -> bytes:
    _fonts()
    s = S(8.6)
    f, a, p, c = facts, analysis, plan, facts["customer"]
    prod, adds = lookups(facts, analysis, plan)
    top, side = 12 * mm, 13 * mm
    W, H = A4[0] - 2 * side, A4[1] - 2 * top
    page1: list = []

    # header band
    ym = (f.get("today") or "")[:7].replace("-", "년 ") + "월"
    who = e(c.get("sex")) + (f" · 만 {e(c['age'])}세" if c.get("age") else "")
    left = [P(e(f"{c['name']} 님 맞춤 보장 제안"), _style("ph1", 17, 22, color=colors.white, bold=True)),
            P(e(f"보유 보험 {len(f['contracts'])}건과 병의원 이용 기록({f['records']['count']}건)을 함께 살펴본 결과입니다"),
              _style("psub", 8.8, 12, color=colors.HexColor("#d5def0"), spaceBefore=1 * mm))]
    right = P(f"{e(ym)}<br/>{who}", _style("pdate", 8.5, 12, color=colors.HexColor("#e0e6f2"), alignment=TA_RIGHT))
    head = Table([[left, right]], colWidths=[W - 32 * mm, 32 * mm])
    head.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("ROUNDEDCORNERS", [3 * mm] * 4),
                              ("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm), ("LEFTPADDING", (0, 0), (0, -1), 5 * mm),
                              ("RIGHTPADDING", (-1, 0), (-1, -1), 5 * mm)]))
    page1.append(head)

    # 1. strengths cards
    page1.append(NumHead(1, "지금 잘 갖춰진 보장"))
    cards = a.get("strengths", [])[:3]
    if cards:
        gap = 2.5 * mm
        cw = (W - 2 * gap) / 3
        cells: list = ["", "", "", "", ""]
        paras = [P(e(txt), s.td) for txt in cards]
        ch = max(x.wrap(cw - 6 * mm, H)[1] for x in paras) + 6 * mm
        for i, para in enumerate(paras):
            t = box([para], cw, colors.HexColor("#eef8f1"), colors.HexColor("#b9e0c6"), 2.5 * mm,
                    (2.5 * mm, 3 * mm), height=ch)
            t.spaceBefore = t.spaceAfter = 0
            cells[2 * i] = t
        row = Table([cells], colWidths=[cw, gap, cw, gap, cw])
        row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")] +
                                [(k, (0, 0), (-1, -1), 0) for k in ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING", "BOTTOMPADDING")]))
        page1.append(row)

    # 2. claims
    page1.append(NumHead(2, "찾아드릴 수 있는 보험금 (청구 확인)"))
    rows = [[P("진료", s.th), P("청구 가능 보험", s.th)]]
    rows += [[P(e(x.get("title")), s.td), P(e(x.get("customer_text")), s.td)] for x in a.get("claims", [])[:3]]
    page1.append(grid(rows, [0.36 * W, 0.64 * W], pad=(1.2 * mm, 1.8 * mm)))

    # 3. gaps
    page1.append(NumHead(3, "보완을 권해 드리는 보장"))
    rows = [[P("순위", s.th), P("보장", s.th), P("지금 → 제안", s.th), P("왜 필요한가요", s.th), P("예상 월 보험료", s.thn)]]
    for g in a.get("gaps", [])[:5]:
        pr, r = prod[g["product_key"]], adds.get(g["product_key"]) or {}
        cur = e(man(pr.get("current_amount")))
        if not pr.get("current_amount"):
            cur = f'<font color="#b33a12"><b>{cur}</b></font>'
        rows.append([P(tag(g["rank"], "hi" if g["rank"] <= 3 else "mid"), _style("pc", 8.5, alignment=TA_CENTER)),
                     P(f"<b>{e(pr['label'])}</b>", s.td), P(f"{cur} → {e(pr.get('proposal'))}", s.td),
                     P(e(g.get("customer_text")), s.td), P(f"{manwon(r.get('min'))}~{manwon(r.get('max'))}", s.tdn)])
    page1.append(grid(rows, [10 * mm, 0.22 * W, 0.17 * W, W - 10 * mm - 0.39 * W - 23 * mm, 23 * mm],
                      pad=(1.2 * mm, 1.8 * mm)))

    # 4. premium flow
    page1.append(NumHead(4, "보험료 전후 비교 (월)"))
    d2 = ""
    if p["paid_up"]:
        d2 += f"납입 끝난 계약 −{manwon(p['paid_up'])}<br/>"
    if p["removed"]:
        d2 += f"중복 정리 −{manwon(p['removed'])}"
    ow = 6 * mm
    sw = (W - 3 * ow) / 4
    now = p["stated"] or p["base"]
    stages = [_stage("지금(보고서 기준)", manwon(now), f"{won(now)}원", "", sw),
              _stage("정리 후", f"약 {manwon(p['after_opt'])}", d2 or "&nbsp;", "down", sw),
              _stage("보강 추가", f"약 {manwon(p['add_mid'])}", f"{manwon(p['add_min'])}~{manwon(p['add_max'])}", "up", sw),
              _stage("제안 후 합계", f"약 {manwon(p['total_mid'])}",
                     f"범위 {manwon(p['total_min'])}~{manwon(p['total_max'])}", "total", sw)]
    op = _style("op", 12, 14, color=NAVY, bold=True, alignment=TA_CENTER)
    flow = Table([[stages[0], P("→", op), stages[1], P("+", op), stages[2], P("=", op), stages[3]]],
                 colWidths=[sw, ow, sw, ow, sw, ow, sw])
    flow.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")] +
                             [(k, (0, 0), (-1, -1), 0) for k in ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING", "BOTTOMPADDING")]))
    page1.append(flow)

    # notice + sign
    note = ""
    if c.get("next_insurance_age_date"):
        note = (f'<font color="#b3540f"><b>보험나이 변경일: {e(c["next_insurance_age_date"])}</b></font><br/>'
                "이날이 지나면 보험나이가 한 살 올라 보험료가 오를 수 있습니다.<br/>")
    note += "예상 보험료는 참고 금액이며, 실제 견적으로 확정해 드립니다."
    sign = Table([[P("담당 설계사 ______________________<br/>연락처 ______________________",
                     _style("sign", 8.6, 16, color=MUTED))]], colWidths=[62 * mm])
    sign.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.75, FILL, None, (2, 2)), ("ROUNDEDCORNERS", [2.5 * mm] * 4),
                              ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                              ("LEFTPADDING", (0, 0), (-1, -1), 3.5 * mm)]))
    inner = W - 7 * mm
    notice = Table([[P(note, _style("note", 8.9, 12.6)), sign]], colWidths=[inner - 62 * mm, 62 * mm])
    notice.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")] +
                               [(k, (0, 0), (-1, -1), 0) for k in ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING", "BOTTOMPADDING")]))
    nb = box([notice], W, WARN_BG, ORANGE_LINE, 2.5 * mm, (2.5 * mm, 3.5 * mm), line=1.1)
    nb.spaceBefore, nb.spaceAfter = 3.5 * mm, 0
    page1.append(nb)
    foot = P("본 제안은 보장분석 보고서와 병의원 이용 기록을 바탕으로 한 상담 참고자료이며, 보험금 지급 여부와 금액은 약관 · 증권 및 "
             "진료 내용에 따라 달라집니다. 건강 위험도는 통계적 예측으로 의학적 진단이 아닙니다. 새 보험 가입 시에는 최근 진료 · "
             "투약 내용을 사실대로 알려 주셔야 보장을 받을 수 있습니다.",
             _style("foot", 7.6, 10.5, color=colors.HexColor("#6b7385"), spaceBefore=3 * mm))
    page1.append(foot)

    # page 1 is the customer's one-pager: shrink rather than spill onto page 2
    out: list = [KeepInFrame(W, H - 1, page1, mode="shrink")]
    out.append(PageBreak())

    # ── page 2: planner checklist
    ck = S(8)
    fill = _style("fill", 8, color=FILL)
    box_c = _style("ckb", 11, 12, color=MUTED, alignment=TA_CENTER)
    page2: list = []
    badge = Table([[P("설계사용 · 고객에게는 1쪽만 전달", _style("p2w", 8.2, 10.5, color=ORANGE, bold=True,
                                                    alignment=TA_CENTER))]], colWidths=[50 * mm])
    badge.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.75, ORANGE), ("ROUNDEDCORNERS", [1.5 * mm] * 4),
                               ("TOPPADDING", (0, 0), (-1, -1), 0.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2 * mm)]))
    t2 = Table([[P("제안 전 확인 필수 사항", _style("p2h", 14, 18, color=ORANGE, bold=True)), badge]],
               colWidths=[W - 50 * mm, 50 * mm])
    t2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")] +
                           [(k, (0, 0), (-1, -1), 0) for k in ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING", "BOTTOMPADDING")]))
    rule = Table([[""]], colWidths=[W], rowHeights=[1])
    rule.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 1.5, ORANGE)]))
    rule.spaceBefore, rule.spaceAfter = 1.5 * mm, 2 * mm
    page2 += [t2, rule]

    def h3(text: str) -> None:
        page2.append(CondPageBreak(25 * mm))
        page2.append(P(e(text), s.h3))

    cpad = (0.9 * mm, 1.6 * mm)
    cb = 7 * mm

    def checklist(rows: list[list], widths: list[float], header: list[str] | None) -> None:
        body = [[P("□", box_c)] + r for r in rows]
        if header:
            body = [[""] + [P(h, ck.th) for h in header]] + body
        if body:
            page2.append(grid(body, [cb] + widths, header=bool(header), pad=cpad,
                              extra=[("VALIGN", (0, 0), (0, -1), "MIDDLE")]))

    h3("A. 보험료 전후 비교의 전제")
    rows = []
    for x in [x for x in f["premium"].get("paid_up_likely", []) if not x.get("confirmed")]:
        rows.append([P(f"<b>{e(x.get('label'))} 납입완료 여부</b> ({e(x.get('basis'))})", ck.td),
                     P(f"아니면 합계 +{manwon(x.get('monthly'))}", ck.td), P("증권·자동이체 → ________", fill)])
    for x in p["reductions"]:
        rows.append([P(f"<b>{e(x['no'])}. {e(x['label'])} 감액 시 절감액</b>", ck.td), P(e(x.get("caution")), ck.td),
                     P("특약별 보험료 → ________", fill)])
    for x in p["removals"]:
        rows.append([P(f"<b>{e(x['no'])}. {e(x['label'])} 정리 동의</b>", ck.td), P(e(x.get("caution")), ck.td),
                     P("고객 동의 → ________", fill)])
    rest = W - cb
    checklist(rows, [0.40 * W, 0.26 * W, rest - 0.66 * W], ["확인할 것", "영향", "결과"])

    h3("B. 추가 권유 보험료 실제 견적 (현재는 추정)")
    rows = [[P(f"{e(x.get('label'))} ({e(x.get('proposal'))})" + (f" — {e(x['note'])}" if x.get("note") else ""), ck.td),
             P(f"{manwon(x.get('min'))}~{manwon(x.get('max'))}", ck.td), P("________________________", fill)]
            for x in p["adds"]]
    checklist(rows, [0.46 * W, 0.16 * W, rest - 0.62 * W], ["담보", "추정(월)", "실제 견적 (보험사 · 월 보험료)"])

    h3("C. 가입 심사 · 알릴의무")
    rows = [[P(e(x), ck.td)] for x in a.get("cautions", [])]
    rows += [[P(f"<b>정기 진료 {e(r['provider'])}</b> ({e(r['from'])}~{e(r['to'])}, {e(r['months'])}개월) — "
                "진단명·약 확인 후 고지 기준 판단", ck.td)] for r in f.get("recurring", [])]
    checklist(rows, [rest], None)

    h3("D. 보험금 청구 (제안 전에 먼저)")
    checklist([[P(f"<b>{e(x.get('title'))}</b> — {e(x.get('check'))}", ck.td)] for x in a.get("claims", [])], [rest], None)

    h3("E. 일정 · 준수")
    rows = []
    if c.get("next_insurance_age_date"):
        rows.append([P(f"<b>상령일 {e(c['next_insurance_age_date'])} 이전 청약</b> 일정 확보", ck.td)])
    rows.append([P("기존 계약 감액·해지 동반 시 <b>비교안내 의무</b> 이행·기록. 보호 계약"
                   f"({e(', '.join(str(x) for x in f.get('protected_contracts', [])))}) 해지 권유 금지", ck.td)])
    checklist(rows, [rest], None)

    # the checklist stays on one page when a mild shrink is enough; a very long one flows on instead
    if _fit(page2, W, H) <= H / 0.8:
        out.append(KeepInFrame(W, H - 1, page2, mode="shrink"))
    else:
        out += page2

    buf = io.BytesIO()
    _doc(buf, f"{c['name']} 님 맞춤 보장 제안", top, side).build(out)
    return buf.getvalue()
