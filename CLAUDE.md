# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

키즈노트(어린이집 알림장 앱) → Notion 백업 → Cloudflare Pages 정적 사이트 공개 파이프라인.
빌드 시스템·테스트·린터가 없는 스크립트 저장소다. Python 3.12 표준 실행이 전부이고,
의존성은 `tools/kidsnote_fetch/requirements.txt`(requests / browser-cookie3 / kiwipiepy)와
정적 export의 `requests` 하나뿐이다. 테스트 스위트가 없으므로 변경 검증은 실제 실행으로 한다.

README.md는 코드 문서가 아니라 비개발자용 셋업 가이드(7.5만 자)다. 구현을 알고 싶으면
README 대신 소스를 읽을 것.

## 아키텍처

정적 파이프라인과 동적 계층 둘로 나뉜다.

```
                   ┌─ 정적: cron 이 하루 한 번 dist/ 를 통째로 다시 만든다 ─┐
키즈노트 API ─(1)─▶ Notion ─(2)─▶ │  알림장·앨범·공지 본문, 사진 파일, photos.json │
                   └────────────────────────────────────────────────────┘
                                          +
                   ┌─ 동적: 사람이 쓰고 D1 에 남는다 ────────────────────┐
   브라우저 ◀─API─▶ │  영상 목록 · 댓글 · 좋아요 · 관리자 비번             │
                   └────────────────────────────────────────────────────┘
```

**사람이 쓴 것은 절대 `dist/` 에 두지 말 것.** 다음 cron 이 지운다. 전부 D1 으로 간다.

- (1) 수집: `tools/kidsnote_fetch/fetch.py` → Notion
- (2) 정적 export: `static-export/export_static_site.py` → `dist/`
- (3) API: `static-export/functions/` (Pages Functions) ↔ D1 `papa_pjh`
- (4) 스케줄: `cron-worker/` (Cloudflare Worker) → GitHub workflow_dispatch

### D1 `papa_pjh` (APAC, id `f3c5379c-7d6b-4bb4-9d42-e5d037b80eed`)

| 표 | 내용 |
|---|---|
| `videos` | 영상. `source` 가 `link`(공유링크·iframe) / `mybox`(API·`<video>`) 를 가른다 |
| `comments` | 익명 댓글. 글(`post`)·영상(`video`) 공용 |
| `photo_likes` | 사진당 1행 카운터. 누른 **횟수** 를 센다 |
| `auth_attempts` | IP 별 잠금·도배 카운터. 비번·댓글·좋아요가 같이 쓴다 |
| `settings` | 관리자 비번 해시 등 런타임 설정 |

스키마는 `static-export/schema.sql`. 적용:
```bash
cd static-export
npx wrangler d1 execute papa_pjh --remote --file=schema.sql
```

### API 목록 (`static-export/functions/api/`)

```
ping                     상태 + D1 바인딩 확인
videos                   GET 목록 / POST 추가(관리자)
videos/[id]              DELETE (관리자)
videos/[id]/play         재생 정보
videos/[id]/stream       MYBOX 영상 중계 — content-type 교정 + Range 통과
comments                 GET / POST (누구나)
comments/[id]            DELETE (관리자)
likes                    GET 집계·랭킹 / POST +1 (누구나)
admin/check              비번 확인만
admin/passcode           비번 변경
mybox/status             토큰 상태 (인증 없음 — Worker 가 점검)
mybox/list               폴더 목록 (관리자)
mybox/sync               폴더 → 영상 등록 (관리자)
mybox/download           10분 URL 발급 (관리자)
```

`_` 로 시작하는 파일(`_lib.js`, `mybox/_client.js`)은 라우트가 아니라 공용 모듈이다.

