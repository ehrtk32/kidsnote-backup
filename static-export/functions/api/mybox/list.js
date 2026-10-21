/**
 * MYBOX 폴더 목록 조회.
 *
 *   POST /api/mybox/list   { passcode, folder? , cursor? }
 *
 * folder 를 비우면 루트를 본다. 응답은 cursor 로 이어서 받는다.
 *
 * 관리자 전용이다. 이건 개인 드라이브를 읽는 것이고, 쓰기를 열어 둔 댓글·
 * 좋아요와는 성격이 다르다.
 *
 * 지금 영상 재생은 이 API 와 무관하다 — naver.me 공유 링크를 iframe 으로
 * 띄우는 방식 그대로다. 이 연동은 그 옆에 붙어 있는 배선이고, 영상이 늘어
 * 수동 등록이 번거로워질 때 쓰려고 미리 깔아 둔 것이다.
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

  const folder = normalizeText(body.folder, 200);
  const cursor = normalizeText(body.cursor, 500);
  const params = new URLSearchParams({ count: "100" });
  if (cursor) params.set("cursor", cursor);

  // 루트와 하위 폴더는 경로가 다르다.
  const path = folder
    ? `/v1/drive/folders/${encodeURIComponent(folder)}/resources?${params}`
    // 루트는 /v1/drive/resources 다. /v1/drive/root/resources 로 쓰면 404.
    : `/v1/drive/resources?${params}`;

  const result = await myboxGet(env, path);
  if (!result.ok) return myboxError(result);

  const data = result.data;
  return json({
    ok: true,
    folder: folder || "root",
    file_count: data.fileCount ?? null,
    sub_folder_count: data.subFolderCount ?? null,
    cursor: data.responseMetaData?.cursor ?? null,
    resources: (data.resources || []).map((item) => ({
      id: item.resourceId,
      name: item.name,
      type: item.type,
      category: item.category ?? "",
      size: item.size ?? 0,
      created_at: item.createdAt ?? "",
      modified_at: item.modifiedAt ?? "",
    })),
  });
}
