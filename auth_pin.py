"""Name + PIN sign-in for the web pages (an alternative to opening a personal link).

A user who is already signed in (personal token) sets a 6-digit PIN. On another browser they
sign in with their name + PIN and get a *session token* (p3s_...) that works like their personal
token. Personal tokens (used by the Shortcuts) are untouched.

Brute force guard: 5 wrong PINs lock that user's PIN sign-in for 15 minutes, plus a per-IP cap.
PINs are stored as salted PBKDF2-SHA256 hashes, never in plain text.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx

from saved_places import USERS_TABLE, StorageError, _supabase_headers, clear_user_cache, hash_token

SESSIONS_TABLE = "pick3_sessions"
SESSION_PREFIX = "p3s_"
MAX_FAILS = 5
LOCK_MINUTES = 15
IP_FAILS_PER_HOUR = 20
_ITERATIONS = 200_000

_IP_FAILS: dict[str, list[float]] = {}


class PinError(ValueError):
    """User-facing PIN problem (bad format, wrong PIN, locked)."""


# ---- hashing -----------------------------------------------------------------
def validate_pin(pin: str) -> str:
    pin = (pin or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        raise PinError("PIN은 숫자 6자리입니다.")
    if len(set(pin)) == 1 or pin in "0123456789" or pin in "9876543210":
        raise PinError("같은 숫자나 연속된 숫자(예: 111111, 123456)는 쓸 수 없습니다.")
    return pin


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str | None) -> bool:
    try:
        algo, iters, salt, digest = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iters))
        return secrets.compare_digest(calc.hex(), digest)
    except ValueError:
        return False


def new_session_token() -> str:
    return SESSION_PREFIX + secrets.token_urlsafe(24)


# ---- per-IP guard (in memory; resets on restart) -------------------------------
def ip_blocked(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _IP_FAILS.get(ip, []) if now - t < 3600]
    _IP_FAILS[ip] = hits
    return len(hits) >= IP_FAILS_PER_HOUR


def _ip_fail(ip: str) -> None:
    _IP_FAILS.setdefault(ip, []).append(time.monotonic())


# ---- Supabase ----------------------------------------------------------------
def _sb() -> tuple[str, dict]:
    sb = _supabase_headers()
    if not sb:
        raise StorageError("PIN 로그인은 Supabase 사용 시에만 됩니다.")
    return sb


async def _call(method: str, table: str, params: dict | None = None, json=None, prefer: str | None = None):
    base, headers = _sb()
    if prefer:
        headers = {**headers, "Prefer": prefer}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.request(method, f"{base}/rest/v1/{table}", headers=headers, params=params, json=json)
    except Exception as exc:
        raise StorageError(f"Supabase 연결 실패: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 300:
        raise StorageError(f"Supabase {table} HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.text else []


async def set_pin(user_id: int, pin: str) -> None:
    pin = validate_pin(pin)
    await _call("PATCH", USERS_TABLE, params={"id": f"eq.{user_id}"},
                json={"pin_hash": hash_pin(pin), "pin_failed": 0, "pin_locked_until": None})


async def has_pin(user_id: int) -> bool:
    rows = await _call("GET", USERS_TABLE, params={"select": "pin_hash", "id": f"eq.{user_id}", "limit": "1"})
    return bool(rows and rows[0].get("pin_hash"))


_LOGIN_FAIL = "이름 또는 PIN이 맞지 않습니다."


async def login(name: str, pin: str, ip: str) -> dict:
    """Return {token, name} for a correct name + PIN, else raise PinError."""
    name = (name or "").strip()
    pin = (pin or "").strip()
    if ip_blocked(ip):
        raise PinError("시도가 너무 많습니다. 1시간 뒤에 다시 해 주세요.")
    if not name or not re.fullmatch(r"\d{6}", pin):
        raise PinError(_LOGIN_FAIL)
    rows = await _call("GET", USERS_TABLE, params={
        "select": "id,name,disabled,pin_hash,pin_failed,pin_locked_until", "name": f"eq.{name}"})
    now = datetime.now(timezone.utc)
    candidates = [r for r in rows if r.get("pin_hash") and not r.get("disabled")]
    for r in candidates:
        locked = r.get("pin_locked_until")
        if locked and datetime.fromisoformat(locked.replace("Z", "+00:00")) > now:
            raise PinError(f"PIN을 여러 번 틀려 잠겼습니다. {LOCK_MINUTES}분 뒤에 다시 해 주세요.")
    winner = next((r for r in candidates if verify_pin(pin, r["pin_hash"])), None)
    if not winner:
        _ip_fail(ip)
        for r in candidates:
            fails = int(r.get("pin_failed") or 0) + 1
            patch = {"pin_failed": fails}
            if fails >= MAX_FAILS:
                patch = {"pin_failed": 0, "pin_locked_until": (now + timedelta(minutes=LOCK_MINUTES)).isoformat()}
            await _call("PATCH", USERS_TABLE, params={"id": f"eq.{r['id']}"}, json=patch)
        raise PinError(_LOGIN_FAIL)
    if winner.get("pin_failed"):
        await _call("PATCH", USERS_TABLE, params={"id": f"eq.{winner['id']}"}, json={"pin_failed": 0})
    token = new_session_token()
    await _call("POST", SESSIONS_TABLE, json={"token_hash": hash_token(token), "user_id": winner["id"]},
                prefer="return=minimal")
    return {"token": token, "name": winner["name"]}


async def logout(token: str) -> None:
    if token.startswith(SESSION_PREFIX):
        await _call("DELETE", SESSIONS_TABLE, params={"token_hash": f"eq.{hash_token(token)}"})
        clear_user_cache()
