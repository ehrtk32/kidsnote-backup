/**
 * 댓글 삭제.
 *
 *   DELETE /api/comments/{id}   { passcode }
 *
 * 쓰기는 누구나 할 수 있지만 지우는 건 관리자만 한다. 열어 둔 것은 남기는
 * 쪽이지 치우는 쪽이 아니다 — 아무나 지울 수 있으면 남긴 사람이 돌아왔을
 * 때 글이 사라져 있는 일이 생긴다.
 */
import { json, badRequest, readJson, requireAdmin } from "../../_lib.js";

export async function onRequestDelete({ request, env, params }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  const id = Number(params.id);
  if (!Number.isInteger(id)) return badRequest("id must be a number");

  const result = await env.DB
    .prepare("DELETE FROM comments WHERE id = ?")
    .bind(id)
    .run();

  // 이미 없는 것을 지워 달라는 요청도 성공으로 본다. 같은 버튼을 두 번
  // 눌렀을 때 에러가 뜨는 편이 더 당황스럽다.
  return json({ ok: true, deleted: result.meta?.changes ?? 0 });
}
