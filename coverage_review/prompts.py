"""Prompts + response schemas. Written for ANY insurer's coverage-analysis report, not one vendor's layout."""

from __future__ import annotations

# Standard coverage categories (Korean market terms). Extraction maps every 담보 to one of these;
# code aggregates by category, so reports with different wording still compare.
CATEGORIES = [
    "상해사망", "질병사망", "상해후유장해", "질병후유장해",
    "장기요양·간병진단", "간병인·간호간병일당", "치매진단",
    "일반암진단", "유사암·소액암진단", "고액암진단", "암수술", "암치료비", "암입원",
    "뇌혈관질환진단", "뇌졸중진단", "뇌출혈진단", "뇌·심장 수술·치료",
    "허혈성심장질환진단", "급성심근경색진단", "기타심장질환진단",
    "질병수술", "질병종수술", "특정질병수술", "상해수술", "상해종수술",
    "질병입원일당", "상해입원일당",
    "실손입원", "실손통원", "실손처방",
    "치아보존", "치아보철", "치주",
    "골절·깁스·화상", "운전자비용", "자동차사고부상",
    "일상배상", "화재", "자동차보험", "기타",
]

_AMOUNT = {"type": "NUMBER", "nullable": True, "description": "표에 적힌 숫자 그대로(단위 환산 금지)"}
_UNIT = {"type": "STRING", "enum": ["원", "천원", "만원", "억원", "미상"]}

EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "report": {"type": "OBJECT", "properties": {
            "issuer": {"type": "STRING", "description": "보고서를 만든 회사/서비스"},
            "analysis_date": {"type": "STRING", "nullable": True, "description": "YYYY-MM-DD"},
            "checkup_date": {"type": "STRING", "nullable": True, "description": "건강검진일 YYYY-MM-DD"},
            "stated_contract_count": {"type": "INTEGER", "nullable": True},
            "stated_monthly_premium": {"type": "NUMBER", "nullable": True, "description": "보고서가 적은 월 보험료 합계(원)"},
            "caveats": {"type": "ARRAY", "items": {"type": "STRING"},
                        "description": "합계 제외 계약, 조회 기준 등 보고서의 유의사항 원문 요약"},
        }, "required": ["issuer", "caveats"]},
        "customer": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"},
            "sex": {"type": "STRING", "enum": ["남", "여", "미상"]},
            "birth_date": {"type": "STRING", "nullable": True, "description": "YYYY-MM-DD"},
            "age_in_report": {"type": "NUMBER", "nullable": True},
            "insurance_age_date": {"type": "STRING", "nullable": True, "description": "상령일 YYYY-MM-DD"},
        }, "required": ["name", "sex"]},
        "contracts": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "no": {"type": "INTEGER"},
            "insurer": {"type": "STRING"},
            "product": {"type": "STRING"},
            "start": {"type": "STRING", "description": "계약 시작 YYYY-MM"},
            "end": {"type": "STRING", "nullable": True, "description": "만기 YYYY-MM. 종신·9999는 null"},
            "pay_cycle": {"type": "STRING", "enum": ["월납", "3개월납", "6개월납", "연납", "일시납", "미상"]},
            "premium": {"type": "NUMBER", "nullable": True, "description": "1회 보험료(원)"},
            "paid_count": {"type": "INTEGER", "nullable": True, "description": "납입 횟수"},
            "paid_total": _AMOUNT, "paid_total_unit": _UNIT,
            "type": {"type": "STRING", "enum": ["실손", "건강·종합", "암", "종신·정기", "상해", "운전자",
                                                 "치아", "화재·가정", "자동차", "연금·저축", "기타"]},
            "coverages": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "name": {"type": "STRING"},
                "amount": _AMOUNT, "unit": _UNIT,
                "category": {"type": "STRING", "enum": CATEGORIES},
            }, "required": ["name", "category", "unit"]}},
        }, "required": ["no", "insurer", "product", "start", "pay_cycle", "type", "coverages"]}},
        "summary": {"type": "ARRAY", "description": "보고서의 보장 요약표(권장/가입/부족) 행", "items": {
            "type": "OBJECT", "properties": {
                "item": {"type": "STRING"},
                "category": {"type": "STRING", "enum": CATEGORIES},
                "recommended": _AMOUNT, "current": _AMOUNT, "unit": _UNIT,
                "status": {"type": "STRING", "description": "부족/보통/적절 등 보고서 표현"},
            }, "required": ["item", "category", "unit"]}},
        "risks": {"type": "ARRAY", "description": "질병·암 위험도", "items": {"type": "OBJECT", "properties": {
            "name": {"type": "STRING"}, "grade": {"type": "STRING"},
            "relative": {"type": "STRING", "nullable": True, "description": "동년배 대비 등 수치 원문"},
        }, "required": ["name", "grade"]}},
    },
    "required": ["report", "customer", "contracts", "summary", "risks"],
}

