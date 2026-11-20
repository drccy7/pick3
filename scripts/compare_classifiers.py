"""Compare the rule registry (doc_registry) with the Gemini page classifier (doc_ai) over a folder of PDFs.

    python scripts/compare_classifiers.py <folder> [--password PW] [--skip TEXT ...] [--rules-only] [--json OUT]

One Gemini call per readable PDF (masked page text only, see doc_ai). Encrypted PDFs are opened with
--password; a PDF the password does not open is skipped without any call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_env(path: Path) -> None:  # same as app._load_local_env, without importing the app
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")

import doc_ai  # noqa: E402
import doc_registry  # noqa: E402
from coverage_review import pdf_io  # noqa: E402


def _pages(p: list[int]) -> str:
    return f"{p[0]}-{p[-1]}" if len(p) > 1 else str(p[0]) if p else "-"


def _sections(res: dict) -> str:
    return ",".join(f"{s['section']}:{_pages(s['pages'])}" for s in res.get("sections") or []) or "-"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--password", default="")
    ap.add_argument("--skip", nargs="*", default=[], help="skip files whose name contains any of these")
    ap.add_argument("--rules-only", action="store_true", help="no Gemini call")
    ap.add_argument("--json", help="write the full results here")
    a = ap.parse_args()
    files = sorted(p for p in Path(a.folder).iterdir() if p.suffix.lower() == ".pdf")
    rows, total_usd = [], 0.0
    for n, path in enumerate(files, 1):
        if any(s in path.name for s in a.skip):
            print(f"[{n}] {path.name}: skipped")
            continue
        raw = path.read_bytes()
        try:
            raw = pdf_io.decrypt(raw, a.password)
        except Exception:
            print(f"[{n}] {path.name}: password does not open it, skipped")
            continue
        out = ({"rule": doc_registry.classify(raw), "ai": None, "agreement": None} if a.rules_only
               else doc_ai.compare(raw))
        rule, ai, ag = out["rule"], out["ai"], out["agreement"]
        print(f"[{n}] {path.name}")
        print(f"    rule: {rule['type'] or '-'} / {rule['box'] or '-'} / {rule['confidence']} via {rule['via'] or '-'}"
              f" / sections {_sections(rule)}")
        if ai:
            u = ai.get("usage") or {}
            usd = u.get("usd")
            total_usd += usd or 0
            print(f"    ai:   [{ai['status']}] {ai['type'] or '-'} / {ai['box'] or '-'} / {ai['confidence']}"
                  f" / sections {_sections(ai)} / pages {ai['sent_pages']}/{ai['pages']}"
                  f" / image pages {ai['needs_image'] or '-'} / uncovered {ai['uncovered'] or '-'}")
            for s in ai["segments"]:
                print(f"          {s['type']:<14} p{s['page_from']}-{s['page_to']} conf {s['confidence']}")
            print(f"    agreement: {ag['status']} (type {ag['same_type']}, box {ag['same_box']},"
                  f" sections {ag['same_sections']}, pages {ag['same_pages']})")
            print(f"    usage: {u.get('model', '-')} in {u.get('prompt_tokens', '-')} out {u.get('output_tokens', '-')}"
                  f" think {u.get('thoughts_tokens', '-')} = {f'${usd:.5f}' if usd is not None else 'n/a'}")
            if ai.get("error"):
                print(f"    error: {ai['error']}")
        rows.append({"file": path.name, **out})
    if not a.rules_only:
        print(f"total ${total_usd:.5f} over {sum(1 for r in rows if r['ai'])} calls")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
