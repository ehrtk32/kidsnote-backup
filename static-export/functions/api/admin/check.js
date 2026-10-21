/**
 * 관리자 비밀번호 확인만 한다.
 *
 *   POST /api/admin/check   { passcode }
 *
 * 관리자 세션에 들어갈 때 쓴다. 이게 없으면 틀린 비번으로도 화면상
 * 관리자가 되고, 삭제 버튼은 보이는데 누를 때마다 실패하는 상태가 된다.
 *
 * 실패는 다른 관리자 요청과 같은 잠금 카운터를 탄다 — 여기만 공짜로
 * 시도할 수 있으면 잠금이 의미가 없다.
 */
import { json, badRequest, readJson, requireAdmin } from "../../_lib.js";

export async function onRequestPost({ request, env }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  return json({ ok: true });
}
