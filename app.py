"""Pick3 - No-List 3-Restaurant Decision Web App (FastAPI Backend)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Union

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

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

try:
    from .providers import ProviderType, SituationType, get_kakao_diag, get_recommendations
    from .reels_script import build_reels_script
    from . import medical
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
    import medical
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
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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


@app.get("/account.js")
@app.get("/pick3.html")
@app.get("/medical.html")
@app.get("/start.html")
async def extra_page(request: Request) -> FileResponse:
    """Serve the service pages next to the index.html launcher (Render origin)."""
    path = BASE_DIR / request.url.path.lstrip("/")
    if not path.exists():
        raise HTTPException(status_code=404, detail="page not found")
    return FileResponse(str(path))


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


# ---- medical expense records (병원·약국 결제 내역) ---------------------------


@app.post("/api/ingest/medical")
async def ingest_medical(
    image: UploadFile = File(...),
    x_pick3_token: Optional[str] = Header(default=None),
    patient: Optional[str] = Form(default=None),
) -> dict:
    """Screenshot or PDF of hospital/pharmacy payments -> rows saved for the token's user.

    `patient` (form field, optional) names the customer/patient explicitly - e.g. an insurance
    planner saving a customer's records under their own token. It wins over the name on screen.
    """
    chosen = (patient or "").strip()[:40]
    user = await _current_user(x_pick3_token)
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(raw) > medical.MAX_UPLOAD_BYTES:
        return {"status": "error", "message": "❌ 파일이 너무 큽니다(15MB 초과). 쪽을 나눠 보내 주세요."}
    if not os.getenv("GEMINI_API_KEY", "").strip():
        return {"status": "error", "message": "❌ 서버에 GEMINI_API_KEY가 설정되지 않아 분석할 수 없습니다."}
    try:
        usage = await bump_usage(user["id"])
    except StorageError as exc:
        return {"status": "error", "message": "❌ 저장소 연결 문제입니다. 잠시 후 다시 시도해 주세요.", "detail": str(exc)}
    per_user_limit, total_limit = _daily_limits(user)
    if usage["user_count"] > per_user_limit or usage["total_count"] > total_limit:
        return {"status": "limited", "message": f"오늘 분석 한도({per_user_limit}건)를 넘었습니다. 내일 다시 시도해 주세요."}

    mime = medical.sniff_mime(raw, image.content_type)
    try:
        extracted = await medical.extract_medical_records(raw, mime)
    except Exception as exc:
        return {
            "status": "error",
            "message": "❌ 분석 실패 (Gemini). 잠시 후 다시 시도해 주세요.",
            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    on_screen, rows, skipped = medical.normalize_records(extracted)
    patient = chosen or on_screen
    if not rows:
        return {"status": "not_found", "message": "결제 내역(기관·금액·날짜)을 찾지 못했습니다. 내역이 보이는 화면으로 다시 시도해 주세요."}
    rows = [{**r, "patient": patient} for r in rows]
    assumed = False
    if not patient:
        # Nameless screen (e.g. a receipt): default to the patient this user saved most recently.
        try:
            recent = await medical.recent_patient(user["id"])
        except StorageError:
            recent = ""
        if recent:
            patient, assumed = recent, True
            rows = [{**r, "patient": recent} for r in rows]
    try:
        inserted, duplicates = await medical.save_medical_records(
            rows, user["id"], "pdf" if mime == "application/pdf" else "image")
    except StorageError as exc:
        return {"status": "error", "message": "❌ 저장 실패 - 저장소 연결 문제입니다.", "detail": str(exc)}

    who = patient or "이름 없음"
    total = sum(r["amount"] for r in rows)
    lines = [f"✅ {who}: {inserted}건 저장" + (f" (이미 있던 {duplicates}건 제외)" if duplicates else "")]
    lines += [f"{r['visit_date']} {r['provider']} {r['amount']:,}원" for r in rows[:15]]
    if len(rows) > 15:
        lines.append(f"… 외 {len(rows) - 15}건")
    lines.append(f"화면 합계 {total:,}원")
    if chosen and on_screen and on_screen.replace(" ", "") != chosen.replace(" ", ""):
        lines.append(f"⚠ 화면의 이름은 '{on_screen}'인데 입력한 '{chosen}'(으)로 저장했습니다. 다르면 의료비 화면에서 바꾸세요.")
    if assumed:
        lines.append(f"⚠ 이름이 없어 최근 고객·환자 '{patient}'(으)로 저장했습니다. 다르면 의료비 화면에서 행을 눌러 바꾸세요(같이 저장된 건 한 번에).")
    elif not patient:
        lines.append("⚠ 고객·환자 이름을 찾지 못했습니다. 의료비 화면에서 행을 눌러 이름을 입력하세요.")
    if skipped:
        lines.append(f"⚠ 잘려서 읽지 못한 {skipped}건은 빠졌습니다. 스크롤해서 한 장 더 보내 주세요.")
    return {"status": "saved", "message": "\n".join(lines), "patient": patient, "patient_assumed": assumed,
            "patient_on_screen": on_screen,
            "inserted": inserted, "duplicates": duplicates, "skipped": skipped, "records": rows}


@app.get("/api/medical")
async def list_medical(
    patient: str = "",
    year: Optional[int] = None,
    x_pick3_token: Optional[str] = Header(default=None),
) -> dict:
    user = await _current_user(x_pick3_token)
    try:
        rows = await medical.list_medical_records(user["id"], patient=patient.strip(), year=year)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"user": user["name"], "count": len(rows), "items": rows, "totals": medical.summarize(rows)}


class MedicalEdit(BaseModel):
    patient: Optional[str] = None
    provider: Optional[str] = None
    kind: Optional[str] = None
    amount: Optional[Union[int, str]] = None
    visit_date: Optional[str] = None


@app.patch("/api/medical/{record_id}")
async def edit_medical(
    record_id: int,
    payload: MedicalEdit,
    x_pick3_token: Optional[str] = Header(default=None),
) -> dict:
    user = await _current_user(x_pick3_token)
    try:
        changes = medical.clean_update(payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        row = await medical.update_medical_record(record_id, user["id"], changes)
    except medical.DuplicateRecordError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="Record not found")
    return row


class MedicalPatientChange(BaseModel):
    ids: list[int]
    patient: str


@app.post("/api/medical/patient")
async def change_medical_patient(
    payload: MedicalPatientChange,
    x_pick3_token: Optional[str] = Header(default=None),
) -> dict:
    """Rename the patient on several rows (e.g. all rows from one capture saved under a wrong name)."""
    user = await _current_user(x_pick3_token)
    if not payload.ids or len(payload.ids) > 500:
        raise HTTPException(status_code=422, detail="바꿀 기록을 1~500건 골라 주세요.")
    try:
        rows = await medical.set_patient(payload.ids, user["id"], payload.patient)
    except medical.DuplicateRecordError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"updated": len(rows), "items": rows}


@app.delete("/api/medical")
async def delete_medical_patient(
    patient: str,
    x_pick3_token: Optional[str] = Header(default=None),
) -> dict:
    """Delete ALL of one customer's/patient's records for this user (e.g. on a customer's request)."""
    user = await _current_user(x_pick3_token)
    name = patient.strip()
    if not name:
        raise HTTPException(status_code=422, detail="삭제할 고객·환자 이름을 지정해 주세요.")
    try:
        count = await medical.delete_patient_records(user["id"], name)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"patient": name, "deleted": count}


@app.delete("/api/medical/{record_id}")
async def delete_medical(record_id: int, x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    user = await _current_user(x_pick3_token)
    try:
        deleted = await medical.delete_medical_record(record_id, user["id"])
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"deleted": record_id}
