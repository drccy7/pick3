"""Merge the LLM analysis with facts: drop unsupported claims, enforce guards, compute the premium before/after."""

from __future__ import annotations

from .facts import load_rules, premium_range


def _known_ids(facts: dict) -> set[str]:
    ids = {c["id"] for c in facts["contracts"]} | {k["id"] for k in facts["clusters"]}
    ids |= {p["id"] for p in facts["recurring"]} | {facts["indemnity_estimate"]["id"]}
    for k in facts["clusters"]:
        ids |= set(k["records"])
    for p in facts["recurring"]:
        ids |= set(p["records"])
    for row in facts["indemnity_estimate"]["rows"]:
        ids |= set(row["records"])
    return ids | SECTION_IDS


# Citing a whole section is allowed (e.g. the report's summary table or risk grades have no row ids).
SECTION_IDS = {"summary", "risks", "products", "premium", "coverage_by_category", "records", "customer",
               "report", "indemnity_periods", "over_coverage", "blank_contracts"}


def sanitize(analysis: dict, facts: dict) -> tuple[dict, list[str]]:
    """Keep only what the facts support. Returns (clean analysis, warnings)."""
    warns: list[str] = []
    known = _known_ids(facts)
    product_keys = {p["key"] for p in facts["products"]}
    by_no = {c["no"]: c for c in facts["contracts"]}
    protected = set(facts["protected_contracts"])

    def ev(items: list[str], where: str) -> list[str]:
        bad = [e for e in items if e not in known]
        if bad:
            warns.append(f"{where}: 근거 id 없음 {bad}")
        return [e for e in items if e in known]

    claims = []
    for c in sorted(analysis.get("claims", []), key=lambda x: x.get("rank", 99)):
        c["evidence"] = ev(c.get("evidence", []), f"청구 '{c.get('title')}'")
        if c["evidence"]:
            claims.append(c)
        else:
            warns.append(f"청구 '{c.get('title')}' 제외: 근거 없음")
    gaps, seen = [], set()
    for g in sorted(analysis.get("gaps", []), key=lambda x: x.get("rank", 99)):
        if g.get("product_key") not in product_keys or g["product_key"] in seen:
            warns.append(f"제안 '{g.get('product_key')}' 제외: 설정표에 없거나 중복")
            continue
        seen.add(g["product_key"])
        g["evidence"] = ev(g.get("evidence", []), f"제안 '{g['product_key']}'")
        gaps.append(g)
    opts = []
    for o in analysis.get("optimization", []):
        c = by_no.get(o.get("contract_no"))
        if not c:
            warns.append(f"최적화: 계약 {o.get('contract_no')} 없음")
            continue
        if o.get("action") != "유지" and o["contract_no"] in protected:
            warns.append(f"최적화: 계약 {o['contract_no']} 은 보호 대상 → 유지로 바꿈")
            o = {**o, "action": "유지"}
        opts.append(o)
    for i, x in enumerate(claims, 1):
        x["rank"] = i
    for i, x in enumerate(gaps, 1):
        x["rank"] = i
    return {**analysis, "claims": claims, "gaps": gaps, "optimization": opts}, warns


def premium_plan(facts: dict, analysis: dict, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    by_no = {c["no"]: c for c in facts["contracts"]}
    age = facts.get("premium_table_age")
    p = facts["premium"]
    removals, reductions = [], []
    for o in analysis["optimization"]:
        c = by_no[o["contract_no"]]
        if o["action"] == "정리검토" and c["active"] and not c["paid_up_likely"] and c["pay_cycle"] == "월납":
            removals.append({"no": c["no"], "label": f"{c['insurer']} {c['product']}", "monthly": c["monthly_equiv"],
                             "reason": o["reason"], "caution": o["caution"]})
        elif o["action"] == "감액검토":
            reductions.append({"no": c["no"], "label": f"{c['insurer']} {c['product']}", "monthly": c["monthly_equiv"],
                               "reason": o["reason"], "caution": o["caution"]})
    adds = []
    for g in analysis["gaps"]:
        r = premium_range(g["product_key"], age, rules)
        if r:
            adds.append({"rank": g["rank"], "key": g["product_key"], **r})
    add_min, add_max = sum(a["min"] for a in adds), sum(a["max"] for a in adds)
    add_mid = (add_min + add_max) // 2
    removed = sum(r["monthly"] for r in removals)
    paid_up = sum(x["monthly"] for x in p["paid_up_likely"])
    base = p["monthly_sum"]
    after_opt = base - paid_up - removed
    return {
        "stated": p["stated_monthly"], "base": base, "paid_up": paid_up, "paying_now": base - paid_up,
        "removals": removals, "removed": removed, "reductions": reductions, "after_opt": after_opt,
        "adds": adds, "add_min": add_min, "add_max": add_max, "add_mid": add_mid,
        "total_mid": after_opt + add_mid, "total_min": after_opt + add_min, "total_max": after_opt + add_max,
        "total_if_not_paid_up": after_opt + paid_up + add_mid, "age_for_table": age,
    }
