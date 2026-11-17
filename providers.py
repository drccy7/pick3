"""Data providers and situation-based ranking engine for Pick3."""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import asdict, dataclass, replace
from typing import Literal
from urllib.parse import quote

import httpx

SituationType = Literal["solo", "date", "group"]
ProviderType = Literal["auto", "google", "kakao", "naver"]

_CACHE_TTL_SECONDS = 6 * 3600  # 6 hours to prevent redundant paid API calls
_CACHE_MAX_ENTRIES = 500
_RECOMMEND_CACHE: dict[tuple[str, str, str, str], tuple[float, dict]] = {}


def clear_recommend_cache() -> None:
    """Clear in-memory recommendation cache (used in tests)."""
    _RECOMMEND_CACHE.clear()


def _get_cached_result(key: tuple[str, str, str, str]) -> dict | None:
    entry = _RECOMMEND_CACHE.get(key)
    if not entry:
        return None
    expires_at, data = entry
    if time.monotonic() > expires_at:
        _RECOMMEND_CACHE.pop(key, None)
        return None
    return {**data, "cached": True}


def _set_cached_result(key: tuple[str, str, str, str], data: dict) -> None:
    if len(_RECOMMEND_CACHE) >= _CACHE_MAX_ENTRIES:
        # Evict oldest inserted entry to keep memory bounded
        oldest_key = next(iter(_RECOMMEND_CACHE))
        _RECOMMEND_CACHE.pop(oldest_key, None)
    _RECOMMEND_CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, data)

_LAST_KAKAO_DIAG: dict[str, object] = {"status": None, "error": None}
_LAST_GOOGLE_DIAG: dict[str, object] = {"status": None, "error": None}
_LAST_NAVER_DIAG: dict[str, object] = {"status": None, "error": None}


def get_kakao_diag() -> dict[str, object]:
    """Return non-secret external API status diagnostics."""
    kakao_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    google_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    return {
        "google_key_configured": bool(google_key),
        "google_last_status": _LAST_GOOGLE_DIAG["status"],
        "google_last_error": _LAST_GOOGLE_DIAG["error"],
        "kakao_key_configured": bool(kakao_key),
        "last_status": _LAST_KAKAO_DIAG["status"],
        "last_error": _LAST_KAKAO_DIAG["error"],
        "naver_key_configured": bool(naver_id and naver_secret),
        "naver_last_status": _LAST_NAVER_DIAG["status"],
        "naver_last_error": _LAST_NAVER_DIAG["error"],
    }


@dataclass(frozen=True)
class RestaurantPick:
    """Single recommended restaurant card."""

    name: str
    category: str
    area: str
    reason: str
    price_note: str
    service_tags: list[str]
    open_status: str
    naver_map_url: str
    kakao_map_url: str
    google_maps_url: str = ""
    rating: float | None = None
    review_count: int | None = None
    is_verified_detail: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _build_map_urls(name: str, area: str) -> tuple[str, str]:
    q = quote(f"{area} {name}".strip())
    return (
        f"https://map.naver.com/p/search/{q}",
        f"https://map.kakao.com/link/search/{q}",
    )


