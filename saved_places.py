"""Screenshot ingestion (Gemini Vision -> Kakao Local) and saved-places recall for Pick3."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

try:
    from .providers import RestaurantPick, _build_map_urls, _format_walk_distance, _haversine_meters
except ImportError:
    from providers import RestaurantPick, _build_map_urls, _format_walk_distance, _haversine_meters

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "saved_places.db"


def _get_db_path() -> Path:
    custom = os.getenv("PICK3_SQLITE_PATH", "").strip()
    return Path(custom) if custom else DEFAULT_DB_PATH


def _init_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pick3_saved_places (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            area TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            signature_menu TEXT NOT NULL DEFAULT '',
            memo TEXT NOT NULL DEFAULT '',
            lat REAL,
            lng REAL,
            phone TEXT NOT NULL DEFAULT '',
            kakao_url TEXT NOT NULL DEFAULT '',
            naver_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


def gemini_model() -> str:
    """Model id is env-configurable so a retired model can be swapped without a code change.

    Pin a concrete model (not a *-latest alias) so extraction behaviour does not drift silently.
    """
    return os.getenv("PICK3_GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL


async def extract_place_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Use Gemini Vision (PICK3_GEMINI_MODEL) to extract restaurant info from an Instagram screenshot."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "이 인스타그램 스크린샷에서 음식점/카페 정보를 추출해 JSON으로만 답하세요. "
        '형식: {"name": "상호명", "area": "동네/역(예: 강남, 성수)", '
        '"category": "음식종류(예: 돼지갈비, 파스타)", '
        '"signature_menu": "대표메뉴 또는 특징", "memo": "웨이팅/주차/분위기 한줄 메모"}. '
        "상호명을 찾을 수 없으면 name을 빈 문자열로 두세요."
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model()}:generateContent"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type or "image/jpeg", "data": b64}},
                ]
            }
        ],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
        resp.raise_for_status()
        data = resp.json()

    text = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "{}")
    )
    parsed = json.loads(text)
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else {}
    return {
        "name": str(parsed.get("name") or "").strip(),
        "area": str(parsed.get("area") or "").strip(),
        "category": str(parsed.get("category") or "맛집").strip(),
        "signature_menu": str(parsed.get("signature_menu") or "").strip(),
        "memo": str(parsed.get("memo") or "").strip(),
    }


async def resolve_place_with_kakao(extracted: dict) -> dict:
    """Resolve extracted restaurant name + area to exact Kakao coordinates and map links."""
    name = extracted["name"]
    area = extracted["area"]
    naver_url, kakao_url = _build_map_urls(name, area)
    resolved = {
        **extracted,
        "address": area or "위치 확인",
        "lat": None,
        "lng": None,
        "phone": "",
        "kakao_url": kakao_url,
        "naver_url": naver_url,
    }

    kakao_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    if not kakao_key or not name:
        return resolved

    kw = f"{area} {name}".strip()
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(
                "https://dapi.kakao.com/v2/local/search/keyword.json",
                headers={"Authorization": f"KakaoAK {kakao_key}"},
                params={"query": kw, "category_group_code": "FD6", "size": 3},
            )
            if resp.status_code == 200:
                docs = resp.json().get("documents", [])
                if docs:
                    top = docs[0]
                    resolved["name"] = top.get("place_name") or name
                    resolved["address"] = top.get("road_address_name") or top.get("address_name") or resolved["address"]
                    resolved["lat"] = float(top["y"]) if top.get("y") else None
                    resolved["lng"] = float(top["x"]) if top.get("x") else None
                    resolved["phone"] = str(top.get("phone") or "").strip()
                    resolved["kakao_url"] = top.get("place_url") or kakao_url
                    resolved["naver_url"] = _build_map_urls(resolved["name"], area)[0]
    except Exception:
        pass
    return resolved


class StorageError(RuntimeError):
    """Raised when the configured persistent store (Supabase) cannot be used."""


SUPABASE_TABLE = "pick3_saved_places"


def _supabase_headers() -> tuple[str, dict[str, str]] | None:
    """Return (base_url, headers) when Supabase is configured, else None.

    New-style keys (sb_secret_/sb_publishable_) are not JWTs, so they go only in the
    `apikey` header. Legacy JWT keys (eyJ...) are also sent as a Bearer token.
    """
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_KEY", "").strip()
    if not (url and key):
        return None
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return url, headers


async def storage_status() -> dict:
    """Report which store is active and whether it is reachable (for /api/health)."""
    sb = _supabase_headers()
    if not sb:
        return {"storage": "sqlite", "storage_ok": True, "storage_note": "SUPABASE_URL/KEY 미설정 - 로컬 SQLite 사용"}
    base_url, headers = sb
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{base_url}/rest/v1/{SUPABASE_TABLE}",
                headers=headers,
                params={"select": "id", "limit": "1"},
            )
        ok = resp.status_code == 200
        note = "" if ok else f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # network / DNS / timeout
        ok, note = False, f"{type(exc).__name__}: {exc}"
    return {"storage": "supabase", "storage_ok": ok, "storage_note": note}


async def _save_place_supabase(row: dict, base_url: str, headers: dict[str, str]) -> dict:
    """Upsert into Supabase; duplicates (same name + address) are ignored, never overwritten.

    No silent SQLite fallback: on a server host (Render free) local disk is ephemeral,
    so a fallback would report "saved" and then lose the data.
    """
    endpoint = f"{base_url}/rest/v1/{SUPABASE_TABLE}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            ins = await client.post(
                endpoint,
                headers={**headers, "Prefer": "return=representation,resolution=ignore-duplicates"},
                params={"on_conflict": "name,address"},
                json=row,
            )
            if ins.status_code not in (200, 201):
                raise StorageError(f"Supabase insert HTTP {ins.status_code}: {ins.text[:200]}")
            body = ins.json()
            if isinstance(body, list) and body:
                return {**body[0], "storage": "supabase", "duplicate": False}
            # Empty representation => conflict ignored => fetch the existing row.
            dup = await client.get(
                endpoint,
                headers=headers,
                params={"name": f"eq.{row['name']}", "address": f"eq.{row['address']}", "limit": "1"},
            )
            if dup.status_code == 200 and dup.json():
                return {**dup.json()[0], "storage": "supabase", "duplicate": True}
            return {**row, "storage": "supabase", "duplicate": True}
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(f"Supabase 연결 실패: {type(exc).__name__}: {exc}") from exc


async def save_place(place: dict) -> dict:
    """Persist saved place to Supabase (if configured) or local SQLite."""
    row = {
        "name": place["name"],
        "area": place.get("area", ""),
        "address": place.get("address", ""),
        "category": place.get("category", "맛집"),
        "signature_menu": place.get("signature_menu", ""),
        "memo": place.get("memo", ""),
        "lat": place.get("lat"),
        "lng": place.get("lng"),
        "phone": place.get("phone", ""),
        "kakao_url": place.get("kakao_url", ""),
        "naver_url": place.get("naver_url", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    sb = _supabase_headers()
    if sb:
        return await _save_place_supabase(row, *sb)

    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _init_sqlite(conn)
        existing = conn.execute(
            "SELECT * FROM pick3_saved_places WHERE name = ? AND address = ? LIMIT 1",
            (row["name"], row["address"]),
        ).fetchone()
        if existing:
            return {**dict(existing), "storage": "sqlite", "duplicate": True}
        cur = conn.execute(
            """
            INSERT INTO pick3_saved_places
            (name, area, address, category, signature_menu, memo, lat, lng, phone, kakao_url, naver_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["name"],
                row["area"],
                row["address"],
                row["category"],
                row["signature_menu"],
                row["memo"],
                row["lat"],
                row["lng"],
                row["phone"],
                row["kakao_url"],
                row["naver_url"],
                row["created_at"],
            ),
        )
        conn.commit()
        return {**row, "id": cur.lastrowid, "storage": "sqlite", "duplicate": False}