**(1) 수집 — `tools/kidsnote_fetch/`**
- `fetch.py`: 키즈노트 API 크롤링(알림장·공지·앨범·식단·댓글) + CLI. 인증은 `sessionid`
  쿠키로 하고, `--auth-mode`가 그 쿠키를 어디서 얻을지 정한다 — `session-cookie-env`(기본),
  `browser-cookie`, `login`(아이디/비밀번호), `auto`(쿠키 재사용 후 죽으면 로그인).
  `TIME_BUDGET_SEC`(4h45m) + `DASHBOARD_RESERVE_SEC`(30m)로 스스로 시간을 재다가 중단하는데,
  GHA 러너 한도 안에서 끝내고 남은 분량은 다음 cron이 이어받게 하려는 설계다.
- `notion_mirror.py`: Notion 업로드 + 선택적 LLM 가공. 여기 코드의 절반은 방어 로직이다 —
  이미지 EXIF/GPS 제거(`_strip_gps_in_memory`), Notion 무료 플랜 5 MiB 한도에 맞춘 압축
  (`compress_image_to_bytes`), 그리고 로컬 LLM 출력 검증(`_strip_cjk`,
  `_english_word_leak_ratio`, `_looks_like_input_copy` + 온도 바꿔 재시도). 한국어 조사·호격
  처리는 kiwipiepy를 쓴다(`_vocative_marker`, `_topic_form`).
- 멱등성: Notion 페이지에 키즈노트 Report ID를 저장하고, 실행 시작 시 DB를 한 번 조회해
  이미 있는 id는 건너뛴다. 재실행은 안전하다.

**(2) 정적 export — `static-export/export_static_site.py`**
- Notion DB를 읽어 `dist/`에 정적 사이트 생성. HTML/CSS/JS가 파일 하단의 인라인 문자열
  상수(`INDEX_HTML`, `STYLES_CSS`, `APP_JS`, `HEADERS`)에 통째로 들어 있다. **UI 수정은 여기서
  한다.** 별도 프론트엔드 빌드가 없다.
- 라우팅은 클라이언트 사이드다. `write_post_routes`가 *동일한* `index.html`을 모든
  `posts/<id>/index.html`에 복사하고, `APP_JS`가 `data/posts/<id>.json`을 fetch해 렌더한다.
  딥링크가 되는 이유가 이것이다.
- `styles`/`app`은 내용 해시 파일명으로 나가고(`hashed_asset_name`) 매 export마다 옛 파일이
  삭제된다. asset 경로를 수동으로 참조하지 말 것.

## 명령어

```bash
# 정적 사이트 로컬 생성 + 미리보기
cd static-export
python3 export_static_site.py --limit 5        # 5개만 빠르게
python3 -m http.server 9500 --directory dist   # http://127.0.0.1:9500

# 전체 export + Cloudflare Pages 프로덕션 배포 (로컬에서 배포하는 유일한 정식 경로)
./run_daily_export.sh
STATIC_EXPORT_DEPLOY=0 ./run_daily_export.sh   # 배포 없이 export만

# 수집기 단독 실행 (Notion 반영 없이 로컬 저장만). 로컬엔 sessionid가 없으므로
# 로그인된 브라우저에서 쿠키를 빌려온다. --backup-root는 --no-local-save가 없으면 필수다.
python3 tools/kidsnote_fetch/fetch.py \
  --auth-mode browser-cookie --backup-root /tmp/kidsnote-check --limit 3 --verbose

# Functions + D1 까지 띄우는 로컬 실행. pages dev 는 wrangler.jsonc 의 바인딩을
# 읽지 않으므로 플래그로 줘야 한다. 로컬 D1 은 원격과 별개의 빈 복제본이다.
cd static-export
npx wrangler pages dev --port 9600 \
  --d1 DB=f3c5379c-7d6b-4bb4-9d42-e5d037b80eed \
  --binding ADMIN_PASSCODE=... --binding MYBOX_TOKEN=...

# cron Worker (CI 가 배포하지 않는다 — 고쳤으면 직접 올려야 한다)
cd cron-worker && npx wrangler deploy
npx wrangler tail kidsnote-cron --format pretty

# D1 들여다보기
cd static-export
npx wrangler d1 execute papa_pjh --remote --command "SELECT COUNT(*) FROM videos"

# 워크플로 수동 트리거 / 상태 확인
gh workflow run "Seoi Kidsnote static site" -R ehrtk32/kidsnote-backup
gh run list -R ehrtk32/kidsnote-backup -L 5
```

