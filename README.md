# Pick3 · 10초 맛집 결정기 (No-List Mobile Web)

목록 20개를 스크롤하는 대신, **메뉴 입력 + 원탭 상황 선택**만으로 지금 바로 갈 **1순위 1곳 + 대안 2곳**만 골라주는 초간단 모바일 웹 MVP입니다.

## 주요 기능
- **원탭 상황 필터**: `[혼자 / 점심]`, `[둘이서 / 데이트]`, `[여럿이 / 회식]`
- **No-List 큐레이션**: 최대 3곳(1순위 + 대안 2곳)만 추천 사유·가격 노트·태그와 함께 제시
- **원탭 지도 연결**: 네이버 지도 / 카카오맵 검색 바로가기 및 카톡 공유용 요약 텍스트 복사
- **선택적 실시간 연동**: `KAKAO_REST_API_KEY` 환경변수 설정 시 카카오 로컬 API 연동 (미설정/오류 시 안전 폴백)

## 빠른 실행

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8088
```

브라우저에서 `http://127.0.0.1:8088` 접속 (Windows는 `run_pick3.bat` 실행).

## 테스트

```bash
pytest tests -q        # 전체 (맛집·의료비·처방·PIN)
```

## 저장 단축어 `Pick3` (공유 시트 · 뒷면 탭)

단축어 **`Pick3` 하나**로 모두 저장합니다. 인스타 맛집 캡처, 병원·약국 결제 내역 화면, 처방조제정보·진료비 **PDF**를 공유하거나 iPhone 뒷면을 두 번 탭하면 서버가 종류를 가려 각 서비스에 저장하고 결과 문구를 보여 줍니다.

```
공유(이미지·PDF·파일) 또는 뒷면 탭(스크린샷) → 단축어 Pick3 → POST /api/ingest/screenshot (X-Pick3-Token)
 → PDF: 의료 문서 → 처방조제정보면 처방·조제 기록, 아니면 의료비(결제 행이 없으면 처방으로 재시도)
 → 이미지: Gemini로 화면 종류 판별(screen_router.py: 맛집 / 의료비 / 처방 / 기타)
     맛집 → Gemini 추출 + 카카오 로컬 확인(음식점 FD6·카페 CE7) → 사용자별 저장
     의료비·처방 → 각 추출기 → 사용자별 저장
 → 결과 문구(message) 표시
```

### 설치 (사용자)

개인 시작 링크(`start.html#t=<토큰>`)를 열고 버튼 두 개:

1. **① 설치** → [단축어 추가]. 설치 링크(iCloud, 토큰 없음, 모두 같음): https://www.icloud.com/shortcuts/5de552eb9c4b43958b160d64e31152e6
2. **② 내 계정 연결** → 단축어 앱에서 "✅ 연결 완료". 파일 접근을 물으면 허용

- 카톡에서 시작 링크를 열면 Safari로 자동으로 넘어갑니다(카톡 안 브라우저는 단축어 설치·연결이 안 됨)
- 같은 이름(`Pick3`)의 단축어가 이미 있으면 먼저 지웁니다. 예전 단축어(`Pick3 저장`·`의료비 저장` 등)는 지워도 됩니다
- **뒷면 탭**: 설정 → 손쉬운 사용 → 터치 → 뒷면 탭 → 이중 탭 → `Pick3`
- **공유로 저장**: 캡처 → 왼쪽 아래 썸네일 → 공유 → `Pick3`. PDF는 카톡·파일 앱에서 파일을 열고 공유 → `Pick3`. 목록에 없으면 공유 시트 맨 아래 **작업 편집…**에서 즐겨찾기 추가

### 동작 방식 (관리자)

- 토큰은 단축어 안에 없습니다. [② 내 계정 연결]이 `shortcuts://run-shortcut?name=Pick3&input=text&text=<토큰>`으로 단축어를 실행하면, 단축어가 `p3_`로 시작하는 입력을 `Shortcuts/pick3_token.txt`에 저장하고 이후 매번 그 파일을 읽어 헤더에 넣습니다. 토큰을 바꾸면(`rotate`) 새 시작 링크에서 [② 내 계정 연결]만 다시 누르면 됩니다
- 단축어는 받아서 보내고 결과를 보여 주는 일만 합니다. 판단은 서버가 하므로 **기능을 바꿀 때 단축어와 iCloud 링크는 그대로** 둡니다
- 단축어를 꼭 고쳐야 하면 `shortcuts/`(Cherri 소스 → 서명) 안내대로 다시 만들고, 설치해서 새 iCloud 링크를 만들어 `start.html`과 위 링크를 바꿉니다
- 단축어 이름은 **영문만**(한글 이름은 파일로 가져오면 자모 분리되어 `run-shortcut?name=`으로 찾지 못함)
- 화면 판별 호출이 1회 늘어(약 2초) 끄려면 `PICK3_AUTO_ROUTE=0`(모든 이미지를 맛집으로 처리)
- 새 서비스를 붙일 때: `screen_router.py`에 종류를 더하고 `app.py`의 라우팅 분기에서 그 서비스의 저장 함수를 부름

