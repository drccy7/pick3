"""Local runner:

  python -m coverage_review.cli --pdf 보고서.pdf --password 19xx0101 --user-id 2 --out OUTDIR

Intermediate JSON (extract / analysis) is cached in OUTDIR/_work so a re-run skips Gemini;
--refresh extract|analysis|all redoes a step. Records come from Supabase (report name + user) or --records-json.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import date

from . import pdf_io, service
from .facts import parse_date


def _load_env() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if root.exists():
        for line in root.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--password", default="")
    ap.add_argument("--patient", help="병의원 기록의 환자 이름(기본: 보고서 고객 이름)")
    ap.add_argument("--user-id", type=int)
    ap.add_argument("--records-json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--today")
    ap.add_argument("--refresh", choices=["extract", "analysis", "all"])
    ap.add_argument("--no-pdf", action="store_true", help="HTML만 만들기(PDF 인쇄 생략)")
    args = ap.parse_args(argv)
    _load_env()
    sys.stdout.reconfigure(encoding="utf-8")

    out = pathlib.Path(args.out)
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    refresh = args.refresh or ""

    def cached(name: str, step: str):
        p = work / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() and refresh not in (step, "all") else None

    rep = cached("extract.json", "extract")
    analysis = cached("analysis.json", "analysis") if rep is not None else None
    raw = pdf_io.decrypt(pathlib.Path(args.pdf).read_bytes(), args.password)
    records = json.loads(pathlib.Path(args.records_json).read_text(encoding="utf-8")) if args.records_json else None
    res = service.run(raw, user_id=args.user_id, today=parse_date(args.today) if args.today else date.today(),
                      patient=args.patient, records=records, rep=rep, analysis=analysis,
                      progress=lambda p: print(f"· {p['label']}" + (f" {p['done']}/{p.get('total') or '?'}" if p.get("done") is not None else ""), flush=True))
    for name, key in (("extract.json", "extract"), ("facts.json", "facts"), ("analysis.json", "analysis"),
                      ("plan.json", "plan")):
        (work / name).write_text(json.dumps(res[key], ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for fn, h in res["html"].items():
        (out / fn).write_text(h, encoding="utf-8")
        print("  →", out / fn)
        if not args.no_pdf:
            pdf = out / (fn[:-5] + ".pdf")
            pdf.write_bytes(service.html_to_pdf(h))
            print("  →", pdf)
    for w in res["warnings"]:
        print("  ! ", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
