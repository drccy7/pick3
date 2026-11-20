"""나의건강기록 exports: section split (any order, shared page), 진료이력/투약이력 rows, saving. Fictitious data."""

from __future__ import annotations

import asyncio
import io

import pytest

import mhr

rl = pytest.importorskip("reportlab")  # test-only: builds synthetic PDFs with real table rulings
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

FONT = "HYSMyeongJo-Medium"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))
H = 842

VISIT_HEAD = [["진료일자", "진료기관", "상병명", "방문일수", "처방횟수", "투약(요양)일수"],
              ["진료구분", "방문위치", "", "", "", ""]]
VISITS = [["2026-03-06", "가나내과의원", "본태성 고혈압", "1", "1", "30"], ["방문 외래", "서울시 가나구", "", "", "", ""],
          ["2026-03-06", "다라약국", "본태성 고혈압", "1", "0", "30"], ["처방 조제", "서울시 가나구", "", "", "", ""],
          ["2026-05-02", "마바병원", "요통", "3", "1", "3"], ["입원", "서울시 마바구", "", "", "", ""]]
MED_HEAD = [["조제일자", "조제기관", "제품명", "성분명", "약품코드", "함량", "1회투약량", "1일투약횟수", "총투약일수"]]
MEDS = [["2026-03-06", "다라약국", "가나정5밀리그램", "amlodipine", "111", "5mg", "1", "1", "30"],
        ["", "", "다라정", "aspirin", "222", "100mg", "1", "1", "30"],
        ["2026-05-02", "마바약국", "사아정", "acetaminophen", "333", "500mg", "1", "3", "3"]]
CHECK = [["구분", "검사항목", "결과", "참고치"], ["계측", "혈압", "120/80", "120/80 미만"]]


def _table(c, top: float, rows: list[list[str]], width: float = 500) -> float:
    n = len(rows[0])
    w, h = width / n, 16
    for i, row in enumerate(rows):
        y = H - top - (i + 1) * h
        for j, cell in enumerate(row):
            c.rect(40 + j * w, y, w, h)
            c.setFont(FONT, 7.5)
            c.drawString(42 + j * w, y + 5, cell)
    return top + len(rows) * h


def _title(c, top: float, heading: str) -> float:
    c.setFont(FONT, 20)
    c.drawString(40, H - top, "나의건강기록")
    c.setFont(FONT, 9)
    c.drawString(40, H - top - 22, "성명 홍길동 생년월일 600101-1******")
    c.setFont(FONT, 12)
    c.drawString(40, H - top - 50, heading)
    return top + 60


