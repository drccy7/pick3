"""Document classification phase 2 (Gemini, shadow mode): masking, schema, segment cleanup, agreement.
Fictitious data only; Gemini is faked."""

from __future__ import annotations

import io
import json

import pytest

import doc_ai
from coverage_review import gemini, pdf_io

pytest.importorskip("reportlab")
from reportlab.pdfgen import canvas  # noqa: E402

from test_doc_registry import pdf as make_pdf  # noqa: E402
from test_mhr import build as build_mhr  # noqa: E402


@pytest.fixture
def fake(monkeypatch):
    """Replace generate_json; .answer is returned, .calls keeps (parts, schema, kw)."""
    class Fake:
        answer: dict = {"segments": []}
        calls: list = []
        usage = {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 50}

        def __call__(self, model, parts, schema, temperature=0.0, feature="", thinking=None):
            self.calls.append((parts, schema, {"model": model, "temperature": temperature, "feature": feature,
                                               "thinking": thinking}))
            gemini._usage(feature, model, {"usageMetadata": self.usage, "modelVersion": "gemini-3.6-flash"})
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    f = Fake()
    f.calls = []
    monkeypatch.setattr(gemini, "USAGE_HOOK", None)
    monkeypatch.setattr(gemini, "generate_json", f)
    return f


def _image_only_pdf(pages: int = 1) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    for _ in range(pages):
        c.rect(50, 50, 400, 600, fill=1)
        c.showPage()
    c.save()
    return buf.getvalue()


# ---- masking -----------------------------------------------------------------------------------
@pytest.mark.parametrize("text, gone", [
    ("주민등록번호 900101-1234567", "1234567"),
    ("주민번호: 900101 - 2******", "900101"),
    ("생년 900101-1", "900101-1"),
    ("연락처 010-1234-5678", "1234-5678"),
    ("전화 02)123-4567", "123-4567"),
    ("휴대폰 01012345678", "01012345678"),
    ("카드 1234-5678-9012-3456", "9012"),
    ("계좌 123-456789-01-234", "456789"),
    ("메일 someone@example.com", "someone"),
    ("성명 : 가나다", "가나다"),
    ("수진자 가나다라 님", "가나다라"),
    ("판정의사명 가나다 면허번호 *****", "가나다"),
    ("주소: 서울시 가나구 다라로 1", "다라로"),
])
def test_mask_removes_personal_data(text, gone):
    assert gone not in doc_ai.mask(text)


@pytest.mark.parametrize("text", [
    "진료개시일 2026-11-20 공단부담금 1,234,567",
    "20261120 처방조제정보",
    "환자부담총액 12,000",
    "계약자 피보험자 보험료",
    "성명 성별 나이",
    "건강보험 요양급여내역",
])
def test_mask_keeps_signals(text):
    assert doc_ai.mask(text) == text


# ---- catalog, prompt, schema -------------------------------------------------------------------
def test_catalog_drives_enum_and_prompt():
    pdf_types = doc_ai.segment_types("pdf")
    assert pdf_types[-1] == "unknown"
    assert "mhr" not in pdf_types  # a container is derived from its parts
    assert {"mhr_checkup", "mhr_visits", "mhr_meds", "nhis", "prescription", "coverage", "receipt"} <= set(pdf_types)
    assert "restaurant" not in pdf_types and "restaurant" in doc_ai.segment_types("image")
    enum = doc_ai.schema()["properties"]["segments"]["items"]["properties"]["type"]["enum"]
    assert enum == pdf_types
    p = doc_ai.prompt()
    assert all(f"- {t}:" in p for t in pdf_types)
    keys = [k.key for k in doc_ai.CATALOG]
    assert len(keys) == len(set(keys))
    assert all(doc_ai.BY_KEY[k.part_of].container for k in doc_ai.CATALOG if k.part_of)
    regs = {d.type for d in doc_ai.doc_registry.REGISTRY}  # every rule type exists in the catalog, same box
    assert regs <= set(keys)
    assert all(doc_ai.BY_KEY[d.type].box == d.box for d in doc_ai.doc_registry.REGISTRY if d.box)