### 사용자와 토큰

목록은 사람마다 따로입니다. 각자 받은 **개인 토큰**으로 구분합니다(서버에는 토큰의 SHA-256 해시만 저장).

1. 처음 한 번: Supabase SQL Editor에서 `docs/SUPABASE_MIGRATION_002_users.sql` 실행 (`SUPABASE_SETUP.sql` 다음)
2. 사용자 추가: `python scripts/pick3_users.py add 홍길동 [--limit 100]` → 토큰과 **개인 시작 링크**가 **한 번만** 표시됨. 링크만 1:1로 전달. `#` 뒤 토큰은 서버로 전송되지 않음
3. 관리: `list`(한도·PIN 유무), `rotate <id>`(토큰 분실·유출 시 재발급, 웹 로그인도 끝냄), `disable <id>`, `enable <id>`, `claim-orphans <id>`, `reset-pin <id>`, `logout-all <id>`

시작 링크를 열면 토큰이 그 브라우저(`localStorage`)에 저장되어 웹 화면에서도 같이 씁니다. 다른 계정이 이미 있으면 바꿀지 묻습니다.

하루 한도: 사람당 `PICK3_DAILY_LIMIT_PER_USER`(기본 30건, 사용자별 `daily_limit`이 있으면 그 값), 전체 `PICK3_DAILY_LIMIT_TOTAL`(기본 300건). 넘으면 Gemini를 부르지 않고 "한도" 문구를 돌려줍니다.

#### 이름 + PIN 로그인 (웹 화면)

브라우저마다 개인 링크를 다시 열지 않아도 되게, 웹 화면(홈·의료비·처방·맛집 목록)은 **이름 + PIN 6자리**로도 들어올 수 있습니다. 단축어는 여전히 개인 토큰을 씁니다.

- 처음 한 번: Supabase SQL Editor에서 `docs/SUPABASE_MIGRATION_005_pin.sql` 실행
- PIN은 **사용자가 직접** 만듭니다: 개인 링크로 들어간 기기의 홈 → **PIN 만들기**. 관리자는 PIN을 볼 수 없습니다(소금 친 PBKDF2 해시만 저장)
- 다른 브라우저: 홈의 **이름과 PIN으로 들어가기** → 그 브라우저용 세션 토큰(`p3s_…`)이 발급되어 개인 토큰처럼 쓰임. 홈 아래 **이 기기에서 로그아웃**으로 끝냄
- 막기: 같은 사람 PIN 5번 틀리면 15분 잠금, IP당 시간당 20번 실패 제한. 같은 숫자·연속 숫자(111111, 123456) 금지
- 관리: `reset-pin <id>`(PIN 잊음·잠김 해제), `logout-all <id>`(웹 로그인 모두 끝내기). `rotate <id>`는 웹 로그인도 모두 끝냄. `list`에 PIN 유무 표시

### 서버 환경변수 (Render)

| 키 | 설명 |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | 저장소. 키는 **secret 키(`sb_secret_…`)**. 테이블은 `docs/SUPABASE_SETUP.sql` 실행 |
| `GEMINI_API_KEY` | 화면 분석 |
| `PICK3_GEMINI_MODEL` | 선택. 기본 `gemini-3.5-flash-lite`. 인식이 약하면 `gemini-3.8-flash` |
| `KAKAO_REST_API_KEY` | 가게 확인·좌표 |
| `PICK3_DAILY_LIMIT_PER_USER`, `PICK3_DAILY_LIMIT_TOTAL` | 선택. 하루 분석 한도 (기본 30 / 300) |
| `PICK3_BROWSER` | 서버 브라우저(링크 읽기 · 보장분석 PDF). **기본 꺼짐**, `on`이면 켬. 꺼져 있으면 링크는 스크린샷 안내, 보장분석 결과는 HTML |
| `PICK3_RENDER_DOMAINS` | 링크로 저장할 때 서버 브라우저가 열 수 있는 사이트(예: `myhom.me`) |
| `COVERAGE_EXTRACT_MODEL`, `COVERAGE_ANALYZE_MODEL` | 선택. 보장분석 모델(기본 `gemini-3.5-flash` / `gemini-pro-latest`) |
| `PLAYWRIGHT_BROWSERS_PATH` | `0` (Chromium을 venv 안에 설치) |
| `PICK3_INGEST_TOKEN` | Supabase 없이 로컬에서만 쓰는 공용 토큰. 운영(Supabase)에서는 쓰지 않음 |

