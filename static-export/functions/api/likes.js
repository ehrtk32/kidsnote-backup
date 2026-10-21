/**
 * 사진 좋아요.
 *
 *   GET  /api/likes?post=123        한 글의 사진별 집계 (+ 내가 누른 것)
 *   GET  /api/likes?top=100         좋아요 많은 순 (사진 모아보기용)
 *   POST /api/likes                 { photo_id, post_id, child, voter } 토글
 *
 * photo_id 는 사진의 경로다. media_url() 이 report id 와 글 안에서의 순서
 * 만으로 파일 이름을 짓기 때문에 재-export 해도 바뀌지 않는다. 그래서 별도
 * 식별자 체계가 필요 없다.
 *
 * voter 는 브라우저가 localStorage 에 만드는 랜덤 문자열이다. 같은 사람이
 * 두 번 누르는 걸 막는 용도일 뿐 위조는 막지 못한다 — 쓰기를 열어 둔
 * 사이트라 그 이상은 의도적으로 하지 않는다.
 */
import {
  json,
  badRequest,
  readJson,
  normalizeChild,
  normalizeText,
  CHILD_KEYS,
} from "../_lib.js";

const MAX_TOP = 500;

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const voter = normalizeText(url.searchParams.get("voter"), 64);

  const topParam = url.searchParams.get("top");
  if (topParam !== null) {
    const limit = Math.min(Math.max(Number(topParam) || 100, 1), MAX_TOP);
    const { results } = await env.DB
      .prepare(
        `SELECT photo_id, post_id, child, COUNT(*) AS likes
           FROM likes
          GROUP BY photo_id
          ORDER BY likes DESC, MAX(created_at) DESC
          LIMIT ?`,
      )
      .bind(limit)
      .all();
    return json({ count: results.length, photos: results });
  }

  const postId = normalizeText(url.searchParams.get("post"), 100);
  if (!postId) return badRequest("post or top is required");

  const { results } = await env.DB
    .prepare(
      `SELECT photo_id,
              COUNT(*) AS likes,
              SUM(CASE WHEN voter = ? THEN 1 ELSE 0 END) AS mine
         FROM likes
        WHERE post_id = ?
        GROUP BY photo_id`,
    )
    .bind(voter, postId)
    .all();

  // 프런트가 다루기 쉽도록 photo_id 를 키로 돌려준다.
  const photos = {};
  for (const row of results) {
    photos[row.photo_id] = { likes: row.likes, mine: row.mine > 0 };
  }
  return json({ photos });
}

export async function onRequestPost({ request, env }) {
  const payload = await readJson(request);
  if (!payload) return badRequest("body must be JSON");

  const photoId = normalizeText(payload.photo_id, 400);
  if (!photoId) return badRequest("photo_id is required");

  const voter = normalizeText(payload.voter, 64);
  if (!voter) return badRequest("voter is required");

  const child = normalizeChild(payload.child);
  if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);

  const postId = normalizeText(payload.post_id, 100);

  // 토글. 이미 있으면 지우고, 없으면 넣는다. 눌린 상태를 클라이언트가
  // 알려 주게 하면 두 번 눌렀을 때 어긋나므로 서버가 현재 상태로 판단한다.
  const existing = await env.DB
    .prepare("SELECT 1 FROM likes WHERE photo_id = ? AND voter = ?")
    .bind(photoId, voter)
    .first();

  if (existing) {
    await env.DB
      .prepare("DELETE FROM likes WHERE photo_id = ? AND voter = ?")
      .bind(photoId, voter)
      .run();
  } else {
    await env.DB
      .prepare(
        `INSERT INTO likes (photo_id, voter, child, post_id, created_at)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(photo_id, voter) DO NOTHING`,
      )
      .bind(photoId, voter, child, postId, Date.now())
      .run();
  }

  const row = await env.DB
    .prepare("SELECT COUNT(*) AS likes FROM likes WHERE photo_id = ?")
    .bind(photoId)
    .first();

  return json({ ok: true, photo_id: photoId, likes: row?.likes ?? 0, mine: !existing });
}
