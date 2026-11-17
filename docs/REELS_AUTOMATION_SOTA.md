# 인스타 릴스 · 유튜브 숏츠 제작 자동화 SOTA 조사 요약 (2025–2026)

> **목표**: 1인 개발 기준 **월 \$0~\$25** 저비용으로 세로형(9:16) 음식·맛집 릴스/숏츠를 제작·업로드하는 SOTA 스택 정리 (`Pick3` `/api/reels-script` 연동 기준)

---

## 1. 영상 조립 및 렌더링 (Programmatic Assembly)

| 도구 | 방식 | 장점 (1인 개발) | 단점 | 비용 |
| :--- | :--- | :--- | :--- | :--- |
| **FFmpeg** (직접 CLI) | C/C++ CLI, filter graph | **가장 빠름** (`-c copy`, NVENC GPU 가속), `libass` 자막 번인·9:16 크롭·오디오 믹싱 완전 제어 | 모션 그래픽/애니메이션 UI 구현은 복잡함 | **\$0** (로컬/VPS) |
| **Remotion** | React + TS → Headless Chromium + FFmpeg | 움직이는 식당 카드·가격표·평점 UI 등 리치 모션 그래픽에 최적, 실시간 프리뷰 | 프레임당 CPU/RAM 소모 큼, React/CSS 애니메이션 세팅 필요 | **\$0** (개인/소규모 로컬 무료) |
| **MoviePy** | Python (NumPy + ImageMagick) | 파이썬 스크립팅이 쉬움 | **가장 느림** (프레임 디코딩·텍스트 렌더링 병목) — 대량 자동화 비추 | **\$0** |
| **Creatomate** | 웹 템플릿 + JSON API | 인프라 관리 불필요, 소셜 텍스트 애니메이션 내장 | 대량 제작 시 비용 높음, 템플릿 제약 | **\$54/월~** |
| **Shotstack** | JSON 타임라인 API | Python/Node SDK 제공, 클라우드 스케일링 | 커스텀 레이아웃 JSON 장황, 분당 과금 | **\$39/월~** (250분) |

- **결론**: 기본 조립(컷 연결·9:16 크롭·오디오 믹싱·ASS 자막 번인)은 **FFmpeg**(\$0), 화려한 식당 비교 카드 애니메이션이 필요할 때만 **Remotion + FFmpeg** 조합을 사용합니다.

---

## 2. 한국어 AI 성우 (TTS) 비교

| 엔진 | 한국어 품질 | 비용 | 음식 숏츠 적합성 / 주의점 |
| :--- | :--- | :--- | :--- |
| **ElevenLabs** (Multilingual v2/v4) | **9.5 / 10** (최상) | 무료(1만자 비영리), **\$6/월**(3만자), **\$22/월**(12.1만자) | 한국어 억양·호흡·강조·훅 전달력이 가장 자연스러움. 대량 사용 시 유료 |
| **Fish Speech** (Fish Audio) | **8.5 / 10** (오픈소스 최상) | **\$0** (로컬 GPU 필요) | 제로샷 보이스 클로닝 및 한국어 자연스러움 우수. GPU 서버 보유 시 최고 가성비 |
| **OpenAI TTS** (`tts-1`, `tts-1-hd`, `gpt-4o-mini-tts`) | **8.0 / 10** | 종량제 (`tts-1`: 1천자당 \$0.015) | 발음이 깔끔하고 API가 빠름. 감정 기복은 다소 평탄함 |
| **Edge-TTS** (`edge-tts` 파이썬 래퍼) | **7.5 / 10** | **\$0** (무제한) | MS Azure 한국어 음성(SunHi/InJoon) 무료 활용. 프로토타입용 최적이나 비공식 래퍼로 상업 SLA·감정 조절 한계 |

- **결론**: 초기 테스트는 **Edge-TTS**(\$0) 또는 **OpenAI `tts-1`**(편당 약 \$0.015), 본 채널 퀄리티 확보 시 **ElevenLabs**(\$6~\$22/월) 또는 로컬 GPU 환경의 **Fish Speech**(\$0)로 전환합니다.

---

## 3. 자동 자막 & 캡컷(CapCut) 스타일 단어 강조

