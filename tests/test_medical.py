"""Medical expense records: extraction normalization, per-user storage, dedupe, auth."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PICK3_DIR = Path(__file__).resolve().parent.parent
if str(PICK3_DIR) not in sys.path:
    sys.path.insert(0, str(PICK3_DIR))

import medical  # noqa: E402
import saved_places  # noqa: E402
from app import app  # noqa: E402

client = TestClient(app)
TOKEN = "p3_medical_user"
AUTH = {"X-Pick3-Token": TOKEN}
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


class _Resp:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        pass


def _gemini(payload: dict) -> _Resp:
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}]})


SCREEN = {
    "patient": "정치연",
    "records": [
        {"provider": "브리즘 동탄점", "kind": "안경", "amount": "169,000원", "date": "2025.12.16"},
        {"provider": "준약국", "kind": "약국", "amount": 18000, "date": "2025-12-01"},
        {"provider": "연세 휴내과", "amount": "5,500", "date": "2025.10.15"},   # kind missing -> 병원
        {"provider": "세일금", "amount": "", "date": "2025.07.1"},             # cut off -> skipped
    ],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_fake")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    saved_places.clear_user_cache()


def _fake(monkeypatch: pytest.MonkeyPatch, gemini_payload: dict, insert_returns=None, rows=None) -> dict:
    import httpx

    seen: dict = {"gemini": [], "posts": [], "gets": [], "deletes": []}
    user_hash = saved_places.hash_token(TOKEN)

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            pass

        async def post(self, url, **kw):
            if "generativelanguage" in url:
                seen["gemini"].append(kw["json"])
                if isinstance(gemini_payload, list):  # successive calls (classify, then extract)
                    return _gemini(gemini_payload[len(seen["gemini"]) - 1])
                return _gemini(gemini_payload)
            if url.endswith("/rpc/pick3_bump_usage"):
                return _Resp(200, {"user_count": 1, "total_count": 1})
            seen["posts"].append((url, kw))
            body = kw["json"]
            return _Resp(201, body if insert_returns is None else insert_returns(body))

        async def get(self, url, **kw):
            if url.endswith("/pick3_users"):
                ok = kw["params"]["token_hash"] == f"eq.{user_hash}"
                return _Resp(200, [{"id": 7, "name": "가족", "daily_limit": None, "disabled": False}] if ok else [])
            seen["gets"].append((url, kw))
            return _Resp(200, rows or [])

        async def delete(self, url, **kw):
            seen["deletes"].append((url, kw))
            return _Resp(200, [{"id": 1}])

        async def request(self, method, url, **kw):
            return await getattr(self, method.lower())(url, **kw)

        async def patch(self, url, **kw):
            seen.setdefault("patches", []).append((url, kw))
            if kw["json"].get("amount") == 999:
                return _Resp(409, {"code": "23505", "message": "duplicate key"})
            return _Resp(200, [{"id": 2, **kw["json"]}])

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return seen


def test_normalize_records_parses_amount_date_kind_and_skips_cut_rows() -> None:
    patient, rows, skipped = medical.normalize_records(SCREEN)
    assert patient == "정치연" and skipped == 1
    assert rows[0] == {"patient": "정치연", "provider": "브리즘 동탄점", "kind": "안경",
                       "amount": 169000, "visit_date": "2025-12-16"}
    assert rows[2]["kind"] == "병원" and rows[2]["amount"] == 5500


def test_sniff_mime_prefers_bytes() -> None:
    assert medical.sniff_mime(b"%PDF-1.7 ...", "application/octet-stream") == "application/pdf"
    assert medical.sniff_mime(PNG, None) == "image/png"


def test_ingest_medical_saves_rows_for_user_and_reports_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Supabase "ignores" the first row as an existing duplicate.
    seen = _fake(monkeypatch, SCREEN, insert_returns=lambda body: body[1:])
    res = client.post("/api/ingest/medical", files={"image": ("s.png", PNG, "image/png")}, headers=AUTH).json()
    assert res["status"] == "saved"
    assert res["inserted"] == 2 and res["duplicates"] == 1 and res["skipped"] == 1
    assert res["message"].startswith("✅ 정치연: 2건 저장 (이미 있던 1건 제외)")
    assert "192,500원" in res["message"]  # 169000 + 18000 + 5500

    url, kw = seen["posts"][0]
    assert url.endswith("/pick3_medical_records")
    assert kw["params"] == {"on_conflict": "user_id,patient,provider,visit_date,amount"}
    assert all(r["user_id"] == 7 and r["source"] == "image" for r in kw["json"])
    assert seen["gemini"][0]["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"


def test_ingest_medical_pdf_and_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, {"patient": "", "records": []})
    res = client.post("/api/ingest/medical", files={"image": ("a.pdf", b"%PDF-1.4 x", "application/octet-stream")},
                      headers=AUTH).json()
    assert res["status"] == "not_found"
    assert seen["gemini"][0]["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "application/pdf"
    assert seen["posts"] == []


def test_medical_endpoints_require_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN)
    files = {"image": ("s.png", PNG, "image/png")}
    assert client.post("/api/ingest/medical", files=files).status_code == 401
    assert client.post("/api/ingest/medical", files=files, headers={"X-Pick3-Token": "x"}).status_code == 401
    assert client.get("/api/medical").status_code == 401
    assert client.delete("/api/medical/1").status_code == 401
    assert seen["gemini"] == [] and seen["posts"] == [] and seen["deletes"] == []


def test_list_and_delete_are_scoped_to_user(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"id": 2, "patient": "정치연", "provider": "준약국", "kind": "약국", "amount": 18000, "visit_date": "2025-12-01"},
        {"id": 1, "patient": "정치연", "provider": "연세 휴내과", "kind": "병원", "amount": 5500, "visit_date": "2025-10-15"},
    ]
    seen = _fake(monkeypatch, SCREEN, rows=rows)
    body = client.get("/api/medical", params={"patient": "정치연", "year": 2025}, headers=AUTH).json()
    assert body["count"] == 2
    params = seen["gets"][-1][1]["params"]
    assert params["user_id"] == "eq.7" and params["patient"] == "eq.정치연"
    assert params["and"] == "(visit_date.gte.2025-01-01,visit_date.lte.2025-12-31)"
    assert {(t["kind"], t["amount"]) for t in body["totals"]} == {("약국", 18000), ("병원", 5500)}

    assert client.delete("/api/medical/2", headers=AUTH).json() == {"deleted": 2}
    assert seen["deletes"][0][1]["params"] == {"id": "eq.2", "user_id": "eq.7"}


def test_edit_medical_record_validates_and_scopes_to_user(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN)
    res = client.patch("/api/medical/2", json={"kind": "안경", "amount": "169,000", "visit_date": "2025.12.16"},
                       headers=AUTH)
    assert res.status_code == 200
    url, kw = seen["patches"][0]
    assert kw["params"] == {"id": "eq.2", "user_id": "eq.7"}
    assert kw["json"] == {"kind": "안경", "amount": 169000, "visit_date": "2025-12-16"}

    assert client.patch("/api/medical/2", json={"kind": "한약"}, headers=AUTH).status_code == 422
    assert client.patch("/api/medical/2", json={"provider": "  "}, headers=AUTH).status_code == 422
    assert client.patch("/api/medical/2", json={}, headers=AUTH).status_code == 422
    assert client.patch("/api/medical/2", json={"amount": 999}, headers=AUTH).status_code == 409
    assert client.patch("/api/medical/2", json={"kind": "약국"}).status_code == 401


def test_pages_served_on_api_origin() -> None:
    assert "의료비 기록" in client.get("/medical.html").text
    assert "Pick3 시작하기" in client.get("/start.html").text
    js = client.get("/account.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    for page in ("/", "/pick3.html", "/medical.html", "/prescriptions.html", "/start.html"):
        assert '<script src="account.js"></script>' in client.get(page).text


NAMELESS = {"patient": "", "records": [{"provider": "준약국", "kind": "약국", "amount": 18000, "date": "2025-12-01"}]}


def test_nameless_screen_defaults_to_most_recent_patient(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, NAMELESS, rows=[{"patient": "정지수"}])
    res = client.post("/api/ingest/medical", files={"image": ("s.png", PNG, "image/png")}, headers=AUTH).json()
    assert res["status"] == "saved" and res["patient"] == "정지수" and res["patient_assumed"] is True
    assert "최근 고객·환자 '정지수'" in res["message"]
    assert seen["posts"][0][1]["json"][0]["patient"] == "정지수"
    recent_q = seen["gets"][0][1]["params"]
    assert recent_q["user_id"] == "eq.7" and recent_q["patient"] == "neq." and recent_q["limit"] == "1"


def test_nameless_screen_without_history_keeps_blank_patient(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, NAMELESS, rows=[])
    res = client.post("/api/ingest/medical", files={"image": ("s.png", PNG, "image/png")}, headers=AUTH).json()
    assert res["patient"] == "" and res["patient_assumed"] is False
    assert "이름을 찾지 못했습니다" in res["message"]
    assert seen["posts"][0][1]["json"][0]["patient"] == ""


def test_bulk_patient_change(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN)
    res = client.post("/api/medical/patient", json={"ids": [3, 1, 3], "patient": " 정지수 "}, headers=AUTH)
    assert res.status_code == 200
    _, kw = seen["patches"][0]
    assert kw["params"] == {"id": "in.(1,3)", "user_id": "eq.7"}
    assert kw["json"] == {"patient": "정지수"}
    assert client.post("/api/medical/patient", json={"ids": [], "patient": "x"}, headers=AUTH).status_code == 422
    assert client.post("/api/medical/patient", json={"ids": [1], "patient": "x"}).status_code == 401


def test_customer_name_form_field_overrides_screen_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    # Insurance planner flow: own token, customer named explicitly.
    seen = _fake(monkeypatch, SCREEN)
    res = client.post("/api/ingest/medical", files={"image": ("s.png", PNG, "image/png")},
                      data={"patient": " 김고객 "}, headers=AUTH).json()
    assert res["status"] == "saved" and res["patient"] == "김고객" and res["patient_assumed"] is False
    assert res["patient_on_screen"] == "정치연"
    assert "화면의 이름은 '정치연'" in res["message"]
    assert {r["patient"] for r in seen["posts"][0][1]["json"]} == {"김고객"}
    assert all(r["user_id"] == 7 for r in seen["posts"][0][1]["json"])
    assert seen["gets"] == []  # explicit name: no 'recent patient' lookup


def test_customer_name_on_nameless_screen_has_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, NAMELESS, rows=[{"patient": "다른고객"}])
    res = client.post("/api/ingest/medical", files={"image": ("s.png", PNG, "image/png")},
                      data={"patient": "김고객"}, headers=AUTH).json()
    assert res["patient"] == "김고객" and "⚠" not in res["message"]


def test_delete_all_records_of_one_customer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN)
    res = client.delete("/api/medical", params={"patient": "김고객"}, headers=AUTH)
    assert res.status_code == 200 and res.json() == {"patient": "김고객", "deleted": 1}
    assert seen["deletes"][0][1]["params"]["user_id"] == "eq.7"
    assert seen["deletes"][0][1]["params"]["patient"] == "eq.김고객"
    assert client.delete("/api/medical", params={"patient": " "}, headers=AUTH).status_code == 422
    assert client.delete("/api/medical", params={"patient": "김고객"}).status_code == 401


def test_pick3_shortcut_routes_medical_screens_without_second_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # The existing "Pick3 저장" Shortcut posts to /api/ingest/screenshot; a medical screen lands in medical records.
    monkeypatch.delenv("PICK3_AUTO_ROUTE", raising=False)
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    seen = _fake(monkeypatch, [{"type": "medical"}, SCREEN])
    res = client.post("/api/ingest/screenshot", files={"image": ("s.jpg", b"\xff\xd8\xff" + b"0" * 20, "image/jpeg")},
                      headers=AUTH).json()
    assert res["service"] == "medical" and res["status"] == "saved"
    assert res["message"].startswith("🧾 의료비 기록\n✅ 정치연: 3건 저장")
    assert seen["posts"][0][0].endswith("/pick3_medical_records")
    assert len(seen["gemini"]) == 2


def test_pick3_shortcut_still_saves_restaurants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    seen = _fake(monkeypatch, [{"type": "restaurant"}, {"name": "몽탄", "area": "성수"}],
                 insert_returns=lambda body: [{**body, "id": 1}])
    res = client.post("/api/ingest/screenshot", files={"image": ("s.jpg", b"\xff\xd8\xff" + b"0" * 20, "image/jpeg")},
                      headers=AUTH).json()
    assert res["service"] == "restaurant" and res["message"].startswith("🍽️ 맛집\n✅ 저장 완료: 몽탄")
    assert seen["posts"][0][0].endswith("/pick3_saved_places")


def test_auto_route_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PICK3_AUTO_ROUTE", "0")
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    seen = _fake(monkeypatch, [{"name": "몽탄", "area": "성수"}], insert_returns=lambda body: [{**body, "id": 1}])
    res = client.post("/api/ingest/screenshot", files={"image": ("s.jpg", b"\xff\xd8\xff" + b"0" * 20, "image/jpeg")},
                      headers=AUTH).json()
    assert res["service"] == "restaurant" and len(seen["gemini"]) == 1  # no classification call


def test_shortcut_upload_with_bad_token_gets_a_readable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, SCREEN)
    res = client.post("/api/ingest/screenshot", files={"image": ("s.png", PNG, "image/png")},
                      headers={"X-Pick3-Token": "pick3_token.txt"})
    body = res.json()
    assert res.status_code == 401 and body["detail"] == "Invalid or missing X-Pick3-Token"
    assert "내 계정 연결" in body["message"] and "15자, 'pick…'로 시작" in body["message"]
    assert "token.txt" not in body["message"]  # never echo the token itself


def test_upload_without_file_field_explains_in_message() -> None:
    res = client.post("/api/ingest/screenshot", data={"image": "not a file"}, headers=AUTH)
    body = res.json()
    assert res.status_code == 422 and "파일(image)" in body["message"] and body["detail"]
    plain = client.post("/api/recommend", json={})  # other endpoints keep FastAPI's default shape
    assert "message" not in plain.json()


def test_shared_link_gets_guidance_without_gemini_or_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, SCREEN)
    res = client.post("/api/ingest/screenshot", headers=AUTH,
                      files={"image": ("text.txt", b"https://example.com/result?t=abc", "text/plain")}).json()
    assert res["status"] == "unsupported" and "전체 페이지" in res["message"]
    assert seen["gemini"] == [] and not any("bump_usage" in u for u, _ in seen["posts"])
