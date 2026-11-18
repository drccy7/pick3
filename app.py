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

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

try:
    from .providers import ProviderType, SituationType, get_kakao_diag, get_recommendations
    from .reels_script import build_reels_script
    from .saved_places import (
        extract_place_from_image,
        load_all_saved_places,
        pick3_from_saved,
        resolve_place_with_kakao,
        save_place,
    )
except ImportError:
    from providers import ProviderType, SituationType, get_kakao_diag, get_recommendations
    from reels_script import build_reels_script
    from saved_places import (
        extract_place_from_image,
        load_all_saved_places,
        pick3_from_saved,
        resolve_place_with_kakao,
        save_place,
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
    return {"status": "ok", "service": "pick3", **get_kakao_diag()}


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


@app.post("/api/ingest/screenshot")
async def ingest_screenshot(image: UploadFile = File(...)) -> dict:
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image upload")
    extracted = await extract_place_from_image(raw, mime_type=image.content_type or "image/jpeg")
    if not extracted["name"]:
        return {
            "status": "not_found",
            "message": "화면에서 음식점 이름을 찾지 못했습니다. 상호가 보이는 화면에서 다시 시도해 주세요.",
        }
    resolved = await resolve_place_with_kakao(extracted)
    saved = await save_place(resolved)
    area_text = saved.get("area") or saved.get("address") or ""
    prefix = "이미 저장된 곳입니다" if saved.get("duplicate") else "✅ 저장 완료"
    return {
        "status": "saved",
        "message": f"{prefix}: {saved['name']}" + (f" ({area_text})" if area_text else ""),
        "place": saved,
    }


@app.get("/api/saved")
async def list_saved() -> dict:
    rows = await load_all_saved_places()
    return {"count": len(rows), "items": rows}


class SavedPick3Request(BaseModel):
    area: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None


@app.post("/api/saved/pick3")
async def saved_pick3(payload: SavedPick3Request) -> dict:
    return await pick3_from_saved(area=payload.area, lat=payload.lat, lng=payload.lng)
