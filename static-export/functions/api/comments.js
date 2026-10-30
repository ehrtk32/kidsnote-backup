/**
 * 댓글.
 *
 *   GET  /api/comments?type=post&id=123
 *   GET  /api/comments?counts=photo        사진별 댓글 수 (뱃지용)
 *   POST /api/comments   { type, id, child, body }
 *
 * 쓰기에 인증이 없다. 사이트를 전부 공개로 두기로 한 결정의 연장이고,
 * 의도된 것이다. 대신 길이와 개수는 막는다 — 열어 두는 것과 무제한으로
 * 쌓이게 두는 것은 다르다.
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

// 사진의 target_id 는 사진 경로다. photo_likes 의 photo_id 와 같은 값이라,
// 같은 사진이 두 표에서 같은 이름으로 묶인다.
const TARGET_TYPES = ["post", "video", "photo"];
const MAX_BODY = 1000;
const PAGE_SIZE = 200;

// 뱃지는 "댓글이 달렸다"를 보여주는 것이지 순위가 아니다. 가족이 쓰는
// 사이트에서 댓글 달린 사진이 천 장을 넘을 일은 없고, 넘으면 그때는
// 사진별로 따로 묻는 게 맞다.
const COUNTS_LIMIT = 1000;

// 한 IP 가 1분에 남길 수 있는 댓글 수. 사람이 손으로 쓰는 속도와는 무관한
// 값이라 가족 입장에선 걸릴 일이 없고, 스크립트는 걸린다.
const RATE_WINDOW_MS = 60_000;
const RATE_MAX = 10;

// 사진의 id 는 경로라서 report id 나 video id 보다 길다. photo_likes 가
// photo_id 에 쓰는 한도와 같은 값으로 맞춘다.
const MAX_TARGET_ID = 400;

function readTarget(params) {
  const type = String(params.get("type") ?? "").trim();
  const id = String(params.get("id") ?? "").trim();
  if (!TARGET_TYPES.includes(type)) return null;
  if (!id || id.length > MAX_TARGET_ID) return null;
  return { type, id };
}

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);

  // 사진을 하나씩 열 때마다 묻지 않으려고 한 번에 받아 둔다. 6,700장을
  // 넘기면서 장마다 요청을 하나씩 더 보낼 수는 없다.
  const counts = String(url.searchParams.get("counts") ?? "").trim();
  if (counts) {
    if (!TARGET_TYPES.includes(counts)) {
      return badRequest(`counts must be one of ${TARGET_TYPES.join(", ")}`);
    }
    const { results } = await env.DB
      .prepare(
        `SELECT target_id, COUNT(*) AS count
           FROM comments
          WHERE target_type = ?
          GROUP BY target_id
          ORDER BY count DESC
          LIMIT ?`,
      )
      .bind(counts, COUNTS_LIMIT)
      .all();
    const map = {};
    for (const row of results) map[row.target_id] = row.count;
    return json({ counts: map });
  }

  const target = readTarget(url.searchParams);
  if (!target) return badRequest(`type must be one of ${TARGET_TYPES.join("|")} and id is required`);

  const { results } = await env.DB
    .prepare(
      `SELECT id, body, created_at
         FROM comments
        WHERE target_type = ? AND target_id = ?
        ORDER BY created_at ASC
        LIMIT ?`,
    )
    .bind(target.type, target.id, PAGE_SIZE)
    .all();

  return json({ count: results.length, comments: results });
}

export async function onRequestPost({ request, env }) {
  const payload = await readJson(request);
  if (!payload) return badRequest("body must be JSON");

  const type = String(payload.type ?? "").trim();
  const id = String(payload.id ?? "").trim();
  if (!TARGET_TYPES.includes(type)) return badRequest(`type must be one of ${TARGET_TYPES.join("|")}`);
  if (!id || id.length > MAX_TARGET_ID) return badRequest("id is required");

  const child = normalizeChild(payload.child);
  if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);

  const body = normalizeText(payload.body, MAX_BODY);
  if (!body) return badRequest("body is required");

  const ip = clientIp(request);
  const now = Date.now();

  // 최근 1분 안에 이 IP 가 남긴 수. comments 에 ip 를 저장하지는 않으므로
  // auth_attempts 를 같은 용도로 쓴다 — ip 를 키로 카운터를 들고 있는 표가
  // 이미 있는데 하나 더 만들 이유가 없다.
  const bucketKey = `comment:${ip}`;
  const bucket = await env.DB
    .prepare("SELECT fails, blocked_until FROM auth_attempts WHERE ip = ?")
    .bind(bucketKey)
    .first();

  const windowOpen = bucket && bucket.blocked_until > now;
  const used = windowOpen ? bucket.fails : 0;
  if (used >= RATE_MAX) {
    return json(
      {
        error: "too many comments",
        retry_after_seconds: Math.ceil((bucket.blocked_until - now) / 1000),
      },
      429,
    );
  }

  await env.DB.batch([
    env.DB
      .prepare(
        `INSERT INTO comments (child, target_type, target_id, author, body, created_at)
         VALUES (?, ?, ?, '', ?, ?)`,
      )
      .bind(child, type, id, body, now),
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
  ]);

  return json({ ok: true, comment: { body, created_at: now } }, 201);
}
