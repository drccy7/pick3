"""coverage_review: deterministic facts, guards on the LLM output, premium plan, template rendering. Fictitious data."""

from __future__ import annotations

from datetime import date

from coverage_review import facts as F
from coverage_review import plan, render

TODAY = date(2026, 11, 20)

REPORT = {
    "report": {"issuer": "예시분석", "analysis_date": "2026-11-17", "checkup_date": "2025-02-03",
               "stated_contract_count": 4, "stated_monthly_premium": 300000, "caveats": ["단독 실손은 합계 제외"]},
    "customer": {"name": "홍길동", "sex": "남", "birth_date": "1959-06-10", "age_in_report": 65.7},
    "contracts": [
        {"no": 1, "insurer": "가생명", "product": "종신", "start": "2005-11", "end": None, "pay_cycle": "월납",
         "premium": 100000, "paid_count": 240, "type": "종신·정기",
         "coverages": [{"name": "질병수술", "amount": 100, "unit": "만원", "category": "질병수술"}]},
        {"no": 2, "insurer": "나손보", "product": "실손", "start": "2025-04", "end": "2028-04", "pay_cycle": "월납",
         "premium": 100000, "paid_count": 19, "type": "실손",
         "coverages": [{"name": "외래", "amount": 20, "unit": "만원", "category": "실손통원"}]},
        {"no": 3, "insurer": "다손보", "product": "상해", "start": "2021-01", "end": "2031-01", "pay_cycle": "월납",
         "premium": 50000, "paid_count": 71, "type": "상해",
         "coverages": [{"name": "상해수술", "amount": 50, "unit": "만원", "category": "상해수술"}]},
        {"no": 4, "insurer": "라손보", "product": "치아", "start": "2018-02", "end": "2028-02", "pay_cycle": "월납",
         "premium": 50000, "paid_count": 106, "type": "치아",
         "coverages": [{"name": "크라운", "amount": 10, "unit": "만원", "category": "치아보존"}]},
    ],
    "summary": [{"item": "치아보철", "category": "치아보철", "recommended": 200, "current": 0, "unit": "만원", "status": "부족"}],
    "risks": [{"name": "고지혈증", "grade": "고위험"}],
}

RECORDS = [
    {"provider": "가나정형외과병원", "kind": "병원", "amount": 60000, "visit_date": "2024-04-11"},
    {"provider": "가나정형외과병원", "kind": "병원", "amount": 700000, "visit_date": "2024-04-15"},
    {"provider": "예시치과", "kind": "병원", "amount": 500000, "visit_date": "2025-12-02"},
    {"provider": "옛날병원", "kind": "병원", "amount": 900000, "visit_date": "2023-03-23"},
] + [
    {"provider": "동네의원", "kind": "병원", "amount": 1500, "visit_date": f"2025-{m:02d}-10"} for m in range(4, 13)
] + [
    {"provider": "동네약국", "kind": "약국", "amount": 21400, "visit_date": f"2025-{m:02d}-10"} for m in range(4, 13)
]


def _facts():
    return F.build(REPORT, RECORDS, TODAY)


def test_age_and_insurance_age_change() -> None:
    a = F.age_info(date(1959, 6, 10), TODAY)
    assert a["age"] == 67 and a["insurance_age"] == 67
    assert a["next_insurance_age_date"] == "2026-12-10" and a["insurance_age_after"] == 68


def test_report_age_mismatch_is_flagged() -> None:
    assert "만 67세" in _facts()["customer"]["age_note"]


def test_paid_up_detected_only_when_count_stops_on_whole_years() -> None:
    f = _facts()
    paid = {x["id"] for x in f["premium"]["paid_up_likely"]}
    assert paid == {"C1"}  # 240 payments since 2005-11 → ended 2025-10; others still paying
    assert f["premium"]["paying_now"] == 300000 - 100000
    assert f["premium"]["matches_stated"]


def test_amounts_are_converted_by_unit() -> None:
    assert F.to_won(100, "만원") == 1_000_000 and F.to_won(1.5, "억원") == 150_000_000


