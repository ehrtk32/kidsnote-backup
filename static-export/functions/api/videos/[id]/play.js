/**
 * 영상 재생에 쓸 URL.
 *
 *   GET /api/videos/{id}/play
 *
 * MYBOX 에서 가져온 영상만 해당한다. 공유 링크 영상은 iframe 으로 바로
 * 띄우므로 이 경로가 필요 없다.
 *
 * 관리자 인증이 없다. 보려는 사람은 가족이고 사이트 자체가 공개라, 등록된
 * 영상을 보는 데까지 비번을 요구할 이유가 없다. 다만 열리는 것은 **우리가
 * 명시적으로 등록한 파일**뿐이다 — 이 경로로는 드라이브를 뒤질 수 없고,
 * 임의의 MYBOX 파일 id 를 넣어도 videos 표에 없으면 404 다.
 */
import { json } from "../../../_lib.js";
import { myboxGet, myboxError } from "../../mybox/_client.js";

export async function onRequestGet({ env, params }) {
  const id = String(params.id ?? "").trim();
  if (!id) return json({ error: "id is required" }, 400);

  const row = await env.DB
    .prepare("SELECT source, url, mybox_file_id, title FROM videos WHERE id = ?")
    .bind(id)
    .first();

  if (!row) return json({ error: "not found" }, 404);

  if (row.source !== "mybox") {
    // 공유 링크 영상. 프런트가 iframe 으로 띄우면 된다.
    return json({ ok: true, source: row.source, url: row.url, title: row.title });
  }

  if (!env.MYBOX_TOKEN) {
    return json({ error: "server is missing MYBOX_TOKEN" }, 503);
  }

  const result = await myboxGet(
    env,
    `/v1/drive/files/${encodeURIComponent(row.mybox_file_id)}/download`,
  );
  if (!result.ok) return myboxError(result);

  return json({
    ok: true,
    source: "mybox",
    title: row.title,
    // 10분짜리 1회용이다. 저장하지 말고 바로 <video src> 에 쓸 것.
    url: result.data?.downloadUrl ?? "",
    expires_in: result.data?.expiresIn ?? 0,
  });
}
