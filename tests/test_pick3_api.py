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


@pytest.fixture(autouse=True)
def _no_ingest_token_from_local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """app.py loads a local .env; keep ingest open unless a test sets the token."""
    monkeypatch.delenv("PICK3_INGEST_TOKEN", raising=False)
    import saved_places

    saved_places.clear_user_cache()
    saved_places._LOCAL_USAGE.clear()


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
                            "description": "직원이 구워주고 룸도 있어서 회식 추천! 대표 메뉴 돼지갈비 18,000원 수준.",
                            "postdate": "20261110",
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
    assert "돼지갈비 18,000원 (2026년 기준)" in body["top_pick"]["price_note"]


def test_kakao_distance_walk_time_phone_and_reserve_picks(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class FakeResp:
        status_code = 200
        text = "ok"

        def json(self) -> dict:
            return {
                "documents": [
                    {
                        "id": str(i),
                        "place_name": f"거리 테스트 식당 {i}",
                        "road_address_name": f"서울 강남구 역삼로 {i}",
                        "category_name": "음식점 > 한식",
                        "place_url": f"http://place.map.kakao.com/{i}",
                        "phone": "02-1234-5678" if i == 1 else "",
                        "distance": "320" if i == 1 else "800",
                    }
                    for i in range(1, 6)
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

    monkeypatch.setenv("KAKAO_REST_API_KEY", "fake_kakao_key")
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    res = client.post(
        "/api/recommend",
        json={
            "query": "돼지갈비",
            "situation": "solo",
            "provider": "kakao",
            "lat": 37.498,
            "lng": 127.027,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["top_pick"]["distance_text"] == "320m (도보 4분)"
    assert body["top_pick"]["phone"] == "02-1234-5678"
    assert len(body["reserve_picks"]) == 2
    # Weak value/experience evidence -> falls back honestly to "가까운 대안"
    assert body["alternatives"][0]["role_badge"] == "가까운 대안"


def test_role_badges_use_evidence_when_present_and_fallback_when_weak() -> None:
    import providers

    with_evidence = providers._assign_role_badges(
        [
            providers.RestaurantPick("1위", "갈비", "강남", "종합 1위", "", [], "", "", ""),
            providers.RestaurantPick("2위", "갈비", "강남", "가성비 좋고 점심특선 있음", "", [], "", "", ""),
            providers.RestaurantPick("3위", "갈비", "강남", "직원 구이와 분위기 좋음", "", [], "", "", ""),
        ]
    )
    assert with_evidence[1].role_badge == "가성비 추천"
    assert with_evidence[2].role_badge == "만족·분위기 추천"

    weak_evidence = providers._assign_role_badges(
        [
            providers.RestaurantPick("1위", "갈비", "강남", "인기", "", [], "", "", ""),
            providers.RestaurantPick("2위", "갈비", "강남", "인기", "", [], "", "", "", review_count=120),
            providers.RestaurantPick("3위", "갈비", "강남", "인기", "", [], "", "", ""),
        ]
    )
    assert weak_evidence[1].role_badge == "리뷰 검증 대안"
    assert weak_evidence[2].role_badge == ""


def test_pwa_manifest_icon_and_caution_note() -> None:
    import providers

    m = client.get("/manifest.json")
    assert m.status_code == 200
    assert m.json()["short_name"] == "Pick3"

    ic = client.get("/icon.svg")
    assert ic.status_code == 200
    assert "<svg" in ic.text

    caution = providers._extract_caution_note("평일 브레이크타임 15시~17시이고 저녁엔 웨이팅 있어요")
    assert "브레이크타임" in caution
    assert "웨이팅" in caution


def test_reels_script_endpoint() -> None:
    res = client.post(
        "/api/reels-script",
        json={"query": "돼지갈비", "situation": "date", "area": "강남", "episode": 3},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["episode"] == 3
    assert "#3" in body["title"]
    assert len(body["cuts"]) == 3
    assert "팔로우" in body["outro_cta_14_18s"]
    assert "인스타 본문 캡션" in body["full_text"]


def test_render_reels_ass_subtitles_and_timings(tmp_path: Path) -> None:
    import render_reels

    timings = render_reels.extract_word_timings(tmp_path / "missing.mp3", "강남 돼지갈비 맛집", 6.0)
    assert len(timings) == 3
    ass_path = tmp_path / "test.ass"
    render_reels.build_ass_subtitles("테스트 제목", timings, 6.0, ass_path)
    content = ass_path.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in content
    assert "PlayResY: 1920" in content
    assert "\\c&H0000D4FF\\fscx115\\fscy115" in content


def test_ingest_screenshot_and_recall_saved_pick3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setenv("PICK3_SQLITE_PATH", str(tmp_path / "test_saved.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini")
    monkeypatch.setenv("KAKAO_REST_API_KEY", "fake_kakao")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    class FakeResp:
        def __init__(self, payload: dict) -> None:
            self.status_code = 200
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

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
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": '{"name": "몽탄 성수직영점", "area": "성수", "category": "돼지갈비", "signature_menu": "우대갈비", "memo": "웨이팅 있음"}'
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        async def get(self, *args, **kwargs):
            return FakeResp(
                {
                    "documents": [
                        {
                            "place_name": "몽탄 성수직영점",
                            "category_group_code": "FD6",
                            "road_address_name": "서울 성동구 연무장길 1",
                            "x": "127.055",
                            "y": "37.544",
                            "phone": "02-555-1234",
                            "place_url": "http://place.map.kakao.com/777",
                        }
                    ]
                }
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    ingest_res = client.post(
        "/api/ingest/screenshot",
        files={"image": ("reel.jpg", b"fake_jpeg_bytes", "image/jpeg")},
    )
    assert ingest_res.status_code == 200
    ingest_body = ingest_res.json()
    assert ingest_body["status"] == "saved"
    assert "몽탄 성수직영점" in ingest_body["message"]

    recall_res = client.post(
        "/api/saved/pick3",
        json={"area": "성수", "lat": 37.5445, "lng": 127.0555},
    )
    assert recall_res.status_code == 200
    recall_body = recall_res.json()
    assert recall_body["data_source"] == "saved_places"
    assert recall_body["top_pick"]["name"] == "몽탄 성수직영점"
    assert "도보" in recall_body["top_pick"]["distance_text"]


class _SbResp:
    def __init__(self, status_code: int, payload) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


TOKEN_A = "p3_token_user_a"
AUTH_A = {"X-Pick3-Token": TOKEN_A}
USER_A = {"id": 1, "name": "A", "daily_limit": None, "disabled": False}


def _patch_ingest_upstreams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sb_post, sb_get, calls: list,
    usage=lambda: _SbResp(200, {"user_count": 1, "total_count": 1}),
    users: dict | None = None, gemini_calls: list | None = None,
) -> None:
    """Fake Gemini + Kakao + Supabase. Kakao key unset so only Gemini/Supabase are hit.

    `calls` records only pick3_saved_places traffic; pick3_users lookups and the usage RPC are
    answered separately. `users` maps token -> user row (default: TOKEN_A is user 1).
    """
    import httpx

    from saved_places import hash_token

    known = {hash_token(t): u for t, u in (users or {TOKEN_A: USER_A}).items()}

    monkeypatch.setenv("PICK3_SQLITE_PATH", str(tmp_path / "must_not_be_used.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "fake_gemini")
    monkeypatch.delenv("KAKAO_REST_API_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co/")
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_fake")

    gemini = _SbResp(200, {"candidates": [{"content": {"parts": [{"text": '{"name": "몽탄", "area": "성수"}'}]}}]})
    gemini.raise_for_status = lambda: None

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def post(self, url, **kwargs):
            if "generativelanguage" in url:
                if gemini_calls is not None:
                    gemini_calls.append(url)
                return gemini
            if url.endswith("/rpc/pick3_bump_usage"):
                return usage()
            calls.append(("post", url, kwargs))
            return sb_post()

        async def get(self, url, **kwargs):
            if url.endswith("/pick3_users"):
                h = kwargs["params"]["token_hash"][len("eq."):]
                return _SbResp(200, [known[h]] if h in known else [])
            calls.append(("get", url, kwargs))
            return sb_get()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def test_ingest_supabase_upsert_uses_secret_key_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _patch_ingest_upstreams(
        monkeypatch, tmp_path,
        sb_post=lambda: _SbResp(201, [{"id": 7, "name": "몽탄", "area": "성수", "address": "성수"}]),
        sb_get=lambda: _SbResp(200, []),
        calls=calls,
    )
    res = client.post("/api/ingest/screenshot", files={"image": ("a.jpg", b"x", "image/jpeg")}, headers=AUTH_A).json()
    assert res["status"] == "saved"
    assert res["place"]["storage"] == "supabase" and res["place"]["duplicate"] is False
    method, url, kwargs = calls[0]
    assert url == "https://example.supabase.co/rest/v1/pick3_saved_places"
    assert kwargs["params"] == {"on_conflict": "user_id,name,address"}
    assert kwargs["json"]["user_id"] == 1
    assert "resolution=ignore-duplicates" in kwargs["headers"]["Prefer"]
    assert kwargs["headers"]["apikey"] == "sb_secret_fake"
    assert "Authorization" not in kwargs["headers"]  # sb_secret_ keys are not JWTs
    assert not (tmp_path / "must_not_be_used.db").exists()


def test_ingest_supabase_duplicate_returns_existing_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _patch_ingest_upstreams(
        monkeypatch, tmp_path,
        sb_post=lambda: _SbResp(201, []),  # conflict ignored
        sb_get=lambda: _SbResp(200, [{"id": 3, "name": "몽탄", "area": "성수", "address": "성수"}]),
        calls=calls,
    )
    res = client.post("/api/ingest/screenshot", files={"image": ("a.jpg", b"x", "image/jpeg")}, headers=AUTH_A).json()
    assert res["status"] == "saved"
    assert res["message"].startswith("이미 저장된 곳입니다")
    assert res["place"]["id"] == 3
    assert calls[1][2]["params"]["address"] == "eq.성수"
    assert calls[1][2]["params"]["user_id"] == "eq.1"


def test_ingest_supabase_failure_reports_error_without_sqlite_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    _patch_ingest_upstreams(
        monkeypatch, tmp_path,
        sb_post=lambda: _SbResp(401, {"message": "Invalid API key"}),
        sb_get=lambda: _SbResp(401, {"message": "Invalid API key"}),
        calls=calls,
    )
    res = client.post("/api/ingest/screenshot", files={"image": ("a.jpg", b"x", "image/jpeg")}, headers=AUTH_A).json()
    assert res["status"] == "error"
    assert "저장 실패" in res["message"] and "401" in res["detail"]
    assert not (tmp_path / "must_not_be_used.db").exists()

    assert client.get("/api/saved", headers=AUTH_A).status_code == 503
    assert client.post("/api/saved/pick3", json={"area": "성수"}, headers=AUTH_A).status_code == 503
    health = client.get("/api/health").json()
    assert health["storage"] == "supabase" and health["storage_ok"] is False


def test_supabase_mode_requires_registered_user_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    gemini_calls: list = []
    _patch_ingest_upstreams(
        monkeypatch, tmp_path,
        sb_post=lambda: _SbResp(201, [{"id": 1, "name": "몽탄", "area": "성수", "address": "성수"}]),
        sb_get=lambda: _SbResp(200, []),
        calls=calls, gemini_calls=gemini_calls,
        users={TOKEN_A: USER_A, "p3_disabled": {"id": 2, "name": "B", "daily_limit": None, "disabled": True}},
    )
    # The old shared env token is not a user token in Supabase mode.
    monkeypatch.setenv("PICK3_INGEST_TOKEN", "s3cret-token")
    files = {"image": ("a.jpg", b"x", "image/jpeg")}

    for headers in ({}, {"X-Pick3-Token": "wrong"}, {"X-Pick3-Token": "s3cret-token"}, {"X-Pick3-Token": "p3_disabled"}):
        assert client.post("/api/ingest/screenshot", files=files, headers=headers).status_code == 401
    assert client.get("/api/saved").status_code == 401
    assert client.post("/api/saved/pick3", json={"area": ""}).status_code == 401
    assert calls == [] and gemini_calls == []  # rejected before Gemini/Supabase data are touched

    ok = client.post("/api/ingest/screenshot", files=files, headers=AUTH_A)
    assert ok.status_code == 200 and ok.json()["status"] == "saved"
    assert client.get("/api/me", headers=AUTH_A).json() == {"name": "A", "auth_mode": "per_user"}
    health = client.get("/api/health").json()
    assert health["auth_mode"] == "per_user" and health["ingest_auth_configured"] is True


def test_saved_list_is_filtered_by_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    _patch_ingest_upstreams(
        monkeypatch, tmp_path,
        sb_post=lambda: _SbResp(201, []),
        sb_get=lambda: _SbResp(200, [{"id": 5, "name": "몽탄", "area": "성수", "address": "성수", "lat": None}]),
        calls=calls,
        users={TOKEN_A: USER_A, "p3_b": {"id": 2, "name": "B", "daily_limit": None, "disabled": False}},
    )
    body = client.get("/api/saved", headers={"X-Pick3-Token": "p3_b"}).json()
    assert body["user"] == "B" and body["count"] == 1
    assert calls[-1][2]["params"]["user_id"] == "eq.2"
    client.post("/api/saved/pick3", json={"area": "성수"}, headers=AUTH_A)
    assert calls[-1][2]["params"]["user_id"] == "eq.1"


def test_daily_limits_stop_before_gemini(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    gemini_calls: list = []
    counts = {"user_count": 31, "total_count": 31}
    _patch_ingest_upstreams(
        monkeypatch, tmp_path,
        sb_post=lambda: _SbResp(201, [{"id": 1, "name": "몽탄", "area": "성수", "address": "성수"}]),
        sb_get=lambda: _SbResp(200, []),
        calls=calls, gemini_calls=gemini_calls,
        usage=lambda: _SbResp(200, dict(counts)),
    )
    monkeypatch.delenv("PICK3_DAILY_LIMIT_PER_USER", raising=False)
    monkeypatch.delenv("PICK3_DAILY_LIMIT_TOTAL", raising=False)
    files = {"image": ("a.jpg", b"x", "image/jpeg")}
    res = client.post("/api/ingest/screenshot", files=files, headers=AUTH_A).json()
    assert res["status"] == "limited" and "30건" in res["message"]

    counts.update(user_count=2, total_count=301)
    res = client.post("/api/ingest/screenshot", files=files, headers=AUTH_A).json()
    assert res["status"] == "limited" and "전체" in res["message"]
    assert gemini_calls == [] and calls == []

    counts.update(user_count=2, total_count=2)
    assert client.post("/api/ingest/screenshot", files=files, headers=AUTH_A).json()["status"] == "saved"


def test_local_mode_shared_env_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.setenv("PICK3_SQLITE_PATH", str(tmp_path / "local.db"))
    assert client.get("/api/health").json()["auth_mode"] == "open"
    monkeypatch.setenv("PICK3_INGEST_TOKEN", "s3cret-token")
    assert client.get("/api/saved").status_code == 401
    assert client.get("/api/saved", headers={"X-Pick3-Token": "s3cret-token"}).status_code == 200
    assert client.get("/api/health").json()["auth_mode"] == "shared_token"


def test_ingest_reports_missing_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)  # local .env may configure Supabase
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    res = client.post("/api/ingest/screenshot", files={"image": ("a.jpg", b"x", "image/jpeg")})
    assert res.status_code == 200
    assert res.json()["status"] == "error" and "GEMINI_API_KEY" in res.json()["message"]
    assert client.get("/api/health").json()["gemini_key_configured"] is False


def test_gemini_model_env_override_and_key_in_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    seen: dict = {}
    _patch_ingest_upstreams(
        monkeypatch, tmp_path,
        sb_post=lambda: _SbResp(201, [{"id": 1, "name": "몽탄", "area": "성수", "address": "성수"}]),
        sb_get=lambda: _SbResp(200, []),
        calls=calls,
    )
    import httpx
    fake = httpx.AsyncClient
    orig_post = fake.post

    async def spy_post(self, url, **kwargs):
        if "generativelanguage" in url:
            seen["url"], seen["headers"] = url, kwargs.get("headers", {})
        return await orig_post(self, url, **kwargs)

    monkeypatch.setattr(fake, "post", spy_post)
    monkeypatch.delenv("PICK3_GEMINI_MODEL", raising=False)
    assert client.get("/api/health").json()["gemini_model"] == "gemini-3.5-flash-lite"

    monkeypatch.setenv("PICK3_GEMINI_MODEL", "gemini-3.8-flash")
    client.post("/api/ingest/screenshot", files={"image": ("a.jpg", b"x", "image/jpeg")}, headers=AUTH_A)
    assert "/models/gemini-3.8-flash:generateContent" in seen["url"]
    assert "key=" not in seen["url"] and seen["headers"]["x-goog-api-key"] == "fake_gemini"



def _kakao_doc(name: str, group: str = "FD6", addr: str = "서울 어딘가 1") -> dict:
    return {"place_name": name, "category_group_code": group, "road_address_name": addr,
            "x": "127.0", "y": "37.5", "phone": "", "place_url": "http://place.map.kakao.com/1"}


@pytest.mark.parametrize(
    "extracted,place_name,expected",
    [
        ("선릉 마구로동우니도", "마구로동우니도 선릉점", True),   # area prefix vs branch suffix
        ("마구로동우니도", "마구로동우니도 선릉점", True),
        ("Blue Bottle", "블루보틀 성수", False),                 # different script: honest miss
        ("몽탄", "몽중헌 성수점", False),
        ("카페", "카페 노티드 청담", False),                     # too generic? contained -> see below
    ],
)
def test_name_matches(extracted: str, place_name: str, expected: bool) -> None:
    from saved_places import _name_matches

    if extracted == "카페":
        # generic words are contained in many names; containment accepts them, so only the
        # Kakao query (area + name) and the ambiguity rule protect us here.
        assert _name_matches(extracted, place_name) is True
        return
    assert _name_matches(extracted, place_name) is expected


def test_pick_kakao_doc_cafe_mismatch_and_ambiguity() -> None:
    from saved_places import _pick_kakao_doc

    cafe = _pick_kakao_doc({"name": "어니언", "area": "성수"}, [_kakao_doc("어니언 성수", "CE7")])
    assert cafe and cafe["place_name"] == "어니언 성수"  # cafes (CE7) now resolve

    assert _pick_kakao_doc({"name": "어니언", "area": "성수"}, [_kakao_doc("어니언 주차장", "PK6")]) is None
    assert _pick_kakao_doc({"name": "몽탄", "area": "성수"}, [_kakao_doc("우감만족")]) is None  # no blind first hit

    two = [_kakao_doc("마구로동우니도 선릉점"), _kakao_doc("마구로동우니도 강남점")]
    assert _pick_kakao_doc({"name": "마구로동우니도", "area": ""}, two) is None  # ambiguous branch
    assert _pick_kakao_doc({"name": "마구로동우니도", "area": "선릉"}, two)["place_name"] == "마구로동우니도 선릉점"


def test_ingest_unverified_place_is_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setenv("PICK3_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("KAKAO_REST_API_KEY", "k")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    gem = _SbResp(200, {"candidates": [{"content": {"parts": [{"text": '{"name": "몽탄", "area": "성수"}'}]}}]})
    gem.raise_for_status = lambda: None

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            pass

        async def post(self, url, **kwargs):
            return gem

        async def get(self, url, **kwargs):
            assert "category_group_code" not in kwargs["params"]  # one call covers FD6 + CE7
            return _SbResp(200, {"documents": [_kakao_doc("우감만족", addr="서울 강남구 압구정로54길 26")]})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    body = client.post("/api/ingest/screenshot", files={"image": ("a.jpg", b"x", "image/jpeg")}).json()
    assert body["status"] == "saved" and body["verified"] is False
    assert "위치 확인 필요" in body["message"]
    assert body["place"]["name"] == "몽탄" and body["place"]["lat"] is None  # not replaced by 우감만족

    recall = client.post("/api/saved/pick3", json={"area": "성수"}).json()
    assert recall["top_pick"]["role_badge"] == "내 저장 · 위치 확인 필요"
