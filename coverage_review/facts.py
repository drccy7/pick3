"""Deterministic facts from (extracted report, medical records). No LLM here; every rule's numbers live in rules.json.

Each fact carries an id (C3 contract, R12 record, K2 visit cluster, P1 recurring provider, I indemnity estimate)
so the analysis step must cite ids and the documents can show where a claim comes from.
"""

from __future__ import annotations

import json
import pathlib
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta

RULES_PATH = pathlib.Path(__file__).with_name("rules.json")
UNIT = {"원": 1, "천원": 1_000, "만원": 10_000, "억원": 100_000_000}
INDEMNITY = ("실손입원", "실손통원", "실손처방")
CLAIM_CATS = ("질병수술", "질병종수술", "특정질병수술", "상해수술", "상해종수술", "질병입원일당",
              "상해입원일당", "골절·깁스·화상") + INDEMNITY
DENTAL_CATS = ("치아보존", "치아보철", "치주") + INDEMNITY


def load_rules(path: pathlib.Path | None = None) -> dict:
    return json.loads((path or RULES_PATH).read_text(encoding="utf-8"))


# ---- small parsers ---------------------------------------------------------------------------
def to_won(amount, unit: str | None) -> int | None:
    if amount is None:
        return None
    try:
        return int(round(float(amount) * UNIT.get(unit or "원", 1)))
    except (TypeError, ValueError):
        return None


def parse_ym(s) -> tuple[int, int] | None:
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})", str(s or ""))
    if not m or m.group(1).startswith("9999"):
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return (y, mo) if 1 <= mo <= 12 else None


