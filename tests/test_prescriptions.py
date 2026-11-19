"""Prescription / dispensing records (처방조제정보): parsing helpers, routing, storage, auth.

Uses synthetic rows only - real prescription PDFs are personal health data and stay out of the repo.
"""

from __future__ import annotations

import pytest

import prescriptions
from test_medical import AUTH, PNG, _fake, client  # noqa: F401  (shared fakes + autouse env fixture below)
from test_medical import _env  # noqa: F401

ROWS = [
    {"visit_date": "2025-10-17", "provider": "가나산부인과의원", "kind": "처방", "drug_name": "가약정_(1정)",
     "ingredient": "famotidine", "dose": "1", "times_per_day": "1", "days": "3"},
    {"visit_date": "2025-10-17", "provider": "다라약국", "kind": "조제", "drug_name": "가약정_(1정)",
     "ingredient": "famotidine", "dose": "1", "times_per_day": "1", "days": "3"},
]
PDF = b"%PDF-1.7 synthetic"


@pytest.mark.parametrize("text,korean,expected", [
    ("파모트리정20밀리그램\n(파모티딘)_(20mg/1\n정)", True, "파모트리정20밀리그램(파모티딘)_(20mg/1정)"),
    ("magnesium\nhydroxide", False, "magnesium hydroxide"),
    ("acetaminophen(\nencapsulated)", False, "acetaminophen(encapsulated)"),
    (None, False, ""),
])
def test_join_lines(text, korean, expected) -> None:
    assert prescriptions._join_lines(text, korean) == expected


@pytest.mark.parametrize("filename,expected", [
    ("김예시 약처방.pdf", "김예시"),
    ("홍길동_처방조제정보.pdf", "홍길동"),
    ("처방조제정보.pdf", ""),
    ("약처방 홍길동.pdf", ""),
    ("scan_0001.pdf", ""),
    (None, ""),
])
def test_name_from_filename(filename, expected) -> None:
    assert prescriptions.name_from_filename(filename) == expected


def test_kind_rules() -> None:
    assert prescriptions._kind("처방조제", "다라약국") == "조제"
    assert prescriptions._kind("외래", "가나의원") == "처방"
    assert prescriptions._kind("입원", "가나병원") == "처방"


def test_prescription_pdf_goes_to_its_own_table_with_name_from_file(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, {"patient": "", "records": []}, rows=[])
    monkeypatch.setattr(prescriptions, "is_prescription_pdf", lambda raw: True)
    monkeypatch.setattr(prescriptions, "parse_prescription_pdf", lambda raw: (list(ROWS), [7]))
    res = client.post("/api/ingest/medical", files={"image": ("김예시 약처방.pdf", PDF, "application/pdf")},
                      headers=AUTH).json()
    assert res["status"] == "saved" and res["patient"] == "김예시" and res["patient_source"] == "파일 이름에서"
    assert res["message"].startswith("💊 처방·조제 기록\n✅ 김예시: 2건 저장")
    assert "순번 1개를 읽지 못했습니다: 7" in res["message"]
    url, kw = seen["posts"][0]
    assert url.endswith("/pick3_prescriptions")
    assert kw["params"] == {"on_conflict": "user_id,patient,visit_date,provider,drug_name"}
    assert {(r["patient"], r["user_id"], r["source"]) for r in kw["json"]} == {("김예시", 7, "pdf")}
    assert seen["gemini"] == []  # deterministic PDF parse, no Gemini call


def test_customer_name_field_wins_over_file_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, {}, rows=[])
    monkeypatch.setattr(prescriptions, "is_prescription_pdf", lambda raw: True)
    monkeypatch.setattr(prescriptions, "parse_prescription_pdf", lambda raw: (list(ROWS), []))
    res = client.post("/api/ingest/medical", files={"image": ("김예시 약처방.pdf", PDF, "application/pdf")},
                      data={"patient": "박고객"}, headers=AUTH).json()
    assert res["patient"] == "박고객" and "⚠" not in res["message"]
    assert {r["patient"] for r in seen["posts"][0][1]["json"]} == {"박고객"}


def test_prescription_screenshot_routed_from_pick3_shortcut(monkeypatch: pytest.MonkeyPatch) -> None:
    gem_rows = {"patient": "이환자", "records": [
        {"date": "2025.10.17", "provider": "다라약국", "kind": "조제", "drug_name": "가약정",
         "ingredient": "famotidine", "dose": "1", "times_per_day": "1", "days": "3"},
        {"date": "", "provider": "잘린행", "drug_name": "x"},  # cut off -> dropped
    ]}
    seen = _fake(monkeypatch, [{"type": "prescription"}, gem_rows])
    res = client.post("/api/ingest/screenshot", files={"image": ("s.png", PNG, "image/png")}, headers=AUTH).json()
    assert res["service"] == "prescription" and res["patient"] == "이환자" and res["count"] == 1
    assert seen["posts"][0][0].endswith("/pick3_prescriptions")
    assert seen["posts"][0][1]["json"][0]["visit_date"] == "2025-10-17"