## 반드시 지킬 것

**`dist/`를 직접 편집하지 말 것.** 6시간마다 도는 cron이 재생성하며 덮어쓴다. UI 변경은
`export_static_site.py`의 인라인 상수를 고치고, **반드시 커밋·푸시**해야 한다. 푸시하지 않으면
다음 cron이 옛 코드로 재생성하며 변경을 되돌린다.

**`wrangler pages deploy dist`를 단독 실행하지 말 것.** 로컬 `dist/`는 cron보다 뒤처져 있는 게
정상이라, 그대로 올리면 라이브 사이트가 과거로 되감긴다. 항상 export를 먼저 돌리는
`run_daily_export.sh`를 쓰고, 배포 전 `dist/data/posts.json`의 글 수가 라이브
(`https://seoi-kidsnote.pages.dev/data/posts.json`) 이상인지 확인한다.

**`--clean` 주의.** `dist/`의 `wp-content/`는 미디어 캐시로 재사용된다(`find_cached_media`).
덕분에 전체 재-export도 ~4분이면 끝나지만, `--clean`을 주면 5.4GB를 전부 다시 내려받는다.
`--limit`은 안전하다 — 일부만 렌더한 실행은 미디어 정리를 건너뛴다(예전엔 안 그랬고, 그래서
`--limit 5` 미리보기가 캐시를 통째로 날렸다).

**CI에는 미디어 캐시가 없다.** `dist/`가 gitignore 대상이고 워크플로에 `actions/cache`도
없어서, 매 실행이 빈 `dist/`로 시작해 5.4GB를 다시 받는다. 그래서 새 글이 2건인 날도
export가 30~40분 걸린다. 금전 비용은 0(public repo)이고 깨지는 것도 없어서 의도적으로
두고 있다 — 고칠 거면 GitHub Actions 캐시 10GB 한도가 언젠가 조용히 터진다는 점을 감안할 것.

**Pages Functions는 `static-export/`에서 배포해야 한다.** wrangler는 `functions/`와
`wrangler.jsonc`를 **작업 디렉터리 기준**으로 찾는다. 넘겨주는 asset 디렉터리와는 무관하다.
저장소 루트에서 `wrangler pages deploy static-export/dist`를 돌리면 정적 파일만 올라가고
Functions와 D1 바인딩이 **조용히 빠진다** — 빌드는 성공하고 사이트도 멀쩡해 보이는데
`/api/*`가 전부 SPA 폴백(index.html)으로 떨어진다. 404가 아니라 200에 HTML이 와서 더
헷갈린다. 워크플로의 `working-directory: static-export`를 지우지 말 것.

**`wrangler pages dev`는 `wrangler.jsonc`의 바인딩을 읽지 않는다.** `pages deploy`는 읽는다.
`pages dev`에는 `--config` 플래그 자체가 없고 `--d1` / `--kv` / `--r2`로 따로 줘야 한다.
로컬 실행은 이렇게 한다:

```bash
cd static-export
npx wrangler pages dev --port 9600 --d1 DB=f3c5379c-7d6b-4bb4-9d42-e5d037b80eed
```

로컬 D1은 `.wrangler/state/v3/d1/`의 **별도 빈 복제본**이다(원격과 무관). 스키마를 새로
적용하려면 그 sqlite 파일에 `schema.sql`을 직접 먹이면 된다.

**프리뷰와 프로덕션이 같은 D1을 쓴다.** `wrangler.jsonc`에 최상위 `d1_databases`만 있고
`env.preview` 오버라이드가 없다. 의도적인 선택이다(가족용, 스키마 이중 관리 회피). 대신
`--branch` 프리뷰 배포에서 남긴 테스트 댓글·좋아요가 **실제 DB에 들어간다**는 점에 주의.

