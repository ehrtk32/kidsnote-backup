/**
 * 관리자 비밀번호 변경.
 *
 *   POST /api/admin/passcode   { passcode, new_passcode }
 *
 * 비번은 settings 표에 PBKDF2 해시로 들어간다. ADMIN_PASSCODE 시크릿은
 * 그 행이 없을 때만 쓰이므로, 여기서 한 번 바꾸고 나면 시크릿은 더 이상
 * 통하지 않는다. 새 비번을 잊었다면 그 행을 지워 시크릿으로 되돌아갈 수
 * 있다 (CLAUDE.md 에 명령 적어 둠).
 */
import { json, badRequest, readJson, requireAdmin, savePasscode } from "../../_lib.js";

const MIN_LENGTH = 4;
const MAX_LENGTH = 64;

export async function onRequestPost({ request, env }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  // 현재 비번 확인이 먼저다. 실패하면 잠금 카운터도 여기서 올라간다.
  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  const next = String(body.new_passcode ?? "").trim();
  if (next.length < MIN_LENGTH || next.length > MAX_LENGTH) {
    return badRequest(`new passcode must be ${MIN_LENGTH}-${MAX_LENGTH} characters`);
  }
  if (next === String(body.passcode ?? "")) {
    return badRequest("new passcode must differ from the current one");
  }

  await savePasscode(env, next);
  return json({ ok: true });
}
