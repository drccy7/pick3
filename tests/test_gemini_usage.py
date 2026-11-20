"""Gemini token usage: rows from responses, prices by model version prefix, unknown prices stay unpriced."""

from __future__ import annotations

import gemini_usage as gu

PRICES = {"usd_krw": 1000, "models": {
    "gemini-x-flash-lite": {"input": 0.30, "output": 2.50, "cached_input": 0.03},
    "gemini-x-flash": {"input": None, "output": None},
    "gemini-x-pro": {"input": 2.0, "output": 12.0, "cached_input": 0.2, "over_200k": {"input": 4.0, "output": 18.0}},
}}


def _resp(prompt, out, thoughts=0, version="gemini-x-pro-0612"):
    return {"modelVersion": version, "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": out,
                                                       "thoughtsTokenCount": thoughts}}


def test_row_uses_reported_version_and_bound_user() -> None:
    gu.bind(7, ref="coverage:3")
    row = gu.row_from_response("coverage_analyze", "gemini-pro-latest", _resp(1000, 200, 50))
    assert row["model"] == "gemini-x-pro-0612" and row["user_id"] == 7 and row["ref"] == "coverage:3"
    assert (row["prompt_tokens"], row["output_tokens"], row["thoughts_tokens"]) == (1000, 200, 50)
    assert gu.row_from_response("x", "m", {}) is None


def test_cost_longest_prefix_thinking_billed_as_output_and_200k_tier() -> None:
    lite = {"model": "gemini-x-flash-lite-001", "prompt_tokens": 1_000_000, "output_tokens": 0, "thoughts_tokens": 0}
    assert abs(gu.cost_usd(lite, PRICES) - 0.30) < 1e-9  # flash-lite, not flash
    pro = {"model": "gemini-x-pro-0612", "prompt_tokens": 100_000, "output_tokens": 500_000, "thoughts_tokens": 500_000}
    assert abs(gu.cost_usd(pro, PRICES) - (0.1 * 2.0 + 1.0 * 12.0)) < 1e-9
    big = {**pro, "prompt_tokens": 300_000}
    assert abs(gu.cost_usd(big, PRICES) - (0.3 * 4.0 + 1.0 * 18.0)) < 1e-9


def test_summary_marks_unpriced_models() -> None:
    rows = [{"feature": "coverage_extract", "model": "gemini-x-flash", "prompt_tokens": 50_000, "output_tokens": 8_000},
            {"feature": "coverage_analyze", "model": "gemini-x-pro-1", "prompt_tokens": 100_000, "output_tokens": 0}]
    s = gu.summarize(rows, PRICES)
    assert s["calls"] == 2 and s["usd"] == 0.2 and s["krw"] == 200 and s["unpriced_models"] == ["gemini-x-flash"]


def test_real_price_file_parses() -> None:
    p = gu.load_prices()
    assert p["models"]["gemini-3.5-flash-lite"]["input"] == 0.30
