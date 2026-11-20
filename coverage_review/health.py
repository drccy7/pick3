"""건강·보험 분석: a customer's health records alone (no 보장분석 report) → what to tell an underwriter and which
coverage areas the records point to. Same split as the coverage pipeline: facts in code, wording by Gemini
citing fact ids, sanitize, Jinja HTML (the PDF is printed from that HTML when sent).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import date, timedelta

from . import facts as F
from . import gemini, render

DEFAULT_MODEL = "gemini-pro-latest"


def _d(s) -> date | None:
    return F.parse_date(s)


def meds_facts(rows: list[dict], today: date) -> list[dict]:
    """One entry per drug (ingredient, else name): dispensings, total days, first/last date, recent flag."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.get("ingredient") or r.get("drug_name") or "").strip()
        if key and _d(r.get("visit_date")):
            by[key].append(r)
    out = []
    for key, rs in by.items():
        rs.sort(key=lambda r: str(r["visit_date"]))
        days = sum(int(float(r.get("days") or 0)) for r in rs)
        last = _d(rs[-1]["visit_date"])
        out.append({"ingredient": key, "drug": rs[-1].get("drug_name"), "dispensings": len(rs), "total_days": days,
                    "first": str(rs[0]["visit_date"]), "last": str(rs[-1]["visit_date"]),
                    "long_term": days >= 30, "within_1_year": (today - last).days <= 366})
    out.sort(key=lambda m: (-m["total_days"], m["ingredient"]))
    for i, m in enumerate(out, 1):
        m["id"] = f"M{i}"
    return out


def disclosure(recs: list[dict], dx: list[dict], meds: list[dict], today: date) -> dict:
    """The standard 청약서 questions, answered only from the records (a checklist, not a judgment)."""
    m3, y5 = today - timedelta(days=92), today - timedelta(days=5 * 366)
    recent = [r for r in recs if r["date"] >= m3 and r["type"] != "약국"]
    return {
        "within_3_months": [{"id": r["id"], "date": r["date"].isoformat(), "provider": r["provider"],
                             "dx": r.get("dx_name")} for r in recent],
        "inpatient_5_years": [{"id": r["id"], "date": r["date"].isoformat(), "provider": r["provider"],
                               "dx": r.get("dx_name")} for r in recs if r["date"] >= y5 and r.get("visit_type") == "입원"],
        "repeated_dx_5_years": [{"id": d["id"], "name": d["name"], "visits": d["visits"]}
                                for d in dx if d["within_5_years"] and d["visits"] >= 7],
        "meds_30_days": [{"id": m["id"], "ingredient": m["ingredient"], "total_days": m["total_days"]}
                         for m in meds if m["long_term"]],
    }


def build(name: str, records: list[dict], med_rows: list[dict], today: date, rules: dict | None = None,
          checkups: list[dict] | None = None) -> dict:
    rules = rules or F.load_rules()
    recs = F.normalize_records(records, rules)
    dx = F.diagnoses(recs, today)
    meds = meds_facts(med_rows, today)
    by_year: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in recs:
        by_year[str(r["date"].year)][0] += 1
        by_year[str(r["date"].year)][1] += r["amount"]
    first = min((r["date"] for r in recs), default=None)
    last = max((r["date"] for r in recs), default=None)
    return {
        "today": today.isoformat(), "customer": {"name": name},
        "records": {"count": len(recs), "first": first.isoformat() if first else None,
                    "last": last.isoformat() if last else None, "amount": sum(r["amount"] for r in recs),
                    "without_amount": sum(1 for r in recs if not r["amount_known"]),
                    "by_year": {k: {"count": v[0], "amount": v[1]} for k, v in sorted(by_year.items())}},
        "diagnoses": dx, "recurring": F.recurring(recs, rules), "meds": meds,
        "disclosure": disclosure(recs, dx, meds, today),
        "products": [{"key": p["key"], "label": p["label"], "proposal": p["proposal"]} for p in rules["products"]],
        "checkups": checkups or [],
    }


SCHEMA = {
    "type": "object",
    "properties": {
        "key_points": {"type": "array", "items": {"type": "string"}},
        "needs": {"type": "array", "items": {"type": "object", "properties": {
            "product_key": {"type": "string"}, "priority": {"type": "integer"}, "reason": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}}},
            "required": ["product_key", "priority", "reason", "evidence"]}},
        "disclosure_notes": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "evidence"]}},
        "cautions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["key_points", "needs", "disclosure_notes", "cautions"],
}

