"""Pick3 - No-List 3-Restaurant Decision Web App (FastAPI Backend)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import os


def _load_local_env(path: Path) -> None:
    """Load KEY=VALUE lines from a local .env without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env(Path(__file__).resolve().parent / ".env")

import secrets

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

try:
    from .providers import ProviderType, SituationType, get_kakao_diag, get_recommendations
    from .reels_script import build_reels_script
    from .saved_places import (
        StorageError,
        bump_usage,
        extract_place_from_image,
        find_user_by_token,
        gemini_model,
        load_all_saved_places,
        pick3_from_saved,
        resolve_place_with_kakao,
        save_place,
        storage_status,
        supabase_configured,
    )
except ImportError:
    from providers import ProviderType, SituationType, get_kakao_diag, get_recommendations
    from reels_script import build_reels_script
    from saved_places import (
        StorageError,
        bump_usage,
        extract_place_from_image,
        find_user_by_token,
        gemini_model,
        load_all_saved_places,
        pick3_from_saved,
        resolve_place_with_kakao,
        save_place,
        storage_status,
        supabase_configured,
    )

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pick3 - 10초 맛집 결정기", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://drccy7.github.io",
        "http://127.0.0.1:8088",
        "http://localhost:8088",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class RecommendRequest(BaseModel):
    query: str
    situation: SituationType
    area: str = ""
    provider: ProviderType = "kakao"
    lat: Optional[float] = None
    lng: Optional[float] = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("메뉴나 음식 이름을 입력해 주세요.")
        if len(cleaned) > 50:
            raise ValueError("검색어는 50자 이하로 입력해 주세요.")
        return cleaned

    @field_validator("area")
    @classmethod
    def validate_area(cls, v: str) -> str:
        return v.strip()[:40]


@app.api_route("/", methods=["GET", "HEAD"])
async def index() -> FileResponse:
    index_path = BASE_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(str(index_path))


@app.get("/manifest.json")
async def manifest() -> FileResponse:
    path = BASE_DIR / "manifest.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="manifest.json not found")
    return FileResponse(str(path), media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon() -> FileResponse:
    path = BASE_DIR / "icon.svg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="icon.svg not found")
    return FileResponse(str(path), media_type="image/svg+xml")


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "pick3", **get_kakao_diag(), **(await storage_status()),
            "auth_mode": _auth_mode(),
            "ingest_auth_configured": _auth_mode() != "open",
            "gemini_key_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
            "gemini_model": gemini_model()}


@app.get("/api/presets")
async def presets() -> dict:
    return {
        "menus": ["돼지갈비", "삼겹살", "국밥", "파스타", "소바"],
        "situations": [
            {"id": "solo", "label": "혼자 / 점심", "emoji": "🍚"},
            {"id": "date", "label": "둘이서 / 데이트", "emoji": "💑"},
            {"id": "group", "label": "여럿이 / 회식", "emoji": "🍻"},
        ],
    }


@app.post("/api/recommend")
async def recommend(payload: RecommendRequest) -> dict:
    return await get_recommendations(
        query=payload.query,
        situation=payload.situation,
        area=payload.area,
        provider=payload.provider,
        lat=payload.lat,
        lng=payload.lng,
    )


class ReelsScriptRequest(RecommendRequest):
    episode: int = 1


@app.post("/api/reels-script")
async def reels_script(payload: ReelsScriptRequest) -> dict:
    rec = await get_recommendations(
        query=payload.query,
        situation=payload.situation,
        area=payload.area,
        provider=payload.provider,
        lat=payload.lat,
        lng=payload.lng,
    )
    return build_reels_script(rec, episode=max(1, payload.episode))


def _ingest_token() -> str:
    return os.getenv("PICK3_INGEST_TOKEN", "").strip()


def _auth_mode() -> str:
    """per_user: Supabase pick3_users tokens. shared_token: local dev with one env token. open: local dev."""
    if supabase_configured():
        return "per_user"
    return "shared_token" if _ingest_token() else "open"


_AUTH_ERROR = "Invalid or missing X-Pick3-Token"


