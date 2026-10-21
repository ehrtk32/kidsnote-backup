/**
 * 영상 하나 삭제.
 *
 *   DELETE /api/videos/:id    { passcode }
 *
 * 목록·추가는 ../videos.js 에 있다.
 */
import { json, badRequest, readJson, requireAdmin } from "../../_lib.js";

export async function onRequestDelete({ request, env, params }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  const id = String(params.id ?? "").trim();
  if (!id) return badRequest("id is required");

  const result = await env.DB
    .prepare("DELETE FROM videos WHERE id = ?")
    .bind(id)
    .run();

  // 이미 없는 것을 지워 달라는 요청을 실패로 보지 않는다. 같은 삭제를 두 번
  // 눌러도 같은 결과가 나오는 편이 다루기 쉽다.
  return json({ ok: true, deleted: result.meta?.changes ?? 0 });
}
