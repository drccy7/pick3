"""Pick3 - No-List 3-Restaurant Decision Web App (FastAPI Backend)."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from providers import SituationType, get_kakao_diag, get_recommendations

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


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "pick3", **get_kakao_diag()}


@app.get("/api/presets")
async def presets() -> dict:
    return {
        "menus": ["돼지갈비", "삼겹살", "국밥", "파스타"],
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
    )
