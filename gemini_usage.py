"""Record Gemini token usage per call (table pick3_gemini_usage, migration 007) and estimate cost.

Callers don't pass the user: the request sets it once with bind(user_id) (a ContextVar, so it follows
the request into background tasks and asyncio.to_thread). Recording never raises - a missing table or
network error only loses that row. Prices live in gemini_prices.json (estimates; the bill is the truth).
"""

from __future__ import annotations

import json
import os
import pathlib
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import httpx

TABLE = "pick3_gemini_usage"
PRICES_PATH = pathlib.Path(__file__).with_name("gemini_prices.json")
_CTX: ContextVar[dict] = ContextVar("gemini_usage_ctx", default={})


def bind(user_id=None, ref: str | None = None) -> None:
    """Attach the current request's user (and optionally a reference like 'coverage:12')."""
    cur = dict(_CTX.get())
    if user_id is not None:
        cur["user_id"] = user_id
    if ref is not None:
        cur["ref"] = ref
    _CTX.set(cur)


def row_from_response(feature: str, model: str, data: dict) -> dict | None:
    meta = (data or {}).get("usageMetadata") or {}
    if not meta:
        return None
    ctx = _CTX.get()
    return {"user_id": ctx.get("user_id"), "ref": ctx.get("ref"), "feature": feature,
            "model": (data.get("modelVersion") or model or "")[:80],
            "prompt_tokens": int(meta.get("promptTokenCount") or 0),
            "output_tokens": int(meta.get("candidatesTokenCount") or 0),
            "thoughts_tokens": int(meta.get("thoughtsTokenCount") or 0),
            "cached_tokens": int(meta.get("cachedContentTokenCount") or 0)}


def _conf() -> tuple[str, dict] | None:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not (url and key):
        return None
    h = {"apikey": key, "Content-Type": "application/json", "Prefer": "return=minimal"}
    if key.startswith("eyJ"):
        h["Authorization"] = f"Bearer {key}"
    return url, h


def record_sync(feature: str, model: str, data: dict) -> None:
    row = row_from_response(feature, model, data)
    conf = _conf()
    if not row or not conf:
        return
    try:
        httpx.post(f"{conf[0]}/rest/v1/{TABLE}", headers=conf[1], json=row, timeout=10)
    except Exception:
        pass


async def record(feature: str, model: str, data: dict) -> None:
    row = row_from_response(feature, model, data)
    conf = _conf()
    if not row or not conf:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{conf[0]}/rest/v1/{TABLE}", headers=conf[1], json=row)
    except Exception:
        pass


# ---- cost ------------------------------------------------------------------------------------
def load_prices() -> dict:
    return json.loads(PRICES_PATH.read_text(encoding="utf-8"))


def _price_for(model: str, prices: dict) -> dict | None:
    table = prices["models"]
    best = max((k for k in table if model.startswith(k)), key=len, default=None)  # longest prefix wins
    return table.get(best) if best else None


def cost_usd(row: dict, prices: dict | None = None) -> float | None:
    prices = prices or load_prices()
    p = _price_for(row.get("model", ""), prices)
    if not p:
        return None
    if row.get("prompt_tokens", 0) > 200_000 and p.get("over_200k"):
        p = {**p, **p["over_200k"]}
    if p.get("input") is None or p.get("output") is None:
        return None
    cached = row.get("cached_tokens", 0)
    fresh = max(0, row.get("prompt_tokens", 0) - cached)
    out = row.get("output_tokens", 0) + row.get("thoughts_tokens", 0)
    return (fresh * p["input"] + cached * (p.get("cached_input") or p["input"]) + out * p["output"]) / 1_000_000


def summarize(rows: list[dict], prices: dict | None = None) -> dict:
    """Totals by feature and model; tokens always, cost where the model's price is known."""
    prices = prices or load_prices()
    groups: dict[tuple[str, str], dict] = {}
    unpriced = set()
    for r in rows:
        g = groups.setdefault((r["feature"], r["model"]), {"feature": r["feature"], "model": r["model"], "calls": 0,
                                                            "prompt_tokens": 0, "output_tokens": 0, "usd": 0.0,
                                                            "priced": True})
        g["calls"] += 1
        g["prompt_tokens"] += r.get("prompt_tokens") or 0
        g["output_tokens"] += (r.get("output_tokens") or 0) + (r.get("thoughts_tokens") or 0)
        c = cost_usd(r, prices)
        if c is None:
            g["priced"] = False
            unpriced.add(r["model"])
        else:
            g["usd"] += c
    usd = sum(g["usd"] for g in groups.values())
    return {"calls": sum(g["calls"] for g in groups.values()), "usd": round(usd, 4),
            "krw": round(usd * prices.get("usd_krw", 1400)), "usd_krw": prices.get("usd_krw", 1400),
            "unpriced_models": sorted(unpriced),
            "groups": sorted(groups.values(), key=lambda g: (-g["usd"], -g["prompt_tokens"]))}


async def fetch_rows(user_id=None, since: datetime | None = None, ref: str | None = None,
                     ref_prefix: str | None = None) -> list[dict]:
    conf = _conf()
    if not conf:
        return []
    params = {"select": "feature,model,prompt_tokens,output_tokens,thoughts_tokens,cached_tokens,ref,created_at",
              "order": "created_at.desc", "limit": "10000"}
    if user_id is not None:
        params["user_id"] = f"eq.{user_id}"
    if since:
        params["created_at"] = f"gte.{since.isoformat()}"
    if ref:
        params["ref"] = f"eq.{ref}"
    elif ref_prefix:
        params["ref"] = f"like.{ref_prefix}*"
    h = {k: v for k, v in conf[1].items() if k != "Prefer"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{conf[0]}/rest/v1/{TABLE}", headers=h, params=params)
    except Exception:
        return []
    return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []


def month_start_kst(now: datetime | None = None) -> datetime:
    kst = (now or datetime.now(timezone.utc)) + timedelta(hours=9)
    return datetime(kst.year, kst.month, 1, tzinfo=timezone.utc) - timedelta(hours=9)