async def load_all_saved_places() -> list[dict]:
    sb = _supabase_headers()
    if sb:
        base_url, headers = sb
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{base_url}/rest/v1/{SUPABASE_TABLE}",
                    headers=headers,
                    params={"order": "created_at.desc", "limit": "200"},
                )
        except Exception as exc:
            raise StorageError(f"Supabase 연결 실패: {type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise StorageError(f"Supabase select HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    db_path = _get_db_path()
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _init_sqlite(conn)
        rows = conn.execute(
            "SELECT * FROM pick3_saved_places ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return [dict(r) for r in rows]


async def pick3_from_saved(
    area: str = "",
    lat: float | None = None,
    lng: float | None = None,
) -> dict:
    """Return top 3 saved places filtered by area or sorted by distance from user GPS."""
    rows = await load_all_saved_places()
    clean_area = area.strip()

    scored: list[tuple[int, float, dict]] = []
    for r in rows:
        addr_blob = f"{r.get('area', '')} {r.get('address', '')}"
        area_match = 1 if (clean_area and clean_area in addr_blob) else 0
        dist_m = float("inf")
        if lat is not None and lng is not None and r.get("lat") and r.get("lng"):
            dist_m = _haversine_meters(lat, lng, float(r["lat"]), float(r["lng"]))
        scored.append((area_match, -dist_m if dist_m != float("inf") else -1e9, r))

    if clean_area:
        area_only = [item for item in scored if item[0] == 1]
        if area_only:
            scored = area_only

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picks: list[RestaurantPick] = []
    for _, neg_dist, r in scored[:6]:
        dist_text = _format_walk_distance(-neg_dist) if neg_dist > -1e8 else ""
        sig = r.get("signature_menu") or ""
        memo = r.get("memo") or ""
        reason_parts = [p for p in (memo, f"저장 메뉴: {sig}" if sig else "") if p]
        reason = " · ".join(reason_parts) or "인스타에서 내가 저장한 맛집"
        picks.append(
            RestaurantPick(
                name=r["name"],
                category=r.get("category") or "저장 맛집",
                area=r.get("address") or r.get("area") or "위치 확인",
                reason=reason,
                price_note=f"대표: {sig}" if sig else "인스타 저장 목록",
                service_tags=[t for t in ("내 저장", r.get("category") or "", r.get("area") or "") if t][:3],
                open_status="영업시간 지도 확인",
                naver_map_url=r.get("naver_url") or _build_map_urls(r["name"], r.get("area", ""))[0],
                kakao_map_url=r.get("kakao_url") or _build_map_urls(r["name"], r.get("area", ""))[1],
                phone=r.get("phone") or "",
                distance_text=dist_text,
                role_badge="내 저장 픽",
                is_verified_detail=True,
            )
        )

    if not picks:
        empty_pick = RestaurantPick(
            name="아직 저장된 인스타 맛집이 없어요",
            category="안내",
            area=clean_area or "내 주변",
            reason="아이폰 뒷면 탭 단축어로 인스타 화면을 캡처해 보내면 이곳에 자동 저장됩니다.",
            price_note="POST /api/ingest/screenshot",
            service_tags=["iOS 단축어", "자동 저장"],
            open_status="대기 중",
            naver_map_url="https://map.naver.com/",
            kakao_map_url="https://map.kakao.com/",
            is_verified_detail=False,
        )
        return {
            "query": "내 저장",
            "area": clean_area or "내 주변",
            "situation": "solo",
            "situation_label": "내 저장",
            "criteria": "내가 인스타에서 저장한 맛집",
            "data_source": "saved_places",
            "cached": False,
            "top_pick": empty_pick.to_dict(),
            "alternatives": [],
            "reserve_picks": [],
        }

    return {
        "query": "내 저장",
        "area": clean_area or ("내 위치 주변" if lat is not None else "전체 저장"),
        "situation": "solo",
        "situation_label": "내 저장",
        "criteria": "내가 인스타에서 저장한 맛집 중 가까운/일치 순",
        "data_source": "saved_places",
        "cached": False,
        "top_pick": picks[0].to_dict(),
        "alternatives": [p.to_dict() for p in picks[1:3]],
        "reserve_picks": [p.to_dict() for p in picks[3:6]],
    }