PROMPT = """당신은 보험 설계사를 돕는 분석가입니다. 아래 facts(JSON)는 고객 한 명의 건강 기록입니다
(병의원·약국 이용 기록, 진단명, 정기 진료, 투약, 건강검진). 고객의 현재 보험 계약 정보는 없습니다.
facts 에 있는 사실만 쓰고, 근거는 facts 의 id(R…, D…, P…, M…, E…, C…)로 evidence 에 적으세요. 추측하지 마세요.
facts.checkups: judgments 는 검진 문서에 적힌 판정 그대로, flagged(C…)는 문서 참고치를 벗어나거나 의심·경계·유질환으로
표시된 항목입니다. 검진의 의심·유질환·재검사 소견은 알릴의무(최근 1년 추가검사 등) 대상일 수 있으니 disclosure_notes 에 적고,
needs 의 근거로도 쓰세요(예: 당뇨 의심 → 뇌·심장, 질병수술).

- key_points: 설계사가 먼저 알아야 할 3~5가지(건강 상태의 큰 그림, 가입 심사에 영향을 줄 것, 권할 보장 방향).
- needs: facts.products 의 key 중에서, 기록이 근거가 되는 보장 영역만 골라 priority(1이 가장 높음)와 이유를 쓰세요.
  고객의 현재 가입 여부는 모르므로 "이미 가입돼 있으면 보장금액을 확인"하라는 관점으로 씁니다. 근거 없는 영역은 넣지 마세요.
- disclosure_notes: facts.disclosure(최근 3개월 진료, 5년 내 입원, 5년 내 같은 진단 7회 이상, 30일 이상 투약)를 바탕으로
  청약 때 사실대로 알려야 할 항목을 정리하세요. 숨기거나 줄이는 방향의 조언은 절대 하지 마세요.
  유병자(간편심사) 상품이 맞을지 표준 상품이 가능할지에 대한 판단 근거도 적되, 최종 판단은 보험사 심사라고 쓰세요.
- cautions: 이 분석의 한계(금액 없는 기록, 비급여 없음, 진단명에 의심 진단 포함 가능, 건강검진 결과 미반영 등)와 다음 단계
  (보장분석 보고서나 보험증권을 받으면 현재 보장과 비교 가능). 검진 결과가 없으면 그것도 한계로 적으세요.
진단·치료에 대한 의학적 판단은 하지 마세요. 문장은 짧고 쉬운 한국어로 쓰세요."""


def sanitize(a: dict, facts: dict) -> tuple[dict, list[str]]:
    known = {x["id"] for k in ("diagnoses", "recurring", "meds") for x in facts[k]}
    known |= {u["id"] for lst in facts["disclosure"].values() for u in lst}
    known |= {x for e in facts.get("checkups", []) for x in [e["id"], *(c["id"] for c in e["flagged"])]}
    keys = {p["key"] for p in facts["products"]}
    warns = []
    needs = []
    for n in a.get("needs", []):
        if n.get("product_key") not in keys:
            warns.append(f"모르는 보장 영역 제외: {n.get('product_key')}")
            continue
        ev = [e for e in n.get("evidence", []) if e in known]
        if not ev:
            warns.append(f"근거 없는 보장 영역 제외: {n['product_key']}")
            continue
        needs.append({**n, "evidence": ev})
    needs.sort(key=lambda n: n.get("priority", 9))
    notes = [{**d, "evidence": [e for e in d.get("evidence", []) if e in known]} for d in a.get("disclosure_notes", [])]
    return {**a, "needs": needs, "disclosure_notes": notes}, warns


def analyze(facts: dict) -> dict:
    model = os.getenv("HEALTH_ANALYZE_MODEL", os.getenv("COVERAGE_ANALYZE_MODEL", DEFAULT_MODEL))
    return gemini.generate_json(model, [{"text": PROMPT},
                                        {"text": "facts = " + json.dumps(facts, ensure_ascii=False, default=str)}],
                                SCHEMA, temperature=0.2, feature="health_analyze",
                                thinking=os.getenv("COVERAGE_ANALYZE_THINKING", "low"))


def html(facts: dict, analysis: dict, warnings: list[str]) -> str:
    css = (render.TPL / "base.css").read_text(encoding="utf-8")
    prod = {p["key"]: p for p in facts["products"]}
    return render.env().get_template("health.html.j2").render(f=facts, a=analysis, prod=prod, warnings=warnings, css=css)


def run(name: str, records: list[dict], med_rows: list[dict], *, today: date | None = None,
        analysis: dict | None = None, checkups: list[dict] | None = None) -> dict:
    today = today or date.today()
    facts = build(name, records, med_rows, today, checkups=checkups)
    raw = analysis if analysis is not None else analyze(facts)
    clean, warns = sanitize(dict(raw), facts)
    page = html(facts, clean, warns)
    fname = f"{name or '고객'}_건강보험분석_{today}.html"
    return {"facts": facts, "analysis": raw, "warnings": warns, "html": {fname: page},
            "docs": {fname: page.encode("utf-8")}}
