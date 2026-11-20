"""건강·보험 분석 (no 보장분석 report): facts from records + meds, sanitize, render. Fictitious data."""

from __future__ import annotations

from datetime import date

from coverage_review import health

TODAY = date(2026, 11, 21)
RECORDS = [
    {"visit_date": "2026-10-02", "provider": "가나내과의원", "kind": "병원", "amount": 5600,
     "diagnosis_name": "본태성 고혈압", "diagnosis_code": "I109", "visit_type": "외래"},
    {"visit_date": "2026-09-01", "provider": "가나내과의원", "kind": "병원", "amount": None,
     "diagnosis_name": "본태성 고혈압", "visit_type": "외래"},  # 나의건강기록: no amount, no code
    {"visit_date": "2024-03-10", "provider": "마바병원", "kind": "병원", "amount": 820000,
     "diagnosis_name": "요추 추간판 장애", "visit_type": "입원"},
]
MEDS = [{"visit_date": "2026-10-02", "provider": "다라약국", "drug_name": "가나정", "ingredient": "amlodipine", "days": "30"},
        {"visit_date": "2026-09-01", "provider": "다라약국", "drug_name": "가나정", "ingredient": "amlodipine", "days": "30"},
        {"visit_date": "2026-05-02", "provider": "마바약국", "drug_name": "사아정", "ingredient": "acetaminophen", "days": "3"}]


def test_facts_answer_the_standard_questions() -> None:
    f = health.build("홍길동", RECORDS, MEDS, TODAY)
    assert f["records"]["count"] == 3 and f["records"]["without_amount"] == 1 and f["records"]["amount"] == 825600
    names = {d["name"]: d for d in f["diagnoses"]}
    assert names["본태성 고혈압"]["visits"] == 2 and names["본태성 고혈압"]["code"] == "I109"  # code from either row
    m = {x["ingredient"]: x for x in f["meds"]}
    assert m["amlodipine"]["total_days"] == 60 and m["amlodipine"]["long_term"] and not m["acetaminophen"]["long_term"]
    u = f["disclosure"]
    assert [x["date"] for x in u["within_3_months"]] == ["2026-09-01", "2026-10-02"]  # pharmacy excluded
    assert [x["provider"] for x in u["inpatient_5_years"]] == ["마바병원"]
    assert [x["ingredient"] for x in u["meds_30_days"]] == ["amlodipine"]


def test_sanitize_keeps_only_known_products_with_evidence_and_renders() -> None:
    f = health.build("홍길동", RECORDS, MEDS, TODAY)
    d = next(x["id"] for x in f["diagnoses"] if x["name"] == "본태성 고혈압")
    m = f["meds"][0]["id"]
    key = f["products"][1]["key"]
    a = {"key_points": ["고혈압으로 정기 진료와 투약 중"],
         "needs": [{"product_key": key, "priority": 1, "reason": "고혈압 정기 진료", "evidence": [d, "X9"]},
                   {"product_key": "없는상품", "priority": 2, "reason": "?", "evidence": [d]},
                   {"product_key": f["products"][0]["key"], "priority": 3, "reason": "근거 없음", "evidence": ["Z1"]}],
         "disclosure_notes": [{"text": "30일 이상 투약은 사실대로 고지", "evidence": [m]}], "cautions": ["검진 미반영"]}
    res = health.run("홍길동", RECORDS, MEDS, today=TODAY, analysis=a)
    clean_needs = [n["product_key"] for n in health.sanitize(a, f)[0]["needs"]]
    assert clean_needs == [key] and len(res["warnings"]) == 2
    (name, page), = res["html"].items()
    assert name == "홍길동_건강보험분석_2026-11-21.html"
    for w in ("건강·보험 분석", "본태성 고혈압", "amlodipine", "30일 이상 투약", "가입 심사", "의학적 진단이 아닙니다"):
        assert w in page, w


def test_checkup_reading_uses_the_forms_own_reference() -> None:
    import health_checkup as H
    assert H.judge("130 당뇨병 의심", "100.0 미만") == "flag"  # the form's own words
    assert H.judge("150", "100.0 미만") == "high" and H.judge("95", "100.0 미만") == "normal"
    assert H.judge("38", "60.0 이상") == "low" and H.judge("41", "40.0 이하") == "high"
    assert H.judge("14.5 정상", "남 13.0~16.5 여 12.0~15.5", "남") == "normal"
    assert H.judge("12.5", "남 13.0~16.5 여 12.0~15.5", "남") == "low"
    assert H.judge("12.5", "남 13.0~16.5 여 12.0~15.5") is None  # sex unknown: can't tell
    assert H.judge("135/85", "120/80 미만") == "high" and H.judge("정상", "") is None
    rows = [["성명", "홍길동", "검진일", "2026-01-02", "검진기관명", "가나의원"], ["판정", None], ["정상B(경계) 비만", None],
            ["구분", "검사항목", "결과"], ["계측검사", "체질량지수(kg/m²)", "26.1 과체중"],
            ["구분", "검사항목", "결과", "참고치"], ["", "혈색소(g/dL)", "14.0 정상", "남 13.0~16.5"]]
    page2 = [["구분", "검사항목", "결과", "참고치"], ["혈액검사", "", "", "여 12.0~15.5"],
             [None, "공복혈당(mg/dL)", "130 당뇨병 의심", "100.0 미만"]]
    r = H.parse_tables([rows, page2], sex="남")
    assert (r["date"], r["provider"], r["judgments"]) == ("2026-01-02", "가나의원", ["정상B(경계) 비만"])
    assert [(i["name"], i["status"]) for i in r["items"]] == [
        ("체질량지수(kg/m²)", "flag"), ("혈색소(g/dL)", "normal"), ("공복혈당(mg/dL)", "flag")]
    assert r["items"][1]["ref"] == "남 13.0~16.5 여 12.0~15.5"  # continued on the next page
    f = H.facts([r])
    assert f[0]["id"] == "E1" and [c["id"] for c in f[0]["flagged"]] == ["C1", "C2"]
    assert H.person("성명 홍길동 생년월일 6001011******") == ("홍길동", "남")


def test_health_analysis_shows_checkup_findings() -> None:
    import health_checkup as H
    exam = {"date": "2026-01-02", "provider": "가나의원", "judgments": ["일반질환의심"],
            "items": [{"name": "공복혈당", "result": "130 당뇨병 의심", "ref": "100 미만", "status": "flag"}]}
    chk = H.facts([exam])
    a = {"key_points": ["당뇨 의심"], "needs": [], "cautions": [],
         "disclosure_notes": [{"text": "검진 당뇨 의심 소견 고지 확인", "evidence": ["C1", "X1"]}]}
    res = health.run("홍길동", RECORDS, MEDS, today=TODAY, analysis=a, checkups=chk)
    page = next(iter(res["html"].values()))
    assert "3. 건강검진" in page and "130 당뇨병 의심" in page and "일반질환의심" in page
    assert health.sanitize(a, res["facts"])[0]["disclosure_notes"][0]["evidence"] == ["C1"]


def test_zero_lab_value_is_not_measured() -> None:
    import health_checkup as H
    assert H.judge("0", "60.0 이상") is None
