# 🚀 Quick Start — 25분 셋업

> 자세한 설명·예외처리·트러블슈팅은 [README.md](README.md) 참고. 이 페이지는 **막힘없이 진행했을 때의 최단 경로**만 담았습니다.

## ✅ 시작 전 확인 (1분)

다음 세 가지 계정이 모두 데스크톱 브라우저에서 로그인 가능해야 합니다:

- [ ] **키즈노트** ([www.kidsnote.com](https://www.kidsnote.com)) — 어린이집에서 받은 평소 계정
- [ ] **노션** ([www.notion.so](https://www.notion.so)) — 없으면 1분 가입
- [ ] **GitHub** ([github.com](https://github.com/signup)) — 없으면 1분 가입

> 📱 휴대폰만 있으면 셋업 불가. 데스크톱·노트북 필요.

> 👨‍👩‍👧‍👦 **자녀가 2명 이상이면** 자녀별로 fork + 노션 DB를 따로 만들어야 해요. 아래 셋업은 **자녀 1명 기준**이며, 둘째 아이는 셋업 마친 뒤 [README의 multi-child 가이드](README.md#advanced-multichild-and-ai-off) 참고.

---

## 1️⃣ Fork (1분)

[github.com/redchupa/kidsnote-backup](https://github.com/redchupa/kidsnote-backup) 페이지 우측 상단 **`Fork`** → 녹색 `Create fork` 클릭.

✅ 본인 계정 사본 페이지(`내깃허브아이디/kidsnote-backup`)로 이동되면 성공.

---

## 2️⃣ 노션 DB 만들기 (3분)

1. 노션 좌측 사이드바 **`+ 새 페이지`** → 제목 `키즈노트 백업` 입력
2. 본문에 **영문 입력 모드**로 `/database` 입력 → **`데이터베이스 - 인라인`** 선택
   - ⚠️ "전체 페이지"가 아닌 **"인라인"**
3. 자동 생성된 기본 속성(보통 `이름`)은 그대로 두고, 표 우측 끝 **`+`** 버튼으로 **`날짜`** (Date 타입)과 **`번호`** (Number 타입) 두 속성 추가. 이름은 한글/영문 둘 다 OK (코드가 자동 매칭).

✅ 표가 만들어졌으면 성공.

---

## 3️⃣ 노션 토큰 받기 (3분)

1. [notion.so/profile/integrations](https://www.notion.so/profile/integrations) 접속
2. **`+ 새 통합 만들기`** → 이름 `Kidsnote Backup` → **`Internal`** → **`제출`**
3. 다음 화면에서 **`Internal Integration Secret`** 옆 **`Show`** → 값 복사 (메모장에 저장)
   - 형태: `ntn_...` 또는 `secret_...`

✅ 메모장에 토큰 값이 있으면 성공.

---

## 4️⃣ DB에 통합 연결 (1분)

1. 2번에서 만든 DB 페이지로 돌아옴
2. 우측 상단 `⋯` → **`연결`** → 방금 만든 **`Kidsnote Backup`** 클릭
3. 확인 팝업에서 **`연결`**

✅ 우측 상단에 통합 아이콘이 추가되면 성공.

---

## 5️⃣ DB ID 복사 (2분)

1. DB 표 좌측 상단 제목 옆 **`↗`** (Open as page) 아이콘 클릭 → **풀화면**으로 열기
2. 브라우저 주소창 URL이 `...32자hex?v=...` 형태가 됨
3. **물음표(`?`) 직전 32자 hex**를 복사 (메모장에 저장)

```
https://www.notion.so/내이름/238f5e29c0894adfb6c4d8e1a5b2c3d4?v=...
                            ────────────────────────────────
                            이 32자가 DB ID
```

> 💡 부모 페이지 ID를 잘못 넣어도 코드가 자동 복구하니 너무 걱정 마세요. 그래도 풀화면에서 추출하는 게 깔끔.

✅ 메모장에 32자 hex 문자열이 있으면 성공.

---

## 6️⃣ 키즈노트 sessionid 쿠키 추출 (3분)

1. Chrome으로 [www.kidsnote.com](https://www.kidsnote.com) 접속 + 로그인
2. **F12** (또는 `Ctrl+Shift+I`) → 상단 메뉴 **`Application`** 클릭
3. 좌측 사이드바 **`Storage` → `Cookies` → `https://www.kidsnote.com`** 클릭
4. 오른쪽 표에서 다음 행을 찾아 **`Value`** 컬럼 값을 복사 (메모장):
   - `Name` = `sessionid`
   - `Domain` = `.kidsnote.com` (앞에 점)

![sessionid 쿠키 위치](images/chrome-cookie-sessionid.png)

✅ 메모장에 30자 내외 영문+숫자 쿠키값이 있으면 성공.

---

## 7️⃣ GitHub Secrets 5개 등록 (5분)

1. 본인 fork 페이지 (`https://github.com/내깃허브아이디/kidsnote-backup`) → 메뉴줄 **`Settings`** 클릭
   - ⚠️ 우측 상단 프로필 옆 Settings가 아닌 **repo 안의 Settings**
2. 좌측 사이드바 **`Secrets and variables` → `Actions`** 클릭
3. **`New repository secret`** 버튼을 **5번** 클릭해서 다음 5개를 모두 등록:

| Name (대소문자·언더바 정확히) | 값 |
|---|---|
| `NOTION_TOKEN` | 3번에서 받은 토큰 |
| `NOTION_DATABASE_ID` | 5번에서 추출한 32자 hex |
| `KIDSNOTE_SESSION_COOKIE` | 6번에서 복사한 쿠키 |
| `KIDSNOTE_CHILD_NAME` | 백업할 자녀 이름 (예: `우하린`) |
| `AI_FEATURES` | `on` (AI 가공 켜기) 또는 `off` (AI 가공 끄기) |

✅ Secrets 목록에 위 5개 이름이 정확히 나타나면 성공.

> 👶 **`KIDSNOTE_CHILD_NAME`는 자녀 1명이라도 꼭 입력**. 부분 일치(대소문자 무시) 방식이라 글자 수 제한 없음 — `우하린`도 `정에스더`도 `유주`도 OK. 풀네임 또는 일부 어느 것을 적어도 같은 결과 (예: `정에스더`라면 `정에스더`/`에스더`/`스더` 다 매칭).

> 🤖 **`AI_FEATURES` 값 선택**:
> - **`off`** → 빠른 백업 (1년치 1-3시간). 알림장·사진·식단·통계 대시보드 2개만 깔끔히.
> - **`on`** → 느린 백업 (1년치 5-15시간, cron 자동 재개). 자녀 일기/부모 편지/LLM 대시보드 4종까지 추가.
>
> 어느 쪽이든 cron 자동 재실행에도 같은 값이 적용됩니다. 나중에 마음 바뀌면 값만 바꾸면 끝.

---

## 8️⃣ 실행 (1분 클릭 + 자동 진행)

1. fork 페이지 상단 **`Actions`** 탭 → 처음이면 `I understand my workflows, go ahead and enable them` 버튼 클릭
2. 좌측 **`Kidsnote → Notion mirror`** 클릭 → 우측 **`Run workflow ▾`**
3. **첫 테스트**: `limit` 칸에 **`3`** 입력 → 녹색 **`Run workflow`** 클릭
4. 몇 분 후 노션 DB 확인 — 알림장 3건 + 사진이 들어와 있으면 성공
5. **전체 백업**: 같은 메뉴에서 다시 `Run workflow` → 이번엔 `limit` **비워둠** → 클릭

> 🤖 **이후로는 자동**. 6시간마다 cron이 알아서 새 알림장 백업. 6시간 GitHub Actions cap에 걸려도 자동 재개. **사용자가 다시 할 일 없음**.

---

## 🎉 셋업 완료

이제 **컴퓨터를 꺼도** GitHub의 클라우드 서버가 알아서 백업합니다. 1년치 첫 백업이 끝나면 노션 DB에 알림장·사진·댓글·식단·앨범 + 통계 대시보드가 모두 정리돼요 (AI 켜뒀으면 자녀 일기/부모 편지/LLM 대시보드 4종도 추가). 이후 새 알림장은 6시간 안에 자동 추가.

---

## 📚 더 알아보기

이 문서는 **막힘없이 진행한 사용자 기준**의 최단 경로입니다. 다음 경우에는 [README.md](README.md)를 참고하세요:

- **자녀가 2명 이상**이면 → [`자녀가 여러 명이거나 AI 편지를 끄고 싶을 때`](README.md#advanced-multichild-and-ai-off) 섹션
- **AI 가공을 켜고/끄거나 일부만 끄고 싶음** → [`B. AI 가공 켜기/끄기`](README.md#b-ai-가공을-안-쓰고-단순-백업-통계만-원할-때)
- **어디서 막혔음 / 에러 발생** → [`문제 해결 (자주 나오는 에러)`](README.md#-문제-해결-자주-나오는-에러)
- **30일 후 cookie 만료 / 코드 업데이트 받기** → [`두 번째 이후 백업`](README.md#-두-번째-이후-백업--거의-안-해도-됨), [`Sync fork`](README.md#-코드-업데이트가-있을-때--내-fork-동기화)
- **노션 페이지를 웹에 공개하고 싶음** → [`(선택) 노션 페이지 웹 공개`](README.md#-선택-노션-페이지를-웹에-공개하기)
- **자주 묻는 질문** → [`자주 묻는 질문`](README.md#-자주-묻는-질문)