def test_records_split_by_indemnity_period_and_clusters_have_deadlines() -> None:
    f = _facts()
    assert f["records"]["with_indemnity"]["count"] == 19  # dental + 9 clinic + 9 pharmacy since 2025-04
    by_p = {k["provider"]: k for k in f["clusters"]}
    ortho = by_p["가나정형외과병원"]
    assert ortho["deadline"] == "2027-04-15" and not ortho["expired"]
    assert {i["contract"] for i in ortho["in_force"]} == {"C1", "C3"}  # no indemnity in 2024
    assert by_p["옛날병원"]["expired"]
    assert {i["category"] for i in by_p["예시치과"]["in_force"]} <= set(F.DENTAL_CATS)


def test_recurring_visit_with_same_day_pharmacy() -> None:
    r = _facts()["recurring"]
    assert r and r[0]["provider"] == "동네의원" and r[0]["same_day_pharmacy_visits"] == 9


def test_indemnity_estimate_excludes_dental_and_expired() -> None:
    est = _facts()["indemnity_estimate"]
    assert est["excluded_total"] == 500000 and est["days"] == 9
    assert est["estimate"] == 9 * (22900 - 10000)


def test_existing_indemnity_is_not_proposed_and_protected_contracts() -> None:
    f = _facts()
    assert "실손" not in {p["key"] for p in f["products"]}
    assert set(f["protected_contracts"]) >= {1, 2}


def _analysis():
    return {
        "key_points": ["요약"], "strengths": ["실손 있음"], "cautions": ["정기 처방 고지"],
        "claims": [{"rank": 2, "title": "정형외과", "evidence": ["K1", "ZZ9"], "insurance": "C1 질병수술",
                    "check": "수술 여부", "customer_text": "수술비"},
                   {"rank": 1, "title": "근거 없음", "evidence": ["X1"], "insurance": "-", "check": "-", "customer_text": "-"}],
        "gaps": [{"rank": 1, "product_key": "치아보철", "evidence": ["K2"], "record_basis": "치과 50만",
                  "report_basis": "보철 0", "talking_point": "-", "customer_text": "임플란트 보장 없음"},
                 {"rank": 2, "product_key": "없는상품", "evidence": [], "record_basis": "", "report_basis": "",
                  "talking_point": "", "customer_text": ""}],
        "optimization": [{"contract_no": 1, "action": "정리검토", "reason": "-", "caution": "-"},
                         {"contract_no": 3, "action": "정리검토", "reason": "중복", "caution": "법률비용 소멸"}],
    }


def test_sanitize_drops_unsupported_and_protects_contracts() -> None:
    f = _facts()
    a, warns = plan.sanitize(_analysis(), f)
    assert [c["title"] for c in a["claims"]] == ["정형외과"] and a["claims"][0]["evidence"] == ["K1"]
    assert [g["product_key"] for g in a["gaps"]] == ["치아보철"]
    assert {o["contract_no"]: o["action"] for o in a["optimization"]} == {1: "유지", 3: "정리검토"}
    assert any("ZZ9" in w for w in warns)


def test_premium_plan_before_after() -> None:
    f = _facts()
    a, _ = plan.sanitize(_analysis(), f)
    p = plan.premium_plan(f, a)
    assert p["paying_now"] == 200000 and p["removed"] == 50000 and p["after_opt"] == 150000
    rng = F.premium_range("치아보철", f["premium_table_age"])
    assert p["add_min"] == rng["min"] and p["total_mid"] == 150000 + (rng["min"] + rng["max"]) // 2


def test_templates_render_without_browser() -> None:
    f = _facts()
    a, w = plan.sanitize(_analysis(), f)
    p = plan.premium_plan(f, a)
    doc = render.html("analysis", f, a, p, w)
    assert "홍길동" in doc and "종합(제안 후)" in doc
    prop = render.html("proposal", f, a, p, w)
    assert "제안 전 확인 필수 사항" in prop and "임플란트 보장 없음" in prop


def test_money_formatters() -> None:
    assert render.man(135_000_000) == "1억 3,500만" and render.man(5_000_000) == "500만" and render.man(0) == "없음"
    assert render.manwon(52_380) == "5.2만" and render.manwon(1_172_238) == "117만"