# Curated realistic sample dataset for instant zero-API-key MVP verification.
_SAMPLE_DB: dict[str, dict[SituationType, list[dict]]] = {
    "돼지갈비": {
        "solo": [
            {
                "name": "명륜진사갈비 역삼점",
                "category": "돼지갈비",
                "area": "역삼/강남",
                "reason": "1인분 주문 부담 없고 점심 시간대 웨이팅 거의 없음",
                "price_note": "100g당 약 5,500원 (점심 특선 11,000원)",
                "service_tags": ["혼밥 가능", "점심 특선", "빠른 회전"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "청기와타운 선릉점",
                "category": "돼지갈비",
                "area": "선릉",
                "reason": "1인 정식 메뉴가 따로 있어 혼자서도 눈치 안 보고 식사 가능",
                "price_note": "갈비 정식 13,000원",
                "service_tags": ["1인 정식", "깔끔한 좌석", "역세권"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "돈수백 돼지갈비백반",
                "category": "한식/갈비",
                "area": "강남",
                "reason": "구워져서 나오는 1인 갈비백반으로 15분 컷 점심 가능",
                "price_note": "1인 백반 12,000원",
                "service_tags": ["완조리 제공", "혼밥 특화", "옷 냄새 적음"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "date": [
            {
                "name": "몽탄 성수직영점",
                "category": "우대갈비/돼지갈비",
                "area": "성수",
                "reason": "직원이 처음부터 끝까지 구워줘서 대화에 집중하기 좋음",
                "price_note": "100g당 약 9,200원",
                "service_tags": ["직원 그릴링", "인테리어 우수", "연기 적음"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "화포식당 가로수길점",
                "category": "숙성 돼지갈비",
                "area": "신사",
                "reason": "전 좌석 환기 덕트가 강하고 직원이 직접 컷팅까지 해줌",
                "price_note": "100g당 약 8,500원",
                "service_tags": ["직원 구이", "쾌적한 환기", "예약 가능"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "남영돈 숯불갈비",
                "category": "참숯 돼지갈비",
                "area": "숙대입구",
                "reason": "초벌 후 테이블에서 마무리 구이해 주어 굽기 실패 없음",
                "price_note": "100g당 약 7,800원",
                "service_tags": ["초벌 제공", "분위기 좋음", "사이드 맛집"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "group": [
            {
                "name": "강강술래 역삼점",
                "category": "한돈 돼지갈비",
                "area": "역삼",
                "reason": "프라이빗 룸 완비 + 발렛 주차 가능해 6인 이상 회식에 최적",
                "price_note": "100g당 약 7,500원",
                "service_tags": ["단체 룸 완비", "주차/발렛", "직원 구이"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "송추가마골 인 어반",
                "category": "갈비 전문",
                "area": "광화문/종로",
                "reason": "대형 홀과 12인석 룸 분리로 소음 걱정 없이 회식 진행",
                "price_note": "100g당 약 8,200원",
                "service_tags": ["대형 룸", "예약 확정", "넓은 테이블"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "마포진짜원조최대포",
                "category": "돼지갈비",
                "area": "공덕/마포",
                "reason": "가성비 좋고 단체석 회전이 빨라 2차 이동까지 동선 편리",
                "price_note": "100g당 약 5,500원",
                "service_tags": ["가성비 1위", "단체석 다수", "참숯"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
    },
    "삼겹살": {
        "solo": [
            {
                "name": "혼고집 1인 삼겹",
                "category": "1인 고기",
                "area": "강남",
                "reason": "1인 전용 미니 불판으로 눈치 없이 삼겹살 1인분 가능",
                "price_note": "1인 세트 13,900원",
                "service_tags": ["1인 화로", "혼밥 1위", "공기밥 포함"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "싸움의고수",
                "category": "보쌈/삼겹 도시락",
                "area": "주요 역세권",
                "reason": "1인 바 테이블 좌석 중심, 주문 후 5분 내 제공",
                "price_note": "1인 정식 9,800원",
                "service_tags": ["1인 좌석", "초고속 서빙", "가성비"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "고반식당",
                "category": "숙성 삼겹",
                "area": "역삼",
                "reason": "비혼잡 시간대 1인 2인분 주문 시 직원이 직접 구워줌",
                "price_note": "100g당 약 9,000원",
                "service_tags": ["직원 구이", "김치찌개 서비스", "깔끔함"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "date": [
            {
                "name": "금돼지식당",
                "category": "본삼겹",
                "area": "약수",
                "reason": "전담 직원이 최적 온도로 구워주며 사이드 페어링이 훌륭함",
                "price_note": "100g당 약 11,000원",
                "service_tags": ["직원 그릴링", "미쉐린 빕구르망", "감성 외관"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "꿉당",
                "category": "목살/삼겹",
                "area": "신사/성수",
                "reason": "고기를 직접 구울 필요 없고 고쿠미 밥 조합이 데이트에 인기",
                "price_note": "100g당 약 10,500원",
                "service_tags": ["풀 그릴링", "코쿠미 밥", "바 좌석"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "숙달돼지",
                "category": "습식숙성 삼겹",
                "area": "강남/홍대",
                "reason": "와인 콜키지 가능하고 테이블 간격이 넓어 조용히 대화 가능",
                "price_note": "100g당 약 9,500원",
                "service_tags": ["직원 구이", "콜키지 가능", "구워먹는 치즈"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "group": [
            {
                "name": "하남돼지집",
                "category": "초벌 삼겹살",
                "area": "주요 상권",
                "reason": "초벌 후 직원이 빠르게 컷팅해줘서 다인원 회식 흐름이 안 끊김",
                "price_note": "100g당 약 8,800원",
                "service_tags": ["초벌+직원구이", "단체 예약", "넓은 홀"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "새마을식당",
                "category": "열탄불고기/삼겹",
                "area": "주요 상권",
                "reason": "인원수 많아도 예산 부담 적고 7분돼지김치로 마무리가 깔끔",
                "price_note": "100g당 약 6,500원",
                "service_tags": ["가성비 회식", "넓은 좌석", "빠른 추가주문"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "도드람 본래",
                "category": "한돈 삼겹",
                "area": "방이/잠실",
                "reason": "룸 좌석과 주차 공간이 넉넉해 팀 회식 예약에 안정적",
                "price_note": "100g당 약 8,500원",
                "service_tags": ["룸 보유", "주차 가능", "쾌적한 환기"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
    },
    "국밥": {
        "solo": [
            {
                "name": "농민백암순대",
                "category": "순대국밥",
                "area": "선릉/강남",
                "reason": "1인 회전이 매우 빠르고 잡내 없는 진한 국물로 혼밥 만족도 최상",
                "price_note": "국밥 10,000원",
                "service_tags": ["혼밥 성지", "빠른 회전", "진한 육수"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "옥동식",
                "category": "돼지곰탕",
                "area": "합정/서교",
                "reason": "전 좌석 바 테이블 구조라 1인 방문이 기본인 깔끔한 식사",
                "price_note": "보통 10,000원",
                "service_tags": ["1인 바테이블", "맑은 국물", "미쉐린"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "광화문국밥",
                "category": "돼지국밥",
                "area": "시청/광화문",
                "reason": "깔끔한 토렴식 국밥과 반찬 1인 세팅으로 점심 혼밥 최적",
                "price_note": "국밥 9,500원",
                "service_tags": ["깔끔한 1인상", "빠른 서빙", "조용함"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "date": [
            {
                "name": "안암",
                "category": "모던 돼지국밥",
                "area": "안국",
                "reason": "국밥집 같지 않은 세련된 공간과 수비드 고기 비주얼",
                "price_note": "국밥 11,000원",
                "service_tags": ["감성 인테리어", "깔끔한 맑은국", "데이트 OK"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "하동관",
                "category": "한우 곰탕",
                "area": "명동/여의도",
                "reason": "오랜 전통의 깔끔한 맛으로 주말 낮 가벼운 데이트 코스에 적합",
                "price_note": "보통 16,000원",
                "service_tags": ["전통 노포", "깔끔한 맛", "빠른 식사"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "엄용백 돼지국밥",
                "category": "부산식 돼지국밥",
                "area": "인사동/종각",
                "reason": "정갈한 트레이 세팅과 맑은/진한 육수 선택 가능",
                "price_note": "국밥 11,000원",
                "service_tags": ["정갈한 상차림", "육수 선택", "냄새 적음"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "group": [
            {
                "name": "청진옥",
                "category": "해장국/국밥",
                "area": "종각",
                "reason": "좌석 수가 많고 수육·모둠전과 함께 4~8인 식사 및 반주 가능",
                "price_note": "해장국 12,000원",
                "service_tags": ["대형 홀", "안주 메뉴", "아침/야간 가능"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "진주집",
                "category": "꼬리곰탕/설렁탕",
                "area": "남대문/여의도",
                "reason": "어르신·직장인 모두 호불호 없는 메뉴와 넓은 테이블 보유",
                "price_note": "설렁탕 12,000원",
                "service_tags": ["호불호 없음", "단체 수용", "빠른 서빙"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "신의주찹쌀순대",
                "category": "순대국/전골",
                "area": "주요 상권",
                "reason": "국밥과 순대곱창전골을 섞어 주문할 수 있어 다인원 식사에 유리",
                "price_note": "국밥 10,000원",
                "service_tags": ["전골+국밥", "넓은 좌석", "가성비"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
    },
    "파스타": {
        "solo": [
            {
                "name": "롤링파스타",
                "category": "캐주얼 파스타",
                "area": "주요 역세권",
                "reason": "1인 테이블 다수 + 가격 부담 없어 평일 점심 혼밥에 최적",
                "price_note": "파스타 7,900~9,900원",
                "service_tags": ["1만원 이하", "혼밥 환영", "빠른 조리"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "라구식당",
                "category": "생면 라구 파스타",
                "area": "안암/신촌",
                "reason": "바 좌석이 있어 혼자 가도 자연스럽고 1인 세트 구성이 알참",
                "price_note": "파스타 12,500원",
                "service_tags": ["바 좌석", "가성비", "단일 인기메뉴"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "어거스트힐",
                "category": "파스타/샐러드",
                "area": "강남",
                "reason": "조용한 창가 1인석에서 여유롭게 점심 식사 가능",
                "price_note": "파스타 14,000원대",
                "service_tags": ["창가 좌석", "조용함", "브레이크 확인"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "date": [
            {
                "name": "도우룸",
                "category": "생면 파스타",
                "area": "서래마을",
                "reason": "어두운 조명과 수준 높은 생면 파스타로 기념일·데이트 만족도 높음",
                "price_note": "파스타 24,000~29,000원",
                "service_tags": ["분위기 1위", "와인 페어링", "예약 권장"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "오스테리아 오르조",
                "category": "이탈리안",
                "area": "한남동",
                "reason": "화이트 트러플 라구 파스타가 시그니처이며 서비스 응대가 섬세함",
                "price_note": "파스타 25,000원대",
                "service_tags": ["데이트 성지", "발렛 가능", "캐치테이블"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "바위파스타바",
                "category": "파스타 오마카세/바",
                "area": "성수",
                "reason": "셰프 조리 과정을 바로 보며 대화할 수 있는 2인 바 좌석",
                "price_note": "파스타 22,000원대",
                "service_tags": ["오픈키친 바", "특별한 날", "사전 예약"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "group": [
            {
                "name": "매드포갈릭",
                "category": "이탈리안/스테이크",
                "area": "주요 상권",
                "reason": "6~10인 단체 테이블과 세트 할인이 잘 되어 있어 회식/모임 편리",
                "price_note": "1인 약 25,000~35,000원",
                "service_tags": ["대형 테이블", "주차 지원", "세트 할인"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "세상의모든아침",
                "category": "브런치/파스타",
                "area": "여의도/광교",
                "reason": "넓은 홀과 뷰 덕분에 다인원 모임이나 팀 점심 회식에 적합",
                "price_note": "파스타 22,000원대",
                "service_tags": ["대형 홀", "예약 가능", "뷰 맛집"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "일프리모",
                "category": "이탈리안 비스트로",
                "area": "강남/선릉",
                "reason": "파스타·리조또·피자를 쉐어하기 좋고 단체 룸 예약 가능",
                "price_note": "파스타 18,000원대",
                "service_tags": ["쉐어 메뉴", "단체석", "와인 가능"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
    },
    "소바": {
        "solo": [
            {
                "name": "미진 광화문본점",
                "category": "메밀소바",
                "area": "광화문/종각",
                "reason": "회전이 빠르고 1인 판모밀 주문이 기본이라 점심 혼밥에 최적",
                "price_note": "냉메밀 11,000원",
                "service_tags": ["혼밥 환영", "빠른 회전", "노포 맛집"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "송옥 남대문본점",
                "category": "판모밀/우동",
                "area": "시청/북창동",
                "reason": "혼자 방문하는 직장인이 많고 주문 후 빠르게 제공됨",
                "price_note": "판모밀 10,000원",
                "service_tags": ["직장인 점심", "1인 테이블", "가성비"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "오비야 강남점",
                "category": "자가제면 소바",
                "area": "강남/역삼",
                "reason": "바 좌석이 있어 혼자 조용히 자가제면 소바를 즐기기 좋음",
                "price_note": "자루소바 11,000원",
                "service_tags": ["바 좌석", "자가제면", "깔끔함"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "date": [
            {
                "name": "스바루",
                "category": "수타 소바",
                "area": "이수/방배",
                "reason": "차분한 일식 공간에서 수준 높은 수타 소바와 튀김을 함께 즐기기 좋음",
                "price_note": "자루소바 13,000원",
                "service_tags": ["수타면", "조용한 분위기", "튀김 추천"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "미타니야",
                "category": "일식/소바",
                "area": "동부이촌/여의도",
                "reason": "정갈한 상차림과 깔끔한 좌석 간격으로 대화하기 편함",
                "price_note": "소바 정식 16,000원대",
                "service_tags": ["정갈한 정식", "데이트 OK", "단정한 공간"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "소바야 린",
                "category": "일식 소바",
                "area": "서래마을/반포",
                "reason": "깔끔한 플레이팅과 다양한 토핑 소바로 취향 맞춰 주문 가능",
                "price_note": "냉소바 13,000원대",
                "service_tags": ["분위기 좋음", "깔끔한 육수", "사이드 다양"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
        "group": [
            {
                "name": "메밀공방",
                "category": "메밀소바/막국수",
                "area": "여의도/강남",
                "reason": "좌석이 넓고 수육·만두 등 사이드와 함께 4~6인 식사 가능",
                "price_note": "소바 10,000원대",
                "service_tags": ["넓은 홀", "쉐어 사이드", "점심 모임"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "청수모밀",
                "category": "메밀/일식",
                "area": "여의도",
                "reason": "테이블 회전이 빠르고 다인원 점심 회식 수용이 가능",
                "price_note": "판모밀 11,000원",
                "service_tags": ["단체 점심", "빠른 서빙", "호불호 적음"],
                "open_status": "영업 상태 지도 확인",
            },
            {
                "name": "유림면",
                "category": "메밀국수/냄비우동",
                "area": "시청",
                "reason": "소바·비빔메밀·우동 등 메뉴 선택폭이 넓어 여럿이 가기 무난함",
                "price_note": "메밀국수 10,000원",
                "service_tags": ["메뉴 다양", "가성비", "역세권"],
                "open_status": "영업 상태 지도 확인",
            },
        ],
    },
}

_SITUATION_META: dict[SituationType, dict] = {
    "solo": {
        "label": "혼자 / 점심",
        "criteria": "1인 주문 가능 · 빠른 회전 · 웨이팅 최소",
    },
    "date": {
        "label": "둘이서 / 데이트",
        "criteria": "분위기 · 직원 구이/응대 · 대화하기 좋은 환경",
    },
    "group": {
        "label": "여럿이 / 회식",
        "criteria": "단체석/룸 · 주차/예약 · 예산 및 쉐어 효율",
    },
}


def _build_dynamic_fallback(
    query: str, area: str, situation: SituationType
) -> list[RestaurantPick]:
    """Build honest, non-hallucinated 3-pick guidance for any custom menu query."""
    area_label = area if area else "내 주변"
    sit_meta = _SITUATION_META[situation]
    variants = [
        (
            f"{area_label} {query} 가성비 1순위 후보",
            f"'{sit_meta['label']}' 조건 기준으로 가격 대비 만족도가 높은 후보를 지도에서 바로 확인하세요.",
            ["가성비 우선", sit_meta["label"], "지도 실리뷰 확인"],
            f"{area_label} {query} 가성비",
        ),
        (
            f"{area_label} {query} 평점/리뷰 상위 후보",
            "방문자 리뷰 수가 많고 실패 확률이 낮은 안정적 대안입니다.",
            ["리뷰 검증", "대안 1", "영업시간 확인"],
            f"{area_label} {query} 맛집",
        ),
        (
            f"{area_label} {query} 쾌적/편의 우선 후보",
            "좌석 여유, 주차, 예약 가능 여부를 기준으로 비교할 수 있는 대안입니다.",
            ["좌석/주차", "대안 2", "웨이팅 체크"],
            f"{area_label} {query} 예약",
        ),
    ]
    picks: list[RestaurantPick] = []
    for title, reason, tags, search_term in variants:
        naver_url, kakao_url = _build_map_urls(search_term, "")
        picks.append(
            RestaurantPick(
                name=title,
                category=query,
                area=area_label,
                reason=reason,
                price_note="상세 가격 정보는 지도 링크에서 확인",
                service_tags=tags,
                open_status="실시간 영업 상태 지도 확인",
                naver_map_url=naver_url,
                kakao_map_url=kakao_url,
                is_verified_detail=False,
            )
        )
    return picks


_SITUATION_KEYWORDS: dict[SituationType, list[str]] = {
    "solo": ["혼자", "혼밥", "1인", "점심", "빠르", "간단", "바테이블", "바 좌석"],
    "date": ["구워", "분위기", "데이트", "친절", "깔끔", "인테리어", "조용"],
    "group": ["룸", "단체", "회식", "주차", "넓", "모임", "예약"],
}

_KAKAO_SITUATION_HINT: dict[SituationType, str] = {
    "solo": "혼밥",
    "date": "데이트",
    "group": "룸",
}


_CLOSED_PATTERNS = ("폐업", "영업종료", "영업 종료", "폐점", "문 닫았", "없어졌")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text).replace("&quot;", '"').replace("&amp;", "&").strip()


async def _naver_get(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict[str, object],
    client_id: str,
    client_secret: str,
) -> httpx.Response:
    """Call NAVER API HUB (NCP keys) or legacy openapi.naver.com."""
    ncp_url = f"https://naverapihub.apigw.ntruss.com/search/v1/{endpoint}"
    ncp_headers = {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
    }
    ncp_params = {**params, "format": "json"}
    resp = await client.get(ncp_url, headers=ncp_headers, params=ncp_params)
    if resp.status_code == 200:
        return resp

    # Legacy Naver Developers Client IDs are typically 20 chars; NCP keys are ~10 chars.
    if len(client_id) >= 18:
        legacy_url = f"https://openapi.naver.com/v1/search/{endpoint}.json"
        legacy_headers = {
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        }
        return await client.get(legacy_url, headers=legacy_headers, params=params)
    return resp


async def _fetch_naver_blog_signals(
    name: str, area: str, situation: SituationType, client_id: str, client_secret: str
) -> tuple[bool, list[str]]:
    """Check Naver Blog snippets for closed-restaurant signals and situation highlights."""
    q = f"{area} {name}".strip() if area and area not in name else name
    try:
        async with httpx.AsyncClient(timeout=3.5) as client:
            resp = await _naver_get(
                client,
                "blog",
                {"query": q, "display": 5, "sort": "date"},
                client_id,
                client_secret,
            )
            _LAST_NAVER_DIAG["status"] = resp.status_code
            if resp.status_code != 200:
                _LAST_NAVER_DIAG["error"] = resp.text[:200]
                return False, []
            _LAST_NAVER_DIAG["error"] = None
            items = resp.json().get("items", [])
    except Exception as exc:
        _LAST_NAVER_DIAG["status"] = -1
        _LAST_NAVER_DIAG["error"] = str(exc)[:200]
        return False, []

    if not items:
        return False, []

    combined = " ".join(
        f"{_strip_html(it.get('title', ''))} {_strip_html(it.get('description', ''))}"
        for it in items
    )
    closed_hits = sum(1 for pat in _CLOSED_PATTERNS if pat in combined)
    is_likely_closed = closed_hits >= 2 or ("폐업" in combined and "맛집" not in combined)

    keywords = _SITUATION_KEYWORDS[situation] + ["가성비", "주차", "웨이팅"]
    matched = [kw for kw in keywords if kw in combined]
    return is_likely_closed, matched[:3]


_SITUATION_COPY: dict[SituationType, str] = {
    "solo": "혼자 부담 없이 빠르게 식사하기 좋은 곳",
    "date": "둘이서 편하게 식사·대화하기 좋은 곳",
    "group": "여럿이 함께 모이기 좋은 곳",
}

_KEYWORD_PHRASES: dict[str, str] = {
    "혼자": "혼밥 편함",
    "혼밥": "혼밥 추천",
    "1인": "1인 식사 가능",
    "점심": "점심 식사 인기",
    "빠르": "회전 빠름",
    "간단": "간편 식사",
    "바테이블": "바 좌석 보유",
    "바 좌석": "바 좌석 보유",
    "구워": "직원 구이",
    "분위기": "분위기 좋음",
    "데이트": "데이트 인기",
    "친절": "응대 친절",
    "깔끔": "매장 깔끔함",
    "인테리어": "인테리어 우수",
    "조용": "조용한 분위기",
    "룸": "룸 좌석 있음",
    "단체": "단체석 가능",
    "회식": "회식 추천",
    "주차": "주차 가능",
    "넓": "좌석 여유",
    "모임": "모임 적합",
    "예약": "예약 가능",
    "가성비": "가성비 좋음",
    "웨이팅": "웨이팅 참고",
}


def _humanize_keywords(keywords: list[str]) -> list[str]:
    phrases: list[str] = []
    for kw in keywords:
        label = _KEYWORD_PHRASES.get(kw, kw)
        if label not in phrases:
            phrases.append(label)
    return phrases


async def _enrich_and_filter_with_naver(
    picks: list[RestaurantPick], area: str, situation: SituationType
) -> list[RestaurantPick]:
    """Filter out closed restaurants and enrich picks with Naver Blog signals when keys exist."""
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    if not (naver_id and naver_secret) or not picks:
        return picks[:3]

    candidates = picks[:6]
    results = await asyncio.gather(
        *[
            _fetch_naver_blog_signals(p.name, area, situation, naver_id, naver_secret)
            for p in candidates
        ],
        return_exceptions=True,
    )

    alive: list[RestaurantPick] = []
    closed_backup: list[RestaurantPick] = []
    for pick, res in zip(candidates, results):
        if isinstance(res, Exception):
            alive.append(pick)
            continue
        is_closed, matched_kw = res
        updated = pick
        if matched_kw:
            phrases = _humanize_keywords(matched_kw)[:2]
            new_tags = list(dict.fromkeys([*pick.service_tags, *phrases]))[:4]
            base_reason = _SITUATION_COPY[situation] if not pick.rating else pick.reason
            new_reason = f"{base_reason} · {', '.join(phrases)}"
            updated = replace(pick, reason=new_reason, service_tags=new_tags)
        if is_closed:
            closed_backup.append(replace(updated, open_status="⚠️ 최근 폐업 의심(확인 필요)"))
        else:
            alive.append(updated)

    final_picks = alive[:3]
    if len(final_picks) < 3:
        final_picks.extend(closed_backup[: 3 - len(final_picks)])
    return final_picks


async def _fetch_naver_local(
    query: str, area: str, situation: SituationType, client_id: str, client_secret: str
) -> list[RestaurantPick] | None:
    """Query Naver Local Search API (returns up to 5 local results)."""
    hint = _KAKAO_SITUATION_HINT[situation]
    keyword = f"{area} {query}".strip() if area else query
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await _naver_get(
                client,
                "local",
                {"query": f"{keyword} {hint}", "display": 5, "sort": "comment"},
                client_id,
                client_secret,
            )
            _LAST_NAVER_DIAG["status"] = resp.status_code
            if resp.status_code != 200:
                _LAST_NAVER_DIAG["error"] = resp.text[:200]
                return None
            items = resp.json().get("items", [])
            if len(items) < 3:
                resp2 = await _naver_get(
                    client,
                    "local",
                    {"query": keyword, "display": 5, "sort": "comment"},
                    client_id,
                    client_secret,
                )
                if resp2.status_code == 200:
                    seen = {it.get("title") for it in items}
                    for it in resp2.json().get("items", []):
                        if it.get("title") not in seen:
                            items.append(it)
            _LAST_NAVER_DIAG["error"] = None
    except Exception as exc:
        _LAST_NAVER_DIAG["status"] = -1
        _LAST_NAVER_DIAG["error"] = str(exc)[:200]
        return None

    if not items:
        return None

    sit_label = _SITUATION_META[situation]["label"]
    picks: list[RestaurantPick] = []
    for item in items[:5]:
        name = _strip_html(item.get("title", "상호 미상"))
        addr = item.get("roadAddress") or item.get("address") or area or "위치 확인"
        cat = (item.get("category") or query).split(">")[-1].strip() or query
        naver_url, kakao_url = _build_map_urls(name, area)
        picks.append(
            RestaurantPick(
                name=name,
                category=cat,
                area=addr,
                reason=f"{_SITUATION_COPY[situation]} (주변 인기 매장)",
                price_note="메뉴·가격은 지도에서 확인",
                service_tags=[cat, sit_label],
                open_status="영업시간 지도 확인",
                naver_map_url=naver_url,
                kakao_map_url=kakao_url,
                is_verified_detail=False,
            )
        )
    return picks


async def _fetch_google_places(
    query: str,
    area: str,
    situation: SituationType,
    api_key: str,
    lat: float | None = None,
    lng: float | None = None,
) -> list[RestaurantPick] | None:
    """Query Google Places API (New), filter closed places, and rank by situation + rating."""
    text_query = f"{area} {query}".strip() if area else query
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,places.rating,"
            "places.userRatingCount,places.googleMapsUri,places.businessStatus,"
            "places.currentOpeningHours.openNow,places.reviews.text"
        ),
    }
    body: dict[str, object] = {
        "textQuery": text_query,
        "languageCode": "ko",
        "regionCode": "KR",
        "maxResultCount": 12,
    }
    if lat is not None and lng is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": 1500.0,
            }
        }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers=headers,
                json=body,
            )
            _LAST_GOOGLE_DIAG["status"] = resp.status_code
            if resp.status_code != 200:
                _LAST_GOOGLE_DIAG["error"] = resp.text[:200]
                return None
            _LAST_GOOGLE_DIAG["error"] = None
            places = resp.json().get("places", [])
    except Exception as exc:
        _LAST_GOOGLE_DIAG["status"] = -1
        _LAST_GOOGLE_DIAG["error"] = str(exc)[:200]
        return None

    if not places:
        return None

    # Filter out permanently/temporarily closed places from Google Places status.
    open_places = [
        p
        for p in places
        if p.get("businessStatus") not in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")
    ]
    if open_places:
        places = open_places

    # Prefer places whose formattedAddress matches the requested area (prevents out-of-area
    # restaurants with the area name in their title, e.g. "본강남갈비 인천논현본점").
    area_tokens = [t for t in {area.strip(), area.strip().rstrip("역")} if t] if area else []
    if area_tokens:
        area_matched = [
            p for p in places if any(tok in (p.get("formattedAddress") or "") for tok in area_tokens)
        ]
        if len(area_matched) >= 3:
            places = area_matched

    # Prefer places with >= 10 reviews when at least 3 exist to avoid 1-review 5.0 outliers.
    reliable = [p for p in places if (p.get("userRatingCount") or 0) >= 10 and p.get("rating")]
    pool = reliable if len(reliable) >= 3 else [p for p in places if p.get("rating")] or places

    keywords = _SITUATION_KEYWORDS[situation]
    scored: list[tuple[int, int, float, int, dict, list[str]]] = []
    for p in pool:
        addr_raw = p.get("formattedAddress") or ""
        is_area_match = 1 if (not area_tokens or any(tok in addr_raw for tok in area_tokens)) else 0
        reviews_text = " ".join(
            (rv.get("text") or {}).get("text", "") for rv in p.get("reviews", [])
        )
        matched_kw = [kw for kw in keywords if kw in reviews_text]
        rating = float(p.get("rating") or 0.0)
        count = int(p.get("userRatingCount") or 0)
        scored.append((is_area_match, len(matched_kw), rating, count, p, matched_kw))

    # Sort by area match, then situation review match (>0 vs 0), then highest rating, then review count.
    scored.sort(key=lambda item: (item[0], 1 if item[1] > 0 else 0, item[2], item[3], item[1]), reverse=True)

    sit_label = _SITUATION_META[situation]["label"]
    picks: list[RestaurantPick] = []
    for _, _, rating, count, p, matched_kw in scored[:6]:
        name = (p.get("displayName") or {}).get("text") or "상호 미상"
        addr = p.get("formattedAddress") or area or "위치 확인"
        addr = addr.removeprefix("대한민국 ").strip()
        open_now = (p.get("currentOpeningHours") or {}).get("openNow")
        open_status = (
            "현재 영업 중" if open_now is True else "현재 영업 종료" if open_now is False else "영업시간 지도 확인"
        )
        naver_url, kakao_url = _build_map_urls(name, area)
        google_url = p.get("googleMapsUri") or ""

        rating_str = f"⭐ {rating:.1f} (리뷰 {count:,}개)" if rating else "평점 정보 없음"
        phrases = _humanize_keywords(matched_kw)[:2]
        if phrases:
            reason = f"⭐ {rating:.1f} ({count:,}명) · {', '.join(phrases)}"
        else:
            reason = f"⭐ {rating:.1f} ({count:,}명) · {_SITUATION_COPY[situation]}"

        tags = [f"평점 {rating:.1f}" if rating else "구글 검색", f"리뷰 {count:,}", sit_label, *phrases]

        picks.append(
            RestaurantPick(
                name=name,
                category=query,
                area=addr,
                reason=reason,
                price_note=rating_str,
                service_tags=tags[:4],
                open_status=open_status,
                naver_map_url=naver_url,
                kakao_map_url=kakao_url,
                google_maps_url=google_url,
                rating=rating or None,
                review_count=count or None,
                is_verified_detail=True,
            )
        )
    return picks


async def _fetch_kakao_local(
    query: str,
    area: str,
    situation: SituationType,
    api_key: str,
    lat: float | None = None,
    lng: float | None = None,
) -> list[RestaurantPick] | None:
    """Query Kakao Local API when KAKAO_REST_API_KEY is configured."""
    base_kw = f"{area} {query}".strip() if area else query
    hint = _KAKAO_SITUATION_HINT[situation]
    headers = {"Authorization": f"KakaoAK {api_key}"}

    docs: list[dict] = []
    seen_ids: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            # Fetch up to 6 unique candidates so closed-restaurant filtering still leaves 3.
            for kw in (f"{base_kw} {hint}", base_kw):
                params: dict[str, object] = {
                    "query": kw,
                    "category_group_code": "FD6",
                    "size": 6,
                }
                if lat is not None and lng is not None:
                    params["x"] = str(lng)
                    params["y"] = str(lat)
                    params["radius"] = 1500
                resp = await client.get(
                    "https://dapi.kakao.com/v2/local/search/keyword.json",
                    headers=headers,
                    params=params,
                )
                _LAST_KAKAO_DIAG["status"] = resp.status_code
                if resp.status_code != 200:
                    _LAST_KAKAO_DIAG["error"] = resp.text[:200]
                    return None
                _LAST_KAKAO_DIAG["error"] = None
                for item in resp.json().get("documents", []):
                    pid = item.get("id") or item.get("place_name", "")
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        docs.append(item)
                    if len(docs) >= 6:
                        break
                if len(docs) >= 6:
                    break
    except Exception as exc:
        _LAST_KAKAO_DIAG["status"] = -1
        _LAST_KAKAO_DIAG["error"] = str(exc)[:200]
        return None

    if not docs:
        return None

    sit_label = _SITUATION_META[situation]["label"]
    picks: list[RestaurantPick] = []
    for item in docs[:6]:
        name = item.get("place_name", "상호 미상")
        addr = item.get("road_address_name") or item.get("address_name") or area or "위치 확인"
        cat = item.get("category_name", query).split(">")[-1].strip() or query
        naver_url, _ = _build_map_urls(name, area)
        kakao_url = item.get("place_url") or _build_map_urls(name, area)[1]
        picks.append(
            RestaurantPick(
                name=name,
                category=cat,
                area=addr,
                reason=f"{_SITUATION_COPY[situation]} (주변 인기 매장)",
                price_note="메뉴·가격은 지도에서 확인",
                service_tags=[cat, sit_label],
                open_status="영업시간 지도 확인",
                naver_map_url=naver_url,
                kakao_map_url=kakao_url,
                is_verified_detail=False,
            )
        )
    return picks


async def get_recommendations(
    query: str,
    situation: SituationType,
    area: str = "",
    provider: ProviderType = "kakao",
    lat: float | None = None,
    lng: float | None = None,
) -> dict:
    """Return exactly 1 top pick + up to 2 alternatives (max 3 total) with 6h TTL cache."""
    clean_query = query.strip()
    clean_area = area.strip()
    geo_key = f"{round(lat, 3)},{round(lng, 3)}" if (lat is not None and lng is not None) else ""
    cache_key = (provider, clean_query.lower(), f"{clean_area.lower()}@{geo_key}", situation)

    cached = _get_cached_result(cache_key)
    if cached is not None:
        return cached

    # Cost-saving priority: Kakao Local (100k/day free) is default for "kakao" and "auto".
    # Google Places (paid/credit-limited) runs when user explicitly picks "google" or as fallback.
    kakao_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    google_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    order = (
        ("google", "kakao", "naver")
        if provider == "google"
        else ("naver", "kakao")
        if provider == "naver"
        else ("kakao",)
        if provider == "kakao"
        else ("kakao", "naver", "google")
    )

    for engine in order:
        if engine == "kakao" and kakao_key:
            live_picks = await _fetch_kakao_local(
                clean_query, clean_area, situation, kakao_key, lat=lat, lng=lng
            )
            if live_picks:
                final_picks = await _enrich_and_filter_with_naver(live_picks, clean_area, situation)
                result = {
                    "query": clean_query,
                    "area": clean_area or ("내 위치 주변" if lat is not None else "내 주변"),
                    "situation": situation,
                    "situation_label": _SITUATION_META[situation]["label"],
                    "criteria": _SITUATION_META[situation]["criteria"],
                    "data_source": "kakao_local_api",
                    "cached": False,
                    "top_pick": final_picks[0].to_dict(),
                    "alternatives": [p.to_dict() for p in final_picks[1:3]],
                }
                _set_cached_result(cache_key, result)
                return result

        if engine == "naver" and naver_id and naver_secret:
            naver_picks = await _fetch_naver_local(
                clean_query, clean_area, situation, naver_id, naver_secret
            )
            if naver_picks:
                final_picks = await _enrich_and_filter_with_naver(naver_picks, clean_area, situation)
                result = {
                    "query": clean_query,
                    "area": clean_area or ("내 위치 주변" if lat is not None else "내 주변"),
                    "situation": situation,
                    "situation_label": _SITUATION_META[situation]["label"],
                    "criteria": _SITUATION_META[situation]["criteria"],
                    "data_source": "naver_local_api",
                    "cached": False,
                    "top_pick": final_picks[0].to_dict(),
                    "alternatives": [p.to_dict() for p in final_picks[1:3]],
                }
                _set_cached_result(cache_key, result)
                return result

        if engine == "google" and google_key:
            google_picks = await _fetch_google_places(
                clean_query, clean_area, situation, google_key, lat=lat, lng=lng
            )
            if google_picks:
                final_picks = await _enrich_and_filter_with_naver(google_picks, clean_area, situation)
                result = {
                    "query": clean_query,
                    "area": clean_area or ("내 위치 주변" if lat is not None else "내 주변"),
                    "situation": situation,
                    "situation_label": _SITUATION_META[situation]["label"],
                    "criteria": f"{_SITUATION_META[situation]['criteria']} · 구글 평점순",
                    "data_source": "google_places_api",
                    "cached": False,
                    "top_pick": final_picks[0].to_dict(),
                    "alternatives": [p.to_dict() for p in final_picks[1:3]],
                }
                _set_cached_result(cache_key, result)
                return result

    # 2. Match curated sample DB (exact or substring match)
    matched_key = None
    for key in _SAMPLE_DB:
        if key in clean_query or clean_query in key:
            matched_key = key
            break

    if matched_key is not None:
        raw_items = _SAMPLE_DB[matched_key][situation][:3]
        picks: list[RestaurantPick] = []
        for item in raw_items:
            display_area = clean_area or item["area"]
            naver_url, kakao_url = _build_map_urls(item["name"], display_area)
            picks.append(
                RestaurantPick(
                    name=item["name"],
                    category=item["category"],
                    area=display_area,
                    reason=item["reason"],
                    price_note=item["price_note"],
                    service_tags=item["service_tags"],
                    open_status=item["open_status"],
                    naver_map_url=naver_url,
                    kakao_map_url=kakao_url,
                    is_verified_detail=True,
                )
            )
        source = "curated_sample"
    else:
        picks = _build_dynamic_fallback(clean_query, clean_area, situation)
        source = "dynamic_map_search"

    return {
        "query": clean_query,
        "area": clean_area or "내 주변",
        "situation": situation,
        "situation_label": _SITUATION_META[situation]["label"],
        "criteria": _SITUATION_META[situation]["criteria"],
        "data_source": source,
        "top_pick": picks[0].to_dict(),
        "alternatives": [p.to_dict() for p in picks[1:3]],
    }