def build(parts: list[str], shared: str = "") -> bytes:
    """parts in order: visits / meds / checkup. checkup spans 2 pages (heading only on the first).
    shared="footer": checkup starts mid-page below the previous part's footer (as in real combined exports);
    shared="table": it starts on the same page as the previous part's table."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, H))
    for k, part in enumerate(parts):
        if k and not (shared == "table" and part == "checkup"):
            c.showPage()
        if part == "visits":
            _table(c, _title(c, 40, "진료이력"), VISIT_HEAD + VISITS)
        elif part == "meds":
            _table(c, _title(c, 40, "투약이력"), MED_HEAD + MEDS)
        else:
            top = 40
            if shared and k:
                c.setFont(FONT, 8)
                c.drawString(40, H - (300 if shared == "table" else 60), "위의 진료기록은 법정 의무기록으로 인정되지 않습니다.")
                top = 330
            _table(c, _title(c, top, "건강검진(일반)"), CHECK)
            c.showPage()
            _table(c, 60, CHECK)  # continuation page without a heading
    c.save()
    return buf.getvalue()


def test_detects_only_the_app_export() -> None:
    assert mhr.is_mhr_pdf(build(["visits"])) and not mhr.is_mhr_pdf(b"%PDF-1.4 nothing") and not mhr.is_mhr_pdf(b"x")


@pytest.mark.parametrize("parts,shared,expect", [
    (["visits", "meds", "checkup"], "footer", [("visits", [1]), ("meds", [2]), ("checkup", [3, 4])]),
    (["visits", "meds", "checkup"], "table", [("visits", [1]), ("meds", [2]), ("checkup", [2, 3])]),
    (["checkup", "visits", "meds"], "", [("checkup", [1, 2]), ("visits", [3]), ("meds", [4])]),
    (["visits", "meds"], "", [("visits", [1]), ("meds", [2])]),
    (["meds"], "", [("meds", [1])]),
    (["checkup"], "", [("checkup", [1, 2])]),
])
def test_sections_in_any_order(parts, shared, expect) -> None:
    got = [(s["section"], s["pages"]) for s in mhr.split_sections(build(parts, shared))]
    assert got == expect


@pytest.mark.parametrize("shared,checkup_pages", [("footer", "3~4쪽"), ("table", "2~3쪽")])
def test_rows_from_visits_and_meds_in_a_combined_file(shared, checkup_pages) -> None:
    r = mhr.read(build(["visits", "meds", "checkup"], shared))
    assert r["patient"] == "홍길동"
    assert [(v["visit_date"], v["provider"], v["diagnosis_name"], v["visit_type"], v["kind"], v["care_days"])
            for v in r["visits"]] == [
        ("2026-03-06", "가나내과의원", "본태성 고혈압", "외래", "병원", 30),
        ("2026-03-06", "다라약국", "본태성 고혈압", "약국", "약국", 30),
        ("2026-05-02", "마바병원", "요통", "입원", "병원", 3)]
    assert [(m["visit_date"], m["provider"], m["drug_name"], m["ingredient"], m["times_per_day"], m["days"])
            for m in r["meds"]] == [
        ("2026-03-06", "다라약국", "가나정5밀리그램", "amlodipine", "1", "30"),
        ("2026-03-06", "다라약국", "다라정", "aspirin", "1", "30"),  # date/pharmacy carried to the 2nd drug
        ("2026-05-02", "마바약국", "사아정", "acetaminophen", "3", "3")]
    assert "🏥 진료이력 1쪽" in mhr.section_summary(r["sections"]) and f"🩺 건강검진 {checkup_pages}" in mhr.section_summary(r["sections"])


def test_save_visits_links_or_inserts_without_amount(monkeypatch) -> None:
    calls = []

    class Resp:
        def __init__(self, code, data):
            self.status_code, self._d, self.text = code, data, ""

        def json(self):
            return self._d

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            calls.append(("get", params))
            return Resp(200, [{"id": 5, "provider": "가나 내과의원", "visit_date": "2026-03-06",
                               "diagnosis_name": None, "visit_type": None, "visit_days": None, "care_days": None},
                              {"id": 6, "provider": "다라약국", "visit_date": "2026-03-06",
                               "diagnosis_name": "고혈압(I10)", "visit_type": "약국", "visit_days": 1, "care_days": 30}])

        async def patch(self, url, headers=None, json=None, params=None):
            calls.append(("patch", params["id"], json))
            return Resp(204, None)

        async def post(self, url, json=None, headers=None, params=None):
            calls.append(("post", json))
            return Resp(201, json)

    monkeypatch.setattr(mhr.httpx, "AsyncClient", Client)
    monkeypatch.setattr(mhr, "_sb", lambda: ("http://x/rest/v1/t", {}))
    r = mhr.read(build(["visits"]))
    out = asyncio.run(mhr.save_visits(r["visits"], 1, "홍길동"))
    assert out == {"linked": 2, "inserted": 1}
    patches = [c for c in calls if c[0] == "patch"]
    assert patches == [("patch", "eq.5", {"diagnosis_name": "본태성 고혈압", "visit_type": "외래", "visit_days": 1,
                                          "care_days": 30})]  # row 6 already had everything from 요양급여내역
    (post,) = [c[1] for c in calls if c[0] == "post"]
    assert post[0]["provider"] == "마바병원" and post[0]["amount"] is None and post[0]["source"] == "mhr"