# ---- segment cleanup -----------------------------------------------------------------------------
def test_clean_segments_enforces_enum_bounds_and_order():
    segs = doc_ai.clean_segments([
        {"type": "nhis", "page_from": 3, "page_to": 9, "confidence": 1.7, "evidence": "x" * 200},
        {"type": "invented_kind", "page_from": 0, "page_to": 1, "confidence": "abc"},
        {"type": "nhis", "page_from": "x", "page_to": None},  # no usable page: dropped
        "garbage",
    ], n_pages=5)
    assert [(s["type"], s["page_from"], s["page_to"]) for s in segs] == [("unknown", 1, 1), ("nhis", 3, 5)]
    assert segs[1]["confidence"] == 1.0 and len(segs[1]["evidence"]) == 80 and segs[0]["confidence"] == 0.0


def test_clean_segments_merges_and_trims_overlaps():
    segs = doc_ai.clean_segments([
        {"type": "mhr_checkup", "page_from": 1, "page_to": 2, "confidence": 0.9},
        {"type": "mhr_checkup", "page_from": 3, "page_to": 3, "confidence": 0.7},  # touching: merged
        {"type": "mhr_visits", "page_from": 3, "page_to": 5, "confidence": 0.9},  # shares the boundary page
        {"type": "nhis", "page_from": 4, "page_to": 4, "confidence": 0.9},  # inside the previous one: dropped
        {"type": "mhr_meds", "page_from": 4, "page_to": 5, "confidence": 0.9},  # overlap: trimmed to 5
    ], n_pages=6)
    assert [(s["type"], s["page_from"], s["page_to"]) for s in segs] == [
        ("mhr_checkup", 1, 3), ("mhr_visits", 3, 5), ("mhr_meds", 5, 5)]
    assert segs[0]["confidence"] == 0.7
    assert all(a["page_to"] <= b["page_from"] for a, b in zip(segs, segs[1:]))


def test_summarize_container_and_majority():
    segs = doc_ai.clean_segments([
        {"type": "mhr_visits", "page_from": 1, "page_to": 2, "confidence": 0.8, "start_hint": "진료이력"},
        {"type": "mhr_meds", "page_from": 3, "page_to": 3, "confidence": 0.95},
    ], 4)
    s = doc_ai.summarize(segs, 4, images=[])
    assert (s["type"], s["box"], s["confidence"]) == ("mhr", "mhr", 0.95)
    assert [(x["section"], x["pages"]) for x in s["sections"]] == [("visits", [1, 2]), ("meds", [3])]
    assert s["uncovered"] == [4]
    segs = doc_ai.clean_segments([{"type": "receipt", "page_from": 1, "page_to": 1, "confidence": 0.9},
                                  {"type": "bill_detail", "page_from": 2, "page_to": 4, "confidence": 0.8}], 4)
    s = doc_ai.summarize(segs, 4, images=[])
    assert (s["type"], s["box"], s["also"], s["sections"]) == ("bill_detail", "medical", ["receipt"], [])
    assert doc_ai.summarize(doc_ai.clean_segments([{"type": "unknown", "page_from": 1, "page_to": 1}], 1), 1, [])[
        "type"] == ""


# ---- classify_pages ----------------------------------------------------------------------------
def test_classify_pages_sends_masked_capped_text(fake):
    raw = make_pdf([("건강보험 요양급여내역", 16), "성명 : 가나다  주민번호 900101-1234567",
                    "진료개시일 입내원일수 요양기관명 공단부담금"] + ["가나다의원 2026-01-02 1 12,000"] * 60, pages=2)
    fake.answer = {"segments": [{"type": "nhis", "page_from": 1, "page_to": 2, "confidence": 0.95,
                                 "evidence": "건강보험 요양급여내역"}]}
    out = doc_ai.classify_pages(raw, page_chars=300)
    assert (out["status"], out["type"], out["box"], out["pages"], out["sent_pages"]) == ("ok", "nhis", "medical", 2, 2)
    parts, schema, kw = fake.calls[0]
    sent = parts[1]["text"]
    assert "가나다  " not in sent and "1234567" not in sent and "[이름]" in sent
    assert "=== 쪽 2 ===" in sent and len(sent) < 2 * 300 + 100
    assert kw == {"model": doc_ai.model(), "temperature": 0.0, "feature": "doc_ai", "thinking": doc_ai.thinking()}
    assert schema == doc_ai.schema()
    assert out["usage"]["prompt_tokens"] == 1000 and out["usage"]["usd"] == pytest.approx(
        (1000 * 0.75 + 150 * 3.75) / 1e6, abs=1e-6)


