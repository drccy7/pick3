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

import asyncio
import collections
import os
import time


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

import re
import secrets
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

try:
    from .providers import ProviderType, SituationType, get_kakao_diag, get_recommendations
    from .reels_script import build_reels_script
    from . import auth_pin, link_render, medical, nhis_claims, prescriptions, screen_router
    from .coverage_review import pdf_io as cov_pdf, service as cov_service, store as cov_store
    from .coverage_review import gemini as cov_gemini
    from . import gemini_usage
    from .saved_places import (
        StorageError,
        bump_usage,
        extract_place_from_image,
        find_user_by_token,
        gemini_model,
        delete_saved_place,
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
    import auth_pin
    import link_render
    import nhis_claims
    from coverage_review import pdf_io as cov_pdf, service as cov_service, store as cov_store
    from coverage_review import gemini as cov_gemini
    import gemini_usage
    import medical
    import prescriptions
    import screen_router
    from saved_places import (
        StorageError,
        bump_usage,
        extract_place_from_image,
        find_user_by_token,
        gemini_model,
        delete_saved_place,
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
@app.get("/prescriptions.html")
@app.get("/start.html")
@app.get("/saved.html")
@app.get("/coverage.html")
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
        gemini_usage.bind(user.get("id"))
        return user
    expected = _ingest_token()
    if expected and not (token and secrets.compare_digest(token, expected)):
        raise HTTPException(status_code=401, detail=_AUTH_ERROR)
    return {"id": None, "name": "local", "daily_limit": None}


class _IngestAuthError(Exception):
    """401 for the Shortcut: also carries a readable `message` (Shortcuts shows only that)."""

    def __init__(self, status_code: int, detail: str, message: str) -> None:
        self.status_code, self.detail, self.message = status_code, detail, message


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Shortcut uploads that miss the `image` file field: explain it in `message` (shown on the phone)."""
    body = {"detail": jsonable_encoder(exc.errors())}
    if request.url.path.startswith("/api/ingest/"):
        problems = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}" for e in exc.errors())
        body["status"] = "error"
        body["message"] = ("❌ 단축어가 보낸 요청에 파일(image)이 제대로 들어 있지 않습니다."
                           f"\n({problems[:200]} · Content-Type: {request.headers.get('content-type', '없음')[:60]})")
    return JSONResponse(status_code=422, content=body)


@app.exception_handler(_IngestAuthError)
async def _ingest_auth_error(_request: Request, exc: _IngestAuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code,
                        content={"status": "error", "detail": exc.detail, "message": exc.message})


async def _ingest_user(supplied: str | None) -> dict:
    """_current_user for Shortcut uploads: on failure say what token shape arrived (never the token)."""
    try:
        return await _current_user(supplied)
    except HTTPException as exc:
        token = (supplied or "").strip()
        if exc.status_code != 401:
            raise _IngestAuthError(exc.status_code, str(exc.detail), f"❌ 서버 문제: {exc.detail}") from exc
        shape = f"{len(token)}자, '{token[:4]}…'로 시작" if token else "비어 있음"
        message = ("❌ 계정 토큰이 맞지 않습니다. 시작 페이지에서 [② 내 계정 연결]을 다시 눌러 주세요."
                   f"\n(받은 토큰: {shape})")
        raise _IngestAuthError(401, str(exc.detail), message) from exc


def _daily_limits(user: dict) -> tuple[int, int]:
    per_user = user.get("daily_limit") or int(os.getenv("PICK3_DAILY_LIMIT_PER_USER", "30"))
    total = int(os.getenv("PICK3_DAILY_LIMIT_TOTAL", "300"))
    return int(per_user), total


MAX_PDF_BYTES = 40 * 1024 * 1024


@app.post("/api/ingest/screenshot")
async def ingest_screenshot(
    background: BackgroundTasks,
    image: UploadFile = File(...),
    x_pick3_token: Optional[str] = Header(default=None),
) -> dict:
    user = await _ingest_user(x_pick3_token)
    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image upload")
    is_pdf = raw[:5] == b"%PDF-"
    # 보장분석 PDFs go to Gemini via the File API (no inline limit): allow them up to 40 MB.
    if len(raw) > (MAX_PDF_BYTES if is_pdf else medical.MAX_UPLOAD_BYTES):
        limit = "40MB" if is_pdf else "15MB"
        return {"status": "error", "message": f"❌ 파일이 너무 큽니다({limit} 초과). 쪽을 나눠 보내 주세요."}
    link, text_note = _shared_text(raw, image.content_type)
    if text_note:  # a link we can't open, or plain text: no Gemini, no usage
        return {"status": "unsupported", "service": "none", "message": text_note}
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

    if link:  # a shared link: opening it takes 20-40 s, so answer now and work in the background
        job = _new_job(user, link)
        background.add_task(_run_link_job, user, link, job)
        return {"status": "accepted", "service": "link", "job_id": job["id"],
                "message": "🔗 링크를 받았습니다. 페이지를 열어 읽는 중입니다(20~40초).\n"
                           "결과는 Pick3 홈의 '최근 처리'에서 볼 수 있습니다."}

    # One Shortcut for every service: classify first, then use that service's own extractor.
    mime = medical.sniff_mime(raw, image.content_type or "image/jpeg")
    if mime == "application/pdf":
        encrypted = cov_pdf.is_encrypted(raw)
        if encrypted or cov_service.is_coverage_report(raw):  # 보장분석: store only, the user starts it on Pick3
            return await _store_coverage(user, raw, image.filename, encrypted)
        if len(raw) > medical.MAX_UPLOAD_BYTES:  # medical PDFs are sent inline to Gemini
            return {"status": "error", "message": "❌ 의료비·처방 PDF가 너무 큽니다(15MB 초과). 쪽을 나눠 보내 주세요."}
        # Other PDFs are medical documents (처방조제정보, 진료비 영수증); no restaurant arrives as a PDF.
        result = await _save_medical_document(user, raw, mime, "", image.filename)
        if result["service"] == "medical":
            result["message"] = "🧾 의료비 기록\n" + result["message"]
        return result
    if screen_router.auto_route_enabled():
        kind = await screen_router.classify_screen(raw, mime)
        if kind == "medical":
            result = await _save_medical(user, raw, mime)
            result["service"] = "medical"
            result["message"] = "🧾 의료비 기록\n" + result["message"]
            return result
        if kind == "prescription":
            result = await _save_prescriptions(user, raw, mime, filename=image.filename)
            result["service"] = "prescription"
            return result
    result = await _save_restaurant(user, raw, mime)
    result["service"] = "restaurant"
    if result["status"] == "saved":
        result["message"] = "🍽️ 맛집\n" + result["message"]
    elif result["status"] == "not_found":
        result["message"] = ("화면에서 맛집·의료비·처방 내용을 찾지 못했습니다. "
                             "상호나 결제 내역이 보이는 화면에서 다시 시도해 주세요.")
    return result


# ---- background jobs for shared links (in memory, last few per user) ------------------------
_JOBS: dict[object, "collections.deque[dict]"] = {}


def _new_job(user: dict, link: str = "", *, kind: str = "link", site: str | None = None,
             status: str = "running", message: str = "페이지를 여는 중…") -> dict:
    job = {"id": secrets.token_hex(4), "kind": kind, "status": status,
           "site": site if site is not None else (urlsplit(link).hostname or ""), "started": _kst_now(),
           "message": message}
    _JOBS.setdefault(user.get("id"), collections.deque(maxlen=5)).append(job)
    return job


def _kst_now() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%m-%d %H:%M")


async def _run_link_job(user: dict, link: str, job: dict) -> None:
    try:
        result = await _process_link(user, link)
    except Exception as exc:  # never leave a job "running"
        result = {"status": "error", "message": f"❌ 처리 중 오류: {type(exc).__name__}"}
    job.update(status="done" if result.get("status") == "saved" else "error", message=result.get("message", ""),
               finished=_kst_now())


# ---- 보장분석: shared PDFs are only stored; the user starts the proposal on coverage.html --------------
cov_gemini.USAGE_HOOK = gemini_usage.record_sync  # 보장분석 Gemini calls run in a worker thread
_MEDIA = {"pdf": "application/pdf", "html": "text/html; charset=utf-8"}
_COV_ACTIVE: dict[int, dict] = {}  # report id -> progress {stage, label, done, total, started, stage_started}


def _cov_progress(rid: int, p: dict) -> None:
    """Record a progress step; keeps the run's start time and when the current stage began."""
    now = time.monotonic()
    cur = _COV_ACTIVE.get(rid) or {"started": now, "stage": 0}
    stage_started = cur.get("stage_started", now) if p.get("stage") == cur.get("stage") else now
    _COV_ACTIVE[rid] = {"started": cur["started"], "stage_started": stage_started, "done": None, "total": None, **p}


def _doc_hint(filename: str | None) -> str:
    """What an encrypted PDF probably is, from its file name (the content can't be read before the password)."""
    name = (filename or "").replace(" ", "")
    return "nhis" if "요양급여" in name else ""


async def _store_coverage(user: dict, raw: bytes, filename: str | None, encrypted: bool) -> dict:
    path = f"{user.get('id')}/{secrets.token_hex(8)}/original.pdf"
    try:
        await cov_store.put_file(path, raw)
        row = await cov_store.create(user.get("id"), filename or "보장분석.pdf", len(raw), encrypted, path)
    except cov_store.StoreError as exc:
        return {"status": "error", "service": "coverage", "message": "❌ 보장분석 보고서를 저장하지 못했습니다.",
                "detail": str(exc)}
    if encrypted and _doc_hint(filename) == "nhis":
        lines = ["🩺 건강보험 요양급여내역(암호 PDF)을 받았습니다.",
                 "Pick3 → 🧾 의료비 기록 화면 위쪽에서 암호(보통 생년월일 6자리)를 넣으면",
                 "진단명과 함께 의료비 기록으로 저장합니다."]
    elif encrypted:
        lines = ["🔒 암호가 걸린 PDF를 저장했습니다.",
                 "Pick3 → 📋 보장분석에서 암호를 넣어 주세요(보통 생년월일).",
                 "보장분석 보고서면 제안서를, 요양급여내역·의료비 PDF면 의료비 기록으로 처리합니다."]
    else:
        lines = ["📋 보장분석 보고서를 저장했습니다.",
                 "제안서는 Pick3 → 📋 보장분석에서 [제안서 작성]을 눌러 시작하세요."]
    return {"status": "saved", "service": "coverage", "report_id": row["id"], "message": "\n".join(lines)}


def _store_failed(exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail=f"보장분석 저장소 문제: {exc}")


@app.get("/api/coverage")
async def coverage_list(kind: str = "", x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """Stored report PDFs. kind=medical: only encrypted 요양급여내역 waiting on the 의료비 page."""
    user = await _current_user(x_pick3_token)
    try:
        rows = await cov_store.list_reports(user["id"])
    except cov_store.StoreError as exc:
        raise _store_failed(exc)
    want = "nhis" if kind == "medical" else ""
    rows = [r for r in rows if _doc_hint(r.get("filename")) == want]
    usage_by_ref: dict[str, list] = {}
    for u in await gemini_usage.fetch_rows(user["id"], ref_prefix="coverage:"):
        usage_by_ref.setdefault(u["ref"], []).append(u)
    for r in rows:
        if f"coverage:{r['id']}" in usage_by_ref:
            sm = gemini_usage.summarize(usage_by_ref[f"coverage:{r['id']}"])
            r["usage"] = {"calls": sm["calls"], "krw": sm["krw"], "usd": sm["usd"],
                          "tokens": sum(g["prompt_tokens"] + g["output_tokens"] for g in sm["groups"]),
                          "partial": bool(sm["unpriced_models"])}
        r["has_extract"] = bool(r.pop("extract_ready", None))
        if r["status"] == "running":
            if r["id"] in _COV_ACTIVE:
                p = _COV_ACTIVE[r["id"]]
                now = time.monotonic()
                r["message"] = p.get("label", "")
                r["progress"] = {"stage": p.get("stage", 1), "done": p.get("done"), "total": p.get("total"),
                                 "elapsed": int(now - p["started"]),
                                 "stage_elapsed": int(now - p.get("stage_started", p["started"]))}
            else:
                r["status"], r["message"] = "error", "중단됐습니다(서버가 다시 시작됨). 다시 작성해 주세요."
        # Older runs also stored ReportLab PDFs; only the HTML results are shown (each can be sent as PDF).
        r["files"] = [{"index": i, "name": f["name"], "pdf": bool(f.get("pdf_path"))}
                      for i, f in enumerate(r.pop("results") or []) if f["name"].endswith(".html")]
    return {"reports": rows}


class CoverageRun(BaseModel):
    password: str = ""


@app.post("/api/coverage/{report_id}/run")
async def coverage_run(report_id: int, body: CoverageRun, background: BackgroundTasks,
                       x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    user = await _current_user(x_pick3_token)
    if report_id in _COV_ACTIVE:
        return {"ok": False, "message": "이미 작성 중입니다."}
    try:
        row = await cov_store.get(user["id"], report_id)
        if not row:
            raise HTTPException(status_code=404, detail="보고서가 없습니다.")
        if row.get("status") == "other":
            return {"ok": False, "message": "이미 의료비 기록으로 처리했습니다. 다시 하려면 PDF를 다시 공유해 주세요."}
        raw = b""
        if not row.get("extract"):  # first run: the PDF must be read (password needed if encrypted)
            raw = await cov_store.get_file(row["storage_path"])
            if row["encrypted"]:
                if not body.password.strip():
                    return {"ok": False, "need_password": True, "message": "PDF 암호를 넣어 주세요."}
                try:
                    raw = cov_pdf.decrypt(raw, body.password.strip())
                except cov_pdf.PdfPasswordError:
                    return {"ok": False, "need_password": True, "message": "암호가 맞지 않습니다."}
            if not cov_service.is_coverage_report(raw):
                _cov_progress(report_id, {"stage": 1, "label": "보장분석 보고서가 아니어서 의료비·처방 문서로 읽는 중"})
                await cov_store.update(user["id"], report_id, status="running", message="")
                background.add_task(_coverage_as_medical, user, row, raw)
                return {"ok": True, "message": _COV_ACTIVE[report_id]["label"]}
    except cov_store.StoreError as exc:
        raise _store_failed(exc)
    usage = await bump_usage(user["id"])
    per_user_limit, _ = _daily_limits(user)
    if usage["user_count"] > per_user_limit:
        return {"ok": False, "message": f"오늘 분석 한도({per_user_limit}건)를 넘었습니다. 내일 다시 시도해 주세요."}
    _cov_progress(report_id, {"stage": 1, "label": "시작"})
    await cov_store.update(user["id"], report_id, status="running", message="")
    background.add_task(_run_coverage, user, row, raw)
    return {"ok": True, "message": "제안서 작성을 시작했습니다(약 1분)."}


async def _run_coverage(user: dict, row: dict, raw: bytes) -> None:
    rid, uid = row["id"], user["id"]
    gemini_usage.bind(uid, ref=f"coverage:{rid}")  # this run's Gemini calls, for the per-report cost

    def progress(p: dict) -> None:
        _cov_progress(rid, p)
    try:
        res = await asyncio.to_thread(cov_service.run, raw, user_id=uid, rep=row.get("extract"), progress=progress,
                                      overrides=row.get("overrides") or {})
        _cov_progress(rid, {"stage": 4, "label": "결과 저장 중"})
        base = row["storage_path"].rsplit("/", 1)[0]
        results = []
        for i, (name, data) in enumerate(res["docs"].items()):
            ext = name.rsplit(".", 1)[-1]
            path = f"{base}/result_{i}.{ext}"
            await cov_store.put_file(path, data, _MEDIA[ext])
            results.append({"name": name, "path": path})
        kept = {f["path"] for f in results}
        stale = [p for f in row.get("results") or [] for p in (f.get("path"), f.get("pdf_path")) if p and p not in kept]
        if stale:  # PDFs printed from the previous HTML (and older runs' files) no longer match
            try:
                await cov_store.delete_files(stale)
            except cov_store.StoreError:
                pass
        c, a, pp = res["customer"], res["analysis"], res["plan"]
        lines = [f"계약 {len(res['facts']['contracts'])}건 · 병의원 기록 {res['records']}건 · "
                 f"청구 후보 {len(a.get('claims', []))}건 · 보완 제안 {len(a.get('gaps', []))}건",
                 f"제안 후 월 약 {round(pp['total_mid'] / 10000):,}만 원"]
        lines += [f"⚠ {w}" for w in res["warnings"][:3]]
        from datetime import datetime, timezone
        await cov_store.update(uid, rid, status="done", message="\n".join(lines), customer_name=c.get("name"),
                               birth_date=c.get("birth_date"), records_count=res["records"], extract=res["extract"],
                               analysis=res["analysis"], plan=pp, warnings=res["warnings"], results=results,
                               analyzed_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:  # never leave a report "running"
        try:
            await cov_store.update(uid, rid, status="error",
                                   message=f"❌ 작성 실패: {type(exc).__name__}. 잠시 후 다시 시도해 주세요.")
        except Exception:
            pass
    finally:
        _COV_ACTIVE.pop(rid, None)


async def _coverage_as_medical(user: dict, row: dict, raw: bytes) -> None:
    rid = row["id"]
    try:
        if len(raw) > medical.MAX_UPLOAD_BYTES:
            result = {"message": "❌ 의료비·처방 PDF가 너무 큽니다(15MB 초과)."}
        else:
            result = await _save_medical_document(user, raw, "application/pdf", "", row.get("filename"))
        if result.get("status") == "saved":
            await cov_store.delete_files([row["storage_path"]])  # done: don't keep the health-data PDF
        await cov_store.update(user["id"], rid, status="other",
                               message=result.get("message", "") + (
                                   "" if _doc_hint(row.get("filename")) == "nhis" else
                                   "\n(보장분석 보고서가 아니라 의료비·처방으로 처리했습니다. 이 항목은 지워도 됩니다.)"))
    except Exception as exc:
        try:
            await cov_store.update(user["id"], rid, status="error", message=f"❌ 처리 실패: {type(exc).__name__}")
        except Exception:
            pass
    finally:
        _COV_ACTIVE.pop(rid, None)


@app.get("/api/coverage/{report_id}/contracts")
async def coverage_contracts(report_id: int, x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """Contracts read from the report with Pick3's payment-status estimate and the planner's confirmed status."""
    from coverage_review import facts as cov_facts
    from datetime import date as _date
    user = await _current_user(x_pick3_token)
    try:
        row = await cov_store.get(user["id"], report_id)
    except cov_store.StoreError as exc:
        raise _store_failed(exc)
    if not row:
        raise HTTPException(status_code=404, detail="보고서가 없습니다.")
    if not row.get("extract"):
        return {"contracts": [], "message": "먼저 [제안서 작성]으로 보고서를 읽어 주세요."}
    rules = cov_facts.load_rules()
    est = cov_facts.contract_facts(row["extract"], _date.today(), rules)
    ov = row.get("overrides") or {}
    return {"statuses": cov_facts.STATUS, "contracts": [
        {"no": c["no"], "insurer": c["insurer"], "product": c["product"], "type": c["type"], "premium": c["premium"],
         "pay_cycle": c["pay_cycle"], "paid_count": c["paid_count"], "start": c["start"], "end": c["end"],
         "estimate": "paid_up" if c["paid_up_likely"] else "paying", "estimate_basis": c.get("paid_up_basis"),
         "status": ov.get(str(c["no"]), "auto")} for c in est]}


class CoverageOverrides(BaseModel):
    overrides: dict[str, str]


@app.post("/api/coverage/{report_id}/overrides")
async def coverage_overrides(report_id: int, body: CoverageOverrides,
                             x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """Save the planner's contract statuses ("auto" = keep Pick3's estimate). Takes effect on the next run."""
    from coverage_review import facts as cov_facts
    user = await _current_user(x_pick3_token)
    clean = {str(int(k)): v for k, v in body.overrides.items() if v in cov_facts.STATUS and str(k).isdigit()}
    try:
        if not await cov_store.get(user["id"], report_id):
            raise HTTPException(status_code=404, detail="보고서가 없습니다.")
        await cov_store.update(user["id"], report_id, overrides=clean)
    except cov_store.StoreError as exc:
        raise _store_failed(exc)
    return {"ok": True, "overrides": clean}


@app.get("/api/coverage/{report_id}/files/{index}")
async def coverage_file(report_id: int, index: int, x_pick3_token: Optional[str] = Header(default=None)) -> Response:
    user = await _current_user(x_pick3_token)
    try:
        row = await cov_store.get(user["id"], report_id)
        files = (row or {}).get("results") or []
        if not 0 <= index < len(files):
            raise HTTPException(status_code=404, detail="파일이 없습니다.")
        data = await cov_store.get_file(files[index]["path"])
    except cov_store.StoreError as exc:
        raise _store_failed(exc)
    ext = files[index]["name"].rsplit(".", 1)[-1]
    return Response(content=data, media_type=_MEDIA.get(ext, "application/octet-stream"),
                    headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(files[index]["name"]),
                             "Cache-Control": "no-store"})


_PDF_LOCKS: dict[int, asyncio.Lock] = {}


@app.get("/api/coverage/{report_id}/pdf/{index}")
async def coverage_pdf(report_id: int, index: int, x_pick3_token: Optional[str] = Header(default=None)) -> Response:
    """The result HTML printed to PDF (no password). Printed on the first send, then reused until the next run."""
    user = await _current_user(x_pick3_token)
    lock = _PDF_LOCKS.setdefault(report_id, asyncio.Lock())
    async with lock:  # two taps on the same report print once
        try:
            row = await cov_store.get(user["id"], report_id)
            files = (row or {}).get("results") or []
            if not 0 <= index < len(files) or not files[index]["name"].endswith(".html"):
                raise HTTPException(status_code=404, detail="파일이 없습니다.")
            f = files[index]
            name = f["name"][:-5] + ".pdf"
            if f.get("pdf_path"):
                data = await cov_store.get_file(f["pdf_path"])
            else:
                if not cov_service.pdf_enabled():
                    raise HTTPException(status_code=503, detail="PDF 만들기가 꺼져 있습니다.")
                html = (await cov_store.get_file(f["path"])).decode("utf-8")
                try:
                    data = await asyncio.to_thread(cov_service.html_to_pdf, html)
                except Exception as exc:
                    raise HTTPException(status_code=503, detail=f"PDF를 만들지 못했습니다({type(exc).__name__}).")
                pdf_path = f["path"][:-5] + ".pdf"
                await cov_store.put_file(pdf_path, data, _MEDIA["pdf"])
                fresh = await cov_store.get(user["id"], report_id)  # a run may have replaced the results meanwhile
                cur = (fresh or {}).get("results") or []
                if index < len(cur) and cur[index].get("path") == f["path"] and report_id not in _COV_ACTIVE:
                    cur[index] = {**cur[index], "pdf_path": pdf_path}
                    await cov_store.update(user["id"], report_id, results=cur)
        except cov_store.StoreError as exc:
            raise _store_failed(exc)
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(name),
                             "Cache-Control": "no-store"})


@app.delete("/api/coverage/{report_id}")
async def coverage_delete(report_id: int, x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    user = await _current_user(x_pick3_token)
    if report_id in _COV_ACTIVE:
        return {"ok": False, "message": "작성 중에는 지울 수 없습니다."}
    try:
        row = await cov_store.get(user["id"], report_id)
        if not row:
            raise HTTPException(status_code=404, detail="보고서가 없습니다.")
        await cov_store.delete_files([row["storage_path"]] + [p for f in row.get("results") or []
                                                              for p in (f.get("path"), f.get("pdf_path")) if p])
        await cov_store.delete(user["id"], report_id)
    except cov_store.StoreError as exc:
        raise _store_failed(exc)
    return {"ok": True}


async def _process_link(user: dict, link: str) -> dict:
    """Render a shared link like a phone browser and save it as medical PDF(s)."""
    try:
        pages, final_url, text_chars = await link_render.render(link)
    except link_render.LinkError as exc:
        return {"status": "error", "service": "none", "message": f"🔗 링크를 열지 못했습니다: {exc}"}
    except Exception as exc:
        return {"status": "error", "service": "none",
                "message": "🔗 링크를 여는 중 문제가 생겼습니다. 잠시 후 다시 시도하거나 화면을 캡처해 보내 주세요.",
                "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    # One PDF per person tab on the page (name = that tab's label), or one PDF without a name.
    results = [await _save_medical_document(user, pdf, "application/pdf", name, None) for name, pdf in pages]
    result = next((r for r in results if r.get("status") == "saved"), results[0])
    if len(results) > 1:
        result = {**result, "results": results,
                  "status": "saved" if any(r.get("status") == "saved" for r in results) else result.get("status"),
                  "message": "\n\n".join(r["message"] for r in results)}
    result["message"] = "🔗 링크에서 읽음\n" + result["message"]
    if result.get("status") != "saved":
        final = urlsplit(final_url)
        result["message"] += f"\n(연 페이지: {final.hostname}{final.path} · 글자 {text_chars}자)"
    return result


_KNOWN_MAGIC = (b"%PDF-", b"\x89PNG", b"\xff\xd8\xff", b"RIFF")


def _shared_text(raw: bytes, declared: str | None = None) -> tuple[str, str]:
    """Upload that is text (a shared link or copied words) instead of an image/PDF.

    Returns (link, note): a link on a PICK3_RENDER_DOMAINS site to open, or a note telling the user
    what to send instead. ("", "") means it is not text - handle as image/PDF.
    """
    if raw.startswith(_KNOWN_MAGIC) or raw[4:8] == b"ftyp" or len(raw) > 20000:
        return "", ""
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return "", ""
    if not text or "\x00" in text:
        return "", ""
    if text[:200].lstrip().lower().startswith(("<!doctype html", "<html")):
        # The Shortcut downloaded the page instead of passing the link: the JS-filled data isn't in it.
        return "", ("🔗 링크 대신 웹페이지 원본(HTML)을 받았습니다. 이 방식으로는 내역을 읽을 수 없습니다.\n"
                    "Safari에서 열고 스크린샷 → [전체 페이지] → 공유 → Pick3 으로 보내 주세요.")
    link = link_render.find_link(text)
    is_link =bool(link) and len(text) < len(link) + 300  # a link, maybe with a short title around it
    if not is_link and not (declared or "").startswith("text/"):
        return "", ""
    if is_link:
        host = urlsplit(link).hostname or ""
        if link.startswith("https://") and link_render.host_allowed(host):
            if not link_render.browser_enabled():
                return "", ("🔗 링크 읽기는 잠시 꺼져 있습니다.\n"
                            "그 페이지를 Safari에서 열고 스크린샷 → 썸네일 → 위쪽 [전체 페이지] → 공유 → Pick3 으로 보내 주세요(PDF로 저장됨).")
            return link, ""
        return "", ("🔗 링크를 받았습니다. 아직 링크로 읽을 수 없는 사이트입니다" + (f"({host})" if host else "") + ".\n"
                    "그 페이지를 Safari에서 열고 스크린샷 → 썸네일 → 위쪽 [전체 페이지] → 공유 → Pick3 으로 보내 주세요(PDF로 저장됨).")
    return "", "글자만 받았습니다. 화면 캡처나 PDF를 공유해 주세요."


async def _save_restaurant(user: dict, raw: bytes, mime: str) -> dict:
    """Extract a restaurant/cafe, verify with Kakao, save for `user`."""
    try:
        extracted = await extract_place_from_image(raw, mime_type=mime)
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


@app.get("/api/jobs")
async def recent_jobs(x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """Recent background jobs (shared links) of this user, newest first."""
    user = await _current_user(x_pick3_token)
    return {"jobs": [{k: v for k, v in j.items() if not k.startswith("_")}
                     for j in reversed(_JOBS.get(user.get("id"), []))]}


@app.get("/api/usage")
async def usage_summary(x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """This user's Gemini usage this month (KST), by feature and model, with estimated cost."""
    user = await _current_user(x_pick3_token)
    rows = await gemini_usage.fetch_rows(user.get("id"), since=gemini_usage.month_start_kst())
    return {"month_rows": len(rows), **gemini_usage.summarize(rows)}


@app.get("/api/me")
async def me(x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """Lets the web app check a pasted token and greet the user."""
    user = await _current_user(x_pick3_token)
    out = {"name": user["name"], "auth_mode": _auth_mode()}
    if user.get("id") is not None:
        try:
            out["has_pin"] = await auth_pin.has_pin(user["id"])
        except StorageError:
            pass  # PIN columns missing (migration 005 not run yet) - sign-in still works
    return out


class PinSet(BaseModel):
    pin: str


@app.post("/api/pin")
async def set_pin(body: PinSet, x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """A signed-in user creates or changes their own 6-digit PIN."""
    user = await _current_user(x_pick3_token)
    if user.get("id") is None:
        raise HTTPException(status_code=400, detail="PIN 로그인은 서버 저장소(Supabase)에서만 됩니다.")
    try:
        await auth_pin.set_pin(user["id"], body.pin)
    except auth_pin.PinError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "message": "PIN을 저장했습니다. 다른 브라우저에서는 이름과 PIN으로 들어오세요."}


class PinLogin(BaseModel):
    name: str
    pin: str


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


@app.post("/api/login")
async def login(body: PinLogin, request: Request) -> dict:
    """Name + PIN -> a web session token (sent as X-Pick3-Token like a personal token)."""
    if not supabase_configured():
        raise HTTPException(status_code=400, detail="PIN 로그인은 서버 저장소(Supabase)에서만 됩니다.")
    try:
        return await auth_pin.login(body.name, body.pin, _client_ip(request))
    except auth_pin.PinError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/logout")
async def logout(x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """Ends a name+PIN web session. Personal tokens (Shortcuts) are not affected."""
    token = (x_pick3_token or "").strip()
    if token.startswith(auth_pin.SESSION_PREFIX) and supabase_configured():
        try:
            await auth_pin.logout(token)
        except StorageError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/api/saved")
async def list_saved(x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    user = await _current_user(x_pick3_token)
    try:
        rows = await load_all_saved_places(user["id"])
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"user": user["name"], "count": len(rows), "items": rows}


@app.delete("/api/saved/{place_id}")
async def delete_saved(place_id: int, x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    """Remove one of the caller's saved places (only their own rows can match)."""
    user = await _current_user(x_pick3_token)
    try:
        removed = await delete_saved_place(place_id, user["id"])
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": place_id}


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
    user = await _ingest_user(x_pick3_token)
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
    return await _save_medical_document(user, raw, mime, chosen, image.filename)


async def _save_medical_document(user: dict, raw: bytes, mime: str, chosen: str, filename: str | None) -> dict:
    """A medical document (PDF or image): 요양급여내역 -> visits with 진단명, 처방조제정보 -> prescriptions,
    else payments (-> prescriptions)."""
    if mime == "application/pdf" and nhis_claims.is_nhis_pdf(raw):  # 건강보험 요양급여내역 (standard form)
        return await _save_nhis(user, raw, chosen)
    if prescriptions.is_prescription_pdf(raw):  # 처방조제정보 PDF -> its own table
        result = await _save_prescriptions(user, raw, mime, chosen, filename=filename)
        result["service"] = "prescription"
        return result
    if mime != "application/pdf" and screen_router.auto_route_enabled():
        # An image here may be a prescription screen (e.g. a PDF viewer captured instead of the file).
        if await screen_router.classify_screen(raw, mime) == "prescription":
            result = await _save_prescriptions(user, raw, mime, chosen, filename=filename)
            result["service"] = "prescription"
            return result
    result = await _save_medical(user, raw, mime, chosen)
    result["service"] = "medical"
    if result.get("status") == "not_found":
        # No payment rows: the document may still be a prescription list the title check missed.
        rx = await _save_prescriptions(user, raw, mime, chosen, filename=filename)
        if rx.get("status") == "saved":
            rx["service"] = "prescription"
            return rx
        result["message"] += "\n" + _received_note(raw, mime, filename)
    return result


async def _save_nhis(user: dict, raw: bytes, chosen: str = "") -> dict:
    try:
        parsed = nhis_claims.parse(raw)
    except Exception as exc:
        return {"status": "error", "service": "medical", "message": "❌ 요양급여내역을 읽지 못했습니다.",
                "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    patient = (chosen or parsed["patient"]).strip()[:40]
    if not parsed["rows"]:
        return {"status": "not_found", "service": "medical", "message": "요양급여내역에서 진료 내역을 찾지 못했습니다."}
    try:
        counts = await nhis_claims.save(parsed, user["id"], patient)
    except StorageError as exc:
        hint = " (DB에 진단명 칸이 없습니다: migration 008 실행 필요)" if "column" in str(exc) else ""
        return {"status": "error", "service": "medical", "message": "❌ 저장 실패" + hint, "detail": str(exc)}
    return {"status": "saved", "service": "medical", "message": nhis_claims.summary_message(parsed, patient, counts),
            "count": len(parsed["rows"]), **counts}


def _received_note(raw: bytes, mime: str, filename: str | None) -> str:
    """Short description of what the Shortcut actually sent - lets users report failures precisely."""
    kind = {"application/pdf": "PDF"}.get(mime, mime.split("/")[-1].upper())
    head = raw[:8].decode("latin-1", "replace").encode("ascii", "replace").decode()
    return f"(받은 파일: {kind} · {max(1, len(raw) // 1024)}KB · {filename or '이름 없음'} · {head!r})"


async def _save_prescriptions(user: dict, raw: bytes, mime: str, chosen: str = "", filename: str | None = None) -> dict:
    """처방·조제 rows: PDF table via pdfplumber, else Gemini. Name: given > on screen > file name > recent."""
    missing: list[int] = []
    on_screen = ""
    rows: list[dict] = []
    if mime == "application/pdf":
        try:
            rows, missing = prescriptions.parse_prescription_pdf(raw)
        except Exception:
            rows = []
    if not rows:
        try:
            on_screen, rows = await prescriptions.extract_with_gemini(raw, mime)
        except Exception as exc:
            return {"status": "error", "message": "❌ 처방 내역 분석 실패. 잠시 후 다시 시도해 주세요.",
                    "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if not rows:
        return {"status": "not_found", "message": "처방·조제 내역(날짜·기관·약품명)을 찾지 못했습니다."}

    from_file = prescriptions.name_from_filename(filename)
    patient, source_note = chosen, "입력한 이름"
    if not patient and on_screen:
        patient, source_note = on_screen, "화면의 이름"
    if not patient and from_file:
        patient, source_note = from_file, "파일 이름에서"
    if not patient:
        try:
            patient = await prescriptions.recent_patient(user["id"])
        except StorageError:
            patient = ""
        source_note = "최근 이름" if patient else ""
    try:
        inserted, duplicates = await prescriptions.save(
            rows, user["id"], patient, "pdf" if mime == "application/pdf" else "image")
    except StorageError as exc:
        return {"status": "error", "message": "❌ 저장 실패 - 저장소 연결 문제입니다.", "detail": str(exc)}

    # Same date/provider/drug twice in one document is one record; report it apart from earlier saves.
    in_doc = len(rows) - len({(r["visit_date"], r["provider"], r["drug_name"]) for r in rows})
    already = max(duplicates - in_doc, 0)
    dates = sorted(r["visit_date"] for r in rows)
    providers = {r["provider"] for r in rows}
    lines = [
        "💊 처방·조제 기록",
        f"✅ {patient or '이름 없음'}: {inserted}건 저장"
        + (f" (이미 있던 {already}건 제외)" if already else "")
        + (f" (문서 안 같은 약 {in_doc}건 합침)" if in_doc else ""),
        f"기간 {dates[0]} ~ {dates[-1]} · 기관 {len(providers)}곳",
    ]
    if patient and source_note in ("파일 이름에서", "최근 이름"):
        lines.append(f"⚠ 이름은 {source_note} 가져왔습니다. 다르면 처방·조제 화면에서 바꾸세요.")
    elif not patient:
        lines.append("⚠ 고객·환자 이름을 찾지 못했습니다. 처방·조제 화면에서 이름을 입력하세요.")
    if missing:
        lines.append(f"⚠ 순번 {len(missing)}개를 읽지 못했습니다: {', '.join(map(str, missing[:10]))}")
    return {"status": "saved", "message": "\n".join(lines), "patient": patient, "patient_source": source_note,
            "inserted": inserted, "duplicates": duplicates, "missing": missing, "count": len(rows)}


async def _save_medical(user: dict, raw: bytes, mime: str, chosen: str = "") -> dict:
    """Extract + save medical rows for `user` (auth and usage limits already checked)."""
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


# ---- prescription / dispensing records (처방·조제) -----------------------------


@app.get("/api/prescriptions")
async def list_prescriptions(
    patient: str = "",
    year: Optional[int] = None,
    x_pick3_token: Optional[str] = Header(default=None),
) -> dict:
    user = await _current_user(x_pick3_token)
    try:
        rows = await prescriptions.list_rows(user["id"], patient=patient.strip(), year=year)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"user": user["name"], "count": len(rows), "items": rows}


class PatientChange(BaseModel):
    ids: list[int]
    patient: str


@app.post("/api/prescriptions/patient")
async def change_prescription_patient(payload: PatientChange, x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    user = await _current_user(x_pick3_token)
    if not payload.ids or len(payload.ids) > 5000:
        raise HTTPException(status_code=422, detail="바꿀 기록을 골라 주세요.")
    try:
        rows = await prescriptions.set_patient(payload.ids, user["id"], payload.patient)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"updated": len(rows), "items": rows}


@app.delete("/api/prescriptions")
async def delete_prescription_patient(patient: str, x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    user = await _current_user(x_pick3_token)
    name = patient.strip()
    if not name:
        raise HTTPException(status_code=422, detail="삭제할 고객·환자 이름을 지정해 주세요.")
    try:
        count = await prescriptions.delete_patient(user["id"], name)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"patient": name, "deleted": count}


@app.delete("/api/prescriptions/{record_id}")
async def delete_prescription(record_id: int, x_pick3_token: Optional[str] = Header(default=None)) -> dict:
    user = await _current_user(x_pick3_token)
    try:
        deleted = await prescriptions.delete_one(record_id, user["id"])
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"deleted": record_id}


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
