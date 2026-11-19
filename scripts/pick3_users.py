"""Manage Pick3 users (per-user X-Pick3-Token). Uses SUPABASE_URL/SUPABASE_KEY from .env.

  python scripts/pick3_users.py add 홍길동 [--limit 30]     # prints the new token ONCE
  python scripts/pick3_users.py add 나 --token-env PICK3_INGEST_TOKEN   # register an existing token
  python scripts/pick3_users.py list
  python scripts/pick3_users.py rotate <id>                 # new token, old one stops working
  python scripts/pick3_users.py disable <id> | enable <id>
  python scripts/pick3_users.py claim-orphans <id>          # give saved rows without user_id to <id>
  python scripts/pick3_users.py reset-pin <id>              # forget the PIN + unlock (user makes a new one)
  python scripts/pick3_users.py logout-all <id>             # end every name+PIN web session of <id>

Only a SHA-256 hash of each token is stored, so a lost token cannot be shown again: rotate it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import _load_local_env  # noqa: E402  (also loads .env on import)
from saved_places import SUPABASE_TABLE, USERS_TABLE, _supabase_headers, hash_token, new_token  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _sb() -> tuple[str, dict]:
    _load_local_env(ROOT / ".env")
    sb = _supabase_headers()
    if not sb:
        sys.exit("SUPABASE_URL / SUPABASE_KEY 가 .env 에 없습니다.")
    base, headers = sb
    return f"{base}/rest/v1", {**headers, "Prefer": "return=representation"}


def _check(resp: httpx.Response) -> list:
    if resp.status_code >= 300:
        sys.exit(f"Supabase HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.text else []


START_URL = os.getenv("PICK3_START_URL", "https://drccy7.github.io/pick3/start.html")


def _print_token(name: str, token: str) -> None:
    print(f"\n{name} 님의 토큰 (지금 한 번만 표시됩니다):\n\n  {token}\n")
    print("보낼 메시지 (개인 시작 링크 - 열고 버튼 2개):\n")
    print(f"  Pick3 인스타 맛집 저장 - 아래 링크를 열고 버튼 두 개만 누르세요\n  {START_URL}#t={token}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--limit", type=int, default=None, help="하루 저장 한도 (기본: 서버 설정)")
    a.add_argument("--token-env", default=None, help="새로 만들지 않고 이 환경변수의 기존 토큰을 등록")
    sub.add_parser("list")
    for cmd in ("rotate", "disable", "enable", "claim-orphans", "reset-pin", "logout-all"):
        sub.add_parser(cmd).add_argument("id", type=int)
    args = ap.parse_args()

    rest, headers = _sb()
    users = f"{rest}/{USERS_TABLE}"
    with httpx.Client(timeout=10.0) as c:
        if args.cmd == "add":
            token = os.getenv(args.token_env, "").strip() if args.token_env else new_token()
            if not token:
                sys.exit(f"환경변수 {args.token_env} 가 비어 있습니다.")
            row = _check(c.post(users, headers=headers, json={
                "name": args.name, "token_hash": hash_token(token), "daily_limit": args.limit}))[0]
            print(f"등록됨: id={row['id']} name={row['name']}")
            if not args.token_env:
                _print_token(row["name"], token)
        elif args.cmd == "list":
            rows = _check(c.get(users, headers=headers, params={
                "select": "id,name,daily_limit,disabled,created_at,pin_hash", "order": "id"}))
            for r in rows:
                state = "사용 중지" if r["disabled"] else "사용 중"
                pin = "PIN 있음" if r.get("pin_hash") else "PIN 없음"
                print(f"{r['id']:>3}  {r['name']:<12} 한도={r['daily_limit'] or '기본'}  {state}  {pin}  {r['created_at'][:10]}")
        elif args.cmd == "rotate":
            token = new_token()
            rows = _check(c.patch(users, headers=headers, params={"id": f"eq.{args.id}"},
                                  json={"token_hash": hash_token(token)}))
            if not rows:
                sys.exit(f"id={args.id} 사용자가 없습니다.")
            _check(c.delete(f"{rest}/pick3_sessions", headers=headers, params={"user_id": f"eq.{args.id}"}))
            _print_token(rows[0]["name"], token)
        elif args.cmd == "reset-pin":
            rows = _check(c.patch(users, headers=headers, params={"id": f"eq.{args.id}"},
                                  json={"pin_hash": None, "pin_failed": 0, "pin_locked_until": None}))
            print(f"{rows[0]['name']}: PIN 초기화됨 (개인 링크로 들어가 홈에서 새로 만드세요)" if rows else "없음")
        elif args.cmd == "logout-all":
            rows = _check(c.delete(f"{rest}/pick3_sessions", headers=headers, params={"user_id": f"eq.{args.id}"}))
            print(f"웹 로그인 {len(rows)}건을 끝냈습니다.")
        elif args.cmd in ("disable", "enable"):
            rows = _check(c.patch(users, headers=headers, params={"id": f"eq.{args.id}"},
                                  json={"disabled": args.cmd == "disable"}))
            print(f"{rows[0]['name']}: {'사용 중지' if args.cmd == 'disable' else '사용 재개'}" if rows else "없음")
        elif args.cmd == "claim-orphans":
            rows = _check(c.patch(f"{rest}/{SUPABASE_TABLE}", headers=headers,
                                  params={"user_id": "is.null"}, json={"user_id": args.id}))
            print(f"{len(rows)}건을 id={args.id} 사용자에게 옮겼습니다.")


if __name__ == "__main__":
    main()
