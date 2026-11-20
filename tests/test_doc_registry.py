"""Document classification registry: tiers, order, placeholders, containers. Fictitious data only."""

from __future__ import annotations

import dataclasses
import io

import pdfplumber
import pytest

import doc_registry
import health_checkup
import mhr
import nhis_claims
import prescriptions
from coverage_review import pdf_io, service

rl = pytest.importorskip("reportlab")
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from test_mhr import build as build_mhr  # noqa: E402

FONT = "HYSMyeongJo-Medium"
pdfmetrics.registerFont(UnicodeCIDFont(FONT))
H = 842

CHECKUP_WORDS = ["일반건강검진 판정 결과", "종합판정 정상B", "신장 170 체중 70 허리둘레 80",
                 "혈압 120/80 공복혈당 90 총콜레스테롤 180", "AST 20 ALT 20"]
COVERAGE_WORDS = ["보유계약 현황", "계약자 가나다 피보험자 가나다", "가입금액 1,000만원 보험료 30,000원",
                  "보험기간 100세 만기 납입기간 20년"]


def pdf(lines: list, pages: int = 1) -> bytes:
    """lines: text, or (text, font size). Every page gets the same lines."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, H))
    for _ in range(pages):
        y = H - 50
        for ln in lines:
            text, size = (ln, 9) if isinstance(ln, str) else ln
            c.setFont(FONT, size)
            c.drawString(40, y, text)
            y -= size + 8
        c.showPage()
    c.save()
    return buf.getvalue()


def _old_guess(raw: bytes) -> str:
    """app._guess_kind as of today (mhr → coverage → nhis/rx → checkup)."""
    if mhr.is_mhr_pdf(raw):
        return "mhr"
    if service.is_coverage_report(raw):
        return "coverage"
    if nhis_claims.is_nhis_pdf(raw) or prescriptions.is_prescription_pdf(raw):
        return "medical"
    return "checkup" if health_checkup.is_checkup_pdf(raw) else ""


def test_title_line_is_high() -> None:
    r = doc_registry.classify(pdf([("건강보험 요양급여내역", 13), "발급기간 2026.01.01~2026.06.30"]))
    assert (r["type"], r["box"], r["confidence"], r["via"]) == ("nhis", "medical", "high", "title")
    r = doc_registry.classify(pdf(["2026-11-01 10:00", ("처방조제정보", 15), "순번 진료시작일"]))
    assert (r["type"], r["box"], r["confidence"]) == ("prescription", "medical", "high")


def test_large_font_title_below_the_first_lines() -> None:
    small = [f"안내 {i}" for i in range(7)]
    assert doc_registry.classify(pdf(small + [("처방조제정보", 15)]))["confidence"] == "high"
    assert doc_registry.classify(pdf(small + [("처방조제정보", 9)]))["type"] == ""  # small text deep down: no title


def test_title_must_be_the_whole_line() -> None:
    r = doc_registry.classify(pdf(["처방조제정보 발급 방법 안내문", "가나다"]))
    assert r["type"] == "" and r["confidence"] == "none"


def test_header_set_is_medium() -> None:
    r = doc_registry.classify(pdf(["가나다 발급", "진료개시일 입내원일수 요양기관명 요양기관연락처 공단부담금"]))
    assert (r["type"], r["confidence"], r["via"]) == ("nhis", "medium", "header")
    r = doc_registry.classify(pdf(["순번 진료시작일 병·의원&약국 약품명 성분명"]))
    assert (r["type"], r["confidence"]) == ("prescription", "medium")


def test_vocabulary_is_low() -> None:
    r = doc_registry.classify(pdf(CHECKUP_WORDS))
    assert (r["type"], r["box"], r["confidence"], r["via"]) == ("checkup", "checkup", "low", "words")
    r = doc_registry.classify(pdf(COVERAGE_WORDS))
    assert (r["type"], r["box"], r["confidence"]) == ("coverage", "coverage", "low")


def test_title_wins_over_vocabulary_of_an_earlier_entry() -> None:
    # checkup comes before coverage in the registry, but a coverage title beats checkup words
    r = doc_registry.classify(pdf([("보장분석 보고서", 16)] + CHECKUP_WORDS))
    assert (r["type"], r["confidence"]) == ("coverage", "high") and r["also"] == ["checkup"]


def test_policy_placeholder_beats_coverage_words_once_enabled() -> None:
    raw = pdf([("보험가입증권", 16)] + COVERAGE_WORDS)
    assert doc_registry.classify(raw)["type"] == "coverage"  # disabled today: vocabulary decides
    reg = [dataclasses.replace(d, enabled=True) if d.type == "policy" else d for d in doc_registry.REGISTRY]
    with pdfplumber.open(io.BytesIO(raw)) as p:
        _, out = doc_registry.decide(doc_registry._facts(p), reg)
    assert (out["type"], out["confidence"], out["also"]) == ("policy", "high", ["coverage"])


def test_registry_order_and_placeholders() -> None:
    types = [d.type for d in doc_registry.REGISTRY]
    assert types[0] == "mhr"  # container before the kinds it contains
    assert types.index("policy") < types.index("coverage")
    assert {d.type for d in doc_registry.REGISTRY if not d.enabled} == {"policy", "receipt", "bill_detail"}
    assert doc_registry.classify(pdf([("진료비 계산서·영수증", 16), "가나다"]))["type"] == ""


def test_container_first_with_sections() -> None:
    raw = build_mhr(["visits", "meds", "checkup"])
    r = doc_registry.classify(raw)
    assert (r["type"], r["box"], r["confidence"]) == ("mhr", "mhr", "high")
    assert [(s["section"], s["pages"]) for s in r["sections"]] == [("visits", [1]), ("meds", [2]), ("checkup", [3, 4])]


def test_container_wins_over_checkup_words_inside() -> None:
    raw = pdf([("나의건강기록", 20), "성명 홍길동", ("건강검진(일반)", 12)] + CHECKUP_WORDS)
    r = doc_registry.classify(raw)
    assert (r["type"], r["also"]) == ("mhr", ["checkup"])
    assert r["sections"] and r["sections"][0]["section"] == "checkup"


def test_not_pdf_encrypted_and_empty() -> None:
    assert doc_registry.classify(b"\xff\xd8\xff\xe0 jpeg")["via"] == "not_pdf"
    locked = pdf_io.encrypt(pdf([("처방조제정보", 15)]), "1234")
    assert doc_registry.classify(locked)["confidence"] == "none"
    assert doc_registry.classify(pdf_io.decrypt(locked, "1234"))["type"] == "prescription"
    assert doc_registry.classify(pdf(["가나다 라마바"]))["confidence"] == "none"


@pytest.mark.parametrize("raw", [
    pdf([("건강보험 요양급여내역", 13)]), pdf(["2026-11-01", ("처방조제정보", 15)]), pdf(CHECKUP_WORDS),
    pdf(COVERAGE_WORDS), build_mhr(["visits", "checkup"]), pdf(["가나다"]),
])
def test_agrees_with_todays_detectors_on_clear_documents(raw: bytes) -> None:
    assert doc_registry.classify(raw)["box"] == _old_guess(raw)