**배포 직후 바로 Functions를 때리지 말 것.** Pages는 번들 전파에 시간이 걸린다.
배포 6초 뒤에 친 POST 하나가 **405에 빈 본문**으로 돌아온 적이 있는데, Function이
아직 안 붙어서 정적 핸들러가 받은 것이다(정적 자산에 POST → 405). 같은 요청이 잠시 뒤엔
201로 통과했다. 500이 아니라 405라 코드 버그처럼 안 보이니 주의. 10~20초 기다리고 검증한다.

**영상은 두 가지 출처가 있다.** `videos.source` 가 가른다.
- `link` — naver.me 같은 공유 링크. iframe 으로 띄운다. 호스트 UI가 같이 보인다.
- `mybox` — MYBOX Open API. 관리자 모드에서 폴더를 고르면 그 안의 동영상이 한 번에
  등록된다(`/api/mybox/sync`). 파일명의 `N주차_M일` 을 제목·정렬로 쓴다.

**MYBOX 영상은 `/api/videos/{id}/stream` 을 거쳐야 재생된다.** MYBOX 다운로드 URL 은
바이트는 mp4 인데 `content-type` 이 `application/octet-stream` 이라 `<video>` 가 거부한다
(MediaError 4). Function 이 타입만 바로잡아 흘려보내고 Range 를 그대로 넘긴다(탐색 가능).
그 URL 은 **1회용 10분**이라 D1 에 저장하지 않는다 — 요청마다 새로 받는다.

**MYBOX API 경로 주의.** 루트 목록은 `/v1/drive/resources` 다. `/v1/drive/root/resources`
로 쓰면 404 를 준다(문서에 루트만 경로 규칙이 다르다). 공유 링크를 만들거나 읽는
엔드포인트는 **API 에 없다** — `naver.me/...` 는 웹/앱에서만 만들 수 있다.

**`MYBOX_TOKEN` 은 개인 액세스 토큰이고 최대 180일이다.** 만료되면 401 이 오고 연동이
조용히 멈춘다. cron-worker 의 22:00 점검이 `/api/mybox/status` 를 찔러 이를 잡는다.
갱신은 MYBOX 웹 > 설정 > 계정 및 개인 액세스 토큰 관리에서 새로 만들어
`wrangler pages secret put MYBOX_TOKEN --project-name seoi-kidsnote` (preview 도 따로).
이 토큰은 OAuth 가 아니라 **계정 전체**에 접근하므로, 이걸 쓰는 엔드포인트는 전부
관리자 전용이다. 예외는 `/stream` 과 `/play` — 등록된 영상만 열고 드라이브는 못 뒤진다.

**태아 그림은 `assets/fetus-1..9.png` 다.** 주수에 따라 renderProfile 이 고른다.
달 경계는 `FETAL_MONTH_WEEKS` 표에 있다(달력 기준 9개월이라 간격이 고르지 않다).
원본은 9칸짜리 한 장을 잘라 배경을 투명으로 뺀 것이고, 9장이 **같은 비율이 아니라
상대 크기를 눌러서**(제곱근) 원 지름의 47%→84% 로 자란다. 전부 꽉 채우면 자라는
게 안 보이고, 원본 비율 그대로면 1개월이 원 안에서 길을 잃는다.
이 그림은 외부 일러스트라 MIT 가 아니다 — fork 해서 쓸 거면 교체할 것.

**살구 프로필의 주수는 브라우저가 센다.** `children` 의 `second` 에 있는
`lmp`(2026-07-29 = 임신 0주 0일)에서 매번 다시 계산하므로, 배치가 하루 걸러도
화면의 주차는 맞는다. 출산예정일(2027-05-05)도 같은 날짜에서 +280일로 뽑으므로
따로 적어 둔 값이 없다 — 병원에서 주수를 다시 잡아 주면 `lmp` 만 옮기면 된다.
`birth` 가 들어오면 주수 대신 D+ 가 이긴다.

**관리자는 모달이 아니라 세션이다.** 숨은 핫스팟 → 비번 → `/api/admin/check` 가 확인하면
`body.is-admin` 이 붙고 영상·댓글에 삭제 버튼이 나타난다. 하단에 "관리자 모드" 배지가 뜬다.
핫스팟을 다시 누르면 패널이 열리고 거기 "관리자 모드 끄기" 가 있다.