def test_prescription_endpoints_scoped_and_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, {}, rows=[{"id": 1, **ROWS[0], "patient": "김예시"}])
    body = client.get("/api/prescriptions", params={"patient": "김예시", "year": 2025}, headers=AUTH).json()
    assert body["count"] == 1
    params = seen["gets"][-1][1]["params"]
    assert params["user_id"] == "eq.7" and params["patient"] == "eq.김예시"
    assert client.post("/api/prescriptions/patient", json={"ids": [2, 1], "patient": "박고객"}, headers=AUTH).status_code == 200
    assert seen["patches"][0][1]["params"] == {"id": "in.(1,2)", "user_id": "eq.7"}
    assert client.delete("/api/prescriptions", params={"patient": "김예시"}, headers=AUTH).json()["deleted"] == 1
    assert client.delete("/api/prescriptions/1", headers=AUTH).json() == {"deleted": 1}
    for call in (lambda: client.get("/api/prescriptions"),
                 lambda: client.delete("/api/prescriptions/1"),
                 lambda: client.post("/api/prescriptions/patient", json={"ids": [1], "patient": "x"})):
        assert call().status_code == 401


def test_medical_shortcut_image_of_prescription_goes_to_prescriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    # e.g. the Shortcut captured the PDF viewer screen instead of receiving the file
    gem_rows = {"patient": "", "records": [
        {"date": "2025-10-17", "provider": "다라약국", "kind": "조제", "drug_name": "가약정"}]}
    seen = _fake(monkeypatch, [{"type": "prescription"}, gem_rows], rows=[{"patient": "최근이름"}])
    res = client.post("/api/ingest/medical", files={"image": ("s.png", PNG, "image/png")}, headers=AUTH).json()
    assert res["status"] == "saved" and res["message"].startswith("💊 처방·조제 기록")
    assert res["patient"] == "최근이름" and res["patient_source"] == "최근 이름"
    assert seen["posts"][0][0].endswith("/pick3_prescriptions")


def test_in_document_repeats_reported_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, {}, rows=[])
    monkeypatch.setattr(prescriptions, "is_prescription_pdf", lambda raw: True)
    monkeypatch.setattr(prescriptions, "parse_prescription_pdf", lambda raw: ([ROWS[0], ROWS[0], ROWS[1]], []))
    res = client.post("/api/ingest/medical", files={"image": ("홍길동 약처방.pdf", PDF, "application/pdf")},
                      headers=AUTH).json()
    assert "2건 저장 (문서 안 같은 약 1건 합침)" in res["message"] and "이미 있던" not in res["message"]


def test_pdf_missed_by_title_check_falls_back_to_prescriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch, {"patient": "", "records": []}, rows=[])
    monkeypatch.setattr(prescriptions, "is_prescription_pdf", lambda raw: False)
    monkeypatch.setattr(prescriptions, "parse_prescription_pdf", lambda raw: (list(ROWS), []))
    res = client.post("/api/ingest/medical", files={"image": ("홍길동 약처방.pdf", PDF, "application/pdf")},
                      headers=AUTH).json()
    assert res["status"] == "saved" and res["message"].startswith("💊 처방·조제 기록")
    assert seen["posts"][0][0].endswith("/pick3_prescriptions")


def test_not_found_reports_what_was_received(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, {"patient": "", "records": []}, rows=[])
    monkeypatch.setattr(prescriptions, "is_prescription_pdf", lambda raw: False)
    monkeypatch.setattr(prescriptions, "parse_prescription_pdf", lambda raw: ([], []))
    res = client.post("/api/ingest/medical", files={"image": ("a.pdf", PDF, "application/pdf")}, headers=AUTH).json()
    assert res["status"] == "not_found" and "(받은 파일: PDF · 1KB · a.pdf · '%PDF-1.7')" in res["message"]


def test_pdf_shared_to_the_single_pick3_shortcut_is_handled_as_a_medical_document(monkeypatch: pytest.MonkeyPatch) -> None:
    # The one "Pick3" Shortcut posts everything to /api/ingest/screenshot; a PDF must not go to restaurants.
    seen = _fake(monkeypatch, {"patient": "", "records": []}, rows=[])
    monkeypatch.setattr(prescriptions, "is_prescription_pdf", lambda raw: True)
    monkeypatch.setattr(prescriptions, "parse_prescription_pdf", lambda raw: (list(ROWS), []))
    res = client.post("/api/ingest/screenshot", files={"image": ("김예시 약처방.pdf", PDF, "application/pdf")},
                      headers=AUTH).json()
    assert res["status"] == "saved" and res["service"] == "prescription" and res["patient"] == "김예시"
    assert seen["posts"][0][0].endswith("/pick3_prescriptions")
    assert seen["gemini"] == []  # no screen classification for PDFs


def test_payment_pdf_to_single_shortcut_saves_medical_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_medical import SCREEN
    seen = _fake(monkeypatch, SCREEN)
    res = client.post("/api/ingest/screenshot", files={"image": ("영수증.pdf", PDF, "application/pdf")},
                      headers=AUTH).json()
    assert res["service"] == "medical" and res["message"].startswith("🧾 의료비 기록\n")
    assert seen["posts"][0][0].endswith("/pick3_medical_records")
