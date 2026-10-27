/**
 * 올린 영상을 영상 목록에 기록한다.
 *
 *   POST /api/uploads/video   { passcode, child, file_id, file_name, title? }
 *
 * 전용 표를 두지 않고 **기존 `videos` 표**에 `source='mybox'` 로 넣는다.
 * MYBOX 폴더 동기화로 들어온 영상과 저장 형태가 똑같기 때문이다 — 그래서
 * 재생(`/stream`), 썸네일, 관리자 삭제가 전부 그대로 동작한다. 따로 표를
 * 만들면 그 배선을 한 벌 더 만들어야 한다.
 *
 * 기존 `POST /api/videos` 는 공유 링크(`source='link'`) 전용이라 url 을
 * 요구한다. 올린 영상에는 url 이 없고 파일 id 만 있어서 여기로 나뉜다.
 */
import {
  json, badRequest, readJson, requireAdmin,
  normalizeChild, normalizeText, videoSlug, CHILD_KEYS,
} from "../../_lib.js";

export async function onRequestPost({ request, env }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  const child = normalizeChild(body.child);
  if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);

  const fileId = normalizeText(body.file_id, 300);
  if (!fileId) return badRequest("file_id is required");

  const fileName = normalizeText(body.file_name, 200) || "영상";
  // 제목이 없으면 파일명에서 확장자를 뗀다. 폴더 동기화가 쓰는 "N주차 M일"
  // 규칙을 여기서 흉내내지는 않는다 — 폰에서 올리는 영상은 그런 이름이 아니다.
  const title = normalizeText(body.title, 200)
    || fileName.replace(/\.[^.]+$/, "")
    || "영상";

  const id = await videoSlug(`mybox:${fileId}`);
  const now = Date.now();

  await env.DB
    .prepare(
      `INSERT INTO videos (id, child, source, url, mybox_file_id, title, video_date, seq, summary, created_at)
       VALUES (?, ?, 'mybox', '', ?, ?, '', 0, '', ?)
       ON CONFLICT(id) DO UPDATE SET
         child = excluded.child,
         title = excluded.title`,
    )
    .bind(id, child, fileId, title, now)
    .run();

  return json({ ok: true, id, title }, 201);
}