세션은 **sessionStorage** 에 산다(`kidsnote.admin`). 글 상세(`/posts/123/`)가 실제 페이지
이동이라 메모리에만 두면 글 하나 열 때마다 비번을 다시 넣어야 한다. 탭을 닫으면 사라진다.

**수동 영상 추가 폼은 없앴다.** MYBOX 폴더 동기화가 대체했다. 공유 링크 영상을 넣어야 하면
`POST /api/videos` 를 직접 치거나 폼을 되살려야 한다.

**관리자 모드 진입은 보이지 않는다.** PC는 **사이드바 하단**(동기화 시각 줄), 모바일은
**화면 우상단 모서리 56×56px**를 누르면 팝업이 뜬다(`.admin-hotspot-desktop` /
`.admin-hotspot-mobile`, 경계 860px). `aria-hidden` + `tabindex=-1`이라 탭 이동으로는
닿지 않는다 — 키보드만으로 열 일이 생기면 키 조합을 따로 붙여야 한다.

**관리자 비번은 D1의 `settings` 표에 산다.** `ADMIN_PASSCODE` Pages Secret은 **부트스트랩
전용**이다 — 그 표에 행이 없을 때만 쓰이고, 사이트에서 한 번 변경하면 그 뒤로는 무시된다.
Function이 PBKDF2로 대조하고 `auth_attempts` 표로 IP당 5회 실패 시 15분 차단한다.
**반복 횟수는 100,000을 넘기지 말 것** — Workers 상한이고, 넘기면 해시를 *만들* 때만
`NotSupportedError` 가 난다. 검증은 해시에 적힌 횟수를 읽어 쓰므로 영향이 없어서,
비번을 실제로 바꾸기 전까지 드러나지 않는다(2026-10-21 에 150k 로 500 이 났다).

새 비번을 잊었다면 행을 지워 시크릿으로 되돌아간다:
```bash
cd static-export
npx wrangler d1 execute papa_pjh --remote --command \
  "DELETE FROM settings WHERE key='admin_passcode'"
```

**사진 상세(라이트박스)는 화면을 통째로 쓴다.** 예전에는 좌우 64px·위 54px 을
버튼 자리로 비워 두는 그리드였는데, 세로 사진이 특히 작게 보여서 2026-10-27 에
걷어냈다. 이제 버튼은 자리를 차지하지 않고 `position: absolute` 로 사진 위에 뜨고,
평소 `opacity: .5` 였다가 hover/focus 에서 또렷해진다. 두 가지 주의:

- `.like-button` 은 기본이 `position: absolute`(모아보기 타일 모서리에 붙는 용도)라,
  캡션 옆에 줄 세우려면 `.lightbox-like .like-button { position: static }` 이 필요하다.
- 바깥을 눌러 닫는 조건이 `event.target === lightbox` 였는데, 사진 틀이 화면을 다
  덮으면서 그 조건에 걸리는 일이 없어졌다. 지금은 `lightboxImageWrap` 을 눌렀고
  확대 중이 아닐 때로 본다 — 확대 중에 닫으면 끌다가 손을 뗄 때 창이 사라진다.

**`APP_JS` 안의 JS 는 파이썬 문자열을 한 번 거친다.** `\n` 을 쓰면 파이썬이
진짜 줄바꿈으로 바꿔서 JS 문자열이 두 줄로 쪼개지고, 번들 전체가
`Invalid or unexpected token` 으로 죽는다 — 화면이 그냥 안 뜬다. 빌드가
성공해도 이건 못 잡으므로 **만든 번들을 직접 검사할 것**:

```bash
node --check dist/assets/app.*.js
```

**사진·영상 업로드는 브라우저가 MYBOX 로 직접 보낸다.** 서버는 1회용 업로드
URL 만 내주고(`/api/uploads/url`, 관리자 전용) 결과를 받아 적는다. 바이트가
Worker 를 거치지 않으므로 100MB 요청 한도에 걸리지 않는다. MYBOX 업로드
호스트가 `access-control-allow-origin: *` 를 줘서 가능하다.

