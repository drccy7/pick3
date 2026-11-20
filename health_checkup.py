"""건강검진 결과 PDF detection (text layer only, no Gemini).

Terms come from the national general health checkup result form (일반건강검진 결과통보서) and the
judgment words every checkup report uses. A detailed medical bill can list lab names too, so a checkup
needs report words (결과·판정) as well as several measurement names.
"""

from __future__ import annotations

import io
import re

REPORT_WORDS = ("건강검진", "검진결과", "결과통보", "종합판정", "종합소견", "의심질환", "판정", "정상A", "정상B", "질환의심", "유질환")
MEASURES = ("신장", "체중", "허리둘레", "체질량지수", "혈압", "혈색소", "공복혈당", "총콜레스테롤", "HDL", "LDL",
            "중성지방", "크레아티닌", "사구체여과율", "AST", "ALT", "감마지티피", "요단백", "흉부방사선", "흉부촬영", "시력", "청력")


def is_checkup_text(text: str) -> bool:
    t = re.sub(r"\s+", "", text)
    report = sum(w in t for w in REPORT_WORDS)
    measures = sum(m in t for m in MEASURES)
    return report >= 2 and measures >= 5


def is_checkup_pdf(raw: bytes, pages: int = 4) -> bool:
    if raw[:5] != b"%PDF-":
        return False
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = " ".join((p.extract_text() or "") for p in pdf.pages[:pages])
    except Exception:
        return False
    return is_checkup_text(text)


# ---- reading the results: items, the form's own reference ranges, and its 판정 ------------------------------
# Generic: columns are found by header names, and an item is flagged only against the reference printed on the
# same form (or when the form's own result text says 의심/경계/양성). No reference table of our own.
NUM = r"(\d+(?:\.\d+)?)"
# Result words of the national checkup form that mean "not normal" (e.g. 과체중, 유질환자, 당뇨병 의심).
FLAG_WORDS = ("의심", "경계", "양성", "이상", "주의", "과체중", "비만", "저체중", "유질환")


