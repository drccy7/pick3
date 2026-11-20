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