사진은 **두 벌** 올라간다 — 원본은 `키즈노트 업로드/`, 화면에 띄울 사본(긴 변
2048 JPEG)은 `키즈노트 업로드/web/`. 폴더는 설정이 아니라 **이름으로 찾아
없으면 만든다**(`mybox/_folders.js`). 올리는 순서는 **사본 전부 → 원본 전부**다.
폰에서 중간에 끊겨도 사진은 이미 다 보이고 보관본만 비는데, 그게 사진이
아예 없는 것보다 낫다. 보관본이 없는 행은 `original_missing` 으로 표시된다.

업로드한 **영상은 `uploads` 표가 아니라 `videos` 표**에 `source='mybox'` 로
들어간다(`/api/uploads/video`). 폴더 동기화로 들어온 영상과 저장 형태가 같아서
재생·썸네일·삭제 배선을 그대로 쓴다.

**MYBOX 는 이어올리기를 지원하지 않는다.** `Content-Range` 는 raw 든 multipart 든
400 `Invalid Data Format` 이고, 업로드 URL 재사용은 400 `Param Not Exist` 다
(2026-10-27 확인). 끊긴 파일은 처음부터 다시 올려야 한다. 그래서 한 번에 하나씩
보내고, 올리는 동안 Wake Lock 을 잡고, 탭을 닫으려 하면 되묻는다.

**MYBOX Open API 에는 삭제가 없다.** 파일·폴더 모두 DELETE/PUT/PATCH 가 404 고
이름 변경도 안 된다. 그래서 사이트의 "내리기"는 **D1 에서만 지운다** — 원본은
드라이브에 남는다. 확인 문구에도 그렇게 적혀 있다. 잘못 올린 파일은 MYBOX
앱에서 직접 지워야 한다.

**사진 상세에서 좌우로 밀면 넘어간다.** 확대 중에는 끈다 —
`canDragLightboxImage()`(확대 **그리고** 실제로 넘침)가 아니라 확대 여부만
본다. 작은 사진은 확대해도 넘치지 않아서 전자로 막으면 스와이프가 살아 있고,
들여다보다 사진이 넘어간다. 민 직후 400ms 동안은 "바깥 눌러 닫기"도 막는다
(기기에 따라 스와이프 끝에 click 이 한 번 따라온다).

**사이트 입구에 인증이 없다.** 6자리 가족 패스코드 게이트가 있었는데 2026-10-22 에
없앴다(장식이었고 사용자가 지워 달라고 했다). URL 을 아는 사람은 누구나 본다 —
의도된 것이다. 댓글·좋아요 쓰기도 열려 있다. 되살리자고 제안하지 말 것.

**로컬에서 재현되지 않는 500 은 `wrangler pages deployment tail` 로 잡는다.**
로컬 workerd 와 프로덕션 런타임은 제한이 다르다. PBKDF2 150k 가 `pages dev` 에서는
200 이고 프로덕션에서는 1101 이었다. 로컬이 통과한다고 고쳐진 게 아니다:

```bash
cd static-export
DEP=$(npx wrangler pages deployment list --project-name seoi-kidsnote \
      | grep -oE '[0-9a-f-]{36}' | head -1)
npx wrangler pages deployment tail "$DEP" --project-name seoi-kidsnote --format json
```

**공개 저장소에 실제 아이 사진·이름을 올리지 말 것.** README 스크린샷에 아이 실명·반 이름·
예방접종 이력이, 쿠키 안내 스크린샷에 `sessionid` 값 일부가 들어가 있었다(2026-10-21 제거·마스킹).
다시 캡처할 일이 있으면 가명 계정을 쓰거나 값을 가린 뒤 올린다.

**Notion 스키마 의존.** `resolve_schema()`가 property를 이름으로 찾는다 —
`REPORT_ID_CANDIDATES`(현 DB에서는 `번호`)와 `DATE_CANDIDATES`(`날짜`). 이 두 property를
Notion에서 이름 변경하면 export가 하드 에러로 죽는다. 현 DB 스키마가 노션 기본 템플릿
(`Name`/`Status`/`Due`/`Notes`) 잔재를 갖고 있어 이름이 내용과 안 맞아 보이지만 정상이다.