EXTRACT_PROMPT = """첨부한 PDF는 한 고객의 보험 보장분석 보고서입니다(보험사·분석 서비스마다 양식이 다릅니다).
보고서에 적힌 사실만 정해진 JSON으로 옮기세요. 계산·추정·의견을 넣지 마세요.

규칙
- 보유 계약은 모두 빠짐없이. 계약 목록 표와 계약별 상세 페이지를 함께 보고 같은 계약을 하나로 합치세요.
- 담보(특약)는 상세 페이지의 담보명과 가입금액을 한 행씩. 표가 2단(좌우)이면 담보명과 금액의 짝을 같은 칸끼리 맞추세요.
- 금액은 표에 적힌 숫자 그대로 쓰고 unit에 단위를 적으세요(표 머리에 '단위: 만원'이면 만원). 환산하지 마세요.
- 보험료(premium)는 1회 보험료를 원 단위 숫자로.
- category는 목록 중 가장 가까운 것 하나. 실손형 의료비(입원/통원·외래/처방조제)는 실손입원/실손통원/실손처방.
  1~5종·종수술은 질병종수술/상해종수술, 관절·척추·5대기관 등 특정 부위 수술은 특정질병수술.
  표적항암·방사선·중입자·양성자·CAR-T 등 치료비는 암치료비. 맞는 것이 없으면 기타.
- type은 계약 성격(실손보험이면 실손, 자동차보험이면 자동차 등).
- 만기가 종신·9999년이면 end는 null.
- summary는 보고서의 '권장/가입/부족' 요약표 행을 그대로(없으면 빈 배열). risks는 질병·암 위험도 등급.
- caveats에는 '단독 실손은 합계 제외', '조회 기준 기간' 같은 보고서의 유의사항을 짧게.
- 날짜는 YYYY-MM-DD(일이 없으면 YYYY-MM). 읽을 수 없는 값은 null.
"""

