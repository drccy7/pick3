"""건강보험 요양급여내역 text → rows (fictitious lines in the national form's layout)."""

from __future__ import annotations

import nhis_claims as n

FORM = """건강보험 요양급여내역
발급번호 20260000X0000000 √ 전체 출력 선택 출력
성명 홍길동 주민등록번호 600101-1******
발급목적 개인 진료내역 확인용 발급기간 2025.11.20~2026.11.20
총 진료건수 외래 입원 약국 방문요양기관 수 총 진료비 공단부담금 본인부담금 100/100 미만
4 2 1 1 3 900,000 700,000 200,000
2026.03.06 1 가나내과의원 031-000-0000 13,240 0
1
외래 1 기타및상세불명의원발성고혈압 I109 5,600 0
2026.03.06 1 가나약국 031-000-0001 46,950 0
2
약국 60 20,100 0
2026.05.01 5 다라병원 02-000-0000 600,000 0
3
입원 5 무릎관절증 M179 150,000 0
2026.09.04 1 마바치과의원 13,000 0
4
외래 1 만성단순치주염 K0530 24,300 0
0001 / 0002
"""


def test_parse_rows_diagnoses_and_self_check(monkeypatch) -> None:
    monkeypatch.setattr(n, "_text", lambda raw: FORM)
    p = n.parse(b"%PDF")
    assert p["patient"] == "홍길동" and p["period"] == ("2025.11.20", "2026.11.20") and p["unparsed"] == 0
    rows = {r["provider"]: r for r in p["rows"]}
    assert rows["가나내과의원"]["diagnosis_code"] == "I109" and rows["가나내과의원"]["amount"] == 5600
    assert rows["가나약국"]["diagnosis_code"] is None and rows["가나약국"]["care_days"] == 60 and rows["가나약국"]["kind"] == "약국"
    assert rows["다라병원"]["visit_type"] == "입원" and rows["다라병원"]["visit_days"] == 5 and rows["다라병원"]["nhis_paid"] == 600000
    assert rows["마바치과의원"]["diagnosis_code"] == "K0530"  # no phone number on the line
    assert p["check"] == {"stated_count": 4, "stated_own": 200000, "ok": True}
    msg = n.summary_message(p, "홍길동", {"linked": 1, "inserted": 3, "skipped": 0})
    assert "입원 1건" in msg and "의증" in msg and "600101" not in msg


def test_self_check_flags_missing_rows(monkeypatch) -> None:
    monkeypatch.setattr(n, "_text", lambda raw: FORM.replace("외래 1 만성단순치주염 K0530 24,300 0\n", ""))
    p = n.parse(b"%PDF")
    assert p["unparsed"] == 1 and not p["check"]["ok"]
    assert "다릅니다" in n.summary_message(p, "홍길동", {"linked": 0, "inserted": 3, "skipped": 0})


def test_provider_match_ignores_spaces_and_brackets() -> None:
    assert n._norm("연세 본내과(의원)") == n._norm("연세본내과의원")
