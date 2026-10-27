/**
 * 올린 사진 삭제.
 *
 *   DELETE /api/uploads/{id}   { passcode }
 *
 * **D1 에서만 지운다. MYBOX 원본은 남는다.** MYBOX Open API 에 삭제가
 * 없기 때문이다 — 파일·폴더 모두 DELETE/PUT/PATCH 가 404 다(2026-10-27 확인).
 * 그래서 이건 "사이트에서 내린다"는 뜻이지 "지운다"가 아니다. 화면에서도
 * 그렇게 말해야 한다.
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
    .prepare("DELETE FROM uploads WHERE id = ?")
    .bind(id)
    .run();

  return json({ ok: true, removed: result.meta?.changes ?? 0 });
}
