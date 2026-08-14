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

두 단계가 GitHub Actions에서 체이닝된다. 로컬 실행도 같은 스크립트를 쓴다.

```
키즈노트 API ──(1) tools/kidsnote_fetch/fetch.py──▶ Notion DB
                                                      │
Cloudflare Pages ◀──(2) static-export/export_static_site.py
```

**(1) 수집 — `tools/kidsnote_fetch/`**
- `fetch.py`: 키즈노트 API 크롤링(알림장·공지·앨범·식단·댓글) + CLI. 로그인은 불가능하고
  `sessionid` 쿠키에 의존한다(`--auth-mode session-cookie-env` 또는 브라우저 쿠키 추출).
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
- 사이트에 6자리 가족 패스코드 게이트가 있고 해시가 `APP_JS`에 하드코딩돼 있다.

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
덕분에 전체 재-export도 ~4분이면 끝나지만, `--clean`을 주면 4GB를 전부 다시 내려받는다.

**Notion 스키마 의존.** `resolve_schema()`가 property를 이름으로 찾는다 —
`REPORT_ID_CANDIDATES`(현 DB에서는 `번호`)와 `DATE_CANDIDATES`(`날짜`). 이 두 property를
Notion에서 이름 변경하면 export가 하드 에러로 죽는다. 현 DB 스키마가 노션 기본 템플릿
(`Name`/`Status`/`Due`/`Notes`) 잔재를 갖고 있어 이름이 내용과 안 맞아 보이지만 정상이다.

**AI 토글은 `AI_FEATURES` 시크릿/변수 하나뿐이다.** cron 이벤트에는 `inputs` 컨텍스트가 없어서
workflow_dispatch 입력으로는 cron 경로를 제어할 수 없다. 그래서 의도적으로 입력에서 제거됐다.
워크플로가 이 값을 뒤집어 `DISABLE_*` 환경변수로 `fetch.py`에 넘긴다. 같은 이유로
자녀 선택(`KIDSNOTE_CHILD_NAME`)도 입력이 아닌 시크릿이다.

## 운영 메모

- `.github/workflows/kidsnote-to-notion.yml`이 KST 03:15/09:15/15:15/21:15에 돌고, **성공했을
  때만** `workflow_run` 트리거로 `seoi-kidsnote-static.yml`이 이어진다.
- 정적 워크플로는 배포 전에 `export-report.json`을 검사해 Cloudflare Pages 한도
  (20,000 파일 / 파일당 25 MiB)와 미디어 누락 0건을 강제한다. 이 게이트를 우회하지 말 것.
- **`.env` 위치가 둘로 갈린다.** `export_static_site.py`는 `static-export/.env`를 읽고,
  `fetch.py`는 저장소 루트 `.env`를 읽는다(`--env-file` 기본값이 `parents[2]/.env`). 현재 루트
  `.env`는 존재하지 않으므로 `fetch.py` 로컬 실행은 `--auth-mode browser-cookie`가 필요하다.
  CI에서는 양쪽 다 프로세스 환경변수가 우선이라 문제되지 않는다.
- 로컬 자격증명은 `static-export/.env`(git 미추적), CI는 repo Secrets. `.env`에 남은 `WP_*`
  값은 WordPress를 쓰던 시절의 레거시로 현재 파이프라인에서 쓰이지 않는다. 값에 공백이
  있어서 `source .env`는 깨진다 — 두 스크립트 모두 자체 파서로 읽는다.
- `KIDSNOTE_SESSION_COOKIE`는 ~30일마다 만료된다. mirror가 갑자기 실패하면 이것부터 의심할 것.
- `logs/`와 `backups/`는 gitignore 대상이다. `backups/launchd/`의 plist는 GitHub Actions로
  이관되기 전 로컬 스케줄러 잔재이며 현재 로드돼 있지 않다.