> Render 서비스는 Auto-Deploy가 꺼져 있습니다. push 후 **Manual Deploy → Deploy latest commit**. 환경변수만 바꿀 때는 "Save only" 후 배포.
> 확인: `GET /api/health` → `storage_ok`, `gemini_key_configured`가 `true`, `auth_mode`가 `per_user`.

### 링크로 저장 (서버 브라우저)

알림톡 버튼으로 열리는 결과 페이지처럼 **내용이 JavaScript로 나중에 채워지는 페이지**는 링크를 `Pick3`으로 공유하면 서버가 휴대폰 브라우저처럼 열어(`link_render.py`, Playwright/Chromium, Apache-2.0) PDF로 만든 뒤 의료 문서로 처리합니다.

- 열 수 있는 사이트: 환경변수 `PICK3_RENDER_DOMAINS`(쉼표 구분, 하위 도메인 포함)만. `https`만, 공인 IP로만 연결(SSRF 방지), 리디렉션도 허용 사이트 안에서만
- 메모리 절약: 한 번에 한 페이지, 이미지·글꼴·동영상 차단, 링크마다 브라우저를 켜고 바로 끔(512MB 인스턴스 기준, 부족하면 플랜 상향)
- 목록에 없는 사이트의 링크는 분석 없이 "Safari [전체 페이지] PDF로 보내기" 안내만 돌려줌(사용 횟수 차감 없음)
- Render 설정(지금 Python 서비스 그대로): Build Command `pip install -r requirements.txt && python -m playwright install chromium-headless-shell`, 환경변수 `PLAYWRIGHT_BROWSERS_PATH=0`, `PICK3_RENDER_DOMAINS=myhom.me`. 시스템 라이브러리가 없어 브라우저가 안 뜨면 `Dockerfile`(Playwright 공식 이미지)로 새 서비스를 만들어야 함(주소가 바뀌므로 단축어도 다시 만들어야 함)

### 보장분석 (📋, `coverage_review/`)

보장분석 보고서 PDF를 `Pick3`으로 공유하면 **저장만** 합니다(Supabase 테이블 `pick3_coverage_reports` + 비공개 버킷 `pick3-coverage`, `docs/SUPABASE_MIGRATION_006_coverage.sql`). 암호 PDF는 암호가 걸린 채로 보관하고 암호는 저장하지 않습니다.
`coverage.html`에서 [제안서 작성]을 누르면(처음엔 PDF 암호 함께) 백그라운드로 분석해 분석서(설계사용)·제안서(고객용 1쪽 + 확인 사항)를 만듭니다. 한 번 읽은 보고서는 암호 없이 [다시 작성].

- 계산은 코드, 읽기·설명은 Gemini: ① 보고서 → 공통 JSON(양식 무관, 담보 분류 40개) ② 코드가 나이·상령일·납입완료 추정·실손 기간·청구 기한·진료 묶음·정기 진료·과잉 보장 계산(`facts.py`) ③ Gemini가 사실 목록만 보고 청구·부족·최적화 작성(근거 id 필수, 근거 없는 항목과 보호 계약 해지는 코드가 제거) ④ 보험료 전후 비교(`plan.py`, 추가 보험료는 `rules.json` 설정표)
- 병의원 기록은 보고서 고객 이름과 같은 환자 이름으로, **공유한 계정 안에서만** 찾음
- 업무 규칙(기관 분류 키워드, 기준 금액, 보험료 표)은 `coverage_review/rules.json`. 보험료 표는 추정치이니 실제 견적으로 교체
- 로컬 실행: `python -m coverage_review.cli --pdf 보고서.pdf --password <암호> --user-id <id> --out <폴더>` (중간 JSON은 `<폴더>/_work`에 캐시)
- PDF 한글 글꼴은 `coverage_review/fonts/`(Noto Sans KR 부분집합, SIL OFL 1.1)를 넣어 서버에 글꼴이 없어도 같게 나옴

### 사용 요령과 결과 문구

- 인스타에서 **캡션 "…더 보기"를 펼친 뒤** 캡처하면 정확합니다(📍상호·주소가 보이게).
- 단축어 앱에서 ▶로 실행하면 단축어 앱 화면이 찍혀 "찾지 못했습니다"가 정상입니다.
- 처음엔 "연결 허용"이 뜰 수 있음 → 항상 허용. 서버가 잠들어 있으면 첫 응답 30~50초.

