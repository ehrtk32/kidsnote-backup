/**
 * 댓글.
 *
 *   GET  /api/comments?type=post&id=123
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

const TARGET_TYPES = ["post", "video"];
const MAX_BODY = 1000;
const PAGE_SIZE = 200;

// 한 IP 가 1분에 남길 수 있는 댓글 수. 사람이 손으로 쓰는 속도와는 무관한
// 값이라 가족 입장에선 걸릴 일이 없고, 스크립트는 걸린다.
const RATE_WINDOW_MS = 60_000;
const RATE_MAX = 10;

function readTarget(params) {
  const type = String(params.get("type") ?? "").trim();
  const id = String(params.get("id") ?? "").trim();
  if (!TARGET_TYPES.includes(type)) return null;
  if (!id || id.length > 100) return null;
  return { type, id };
}

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const target = readTarget(url.searchParams);
  if (!target) return badRequest("type must be post|video and id is required");

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
  if (!TARGET_TYPES.includes(type)) return badRequest("type must be post|video");
  if (!id || id.length > 100) return badRequest("id is required");

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