**AI 토글은 `AI_FEATURES` 시크릿/변수 하나뿐이다.** cron 이벤트에는 `inputs` 컨텍스트가 없어서
workflow_dispatch 입력으로는 cron 경로를 제어할 수 없다. 그래서 의도적으로 입력에서 제거됐다.
워크플로가 이 값을 뒤집어 `DISABLE_*` 환경변수로 `fetch.py`에 넘긴다. 같은 이유로
자녀 선택(`KIDSNOTE_CHILD_NAME`)도 입력이 아닌 시크릿이다.

## 현재 상태 (2026-10-21)

동작 중:
- 스케줄 = Cloudflare Worker (`cron-worker/`). GitHub `schedule:` 은 주석 처리됨
- D1 `papa_pjh` + Pages Functions 가 프로덕션에 배포돼 동작
- 영상: MYBOX 폴더 동기화 → `<video>` 재생 (`/stream` 경유)
- 댓글·사진 좋아요·사진 모아보기(6,598장)
- 관리자 세션 + 영상/댓글 삭제

시크릿 (Pages, production + preview 각각):
```
ADMIN_PASSCODE   초기값만. 한 번 변경하면 settings 표가 우선한다
MYBOX_TOKEN      개인 액세스 토큰, 최대 180일
```
Worker 시크릿: `GH_DISPATCH_TOKEN` (GitHub classic PAT, repo·workflow 스코프)

### 남은 일

| | 내용 |
|---|---|
| 경보 전달 | `cron-worker/src/index.js` 의 `notify()` 가 로그만 남긴다. 웹훅 한 줄이면 됨. **탐지는 이미 돈다** (신선도·MYBOX 토큰) |
| MYBOX 자동 동기화 | 지금은 관리자가 "가져오기" 를 눌러야 한다. 자동화하려면 폴더→자녀 매핑을 저장하고 Worker 가 칠 엔드포인트가 필요 |
| 삭제 동기화 | MYBOX 에서 지운 영상이 목록에 남는다. 개별 삭제로만 치울 수 있다 |
| CI 미디어 캐시 | 매 실행 5.4GB 재다운로드. 하루 1회로 줄어 체감은 작다. 안 고치는 것도 선택지 |
| 살구 정보 | 태어나면 `children` 의 `second` 에 `birth` 를 넣는다. 그 순간 주수 대신 D+ 로 바뀌고, `photo` 를 실제 사진으로 바꾸면서 `illustration: true` 를 지우면 된다 |

## 운영 메모

- **스케줄은 GitHub이 아니라 Cloudflare Worker(`cron-worker/`)가 쏜다.** GitHub은
  2026-10-08부터 12일간 이 저장소의 schedule 이벤트를 전혀 쏘지 않았고(설정은 멀쩡했고
  업스트림 fork는 정상이었다), 원인은 끝내 모른다. 두 워크플로의 `schedule:`은 주석으로
  남겨 뒀으니 되돌릴 수 있다 — 단 **둘 다 켜면 하루 두 번 돈다.**

      KST 19:30  세션 갱신   (30 10 * * * UTC)
      KST 20:00  미러        (0 11 * * * UTC)  → 성공 시 workflow_run 으로 정적 배포
      KST 22:00  신선도 확인 (0 13 * * * UTC)

  Worker 코드를 고쳤으면 `cd cron-worker && npx wrangler deploy` 를 해야 반영된다.
  CI가 배포하지 않는다.
- **신선도 확인은 배포 여부가 아니라 결과물을 본다.** 라이브 `data/posts.json` 의
  `exported_at` 이 24시간을 넘기면 경보다. dispatch 가 204 를 받아도 쿠키·노션·export·배포
  중 어디가 죽었을 수 있는데, `exported_at` 이 안 움직이는 것만이 그 전부를 덮는 신호다.
  경보 전달은 아직 비어 있다(`notify()` 가 로그만 남긴다) — 웹훅 한 줄이면 된다.