| 문구 | 의미 |
|---|---|
| 🍽️ 맛집 / ✅ 저장 완료: 상호 (동네) | 맛집 저장됨 |
| 이미 저장된 곳입니다 | 내 목록에 같은 상호·주소가 이미 있음 |
| ⚠ …위치 확인 필요 | 저장했지만 카카오에서 같은 가게를 확정 못 함(좌표 없음). 지점·동네가 보이는 화면으로 다시 |
| 🧾 의료비 기록 / 💊 처방·조제 기록 | 의료비·처방으로 저장됨(건수·고객 이름 표시) |
| 오늘 저장 한도(…건)를 넘었습니다 | 하루 한도 초과. 다음 날(한국 시간) 초기화 |
| ❌ 계정 토큰이 맞지 않습니다 (받은 토큰: …) | 연결 안 됨·토큰 바뀜. 시작 링크에서 [② 내 계정 연결] 다시 |
| ❌ 단축어가 보낸 요청에 파일(image)이 … | 단축어가 파일을 못 보냄(단축어 손상·구버전). 다시 설치 |
| 서버 응답: <HTML> | 서버가 배포·재시작 중일 때 Render 안내 페이지. 잠시 후 재시도 |
| ❌ 저장 실패 / 분석 실패 | 서버·외부 API 문제. 잠시 후 재시도 |

## 첫 화면 (서비스 선택)

`index.html`은 서비스를 고르는 첫 화면이고, 맛집 앱은 `pick3.html`, 의료비는 `medical.html`입니다. 새 서비스는 `index.html`의 `SERVICES` 배열에 한 줄(아이콘·이름·설명·페이지)을 더하고 페이지 파일을 추가하면 됩니다(Render에서도 열려면 `app.py`의 페이지 라우트에 경로 추가). 개인 토큰은 기기의 `localStorage`(`pick3_token`)에 한 번 저장되어 모든 서비스가 같이 씁니다.

## 의료비 기록 (보험금 청구용)

병원·약국 결제 내역 화면(스크린샷) 또는 PDF를 `Pick3`으로 보내면 **환자 이름 + 기관·구분·금액·날짜**를 표로 저장합니다. 보기: `medical.html`(환자·연도·구분 필터, 열 정렬, 합계, 엑셀용 CSV, 행 수정·삭제).

- 처음 한 번: Supabase에서 `docs/SUPABASE_MIGRATION_003_medical.sql` 실행
- 중복: 같은 환자·기관·날짜·금액은 한 번만 저장(스크롤하며 여러 장 캡처해도 안전). 잘려서 안 보이는 행은 추측하지 않고 뺌
- 이름 탭이 여러 개인 화면은 **선택된 탭의 이름**을 환자로 씀
- 이름이 없는 화면(영수증 등)은 **가장 최근에 저장한 환자**로 저장하고 결과 문구에 알림. 틀리면 의료비 화면에서 행을 눌러 바꾸며, 같은 캡처로 저장된 행을 한 번에 바꿀 수 있음
- **고객 이름 지정(보험설계사용)**: `POST /api/ingest/medical`에 본문 필드 `patient`(텍스트)를 함께 보내면 화면의 이름 대신 그 이름으로 저장(설계사 본인 토큰). 고객 기록 전체 삭제는 의료비 화면에서 고객을 고른 뒤 [전체 삭제](이름 재입력 확인)
- API(모두 `X-Pick3-Token`): `POST /api/ingest/medical`(multipart `image`, 선택 `patient`), `GET /api/medical?patient=&year=`, `PATCH /api/medical/{id}`, `POST /api/medical/patient`, `DELETE /api/medical?patient=`, `DELETE /api/medical/{id}`

## 처방·조제 기록 (💊)

심평원 '내가 먹는 약' **처방조제정보 PDF**를 `Pick3`으로 보내면 첫 쪽 제목을 보고 처방 기록으로 저장합니다(진료비 PDF는 의료비로). 보기: `prescriptions.html`(고객·연도·처방/조제 필터, 약품·성분 검색, 열 정렬, CSV, 이름 일괄 변경, 고객 단위 삭제).

- 처음 한 번: Supabase에서 `docs/SUPABASE_MIGRATION_004_prescriptions.sql` 실행
- 추출: 글자층이 있는 PDF는 **pdfplumber**(MIT)로 표를 그대로 읽음 — 무료·결정적, 순번 1..N 연속 검사로 누락 알림. 표가 없는 PDF(스캔)와 캡처 화면은 Gemini
- 저장: `pick3_prescriptions`, 같은 고객·날짜·기관·약품은 한 번만
- 이름: 입력한 이름(`patient` 필드) > 화면의 이름 > **파일 이름 앞의 한글 이름**(예: `홍길동 약처방.pdf`) > 최근 이름. 파일 이름·최근 이름을 쓴 경우 결과 문구에 표시
- API: `GET /api/prescriptions`, `POST /api/prescriptions/patient`, `DELETE /api/prescriptions?patient=`, `DELETE /api/prescriptions/{id}`
