/**
 * MYBOX 파일의 재생/다운로드 URL 발급.
 *
 *   POST /api/mybox/download   { passcode, file }
 *
 * MYBOX 가 주는 URL 은 **1회용이고 10분**짜리다. 그래서 이 값을 D1 에
 * 저장하면 안 된다 — 필요한 순간에 받아 바로 쓰고 버리는 값이다. og:image
 * 썸네일을 포기했던 이유와 같은 성질이다.
 */
import { json, badRequest, readJson, requireAdmin, normalizeText } from "../../_lib.js";
import { myboxGet, myboxError } from "./_client.js";

export async function onRequestPost({ request, env }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  if (!env.MYBOX_TOKEN) {
    return json({ error: "server is missing MYBOX_TOKEN" }, 503);
  }

  const fileId = normalizeText(body.file, 200);
  if (!fileId) return badRequest("file is required");

  const result = await myboxGet(
    env,
    `/v1/drive/files/${encodeURIComponent(fileId)}/download`,
  );
  if (!result.ok) return myboxError(result);

  return json({
    ok: true,
    file: fileId,
    download_url: result.data?.downloadUrl ?? "",
    expires_in: result.data?.expiresIn ?? 0,
  });
}