def test_classify_pages_chains_the_app_usage_hook(fake, monkeypatch):
    seen = []
    monkeypatch.setattr(gemini, "USAGE_HOOK", lambda f, m, d: seen.append(f))
    doc_ai.classify_pages(make_pdf(["처방조제정보 진료시작일 약품명 성분명 투약일수"]))
    doc_ai.classify_pages(make_pdf(["처방조제정보 진료시작일 약품명 성분명 투약일수"]))
    assert seen == ["doc_ai", "doc_ai"]  # wrapped once, the app's recorder still sees every call


def test_classify_pages_unknown_type_from_model(fake):
    fake.answer = {"segments": [{"type": "lottery_ticket", "page_from": 1, "page_to": 1, "confidence": 0.9}]}
    out = doc_ai.classify_pages(make_pdf(["아무 내용 없는 시험 문서입니다 가나다라마바사"]))
    assert out["status"] == "ok" and out["type"] == "" and out["segments"][0]["type"] == "unknown"


def test_classify_pages_image_pages(fake):
    out = doc_ai.classify_pages(_image_only_pdf(2))
    assert (out["status"], out["needs_image"]) == ("needs_image", [1, 2]) and not fake.calls  # nothing sent
    buf = io.BytesIO()  # text page + image page
    c = canvas.Canvas(buf, pagesize=(595, 842))
    from test_doc_registry import FONT
    c.setFont(FONT, 16)
    c.drawString(40, 790, "처방조제정보 진료시작일 약품명 성분명 투약일수")
    c.showPage()
    c.rect(50, 50, 400, 600, fill=1)
    c.showPage()
    c.save()
    fake.answer = {"segments": [{"type": "prescription", "page_from": 1, "page_to": 1, "confidence": 0.9}]}
    out = doc_ai.classify_pages(buf.getvalue())
    assert out["status"] == "ok" and out["needs_image"] == [2] and out["uncovered"] == []
    assert "(글자 없음: 이미지 쪽)" in fake.calls[0][0][1]["text"]


def test_classify_pages_failures(fake):
    assert doc_ai.classify_pages(b"not a pdf")["status"] == "not_pdf"
    assert doc_ai.classify_pages(pdf_io.encrypt(make_pdf(["처방조제정보"]), "pw"))["status"] == "encrypted"
    assert doc_ai.classify_pages(b"%PDF-1.4 broken")["status"] == "unreadable"
    fake.answer = RuntimeError("Gemini down")
    out = doc_ai.classify_pages(make_pdf(["처방조제정보 진료시작일 약품명 성분명 투약일수"]))
    assert out["status"] == "error" and "Gemini down" in out["error"] and out["type"] == ""
    assert not fake.calls[0][0][1]["text"].count("900101")
    fake.answer = ["not", "a", "dict"]
    assert doc_ai.classify_pages(make_pdf(["처방조제정보 진료시작일 약품명 성분명 투약일수"]))["segments"] == []


def test_page_cap(fake):
    out = doc_ai.classify_pages(make_pdf(["처방조제정보 진료시작일 약품명 성분명 투약일수"], pages=5), max_pages=2)
    assert (out["pages"], out["sent_pages"]) == (5, 2)
    assert "(이하 3쪽 생략)" in fake.calls[0][0][1]["text"]