ANALYZE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "key_points": {"type": "ARRAY", "items": {"type": "STRING"},
                       "description": "설계사용 핵심 요약 3~6줄. 숫자는 facts에 있는 것만"},
        "strengths": {"type": "ARRAY", "items": {"type": "STRING"},
                      "description": "고객용: 이미 잘 갖춰진 보장 2~3개(짧게)"},
        "claims": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "rank": {"type": "INTEGER"},
            "title": {"type": "STRING", "description": "진료 묶음 설명(기관·기간)"},
            "evidence": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "facts의 id"},
            "insurance": {"type": "STRING", "description": "청구 대상 계약·담보(facts에 있는 것만)"},
            "check": {"type": "STRING", "description": "청구 전에 확인할 것"},
            "customer_text": {"type": "STRING", "description": "고객용 한 줄"},
        }, "required": ["rank", "title", "evidence", "insurance", "check", "customer_text"]}},
        "gaps": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "rank": {"type": "INTEGER"},
            "product_key": {"type": "STRING", "description": "facts.products 의 key 중 하나"},
            "evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
            "record_basis": {"type": "STRING", "description": "병의원 기록 근거(없으면 '직접 기록 없음')"},
            "report_basis": {"type": "STRING", "description": "보고서 근거(권장 대비 가입, 위험도)"},
            "talking_point": {"type": "STRING", "description": "설계사 상담 포인트"},
            "customer_text": {"type": "STRING", "description": "고객용 '왜 필요한가요' 1~2문장"},
        }, "required": ["rank", "product_key", "evidence", "record_basis", "report_basis",
                        "talking_point", "customer_text"]}},
        "optimization": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "contract_no": {"type": "INTEGER"},
            "action": {"type": "STRING", "enum": ["감액검토", "정리검토", "유지"]},
            "reason": {"type": "STRING"},
            "caution": {"type": "STRING"},
        }, "required": ["contract_no", "action", "reason", "caution"]}},
        "cautions": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "알릴의무 등 이 고객에 해당하는 주의"},
    },
    "required": ["key_points", "strengths", "claims", "gaps", "optimization", "cautions"],
}

ANALYZE_PROMPT = """당신은 보험 설계사를 돕는 분석가입니다. 아래 facts(JSON)는 고객의 보장분석 보고서와 병의원 이용 기록을
코드로 계산·정리한 사실 목록입니다. facts에 없는 사실·숫자·보험료를 만들지 마세요.
모든 주장에는 근거가 된 facts 항목의 id(C1, K2, P1, I1, R12 등)를 evidence에 적으세요.
행 id가 없는 근거는 섹션 이름(summary, risks, over_coverage, blank_contracts 등)을 적으세요.

해야 할 일
1. claims: 청구할 만한 진료 묶음. 우선순위는 (예상 금액이 크고) (청구 기한이 남았고) (그 시점에 유효한 담보가 있는) 순.
   facts.clusters 의 in_force 담보와 facts.claims_rules 를 근거로. 기한이 지난 것은 넣지 말고 key_points 에서만 언급.
   실손은 facts.indemnity_estimate 를 근거로 묶어서 한 항목으로.
2. gaps: 보장 부족 → 신규 가입 제안. 목적은 근거 있는 신규 가입 권유입니다.
   facts.products 의 key 중에서만 고르고, 병의원 기록과 직접 연결되는 빈틈(예: 지출이 있는데 담보 0)을 먼저,
   그다음 보고서 위험도가 높고 가입금액이 권장보다 크게 부족한 항목. 이미 충분한 담보는 제안하지 마세요.
3. optimization: 보험료 최적화. 다음을 차례로 보세요.
   - facts.over_coverage: 보고서 권장보다 많이 든 담보 → 그 담보를 가진 계약의 특약 '감액검토'.
   - facts.blank_contracts: 보고서에 담보가 비어 있는 계약 → '감액검토'(구성 확인 필요, 적립·저축 비중이 크면 적립분 감액).
   - 같은 범주의 담보가 여러 계약에 겹치고 작은 소멸성 계약이 다른 계약에 대부분 포함되면 → '정리검토'.
   facts.protected_contracts 에 있는 계약은 반드시 '유지'. 해지·감액 시 잃는 보장을 caution에 적으세요.
   최근 1~2년 내 가입한 계약은 해지 시 손해와 재가입 조건을 caution에 적으세요.
4. cautions: 이 고객에 해당하는 알릴의무(정기 진료·최근 진료), 면책·감액, 기존계약 해지 시 비교안내 등.
5. strengths: 고객이 이미 잘 갖춘 보장(고객용 짧은 문장).

문체: 설계사용 항목은 간결한 메모체, 고객용(customer_text, strengths)은 존댓말. 진단명을 단정하지 말고
'~로 보임', '확인 필요'로 쓰세요. 위험도는 통계 예측이지 진단이 아닙니다.
"""