async def _current_user(supplied: str | None) -> dict:
    """Resolve X-Pick3-Token to a user.

    With Supabase every request needs a registered per-user token (scripts/pick3_users.py).
    Without Supabase (local single-user dev) PICK3_INGEST_TOKEN, if set, is the only token.
    """
    token = (supplied or "").strip()
    if supabase_configured():
        if not token:
            raise HTTPException(status_code=401, detail=_AUTH_ERROR)
        try:
            user = await find_user_by_token(token)
        except StorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not user:
            raise HTTPException(status_code=401, detail=_AUTH_ERROR)
        return user
    expected = _ingest_token()
    if expected and not (token and secrets.compare_digest(token, expected)):
        raise HTTPException(status_code=401, detail=_AUTH_ERROR)
    return {"id": None, "name": "local", "daily_limit": None}


def _daily_limits(user: dict) -> tuple[int, int]:
    per_user = user.get("daily_limit") or int(os.getenv("PICK3_DAILY_LIMIT_PER_USER", "30"))
    total = int(os.getenv("PICK3_DAILY_LIMIT_TOTAL", "300"))
    return int(per_user), total


@app.post("/api/ingest/screenshot")
async def ingest_screenshot(
    image: UploadFile = File(...),
    x_pick3_token: Optional[str] = Header(default=None),
) -> dict:
    user = await _current_user(x_pick3_token)
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image upload")
    if not os.getenv("GEMINI_API_KEY", "").strip():
        return {"status": "error", "message": "❌ 서버에 GEMINI_API_KEY가 설정되지 않아 화면을 분석할 수 없습니다."}
    try:
        usage = await bump_usage(user["id"])
    except StorageError as exc:
        return {"status": "error", "message": "❌ 저장소 연결 문제입니다. 잠시 후 다시 시도해 주세요.", "detail": str(exc)}
    per_user_limit, total_limit = _daily_limits(user)
    if usage["user_count"] > per_user_limit:
        return {"status": "limited", "message": f"오늘 저장 한도({per_user_limit}건)를 넘었습니다. 내일 다시 시도해 주세요."}
    if usage["total_count"] > total_limit:
        return {"status": "limited", "message": "오늘 서비스 전체 분석 한도에 도달했습니다. 내일 다시 시도해 주세요."}
    try:
        extracted = await extract_place_from_image(raw, mime_type=image.content_type or "image/jpeg")
    except Exception as exc:
        return {
            "status": "error",
            "message": "❌ 화면 분석 실패 (Gemini). 잠시 후 다시 시도해 주세요.",
            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    if not extracted["name"]:
        return {
            "status": "not_found",
            "message": "화면에서 음식점 이름을 찾지 못했습니다. 상호가 보이는 화면에서 다시 시도해 주세요.",
        }
    resolved = await resolve_place_with_kakao(extracted)
    try:
        saved = await save_place(resolved, user_id=user["id"])
    except StorageError as exc:
        return {
            "status": "error",
            "message": f"❌ 저장 실패: {resolved['name']} - 저장소 연결 문제입니다. 잠시 후 다시 시도해 주세요.",
            "detail": str(exc),
        }
    area_text = saved.get("area") or saved.get("address") or ""
    prefix = "이미 저장된 곳입니다" if saved.get("duplicate") else "✅ 저장 완료"
    message = f"{prefix}: {saved['name']}" + (f" ({area_text})" if area_text else "")
    if not resolved.get("verified"):
        message += "\n⚠ 지도에서 가게를 확정하지 못했습니다(위치 확인 필요). 동네·지점이 보이는 화면에서 다시 시도하면 정확해집니다."
    return {
        "status": "saved",
        "message": message,
        "verified": bool(resolved.get("verified")),
        "place": saved,
    }


@app.get("/api/me")
async def me(x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """Lets the web app check a pasted token and greet the user."""
    user = await _current_user(x_pick3_token)
    return {"name": user["name"], "auth_mode": _auth_mode()}


@app.get("/api/saved")
async def list_saved(x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    user = await _current_user(x_pick3_token)
    try:
        rows = await load_all_saved_places(user["id"])
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"user": user["name"], "count": len(rows), "items": rows}


class SavedPick3Request(BaseModel):
    area: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None


@app.post("/api/saved/pick3")
async def saved_pick3(
    payload: SavedPick3Request,
    x_pick3_token: Optional[str] = Header(default=None),
) -> dict:
    user = await _current_user(x_pick3_token)
    try:
        return await pick3_from_saved(area=payload.area, lat=payload.lat, lng=payload.lng, user_id=user["id"])
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