# ---- compare / agreement -------------------------------------------------------------------------
def test_compare_mhr_sections_agree_and_disagree(fake):
    raw = build_mhr(["visits", "meds", "checkup"])  # pages: 1 visits, 2 meds, 3-4 checkup
    rule = doc_ai.doc_registry.classify(raw)
    if rule["type"] != "mhr":  # build() has no app title; the registry needs it on page 1
        rule = {**rule, "type": "mhr", "box": "mhr", "sections": [
            {"section": "visits", "pages": [1]}, {"section": "meds", "pages": [2]},
            {"section": "checkup", "pages": [3, 4]}]}
    good = [{"type": "mhr_visits", "page_from": 1, "page_to": 1, "confidence": 0.9},
            {"type": "mhr_meds", "page_from": 2, "page_to": 2, "confidence": 0.9},
            {"type": "mhr_checkup", "page_from": 3, "page_to": 4, "confidence": 0.9}]
    fake.answer = {"segments": good}
    ai = doc_ai.classify_pages(raw)
    flags = doc_ai.agreement(rule, ai)
    assert flags == {"same_type": True, "same_box": True, "same_sections": True, "same_pages": True, "status": "agree"}
    fake.answer = {"segments": good[:2] + [{**good[2], "page_from": 4}]}  # misses page 3
    flags = doc_ai.agreement(rule, doc_ai.classify_pages(raw))
    assert (flags["same_sections"], flags["same_pages"], flags["status"]) == (True, False, "disagree")


def test_agreement_statuses():
    ok = {"status": "ok", "sent_pages": 3, "sections": []}
    nhis = {"type": "nhis", "box": "medical", "sections": []}
    none = {"type": "", "box": "", "sections": []}
    assert doc_ai.agreement(nhis, {**ok, **nhis})["status"] == "agree"
    assert doc_ai.agreement(nhis, {**ok, "type": "prescription", "box": "medical"}) == {
        "same_type": False, "same_box": True, "same_sections": None, "same_pages": None, "status": "disagree"}
    assert doc_ai.agreement(none, {**ok, **nhis})["status"] == "ai_only"
    assert doc_ai.agreement(nhis, {**ok, **none})["status"] == "rule_only"
    assert doc_ai.agreement(none, {**ok, **none})["status"] == "both_none"
    assert doc_ai.agreement(nhis, {"status": "error", "type": "", "box": ""})["status"] == "ai_failed"


def test_compare_runs_both(fake):
    raw = make_pdf([("처방조제정보", 16), "진료시작일 약품명 성분명 투약일수"])
    fake.answer = {"segments": [{"type": "prescription", "page_from": 1, "page_to": 1, "confidence": 0.9}]}
    out = doc_ai.compare(raw)
    assert out["rule"]["type"] == "prescription" and out["ai"]["type"] == "prescription"
    assert out["agreement"]["status"] == "agree"
    json.dumps(out)  # loggable as-is


def test_shadow_off_by_default_and_row_has_no_quotes(fake, monkeypatch):
    raw = make_pdf([("처방조제정보", 16), "진료시작일 약품명 성분명 투약일수"])
    rule = doc_ai.doc_registry.classify(raw)
    monkeypatch.delenv("PICK3_DOC_AI_SHADOW", raising=False)
    assert doc_ai.shadow(raw, rule) is None and not fake.calls
    monkeypatch.setenv("PICK3_DOC_AI_SHADOW", "1")
    monkeypatch.setattr(doc_ai.gemini_usage, "_conf", lambda: None)  # no Supabase in tests
    fake.answer = {"segments": [{"type": "prescription", "page_from": 1, "page_to": 1, "confidence": 0.9,
                                 "evidence": "처방조제정보 가나다", "start_hint": "처방조제정보"}]}
    row = doc_ai.shadow(raw, rule, user_id=7, doc_ref="doc:1")
    assert (row["user_id"], row["doc_ref"], row["rule_type"], row["ai_type"], row["agreement"]) == (
        7, "doc:1", "prescription", "prescription", "agree")
    assert "가나다" not in json.dumps(row, ensure_ascii=False) and len(row["sha256"]) == 64
    assert row["prompt_tokens"] == 1000 and row["output_tokens"] == 150 and row["usd"] is not None