def _sq(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def _nums(s: str) -> list[float]:
    return [float(x) for x in re.findall(NUM, s or "")]


def _ref_for_sex(ref: str, sex: str | None) -> str:
    """'남 13~16.5 여 12~15.5' → the part for this sex (or '' when it can't be told)."""
    parts = re.split(r"(남|여)", ref or "")
    if len(parts) < 3:
        return ref or ""
    if not sex:
        return ""
    for i in range(1, len(parts) - 1, 2):
        if parts[i] == sex:
            return parts[i + 1]
    return ""


def judge(result: str, ref: str, sex: str | None = None) -> str | None:
    """'high' | 'low' | 'flag' (the form's own words) | 'normal' | None (can't tell)."""
    res = result or ""
    if any(w in _sq(res) for w in FLAG_WORDS):
        return "flag"
    r = _ref_for_sex(ref, sex)
    vals = _nums(res)
    if not r or not vals:
        if "음성" in _sq(r) and "양성" in _sq(res):
            return "flag"
        return None
    if "/" in r and "/" in res:  # blood pressure: 120/80 미만
        lim, got = _nums(r)[:2], vals[:2]
        if len(lim) == 2 and len(got) == 2:
            return "high" if got[0] >= lim[0] or got[1] >= lim[1] else "normal"
        return None
    v = vals[0]
    if v == 0:
        return None  # 0 on a lab value = not measured that year (e.g. 콜레스테롤 every 4 years)
    m = re.search(NUM + r"\s*~\s*" + NUM, r)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return "low" if v < lo else "high" if v > hi else "normal"
    m = re.search(NUM + r"\s*(이하|미만)", r)
    if m:
        lim = float(m.group(1))
        return "high" if (v > lim if m.group(2) == "이하" else v >= lim) else "normal"
    m = re.search(NUM + r"\s*(이상|초과)", r)
    if m:
        lim = float(m.group(1))
        return "low" if (v < lim if m.group(2) == "이상" else v <= lim) else "normal"
    return None


def _cols(row: list) -> dict[str, int]:
    names = {"구분": "group", "검사항목": "name", "결과": "result", "참고치": "ref", "대상여부": "target"}
    return {names[_sq(c)]: i for i, c in enumerate(row) if _sq(c) in names}


def parse_tables(tables: list[list[list]], head_text: str = "", sex: str | None = None) -> dict:
    """Checkup tables (cut to the checkup pages) → {date, provider, judgments, items, other}.

    Read as one stream of rows, because the form's blocks may print as one table or several and a row can
    continue on the next page: a 검진일 row (date, provider), a 판정 block (the form's own conclusions), and
    result tables whose header row (구분 | 검사항목 | 결과 | 참고치, or 구분 | 대상여부 | 결과) resets the columns.
    A row with no item name but a reference continues the previous item's reference (e.g. 남/여 on two lines)."""
    out = {"date": None, "provider": None, "judgments": [], "items": [], "other": []}
    mode, cols, group = None, {}, ""

    for r in (row for t in tables for row in t):
        raw_cells = [re.sub(r"\s+", " ", str(c or "")).strip() for c in r]
        cells = [_sq(c) for c in r]

        def after(key: str) -> str:  # the first non-empty cell to the right of a label
            return next((c for c in raw_cells[cells.index(key) + 1:] if c), "")
        if "검진일" in cells:
            d = re.search(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2})", after("검진일"))
            out["date"] = f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else out["date"]
            if "검진기관명" in cells:
                out["provider"] = after("검진기관명") or None
            mode = "head"
            continue
        first = next((c for c in raw_cells if c), "")
        if _sq(first) == "판정":
            mode = "judge"
            continue
        c = _cols(r)
        if "result" in c and ("name" in c or "target" in c):
            mode, cols, group = "items", c, ""
            continue
        if mode == "judge":
            if first:
                out["judgments"].append(first)
            continue
        if mode != "items":
            continue

        def cell(k):
            return raw_cells[cols[k]] if k in cols and cols[k] < len(raw_cells) else ""
        group = cell("group") or group
        if "target" in cols:  # 구분 | 대상여부 | 결과: e.g. B형간염, 골밀도
            if cell("group") or cell("result"):
                out["other"].append({"group": group, "target": cell("target"), "result": cell("result")})
            continue
        name, result, ref = cell("name"), cell("result"), cell("ref")
        if not name:
            if ref and not result and out["items"]:  # the previous item's reference goes on
                it = out["items"][-1]
                it["ref"] = ((it["ref"] or "") + " " + ref).strip()
                it["status"] = judge(it["result"], it["ref"], sex)
            continue
        if result:
            out["items"].append({"group": group, "name": name, "result": result, "ref": ref or None,
                                 "status": judge(result, ref, sex)})
    if not out["date"]:
        m = re.search(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2})", head_text)
        out["date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
    return out


def person(text: str) -> tuple[str, str | None]:
    """(name, sex) from the form header. Sex comes from the digit after the birth date; nothing else is kept."""
    m = re.search(r"성\s*명\s*[:：]?\s*([가-힣]{2,10})", text or "")
    s = re.search(r"\d{6}\s*-?\s*([1-4])\*", text or "")
    return (m.group(1) if m else ""), ({"1": "남", "3": "남", "2": "여", "4": "여"}[s.group(1)] if s else None)


def parse_pdf(raw: bytes, pages: list[int] | None = None) -> dict:
    """A checkup PDF (or the 검진 pages of a 나의건강기록) → parse_tables(...) + name/sex."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        nos = pages or list(range(1, len(pdf.pages) + 1))
        head = " ".join((pdf.pages[n - 1].extract_text() or "") for n in nos[:1])
        name, sex = person(head)
        tables = [t for n in nos for t in pdf.pages[n - 1].extract_tables()]
    res = parse_tables(tables, head, sex)
    return {"name": name, "sex": sex, **res}


def facts(exams: list[dict], keep: int = 3) -> list[dict]:
    """Newest exams first (E1…), each with the form's 판정 lines and only the items that are not normal (C…)."""
    out, n = [], 0
    for i, e in enumerate(sorted(exams, key=lambda e: e.get("date") or "", reverse=True)[:keep], 1):
        flagged = []
        for it in e.get("items", []):
            if it.get("status") in ("high", "low", "flag"):
                n += 1
                flagged.append({"id": f"C{n}", "name": it["name"], "result": it["result"], "ref": it.get("ref"),
                                "status": it["status"]})
        out.append({"id": f"E{i}", "date": e.get("date"), "provider": e.get("provider"),
                    "judgments": e.get("judgments", [])[:12], "tested": len(e.get("items", [])), "flagged": flagged})
    return out