- 시크릿을 쓰는 워크플로는 PAT가 필요하다(`GH_SECRETS_PAT`). 내장 `GITHUB_TOKEN`은
  `permissions:`를 어떻게 주든 Actions 시크릿을 관리할 수 없다. PAT가 잘못되면 401
  `Bad credentials`가 나오는데, 이는 scope 부족(403)이 아니라 토큰 자체가 무효라는 뜻이다.
- **`tools/`는 `.gitignore`에 걸려 있지만 그 안의 스크립트는 CI가 실행한다.** 이 디렉터리에
  새 파일을 추가하면 반드시 `git add -f` 할 것 — 아니면 CI가 checkout 후 파일 없음으로 죽는다.
- 정적 워크플로는 배포 전에 `export-report.json`을 검사해 Cloudflare Pages 한도
  (20,000 파일 / 파일당 25 MiB)와 미디어 누락 0건을 강제한다. 이 게이트를 우회하지 말 것.
- **`.env` 위치가 둘로 갈린다.** `export_static_site.py`는 `static-export/.env`를 읽고,
  `fetch.py`는 저장소 루트 `.env`를 읽는다(`--env-file` 기본값이 `parents[2]/.env`). 현재 루트
  `.env`는 존재하지 않으므로 `fetch.py` 로컬 실행은 `--auth-mode browser-cookie`가 필요하다.
  CI에서는 양쪽 다 프로세스 환경변수가 우선이라 문제되지 않는다.
- 로컬 자격증명은 `static-export/.env`(git 미추적), CI는 repo Secrets. `.env`에 남은 `WP_*`
  값은 WordPress를 쓰던 시절의 레거시로 현재 파이프라인에서 쓰이지 않는다. 값에 공백이
  있어서 `source .env`는 깨진다 — 두 스크립트 모두 자체 파서로 읽는다.
- **`KIDSNOTE_SESSION_COOKIE`는 로그인 시점부터 정확히 30일 뒤 만료된다.** 사용해도 연장되지
  않는다. `kidsnote-session-refresh.yml`이 매일 02:40 KST에 쿠키를 probe하고, 죽은 날에만
  로그인해 시크릿을 다시 쓴다(2026-09-08 자동화, 전 구간 검증 완료). 매일 로그인하지 않는
  이유는 키즈노트가 동시 세션을 추적(`already_login`)하고 계정을 `blocked` 처리할 수 있어서다.
- **헤드리스 로그인은 가능하다.** 예전 주석의 "SPA라 불가능"은 틀렸다. 로그인 폼은 평범한
  JSON을 `POST /api/web/login/`으로 보낸다(axios `baseURL`이 `/api`, 경로는 로그인 번들이
  아니라 공유 청크에 있다). 페이로드는 `{username, password, remember_me}`이고 캡차가 없다.
  주의할 함정 두 개: ① 엔드포인트를 추정하지 말 것 — `/api/v1/users/login/`은 무엇을 보내도
  401을 주므로 자격증명 거부처럼 보인다. 진짜 경로는 빈 요청에 `400 invalid_login`을 준다.
  ② 응답의 `sessionid` 쿠키는 `.kidsnote.com`으로 스코프되므로 `domain="www.kidsnote.com"`
  으로 조회하면 `None`이 나온다. `_sessionid_of()`를 쓸 것.
- `invalid_password` 에러는 아이디가 틀렸을 때도 똑같이 나온다(계정 열거 방지).
  `/api/v1/second-factors/{username}/`도 없는 계정에 200을 주므로 존재 확인에 못 쓴다.
- 2FA가 켜진 계정은 자동 갱신이 불가능하다. `fetch.py`가 비밀번호를 보내기 전에 먼저
  확인하고 설명과 함께 중단한다. 현재 계정은 2FA 꺼짐.
- `logs/`와 `backups/`는 gitignore 대상이다. `backups/launchd/`의 plist는 GitHub Actions로
  이관되기 전 로컬 스케줄러 잔재이며 현재 로드돼 있지 않다.
