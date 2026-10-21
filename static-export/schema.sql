-- papa_pjh — 사이트에 사람이 남기는 것들.
--
-- Notion에서 내려오는 알림장·앨범·공지는 매 cron 마다 dist/ 가 통째로
-- 다시 만들어지므로, 사람이 쓴 데이터는 전부 여기에만 산다.
--
-- child 컬럼은 APP_JS 의 children[].key 와 같은 값을 쓴다("seoi", "second").
-- 그 key 는 표시 이름과 분리돼 있어서, 둘째의 태명이 실제 이름으로 바뀌어도
-- 여기 쌓인 행은 그대로 따라온다. key 를 바꾸면 전부 고아가 된다.

CREATE TABLE IF NOT EXISTS videos (
  id          TEXT PRIMARY KEY,           -- video_slug(url) 과 동일 규칙
  child       TEXT NOT NULL,
  url         TEXT NOT NULL,              -- 공유 링크 원본 (naver.me/... 등)
  title       TEXT NOT NULL DEFAULT '',
  video_date  TEXT NOT NULL DEFAULT '',   -- 'YYYY-MM-DD', 비어도 됨
  thumb_url   TEXT NOT NULL DEFAULT '',   -- og:image. 토큰이 만료되므로 캐시 취급
  created_at  INTEGER NOT NULL            -- epoch ms
);

CREATE INDEX IF NOT EXISTS ix_videos_child
  ON videos (child, video_date DESC, created_at DESC);

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

CREATE TABLE IF NOT EXISTS likes (
  -- 사진의 정체성은 파일 경로다. media_url() 이 report id 와 글 안에서의
  -- 순서만으로 이름을 짓기 때문에 재-export 해도 바뀌지 않는다.
  --   /wp-content/uploads/2026/10/kidsnote-1393764239-image-2.jpg
  photo_id    TEXT NOT NULL,
  -- 브라우저가 localStorage 에 만드는 랜덤 id. 같은 사람이 두 번 누르는 걸
  -- 막는 용도일 뿐이고, 위조를 막지는 못한다 — 쓰기를 열어둔 사이트라
  -- 그 이상은 의도적으로 하지 않는다.
  voter       TEXT NOT NULL,
  child       TEXT NOT NULL,
  post_id     TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (photo_id, voter)
);

CREATE INDEX IF NOT EXISTS ix_likes_photo ON likes (photo_id);
CREATE INDEX IF NOT EXISTS ix_likes_child ON likes (child, created_at DESC);