def parse_date(s) -> date | None:
    m = re.search(r"(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})", str(s or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def ym_index(ym: tuple[int, int]) -> int:
    return ym[0] * 12 + ym[1] - 1


def ym_str(i: int) -> str:
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def add_months(d: date, n: int) -> date:
    i = d.year * 12 + d.month - 1 + n
    y, m = divmod(i, 12)
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m + 1, day)
        except ValueError:
            continue
    raise ValueError(d)


# ---- age -------------------------------------------------------------------------------------
def age_info(birth: date | None, today: date) -> dict:
    if not birth:
        return {}
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    last_bd = add_months(birth, 12 * age)
    half = add_months(last_bd, 6)
    if today < half:
        ins_age, change = age, half
    else:
        ins_age, change = age + 1, add_months(last_bd, 18)
    return {"age": age, "insurance_age": ins_age, "next_insurance_age_date": change.isoformat(),
            "insurance_age_after": ins_age + 1, "days_to_change": (change - today).days}


# ---- contracts -------------------------------------------------------------------------------
_PER_MONTH = {"월납": 1, "3개월납": 3, "6개월납": 6, "연납": 12}


def contract_facts(report: dict, today: date, rules: dict) -> list[dict]:
    out = []
    t_idx = today.year * 12 + today.month - 1
    for c in report.get("contracts", []):
        start = parse_ym(c.get("start"))
        end = parse_ym(c.get("end"))
        cycle = c.get("pay_cycle") or "미상"
        premium = int(c["premium"]) if c.get("premium") else None
        f = {"id": f"C{c['no']}", "no": c["no"], "insurer": c.get("insurer", ""), "product": c.get("product", ""),
             "type": c.get("type", "기타"), "start": ym_str(ym_index(start)) if start else None,
             "end": ym_str(ym_index(end)) if end else None, "pay_cycle": cycle, "premium": premium,
             "paid_count": c.get("paid_count"), "active": end is None or ym_index(end) >= t_idx,
             "monthly_equiv": round(premium / _PER_MONTH[cycle]) if premium and cycle in _PER_MONTH else 0,
             "paid_up_likely": False, "paid_up_month": None,
             "coverages": [{"name": v.get("name", ""), "category": v.get("category", "기타"),
                            "amount": to_won(v.get("amount"), v.get("unit"))} for v in c.get("coverages", [])]}
        pu = rules["paid_up"]
        if start and cycle in _PER_MONTH and f["paid_count"]:
            step = _PER_MONTH[cycle]
            due = (t_idx - ym_index(start)) // step + 1  # payments that would be due by now if still paying
            paid = int(f["paid_count"])
            if due - paid >= max(1, pu["min_gap_months"] // step) and (paid * step) % pu["count_multiple"] == 0:
                f["paid_up_likely"] = True
                f["paid_up_month"] = ym_str(ym_index(start) + (paid - 1) * step)
                f["paid_up_basis"] = f"납입 {paid}회 = {paid * step // 12}년납, 경과로는 {due}회째"
        out.append(f)
    return out


def coverage_by_category(contracts: list[dict]) -> dict[str, dict]:
    agg: dict[str, dict] = {}
    for c in contracts:
        if not c["active"]:
            continue
        for v in c["coverages"]:
            a = agg.setdefault(v["category"], {"amount": 0, "contracts": []})
            a["amount"] += v["amount"] or 0
            if c["id"] not in a["contracts"]:
                a["contracts"].append(c["id"])
    return agg


def indemnity_periods(contracts: list[dict]) -> list[dict]:
    out = []
    for c in contracts:
        if any(v["category"] in INDEMNITY for v in c["coverages"]) or c["type"] == "실손":
            amounts = {v["category"]: v["amount"] for v in c["coverages"] if v["category"] in INDEMNITY}
            out.append({"contract": c["id"], "start": c["start"], "end": c["end"], "active": c["active"],
                        "amounts": amounts})
    return out


def _covered(ym: str, start: str | None, end: str | None) -> bool:
    return bool(start) and start <= ym and (end is None or ym <= end)


# ---- records ---------------------------------------------------------------------------------
def provider_type(provider: str, kind: str, rules: dict) -> str:
    for t in rules["provider_types"]:
        if any(k in provider for k in t["keywords"]):
            return t["type"]
    return {"약국": "약국", "안경": "안경"}.get(kind, "기타")


def normalize_records(records: list[dict], rules: dict) -> list[dict]:
    out = []
    for i, r in enumerate(sorted(records, key=lambda r: (str(r.get("visit_date")), str(r.get("provider"))))):
        d = parse_date(r.get("visit_date"))
        if not d or not r.get("amount"):
            continue
        p = str(r.get("provider") or "").strip()
        out.append({"id": f"R{i + 1}", "date": d, "provider": p, "amount": int(r["amount"]),
                    "type": provider_type(p, str(r.get("kind") or ""), rules),
                    # from the 건강보험 요양급여내역 when shared (may include 의증/배제상병)
                    "dx_code": r.get("diagnosis_code"), "dx_name": r.get("diagnosis_name"),
                    "visit_type": r.get("visit_type"), "care_days": r.get("care_days")})
    return out


def clusters(recs: list[dict], contracts: list[dict], today: date, rules: dict) -> list[dict]:
    cfg, years = rules["cluster"], rules["claim_deadline_years"]
    by_p: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if r["type"] not in cfg["exclude_types"]:
            by_p[r["provider"]].append(r)
    groups = []
    for p, rs in by_p.items():
        cur: list[dict] = []
        for r in rs:
            if cur and (r["date"] - cur[-1]["date"]).days > cfg["gap_days"]:
                groups.append(cur)
                cur = []
            cur.append(r)
        if cur:
            groups.append(cur)
    out = []
    for g in groups:
        total, top = sum(r["amount"] for r in g), max(g, key=lambda r: r["amount"])
        if top["amount"] < cfg["large_single"] and total < cfg["large_total"]:
            continue
        ym = top["date"].strftime("%Y-%m")
        deadline = add_months(top["date"], 12 * years)
        cats = DENTAL_CATS if g[0]["type"] == "치과" else CLAIM_CATS
        in_force = []
        for c in contracts:
            if not c["start"] or not _covered(ym, c["start"], c["end"]):
                continue
            for v in c["coverages"]:
                if v["category"] in cats and v["amount"]:
                    in_force.append({"contract": c["id"], "insurer": c["insurer"], "coverage": v["name"],
                                     "category": v["category"], "amount": v["amount"],
                                     "same_month_as_start": c["start"] == ym})
        out.append({"diagnoses": _dx_list(g), "inpatient": any(r.get("visit_type") == "입원" for r in g),
                    "provider": g[0]["provider"], "type": g[0]["type"], "first": g[0]["date"].isoformat(),
                    "last": g[-1]["date"].isoformat(), "visits": len(g), "total": total,
                    "largest": {"date": top["date"].isoformat(), "amount": top["amount"], "id": top["id"]},
                    "records": [r["id"] for r in g], "deadline": deadline.isoformat(),
                    "expired": deadline < today, "in_force": in_force})
    out.sort(key=lambda k: -k["total"])
    for i, k in enumerate(out, 1):
        k["id"] = f"K{i}"
    return out


def _dx_list(rs: list[dict]) -> list[str]:
    return sorted({f"{r['dx_name']}({r['dx_code']})" for r in rs if r.get("dx_code")})


def diagnoses(recs: list[dict], today: date) -> list[dict]:
    """One entry per 상병코드 from the 요양급여내역 rows: visits, dates, providers, 입원, recent (≤5y) flag."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if r.get("dx_code"):
            by[r["dx_code"]].append(r)
    out = []
    for code, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        rs.sort(key=lambda r: r["date"])
        out.append({"code": code, "name": rs[0].get("dx_name"), "visits": len(rs),
                    "first": rs[0]["date"].isoformat(), "last": rs[-1]["date"].isoformat(),
                    "inpatient_visits": sum(1 for r in rs if r.get("visit_type") == "입원"),
                    "providers": sorted({r["provider"] for r in rs})[:5],
                    "within_3_months": (today - rs[-1]["date"]).days <= 92,
                    "within_5_years": (today - rs[-1]["date"]).days <= 5 * 366,
                    "records": [r["id"] for r in rs]})
    for i, d in enumerate(out, 1):
        d["id"] = f"D{i}"
    return out


def recurring(recs: list[dict], rules: dict) -> list[dict]:
    if not recs:
        return []
    cfg = rules["recurring"]
    latest = max(r["date"] for r in recs)
    since = add_months(latest, -cfg["lookback_months"])
    recent = [r for r in recs if r["date"] > since]
    by_p: dict[str, list[dict]] = defaultdict(list)
    for r in recent:
        by_p[r["provider"]].append(r)
    pharmacy_days = defaultdict(list)
    for r in recent:
        if r["type"] == "약국":
            pharmacy_days[r["date"]].append(r)
    out = []
    for p, rs in by_p.items():
        months = sorted({r["date"].strftime("%Y-%m") for r in rs})
        if len(months) < cfg["min_months"] or rs[0]["type"] == "약국":
            continue
        same_day = [ph for r in rs for ph in pharmacy_days.get(r["date"], [])]
        out.append({"diagnoses": _dx_list(rs),
                    "max_supply_days": max((ph.get("care_days") or 0 for ph in same_day), default=0) or None,
                    "provider": p, "type": rs[0]["type"], "months": len(months), "visits": len(rs),
                    "from": months[0], "to": months[-1], "median_amount": int(statistics.median(r["amount"] for r in rs)),
                    "same_day_pharmacy_visits": len(same_day),
                    "pharmacy_median": int(statistics.median(x["amount"] for x in same_day)) if same_day else None,
                    "records": [r["id"] for r in rs]})
    out.sort(key=lambda x: -x["months"])
    for i, x in enumerate(out, 1):
        x["id"] = f"P{i}"
    return out


def indemnity_generation(start_ym: str | None, rules: dict) -> dict | None:
    """실손 세대 from the contract's start month (YYYY-MM). 4세대 and 5세대 split on 2026-05 (5세대 from 05-06)."""
    if not start_ym:
        return None
    for g in rules["indemnity_estimate"]["generations"]:
        if g["from"] <= start_ym <= g["to"]:
            # a 2026-05 start is 4세대 only before 05-06; month precision -> treat 2026-05 as 5세대 (sold from 05-06)
            if g["name"] == "4세대" and start_ym == "2026-05":
                continue
            return g
    return None


def _deduct(amount: int, rule: dict, tier: str) -> float:
    mins = rule["min"]
    base = mins.get(tier, mins.get("의원", 10000)) if isinstance(mins, dict) else mins
    return max(base, amount * rule.get("rate", 0.0))


def indemnity_estimate(recs: list[dict], periods: list[dict], today: date, rules: dict) -> dict:
    """Per visit day, the payout under the generation of the 실손 contract covering that day.

    1~3세대 deduct pharmacy separately from the clinic visit; 4~5세대 deduct the day's total once.
    Cap per day: the contract's 통원 amount when the report has it, else the generation's default.
    """
    cfg, years = rules["indemnity_estimate"], rules["claim_deadline_years"]
    tier_of = cfg.get("tier_of_type", {})
    days: dict[tuple, list[dict]] = defaultdict(list)
    excluded_total = 0
    for r in recs:
        ym = r["date"].strftime("%Y-%m")
        period = next((p for p in periods if _covered(ym, p["start"], p["end"])), None)
        if not period or add_months(r["date"], 12 * years) < today:
            continue
        if r["type"] in cfg["exclude_types"]:
            excluded_total += r["amount"]
            continue
        days[(r["date"], period["contract"])].append(r)
    by_contract = {p["contract"]: p for p in periods}
    rows, est, gens = [], 0, {}
    for (d, cid), rs in sorted(days.items()):
        period = by_contract[cid]
        gen = indemnity_generation(period["start"], rules)
        if gen is None:
            continue
        cap = period["amounts"].get("실손통원") or gen["daily_cap"]
        clinic = [r for r in rs if r["type"] != "약국"]
        pharm = [r for r in rs if r["type"] == "약국"]
        tier = max((tier_of.get(r["type"], "의원") for r in clinic),
                   key=lambda t: {"의원": 0, "병원": 1, "상급종합": 2}.get(t, 0), default="의원")
        c_amt, p_amt = sum(r["amount"] for r in clinic), sum(r["amount"] for r in pharm)
        if gen.get("pharmacy"):  # 1~3세대: clinic and pharmacy deducted separately
            pay = max(0, min(c_amt - _deduct(c_amt, gen["outpatient"], tier), cap)) if c_amt else 0
            pay += max(0, p_amt - _deduct(p_amt, gen["pharmacy"], tier)) if p_amt else 0
        else:  # 4~5세대 (and 1세대 here): one deduction for the day's clinic + pharmacy
            total = c_amt + p_amt
            pay = max(0, min(total - _deduct(total, gen["outpatient"], tier), cap))
        pay = int(pay)
        est += pay
        g = gens.setdefault(gen["name"], {"generation": gen["name"], "contract": cid, "days": 0, "billed": 0,
                                         "estimate": 0, "note": gen.get("note", "")})
        g["days"] += 1
        g["billed"] += c_amt + p_amt
        g["estimate"] += pay
        rows.append({"date": d.isoformat(), "amount": c_amt + p_amt, "estimate": pay, "generation": gen["name"],
                     "contract": cid, "records": [r["id"] for r in rs], "providers": sorted({r["provider"] for r in rs})})
    return {"id": "I1", "days": len(rows), "billed": sum(r["amount"] for r in rows), "estimate": est,
            "generations": list(gens.values()), "excluded_types": cfg["exclude_types"],
            "excluded_total": excluded_total, "rows": rows}


# ---- assemble --------------------------------------------------------------------------------
def _range_for_age(product: dict, age: int | None) -> dict:
    for band in product["premium"]:
        if age is None or age <= band["age_max"]:
            return {"min": band["min"], "max": band["max"]}
    return {"min": product["premium"][-1]["min"], "max": product["premium"][-1]["max"]}


def build(report: dict, records: list[dict], today: date, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    cust = report.get("customer", {})
    birth = parse_date(cust.get("birth_date"))
    age = age_info(birth, today)
    contracts = contract_facts(report, today, rules)
    cov = coverage_by_category(contracts)
    periods = indemnity_periods(contracts)
    recs = normalize_records(records, rules)

    monthly_sum = sum(c["monthly_equiv"] for c in contracts if c["pay_cycle"] == "월납")
    stated = report.get("report", {}).get("stated_monthly_premium")
    paid_up = [c for c in contracts if c["paid_up_likely"]]

    def window(rs):
        return {"count": len(rs), "amount": sum(r["amount"] for r in rs)}

    with_ind = [r for r in recs if any(_covered(r["date"].strftime("%Y-%m"), p["start"], p["end"]) for p in periods)]
    without = [r for r in recs if r not in with_ind]
    by_year, by_type, by_provider = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for r in recs:
        for agg, k in ((by_year, str(r["date"].year)), (by_type, r["type"]), (by_provider, r["provider"])):
            agg[k][0] += 1
            agg[k][1] += r["amount"]

    summary = []
    for s in report.get("summary", []):
        summary.append({"item": s.get("item"), "category": s.get("category"), "status": s.get("status"),
                        "recommended": to_won(s.get("recommended"), s.get("unit")),
                        "current": to_won(s.get("current"), s.get("unit"))})

    has_indemnity = any(p["active"] for p in periods)
    products = []
    for p in rules["products"]:
        if p["key"] == "실손" and has_indemnity:
            continue
        current = sum(cov.get(c, {}).get("amount", 0) for c in p["categories"])
        rows = [s for s in summary if s["category"] in p["categories"]]
        ev_types = p.get("evidence_types") or (["치과"] if "치아" in p["key"] else [])
        spend = sum(r["amount"] for r in recs if r["type"] in ev_types)
        products.append({"key": p["key"], "label": p["label"], "proposal": p["proposal"],
                         "current_amount": current, "report_rows": rows,
                         "record_spend": {"types": ev_types, "amount": spend} if ev_types else None})

    # Over-coverage vs the report's own recommendation, and contracts whose coverage the report left blank.
    over = []
    for s in summary:
        if s["recommended"] and s["current"] and s["current"] >= s["recommended"] * rules.get("over_ratio", 1.5):
            over.append({"item": s["item"], "category": s["category"], "recommended": s["recommended"],
                         "current": s["current"], "contracts": cov.get(s["category"], {}).get("contracts", [])})
    blank = [{"id": c["id"], "type": c["type"], "monthly": c["monthly_equiv"]} for c in contracts
             if not c["coverages"] and c["type"] != "자동차" and c["monthly_equiv"]]

    protected = sorted({c["no"] for c in contracts if c["type"] in rules["protected_types"] or c["paid_up_likely"]
                        or any(v["category"] in INDEMNITY for v in c["coverages"])})

    first = min((r["date"] for r in recs), default=None)
    last = max((r["date"] for r in recs), default=None)
    return {
        "today": today.isoformat(),
        "customer": {"name": cust.get("name"), "sex": cust.get("sex"), "birth_date": birth.isoformat() if birth else None,
                     **age, "age_in_report": cust.get("age_in_report"),
                     "age_note": (f"보고서 나이 {cust.get('age_in_report')}세는 현재 만 {age.get('age')}세와 다름"
                                  f"(검진일 {report.get('report', {}).get('checkup_date')} 기준일 가능성)")
                     if age and cust.get("age_in_report") and abs(float(cust["age_in_report"]) - age["age"]) >= 1 else None},
        "report": {k: report.get("report", {}).get(k) for k in ("issuer", "analysis_date", "checkup_date", "caveats",
                                                                "stated_contract_count", "stated_monthly_premium")},
        "contracts": contracts,
        "premium": {"monthly_sum": monthly_sum, "stated_monthly": stated,
                    "matches_stated": stated is None or abs(monthly_sum - stated) < 10,
                    "paid_up_likely": [{"id": c["id"], "label": f"{c['no']}. {c['insurer']} {c['product']}", "monthly": c["monthly_equiv"], "month": c["paid_up_month"],
                                        "basis": c.get("paid_up_basis")} for c in paid_up],
                    "paying_now": monthly_sum - sum(c["monthly_equiv"] for c in paid_up if c["pay_cycle"] == "월납"),
                    "non_monthly": [{"id": c["id"], "cycle": c["pay_cycle"], "premium": c["premium"]}
                                    for c in contracts if c["pay_cycle"] not in ("월납",) and c["premium"]]},
        "coverage_by_category": cov,
        "indemnity_periods": periods,
        "summary": summary,
        "risks": report.get("risks", []),
        "records": {"count": len(recs), "first": first.isoformat() if first else None,
                    "last": last.isoformat() if last else None, "amount": sum(r["amount"] for r in recs),
                    "by_year": {k: {"count": v[0], "amount": v[1]} for k, v in sorted(by_year.items())},
                    "by_type": {k: {"count": v[0], "amount": v[1]} for k, v in sorted(by_type.items(), key=lambda x: -x[1][1])},
                    "top_providers": [{"provider": k, "count": v[0], "amount": v[1]}
                                      for k, v in sorted(by_provider.items(), key=lambda x: -x[1][1])[:10]],
                    "with_indemnity": window(with_ind), "without_indemnity": window(without)},
        "clusters": clusters(recs, contracts, today, rules),
        "recurring": recurring(recs, rules),
        "indemnity_estimate": indemnity_estimate(recs, periods, today, rules),
        "diagnoses": diagnoses(recs, today),
        "diagnosis_note": "상병명은 요양기관 청구용 코드라 의증·배제상병이 포함될 수 있음(확진 아님)"
                          if any(r.get("dx_code") for r in recs) else None,
        "products": products,
        "over_coverage": over,
        "blank_contracts": blank,
        "premium_table_age": age.get("insurance_age"),
        "protected_contracts": protected,
        "claims_rules": {"deadline_years": rules["claim_deadline_years"],
                         "expired_before": add_months(today, -12 * rules["claim_deadline_years"]).isoformat()},
    }


def premium_range(key: str, age: int | None, rules: dict | None = None) -> dict | None:
    rules = rules or load_rules()
    for p in rules["products"]:
        if p["key"] == key:
            return {**_range_for_age(p, age), "label": p["label"], "proposal": p["proposal"], "note": p.get("note", "")}
    return None
