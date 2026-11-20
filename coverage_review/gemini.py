"""Minimal Gemini REST client: File API upload (large PDFs) + JSON-schema constrained generation."""

from __future__ import annotations

import json
import os
import time

import httpx

BASE = "https://generativelanguage.googleapis.com"


def _key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")
    return key


def upload_pdf(raw: bytes, display_name: str = "report.pdf") -> dict:
    """Upload via the resumable File API; returns {"uri", "name"}. Caller must delete_file(name)."""
    h = {"x-goog-api-key": _key()}
    with httpx.Client(timeout=300.0) as c:
        start = c.post(f"{BASE}/upload/v1beta/files", headers={
            **h, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(raw)),
            "X-Goog-Upload-Header-Content-Type": "application/pdf",
        }, json={"file": {"display_name": display_name}})
        start.raise_for_status()
        url = start.headers["x-goog-upload-url"]
        done = c.post(url, headers={**h, "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"},
                      content=raw)
        done.raise_for_status()
        f = done.json()["file"]
        for _ in range(60):  # wait until processed
            if f.get("state") in (None, "ACTIVE"):
                break
            if f.get("state") == "FAILED":
                raise RuntimeError("Gemini file processing failed")
            time.sleep(2)
            f = c.get(f"{BASE}/v1beta/{f['name']}", headers=h).json()
    return {"uri": f["uri"], "name": f["name"]}


def delete_file(name: str) -> None:
    try:
        httpx.delete(f"{BASE}/v1beta/{name}", headers={"x-goog-api-key": _key()}, timeout=30.0)
    except httpx.HTTPError:
        pass  # files expire on their own after 48 h


def generate_json(model: str, parts: list[dict], schema: dict, temperature: float = 0.0) -> dict:
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                             "temperature": temperature},
    }
    url = f"{BASE}/v1beta/models/{model}:generateContent"
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = httpx.post(url, headers={"x-goog-api-key": _key()}, json=payload, timeout=600.0)
            if r.status_code in (429, 500, 503):
                raise httpx.HTTPStatusError(r.text[:200], request=r.request, response=r)
            r.raise_for_status()
            data = r.json()
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            return json.loads(text)
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Gemini {model} failed: {last}")


def generate_json_stream(model: str, parts: list[dict], schema: dict, on_text, temperature: float = 0.0) -> dict:
    """Same as generate_json but streamed (SSE): on_text(text_so_far) is called as chunks arrive,
    so callers can show real progress. Falls back to the non-streamed call on any stream error."""
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                             "temperature": temperature},
    }
    url = f"{BASE}/v1beta/models/{model}:streamGenerateContent"
    text = ""
    try:
        with httpx.stream("POST", url, params={"alt": "sse"}, headers={"x-goog-api-key": _key()}, json=payload,
                          timeout=httpx.Timeout(600.0, connect=30.0)) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip() or "{}")
                for p in (ev.get("candidates") or [{}])[0].get("content", {}).get("parts", []):
                    if p.get("text") and not p.get("thought"):
                        text += p["text"]
                on_text(text)
        return json.loads(text)
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError):
        return generate_json(model, parts, schema, temperature)


def pdf_part(uri: str) -> dict:
    return {"file_data": {"mime_type": "application/pdf", "file_uri": uri}}
