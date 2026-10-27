-- papa_pjh — 사이트에 사람이 남기는 것들.
--
-- Notion에서 내려오는 알림장·앨범·공지는 매 cron 마다 dist/ 가 통째로
-- 다시 만들어지므로, 사람이 쓴 데이터는 전부 여기에만 산다.
--
-- child 컬럼은 APP_JS 의 children[].key 와 같은 값을 쓴다("seoi", "second").
-- 그 key 는 표시 이름과 분리돼 있어서, 둘째의 태명이 실제 이름으로 바뀌어도
-- 여기 쌓인 행은 그대로 따라온다. key 를 바꾸면 전부 고아가 된다.

CREATE TABLE IF NOT EXISTS videos (
  -- id 는 출처 문자열의 sha256 앞 12자리다. 같은 링크·같은 파일을 다시
  -- 넣으면 중복이 생기는 대신 갱신된다.
  id          TEXT PRIMARY KEY,
  child       TEXT NOT NULL,

  -- 'link'  : 공유 링크를 iframe 으로 띄운다. url 을 쓴다.
  -- 'mybox' : MYBOX Open API 로 재생할 때마다 10분짜리 URL 을 받아
  --           <video> 로 튼다. mybox_file_id 를 쓴다. 그 URL 은 1회용이라
  --           여기 저장하지 않는다.
  source      TEXT NOT NULL DEFAULT 'link',
  url         TEXT NOT NULL DEFAULT '',
  mybox_file_id TEXT NOT NULL DEFAULT '',

  title       TEXT NOT NULL DEFAULT '',
  video_date  TEXT NOT NULL DEFAULT '',   -- 'YYYY-MM-DD', 비어도 됨
  -- 날짜가 없는 묶음(초음파 주차 등)의 순서. 클수록 먼저 온다.
  seq         INTEGER NOT NULL DEFAULT 0,
  summary     TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL            -- epoch ms
);

CREATE INDEX IF NOT EXISTS ix_videos_child
  ON videos (child, video_date DESC, seq DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS comments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  child       TEXT NOT NULL,
  target_type TEXT NOT NULL,              -- 'post' | 'video'
  target_id   TEXT NOT NULL,              -- report id 또는 video id
  -- 지금은 익명으로 받는다. 나중에 이름을 받고 싶어질 때 마이그레이션 없이
  -- 쓸 수 있도록 컬럼만 비워 둔다.
  author      TEXT NOT NULL DEFAULT '',
  body        TEXT NOT NULL,
  created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_comments_target
  ON comments (target_type, target_id, created_at);

-- 사진 좋아요.
--
-- 사람당 한 번이 아니라 누른 횟수를 센다. 로그인이 없어서 "같은 사람"을
-- 알 수 있는 방법이 없고, 브라우저 단위로 묶어 봐야 한 사람이 폰과 노트북
-- 에서 누르면 둘로 세고 거실 태블릿을 온 가족이 쓰면 하나로 센다. 그래서
-- 숫자의 뜻을 "몇 명이 좋아하나"의 근사치가 아니라 "몇 번 눌렸나"로 정했다.
-- 마음에 들면 여러 번 눌러도 되는 버튼이다.
--
-- photo_id 는 사진의 경로다. media_url() 이 report id 와 글 안에서의 순서
-- 만으로 파일 이름을 짓기 때문에 재-export 해도 바뀌지 않는다.
CREATE TABLE IF NOT EXISTS photo_likes (
  photo_id   TEXT PRIMARY KEY,
  child      TEXT NOT NULL,
  post_id    TEXT NOT NULL DEFAULT '',
  count      INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);

-- 모아보기의 "좋아요순" 정렬이 이 인덱스를 탄다.
CREATE INDEX IF NOT EXISTS ix_photo_likes_rank ON photo_likes (count DESC);
CREATE INDEX IF NOT EXISTS ix_photo_likes_post ON photo_likes (post_id);

CREATE TABLE IF NOT EXISTS auth_attempts (
  ip            TEXT PRIMARY KEY,
  fails         INTEGER NOT NULL DEFAULT 0,
  blocked_until INTEGER NOT NULL DEFAULT 0,  -- epoch ms
  updated_at    INTEGER NOT NULL
);

-- 런타임에 바뀌는 설정값.
--
-- 관리자 비번이 여기 산다. Pages Secret 에 두면 Function 이 바꿀 수 없어서
-- (시크릿은 배포 시점에만 쓰인다) 사이트에서 변경하려면 D1 로 내려와야 했다.
--
-- ADMIN_PASSCODE 시크릿은 부트스트랩으로만 남는다: 이 표에 행이 없을 때만
-- 쓰이고, 한 번 변경하면 그 뒤로는 무시된다. 새 비번을 잊었다면 이 행을
-- 지워서 시크릿으로 되돌아갈 수 있다 —
--   wrangler d1 execute papa_pjh --remote --command \
--     "DELETE FROM settings WHERE key='admin_passcode'"
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

-- 직접 올린 사진.
--
-- 영상은 여기 오지 않는다 — videos 표에 source='mybox' 로 들어가서 재생·
-- 썸네일·삭제 배선을 그대로 쓴다. 사진만 따로 두는 이유는 보여 주는 방식이
-- 전혀 다르기 때문이다(격자, 라이트박스, 좋아요).
--
-- MYBOX 에는 **두 벌**이 올라간다. 원본은 보관용이고, 웹용 사본(긴 변
-- 2048px JPEG)이 화면에 뜬다. 원본만 두면 아이폰 HEIC 가 안드로이드에서
-- 안 보이고, 사본만 두면 원본이 사라진다.
CREATE TABLE IF NOT EXISTS uploads (
  id             TEXT PRIMARY KEY,          -- sha256 앞 12자리
  child          TEXT NOT NULL,
  -- 원본. **빈 값일 수 있다** — 웹용 사본을 먼저 올리고 원본을 나중에
  -- 올리는데, 그 사이에 폰에서 앱을 바꾸면 원본만 못 올린 채로 남는다.
  -- 사진은 이미 보이고 보관본만 비어 있는 상태이며, 나중에 채울 수 있다.
  mybox_file_id  TEXT NOT NULL DEFAULT '',
  mybox_web_id   TEXT NOT NULL DEFAULT '',  -- 화면에 띄우는 사본
  file_name      TEXT NOT NULL DEFAULT '',
  caption        TEXT NOT NULL DEFAULT '',
  width          INTEGER NOT NULL DEFAULT 0,
  height         INTEGER NOT NULL DEFAULT 0,
  size           INTEGER NOT NULL DEFAULT 0,
  -- 찍은 날(있으면). 올린 날과 다르다 — 몇 년 전 사진을 오늘 올릴 수 있다.
  taken_at       TEXT NOT NULL DEFAULT '',
  created_at     INTEGER NOT NULL           -- 올린 시각 epoch ms
);

CREATE INDEX IF NOT EXISTS uploads_child_created
  ON uploads (child, created_at DESC);