- **SOTA 로컬 파이프라인 (\$0)**:
  1. **`faster-whisper`** (`small` 또는 `medium`, `word_timestamps=True`)로 단어 단위 타임스탬프 추출
  2. Python으로 1줄당 3~5어절씩 청킹 후 **`.ass` 자막 파일** 생성
  3. `ffmpeg -vf ass=captions.ass`로 영상에 하드코딩(`libass`)
- **캡컷 스타일 노란색 단어 팝 효과 구현 팁 (`.ass`)**:
  - `PlayResX: 1080`, `PlayResY: 1920`, 굵은 폰트 + 두꺼운 외곽선(`BorderStyle=1, Outline=3, Shadow=2, Alignment=2`)
  - 각 단어 타이밍마다 `Dialogue:` 이벤트를 만들고 현재 발화 단어만 인라인 태그로 강조:  
    `{\c&H0000D4FF\fscx115\fscy115}단어{\r}` *(ASS 색상은 BGR 순서라 `&H0000D4FF`가 밝은 노랑/골드)*

---

## 4. 영상 소스(B-Roll) 확보 & 저작권·저품질 제재 회피

- **플랫폼 정책 (2025–2026)**:
  - 유튜브·메타 모두 AI 영상 자체를 금지하진 않지만, 의미 없는 **AI 음식 클립 무한 반복('AI slop')**은 수익 창출 제한 및 노출 억제를 강하게 적용합니다.
  - 극사실적 AI 음식·장면 사용 시 업로드 단계에서 **AI 생성 라벨 체크**가 필수이며(미표기 시 제재), 표기 자체로 노출 불이익은 없습니다.
- **권장 제작 전략**:
  1. **실사 클립 재활용 + 동적 오버레이 (최고 신뢰 · \$0)**: 직접 촬영한 숯불/고기 굽기·국물·면치기 클립을 2~3초 단위로 자른 라이브러리를 구축해, 줌/크롭 모션 + 한국어 TTS + ASS 자막과 결합
  2. **AI 영상 생성(Kling / Runway / Veo 3)**은 도입부 1~2초 임팩트 컷에만 제한적으로 활용(상업용 유료 플랜 필수)

---

## 5. Instagram Graph API 자동 업로드 & 섀도우밴 방지

- **필수 요건**:
  - **Instagram Business** (또는 Creator) 계정 + Facebook 페이지 연결
  - 권한: `instagram_business_basic`, `instagram_business_content_publish`
  - 규격: 9:16 세로, **5~90초**, H.264/HEVC + AAC, **공개 접근 가능한 URL**
- **3단계 비동기 게시 절차**:
  1. `POST /{ig-user-id}/media` (`media_type=REELS`, `video_url`, `caption`) → `creation_id` 수신
  2. `GET /{container-id}?fields=status_code`를 **`FINISHED`가 될 때까지 폴링** *(고정 sleep 후 즉시 게시하면 `9007` 에러 발생)*
  3. `POST /{ig-user-id}/media_publish` (`creation_id`)
- **섀도우밴·스팸 제재 회피 수칙**:
  - 비공식 로그인/브라우저 자동화 봇 사용 금지 (즉시 탐지·정지 위험)
  - 공식 Graph API 사용 + 하루 **1~3건 이하**, 정각이 아닌 랜덤 시간 간격(Jitter)으로 게시
  - 동일 영상·동일 해시태그 무한 복붙 금지, 주기적인 실제 앱 내 댓글 답글·인사이트 확인 병행

---

## 6. 추천 1인 개발 저비용 최종 스택 (월 \$0~\$25)

1. **대본·훅 생성**: `Pick3` `/api/reels-script` (+ 필요 시 LLM 문구 다듬기 월 \$1~\$3)
2. **음성 (TTS)**: **Edge-TTS** (\$0) → 성장 후 **ElevenLabs** (\$6~\$22/월)
3. **자막**: 로컬 **`faster-whisper`** + Python `.ass` 생성기 (\$0)
4. **영상 소스**: 직접 찍은 고기·음식 2~3초 실사 B-roll 라이브러리 (\$0)
5. **렌더링**: **FFmpeg** (Python `subprocess` 자동화, \$0)
6. **업로드**: 공식 **Instagram Graph API** + **YouTube Data API v3** (\$0)
