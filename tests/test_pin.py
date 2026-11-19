"""Name + PIN web sign-in: PIN rules, hashing, login -> session token, lockout, session auth."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PICK3_DIR = Path(__file__).resolve().parent.parent
if str(PICK3_DIR) not in sys.path:
    sys.path.insert(0, str(PICK3_DIR))

import auth_pin  # noqa: E402
import saved_places  # noqa: E402
from app import app  # noqa: E402

client = TestClient(app)
TOKEN = "p3_pin_user"


class _Resp:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self):
        return self._payload


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A tiny fake of the two Supabase tables involved."""
    import httpx

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_fake")
    saved_places.clear_user_cache()
    auth_pin._IP_FAILS.clear()
    state = {
        "user": {"id": 7, "name": "홍길동", "daily_limit": None, "disabled": False,
                 "token_hash": saved_places.hash_token(TOKEN),
                 "pin_hash": None, "pin_failed": 0, "pin_locked_until": None},
        "sessions": {},
    }

    def public(u: dict) -> dict:
        return {k: u[k] for k in ("id", "name", "daily_limit", "disabled")}

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            pass

        async def get(self, url, params=None, **kw):
            return await self.request("GET", url, params=params, **kw)

        async def request(self, method, url, params=None, json=None, **kw):
            u = state["user"]
            params = params or {}
            if url.endswith("/pick3_users"):
                if method == "GET":
                    if "token_hash" in params:
                        return _Resp(200, [public(u)] if params["token_hash"] == f"eq.{u['token_hash']}" else [])
                    if "name" in params:
                        return _Resp(200, [dict(u)] if params["name"] == f"eq.{u['name']}" else [])
                    return _Resp(200, [dict(u)])
                if method == "PATCH":
                    u.update(json)
                    return _Resp(200, [dict(u)])
            if url.endswith("/pick3_sessions"):
                if method == "POST":
                    state["sessions"][json["token_hash"]] = json["user_id"]
                    return _Resp(201, None)
                if method == "GET":
                    h = params["token_hash"].removeprefix("eq.")
                    return _Resp(200, [{"user": public(u)}] if h in state["sessions"] else [])
                if method == "DELETE":
                    state["sessions"].pop(params["token_hash"].removeprefix("eq."), None)
                    return _Resp(200, [])
            raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return state


def test_pin_rules_and_hash_roundtrip() -> None:
    for bad in ("12345", "abcdef", "111111", "123456", "654321"):
        with pytest.raises(auth_pin.PinError):
            auth_pin.validate_pin(bad)
    stored = auth_pin.hash_pin("482913")
    assert "482913" not in stored
    assert auth_pin.verify_pin("482913", stored) and not auth_pin.verify_pin("482914", stored)
    assert not auth_pin.verify_pin("482913", None)


def test_set_pin_then_login_gives_session_that_works_and_logs_out(db: dict) -> None:
    assert client.get("/api/me", headers={"X-Pick3-Token": TOKEN}).json()["has_pin"] is False
    r = client.post("/api/pin", json={"pin": "123456"}, headers={"X-Pick3-Token": TOKEN})
    assert r.status_code == 422  # sequential PIN refused
    assert client.post("/api/pin", json={"pin": "482913"}, headers={"X-Pick3-Token": TOKEN}).status_code == 200
    assert db["user"]["pin_hash"].startswith("pbkdf2$")

    res = client.post("/api/login", json={"name": "홍길동", "pin": "482913"})
    assert res.status_code == 200
    session = res.json()["token"]
    assert session.startswith("p3s_") and res.json()["name"] == "홍길동"
    me = client.get("/api/me", headers={"X-Pick3-Token": session}).json()
    assert me["name"] == "홍길동" and me["has_pin"] is True

    client.post("/api/logout", headers={"X-Pick3-Token": session})
    assert db["sessions"] == {}
    assert client.get("/api/me", headers={"X-Pick3-Token": session}).status_code == 401


def test_wrong_pin_and_unknown_name_look_the_same_and_lock_after_five(db: dict) -> None:
    client.post("/api/pin", json={"pin": "482913"}, headers={"X-Pick3-Token": TOKEN})
    unknown = client.post("/api/login", json={"name": "없는사람", "pin": "482913"})
    wrong = client.post("/api/login", json={"name": "홍길동", "pin": "000001"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]

    for _ in range(4):
        client.post("/api/login", json={"name": "홍길동", "pin": "000001"})
    assert db["user"]["pin_locked_until"]
    locked = client.post("/api/login", json={"name": "홍길동", "pin": "482913"})  # right PIN, still locked
    assert locked.status_code == 401 and "잠겼" in locked.json()["detail"]


def test_personal_token_unaffected_and_no_pin_means_no_login(db: dict) -> None:
    assert client.post("/api/login", json={"name": "홍길동", "pin": "482913"}).status_code == 401
    assert client.get("/api/me", headers={"X-Pick3-Token": TOKEN}).status_code == 200
