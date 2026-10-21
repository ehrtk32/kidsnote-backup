/**
 * 사진 좋아요 — 누른 횟수를 센다.
 *
 *   GET  /api/likes?post=123     한 글의 사진별 횟수
 *   GET  /api/likes?top=500      많이 눌린 순 (사진 모아보기용)
 *   POST /api/likes              { photo_id, post_id, child }  →  +1
 *
 * 사람당 한 번이 아니다. 로그인이 없으니 "같은 사람"을 알 방법이 없고,
 * 브라우저 단위로 묶어도 한 사람이 폰과 노트북에서 누르면 둘로, 거실
 * 태블릿을 온 가족이 쓰면 하나로 센다. 어느 쪽도 사람 수가 아니라서,
 * 숫자의 뜻을 "몇 번 눌렸나"로 정했다 — 마음에 들면 여러 번 눌러도 되는
 * 버튼이고, 그래서 되돌리기도 없다.
 */
import {
  json,
  badRequest,
  readJson,
  clientIp,
  normalizeChild,
  normalizeText,
  CHILD_KEYS,
} from "../_lib.js";

const MAX_TOP = 500;

// 한 IP 가 1분에 올릴 수 있는 횟수. 여러 번 누르는 것이 기능이므로 넉넉
// 하지만, 스크립트로 수천을 꽂는 것과는 자릿수가 다르다.
const RATE_WINDOW_MS = 60_000;
const RATE_MAX = 120;

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);

  const topParam = url.searchParams.get("top");
  if (topParam !== null) {
    const limit = Math.min(Math.max(Number(topParam) || 100, 1), MAX_TOP);
    const { results } = await env.DB
      .prepare(
        `SELECT photo_id, post_id, child, count
           FROM photo_likes
          WHERE count > 0
          ORDER BY count DESC, updated_at DESC
          LIMIT ?`,
      )
      .bind(limit)
      .all();
    return json({ count: results.length, photos: results });
  }

  const postId = normalizeText(url.searchParams.get("post"), 100);
  if (!postId) return badRequest("post or top is required");

  const { results } = await env.DB
    .prepare("SELECT photo_id, count FROM photo_likes WHERE post_id = ?")
    .bind(postId)
    .all();

  const photos = {};
  for (const row of results) photos[row.photo_id] = { count: row.count };
  return json({ photos });
}

export async function onRequestPost({ request, env }) {
  const payload = await readJson(request);
  if (!payload) return badRequest("body must be JSON");

  const photoId = normalizeText(payload.photo_id, 400);
  if (!photoId) return badRequest("photo_id is required");

  const child = normalizeChild(payload.child);
  if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);

  const postId = normalizeText(payload.post_id, 100);
  const now = Date.now();

  // 댓글 쪽과 같은 카운터 표를 쓴다. ip 를 키로 창을 들고 있는 표가 이미
  // 있는데 세 번째를 만들 이유가 없다.
  const bucketKey = `like:${clientIp(request)}`;
  const bucket = await env.DB
    .prepare("SELECT fails, blocked_until FROM auth_attempts WHERE ip = ?")
    .bind(bucketKey)
    .first();

  if (bucket && bucket.blocked_until > now && bucket.fails >= RATE_MAX) {
    return json(
      {
        error: "too many likes",
        retry_after_seconds: Math.ceil((bucket.blocked_until - now) / 1000),
      },
      429,
    );
  }

  const [, , row] = await env.DB.batch([
    env.DB
      .prepare(
        `INSERT INTO photo_likes (photo_id, child, post_id, count, updated_at)
         VALUES (?, ?, ?, 1, ?)
         ON CONFLICT(photo_id) DO UPDATE SET
           count = photo_likes.count + 1,
           updated_at = excluded.updated_at`,
      )
      .bind(photoId, child, postId, now),
    env.DB
      .prepare(
        `INSERT INTO auth_attempts (ip, fails, blocked_until, updated_at)
         VALUES (?, 1, ?, ?)
         ON CONFLICT(ip) DO UPDATE SET
           fails = CASE WHEN auth_attempts.blocked_until > ? THEN auth_attempts.fails + 1 ELSE 1 END,
           blocked_until = CASE WHEN auth_attempts.blocked_until > ? THEN auth_attempts.blocked_until ELSE ? END,
           updated_at = ?`,
      )
      .bind(bucketKey, now + RATE_WINDOW_MS, now, now, now, now + RATE_WINDOW_MS, now),
    env.DB.prepare("SELECT count FROM photo_likes WHERE photo_id = ?").bind(photoId),
  ]);

  return json({ ok: true, photo_id: photoId, count: row.results?.[0]?.count ?? 1 });
}
