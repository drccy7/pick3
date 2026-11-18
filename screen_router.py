"""Decide which service a shared screenshot belongs to, so one Shortcut can feed every service.

A small, separate Gemini call (classification only) keeps each service's own extraction prompt
unchanged, so adding a service does not disturb the others' accuracy.
"""

from __future__ import annotations

import base64
import json
import os

import httpx

try:
    from .saved_places import gemini_model
except ImportError:
    from saved_places import gemini_model

KINDS = ("restaurant", "medical", "other")

_PROMPT = (
    "이 화면이 어떤 종류인지 JSON으로만 답하세요. 형식: {\"type\": \"restaurant\" | \"medical\" | \"other\"}\n"
    "- restaurant: 음식점·카페·술집을 소개하는 게시물, 리뷰, 지도, 메뉴 화면\n"
    "- medical: 병원·의원·약국·안경점 등의 진료비·약제비·결제 내역 목록이나 영수증, 의료비 명세\n"
    "- other: 그 밖의 모든 화면"
)


def auto_route_enabled() -> bool:
    return os.getenv("PICK3_AUTO_ROUTE", "1").strip().lower() not in ("0", "false", "off", "no")


async def classify_screen(raw: bytes, mime_type: str) -> str:
    """Return 'restaurant', 'medical' or 'other'. Any failure -> 'restaurant' (the original behaviour)."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "restaurant"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent"
    payload = {
        "contents": [{"parts": [
            {"text": _PROMPT},
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(raw).decode("ascii")}},
        ]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0},
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
            resp.raise_for_status()
            data = resp.json()
        text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
        parsed = json.loads(text)
        if isinstance(parsed, list):
            parsed = parsed[0] if parsed else {}
        kind = str(parsed.get("type") or "").strip().lower()
    except Exception:
        return "restaurant"
    return kind if kind in KINDS else "restaurant"
