"""Supabase persistence for 보장분석 reports: table pick3_coverage_reports + private Storage bucket pick3-coverage.

docs/SUPABASE_MIGRATION_006_coverage.sql creates both. All calls use the server's secret key.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx

TABLE = "pick3_coverage_reports"
BUCKET = "pick3-coverage"
LIST_COLUMNS = ("id,kind,suggested,filename,size_bytes,encrypted,status,message,customer_name,birth_date,records_count,"
                "results,created_at,analyzed_at,extract_ready:extract->customer")


class StoreError(RuntimeError):
    pass


def _conf() -> tuple[str, dict[str, str]]:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not (url and key):
        raise StoreError("Supabase가 설정되지 않았습니다.")
    h = {"apikey": key}
    if key.startswith("eyJ"):
        h["Authorization"] = f"Bearer {key}"
    return url, h


def _check(r: httpx.Response, what: str) -> httpx.Response:
    if r.status_code >= 400:
        hint = " (migration 006 실행 필요)" if r.status_code in (404, 400) and ("relation" in r.text or "Bucket" in r.text) else ""
        if r.status_code == 400 and ("kind" in r.text or "suggested" in r.text) and "column" in r.text:
            hint = " (migration 010 실행 필요)"
        raise StoreError(f"{what} 실패 {r.status_code}{hint}: {r.text[:200]}")
    return r


async def create(user_id: int, filename: str, size: int, encrypted: bool, storage_path: str,
                 kind: str = "coverage") -> dict:
    url, h = _conf()
    async with httpx.AsyncClient(timeout=30) as c:
        r = _check(await c.post(f"{url}/rest/v1/{TABLE}", headers={**h, "Prefer": "return=representation"},
                                json={"user_id": user_id, "filename": filename, "size_bytes": size,
                                      "encrypted": encrypted, "storage_path": storage_path, "kind": kind}),
                   "문서 저장")
    return r.json()[0]


async def list_reports(user_id: int, kind: str = "coverage") -> list[dict]:
    """One box of the document inbox: coverage (보장분석) | locked (암호 문서함) | checkup (건강검진)."""
    url, h = _conf()
    async with httpx.AsyncClient(timeout=30) as c:
        r = _check(await c.get(f"{url}/rest/v1/{TABLE}", headers=h, params={
            "select": LIST_COLUMNS, "user_id": f"eq.{user_id}", "kind": f"eq.{kind}",
            "order": "created_at.desc", "limit": "100"}),
            "목록")
    return r.json()


async def get(user_id: int, report_id: int) -> dict | None:
    url, h = _conf()
    async with httpx.AsyncClient(timeout=30) as c:
        r = _check(await c.get(f"{url}/rest/v1/{TABLE}", headers=h, params={
            "select": "*", "id": f"eq.{report_id}", "user_id": f"eq.{user_id}"}), "보고서")
    rows = r.json()
    return rows[0] if rows else None


async def update(user_id: int, report_id: int, **fields) -> None:
    url, h = _conf()
    async with httpx.AsyncClient(timeout=30) as c:
        _check(await c.patch(f"{url}/rest/v1/{TABLE}", headers=h, json=fields,
                             params={"id": f"eq.{report_id}", "user_id": f"eq.{user_id}"}), "상태 저장")


async def delete(user_id: int, report_id: int) -> None:
    url, h = _conf()
    async with httpx.AsyncClient(timeout=30) as c:
        _check(await c.delete(f"{url}/rest/v1/{TABLE}", headers=h,
                              params={"id": f"eq.{report_id}", "user_id": f"eq.{user_id}"}), "삭제")


async def put_file(path: str, data: bytes, content_type: str = "application/pdf") -> None:
    url, h = _conf()
    async with httpx.AsyncClient(timeout=120) as c:
        _check(await c.post(f"{url}/storage/v1/object/{BUCKET}/{quote(path)}", content=data,
                            headers={**h, "Content-Type": content_type, "x-upsert": "true"}), "파일 저장")


async def get_file(path: str) -> bytes:
    url, h = _conf()
    async with httpx.AsyncClient(timeout=120) as c:
        r = _check(await c.get(f"{url}/storage/v1/object/{BUCKET}/{quote(path)}", headers=h), "파일 읽기")
    return r.content


async def delete_files(paths: list[str]) -> None:
    if not paths:
        return
    url, h = _conf()
    async with httpx.AsyncClient(timeout=60) as c:
        await c.request("DELETE", f"{url}/storage/v1/object/{BUCKET}", headers=h, json={"prefixes": paths})
