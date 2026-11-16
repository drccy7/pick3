"""Automated scenario and edge-case tests for Pick3 mobile web app."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PICK3_DIR = Path(__file__).resolve().parent.parent
if str(PICK3_DIR) not in sys.path:
    sys.path.insert(0, str(PICK3_DIR))

from app import app  # noqa: E402

client = TestClient(app)


def test_health_and_index_served() -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    home = client.get("/")
    assert home.status_code == 200
    assert "10초 맛집 결정기" in home.text


def test_presets_endpoint() -> None:
    res = client.get("/api/presets")
    assert res.status_code == 200
    data = res.json()
    assert "돼지갈비" in data["menus"]
    assert len(data["situations"]) == 3


@pytest.mark.parametrize("situation", ["solo", "date", "group"])
def test_recommend_curated_no_list_max_three(situation: str) -> None:
    res = client.post(
        "/api/recommend",
        json={"query": "돼지갈비", "situation": situation, "area": "강남"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["data_source"] == "curated_sample"
    assert body["area"] == "강남"
    assert body["top_pick"]["name"]
    assert len(body["alternatives"]) == 2
    assert body["top_pick"]["naver_map_url"].startswith("https://map.naver.com/")
    assert body["top_pick"]["kakao_map_url"].startswith("https://map.kakao.com/")


def test_recommend_custom_query_honest_fallback() -> None:
    res = client.post(
        "/api/recommend",
        json={"query": "마라샹궈", "situation": "date", "area": "홍대"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["data_source"] == "dynamic_map_search"
    assert body["top_pick"]["is_verified_detail"] is False
    assert len(body["alternatives"]) == 2
    assert "홍대" in body["top_pick"]["name"]


def test_recommend_validation_rejects_empty_and_invalid_situation() -> None:
    empty_res = client.post(
        "/api/recommend",
        json={"query": "   ", "situation": "solo"},
    )
    assert empty_res.status_code == 422

    bad_sit_res = client.post(
        "/api/recommend",
        json={"query": "돼지갈비", "situation": "party"},
    )
    assert bad_sit_res.status_code == 422


def test_kakao_api_fallback_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAKAO_REST_API_KEY", "dummy_invalid_key")
    res = client.post(
        "/api/recommend",
        json={"query": "삼겹살", "situation": "group"},
    )
    # Must gracefully fall back to curated_sample instead of crashing with 500
    assert res.status_code == 200
    body = res.json()
    assert body["data_source"] == "curated_sample"
    assert len(body["alternatives"]) == 2
