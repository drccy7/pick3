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
from providers import clear_recommend_cache  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    clear_recommend_cache()


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


def test_cors_allows_github_pages_origin() -> None:
    res = client.options(
        "/api/recommend",
        headers={
            "Origin": "https://drccy7.github.io",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "https://drccy7.github.io"


def test_google_places_ranks_by_situation_and_rating(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import providers

    class FakeResp:
        status_code = 200
        text = "ok"

        def json(self) -> dict:
            return {
                "places": [
                    {
                        "displayName": {"text": "신규 1리뷰 식당"},
                        "formattedAddress": "대한민국 서울 강남구 역삼동 1",
                        "rating": 5.0,
                        "userRatingCount": 1,
                        "googleMapsUri": "https://maps.google.com/?cid=1",
                        "currentOpeningHours": {"openNow": True},
                        "reviews": [],
                    },
                    {
                        "displayName": {"text": "고평점 일반식당"},
                        "formattedAddress": "대한민국 서울 강남구 역삼동 2",
                        "rating": 4.8,
                        "userRatingCount": 200,
                        "googleMapsUri": "https://maps.google.com/?cid=2",
                        "currentOpeningHours": {"openNow": True},
                        "reviews": [{"text": {"text": "맛있어요"}}],
                    },
                    {
                        "displayName": {"text": "룸 완비 회식갈비"},
                        "formattedAddress": "대한민국 서울 강남구 역삼동 3",
                        "rating": 4.6,
                        "userRatingCount": 500,
                        "googleMapsUri": "https://maps.google.com/?cid=3",
                        "currentOpeningHours": {"openNow": False},
                        "reviews": [{"text": {"text": "단체 회식하기 좋고 룸이 넓어요"}}],
                    },
                    {
                        "displayName": {"text": "기본 갈비집"},
                        "formattedAddress": "대한민국 서울 강남구 역삼동 4",
                        "rating": 4.5,
                        "userRatingCount": 300,
                        "googleMapsUri": "https://maps.google.com/?cid=4",
                        "reviews": [],
                    },
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake_google_key")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    res = client.post(
        "/api/recommend",
        json={"query": "돼지갈비", "situation": "group", "area": "강남", "provider": "google"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["data_source"] == "google_places_api"
    # Situation-matched ("룸", "회식") place ranks 1st, 1-review 5.0 outlier is excluded
    assert body["top_pick"]["name"] == "룸 완비 회식갈비"
    assert body["top_pick"]["open_status"] == "현재 영업 종료"
    assert body["top_pick"]["google_maps_url"] == "https://maps.google.com/?cid=3"
    assert [a["name"] for a in body["alternatives"]] == ["고평점 일반식당", "기본 갈비집"]
    assert providers.get_kakao_diag()["google_key_configured"] is True


def test_google_places_filters_out_of_area_name_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class FakeResp:
        status_code = 200
        text = "ok"

        def json(self) -> dict:
            return {
                "places": [
                    {
                        "displayName": {"text": "본강남갈비 인천논현본점"},
                        "formattedAddress": "대한민국 인천광역시 남동구 논현동 1",
                        "rating": 4.9,
                        "userRatingCount": 200,
                        "googleMapsUri": "https://maps.google.com/?cid=99",
                        "reviews": [],
                    },
                    {
                        "displayName": {"text": "강남 본점 1"},
                        "formattedAddress": "대한민국 서울특별시 강남구 역삼동 10",
                        "rating": 4.6,
                        "userRatingCount": 100,
                        "googleMapsUri": "https://maps.google.com/?cid=10",
                        "reviews": [],
                    },
                    {
                        "displayName": {"text": "강남 본점 2"},
                        "formattedAddress": "대한민국 서울특별시 강남구 논현동 20",
                        "rating": 4.5,
                        "userRatingCount": 80,
                        "googleMapsUri": "https://maps.google.com/?cid=20",
                        "reviews": [],
                    },
                    {
                        "displayName": {"text": "강남 본점 3"},
                        "formattedAddress": "대한민국 서울특별시 강남구 대치동 30",
                        "rating": 4.4,
                        "userRatingCount": 60,
                        "googleMapsUri": "https://maps.google.com/?cid=30",
                        "reviews": [],
                    },
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake_google_key")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    res = client.post(
        "/api/recommend",
        json={"query": "돼지갈비", "situation": "date", "area": "강남", "provider": "google"},
    )
    assert res.status_code == 200
    names = [res.json()["top_pick"]["name"]] + [a["name"] for a in res.json()["alternatives"]]
    assert "본강남갈비 인천논현본점" not in names
    assert names == ["강남 본점 1", "강남 본점 2", "강남 본점 3"]


def test_google_places_uses_6h_cache_on_repeat_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    call_count = 0

    class FakeResp:
        status_code = 200
        text = "ok"

        def json(self) -> dict:
            return {
                "places": [
                    {
                        "displayName": {"text": "캐시 테스트 식당"},
                        "formattedAddress": "대한민국 서울 강남구 역삼동 1",
                        "rating": 4.8,
                        "userRatingCount": 120,
                        "googleMapsUri": "https://maps.google.com/?cid=1",
                        "reviews": [],
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            return FakeResp()

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake_google_key")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    first = client.post(
        "/api/recommend",
        json={"query": "소바", "situation": "solo", "area": "강남", "provider": "google"},
    )
    second = client.post(
        "/api/recommend",
        json={"query": "소바", "situation": "solo", "area": "강남", "provider": "google"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert call_count == 1


def test_provider_selection_routes_kakao_or_google(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class FakeResp:
        status_code = 200
        text = "ok"

        def json(self) -> dict:
            return {
                "documents": [
                    {
                        "id": "101",
                        "place_name": "카카오 갈비 1",
                        "road_address_name": "서울 강남구 역삼로 1",
                        "category_name": "음식점 > 한식 > 갈비",
                        "place_url": "http://place.map.kakao.com/101",
                    },
                    {
                        "id": "102",
                        "place_name": "카카오 갈비 2",
                        "road_address_name": "서울 강남구 역삼로 2",
                        "category_name": "음식점 > 한식 > 갈비",
                        "place_url": "http://place.map.kakao.com/102",
                    },
                    {
                        "id": "103",
                        "place_name": "카카오 갈비 3",
                        "road_address_name": "서울 강남구 역삼로 3",
                        "category_name": "음식점 > 한식 > 갈비",
                        "place_url": "http://place.map.kakao.com/103",
                    },
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def get(self, *args, **kwargs):
            return FakeResp()

        async def post(self, *args, **kwargs):
            raise AssertionError("Google Places should not be called when provider='kakao'")

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake_google_key")
    monkeypatch.setenv("KAKAO_REST_API_KEY", "fake_kakao_key")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    res = client.post(
        "/api/recommend",
        json={"query": "돼지갈비", "situation": "solo", "area": "강남", "provider": "kakao"},
    )
    assert res.status_code == 200
    assert res.json()["data_source"] == "kakao_local_api"
    assert res.json()["top_pick"]["name"] == "카카오 갈비 1"


def test_closed_restaurants_filtered_by_google_status_and_naver_blog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    class FakeResp:
        def __init__(self, payload: dict) -> None:
            self.status_code = 200
            self.text = "ok"
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, *args, **kwargs):
            return FakeResp(
                {
                    "places": [
                        {
                            "displayName": {"text": "구글 폐업식당"},
                            "formattedAddress": "대한민국 서울 강남구 역삼동 1",
                            "rating": 5.0,
                            "userRatingCount": 500,
                            "businessStatus": "CLOSED_PERMANENTLY",
                            "googleMapsUri": "https://maps.google.com/?cid=1",
                            "reviews": [],
                        },
                        {
                            "displayName": {"text": "블로그 폐업식당"},
                            "formattedAddress": "대한민국 서울 강남구 역삼동 2",
                            "rating": 4.9,
                            "userRatingCount": 200,
                            "businessStatus": "OPERATIONAL",
                            "googleMapsUri": "https://maps.google.com/?cid=2",
                            "reviews": [],
                        },
                        {
                            "displayName": {"text": "정상 영업식당 A"},
                            "formattedAddress": "대한민국 서울 강남구 역삼동 3",
                            "rating": 4.7,
                            "userRatingCount": 150,
                            "businessStatus": "OPERATIONAL",
                            "googleMapsUri": "https://maps.google.com/?cid=3",
                            "reviews": [],
                        },
                        {
                            "displayName": {"text": "정상 영업식당 B"},
                            "formattedAddress": "대한민국 서울 강남구 역삼동 4",
                            "rating": 4.6,
                            "userRatingCount": 120,
                            "businessStatus": "OPERATIONAL",
                            "googleMapsUri": "https://maps.google.com/?cid=4",
                            "reviews": [],
                        },
                        {
                            "displayName": {"text": "정상 영업식당 C"},
                            "formattedAddress": "대한민국 서울 강남구 역삼동 5",
                            "rating": 4.5,
                            "userRatingCount": 100,
                            "businessStatus": "OPERATIONAL",
                            "googleMapsUri": "https://maps.google.com/?cid=5",
                            "reviews": [],
                        },
                    ]
                }
            )

        async def get(self, url: str, **kwargs):
            q = (kwargs.get("params") or {}).get("query", "")
            if "블로그 폐업식당" in q:
                return FakeResp(
                    {
                        "items": [
                            {
                                "title": "블로그 폐업식당 영업종료 후기",
                                "description": "아쉽게도 최근 폐업해서 문 닫았습니다.",
                            }
                        ]
                    }
                )
            return FakeResp(
                {
                    "items": [
                        {
                            "title": "정상 식당 후기",
                            "description": "직원이 구워주고 룸도 있어서 회식 추천! 1인분 18,000원 수준.",
                        }
                    ]
                }
            )

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake_google_key")
    monkeypatch.setenv("NAVER_CLIENT_ID", "fake_naver_id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "fake_naver_secret")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    res = client.post(
        "/api/recommend",
        json={
            "query": "돼지갈비",
            "situation": "group",
            "area": "강남",
            "provider": "google",
            "lat": 37.498,
            "lng": 127.027,
        },
    )
    assert res.status_code == 200
    body = res.json()
    names = [body["top_pick"]["name"]] + [a["name"] for a in body["alternatives"]]
    assert "구글 폐업식당" not in names
    assert "블로그 폐업식당" not in names
    assert names == ["정상 영업식당 A", "정상 영업식당 B", "정상 영업식당 C"]
    assert "직원 구이" in body["top_pick"]["reason"] or "룸 좌석 있음" in body["top_pick"]["reason"]
    assert "1.8만원" in body["top_pick"]["price_note"]
