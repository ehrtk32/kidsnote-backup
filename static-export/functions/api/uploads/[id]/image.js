/**
 * 올린 사진 중계.
 *
 *   GET /api/uploads/{id}/image
 *
 * 영상의 /stream 과 같은 이유로 거쳐 간다. MYBOX 다운로드 URL 은 사진도
 * `content-type: application/octet-stream` 으로 주고, 게다가
 * `content-disposition: attachment` 가 붙어 있다 — 그대로 <img> 에 물리면
 * 표시되는 대신 **다운로드가 시작된다**.
 *
 * 여기서 타입을 바로잡고 disposition 을 떼고 흘려 보낸다.
 *
 * 영상과 달리 캐시를 길게 둔다. 사진은 한 번 올리면 바뀌지 않고, 모아보기
 * 격자에서 수십 장이 한꺼번에 뜨기 때문에 매번 받으면 폰에서 느리다.
 */
import { json } from "../../../_lib.js";
import { myboxGet } from "../../mybox/_client.js";

const TYPE_BY_EXT = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  webp: "image/webp",
  gif: "image/gif",
  heic: "image/heic",
  heif: "image/heif",
};

function guessType(name) {
  const ext = String(name || "").split(".").pop()?.toLowerCase();
  return TYPE_BY_EXT[ext] || "image/jpeg";
}

export async function onRequestGet({ env, params }) {
  const id = String(params.id ?? "").trim();
  if (!id) return json({ error: "id is required" }, 400);

  const row = await env.DB
    .prepare("SELECT mybox_web_id, mybox_file_id, file_name FROM uploads WHERE id = ?")
    .bind(id)
    .first();

  if (!row) return json({ error: "not found" }, 404);
  if (!env.MYBOX_TOKEN) return json({ error: "server is missing MYBOX_TOKEN" }, 503);

  // 사본이 정상이고, 없을 때만 원본으로 떨어진다. 둘 다 없으면 행이 잘못된 것.
  const fileId = row.mybox_web_id || row.mybox_file_id;
  if (!fileId) return json({ error: "no file behind this row" }, 502);

  const issued = await myboxGet(
    env,
    `/v1/drive/files/${encodeURIComponent(fileId)}/download`,
  );
  if (!issued.ok || !issued.data?.downloadUrl) {
    return json({ error: "could not issue a download url" }, 502);
  }

  const upstream = await fetch(issued.data.downloadUrl);
  if (!upstream.ok) {
    return json({ error: "upstream refused", status: upstream.status }, 502);
  }

  const headers = new Headers();
  // 사본은 항상 JPEG 이지만, 사본이 없어 원본으로 떨어진 경우를 위해 이름을 본다.
  headers.set("Content-Type", row.mybox_web_id ? "image/jpeg" : guessType(row.file_name));
  headers.set("Cache-Control", "public, max-age=86400");
  const length = upstream.headers.get("Content-Length");
  if (length) headers.set("Content-Length", length);

  return new Response(upstream.body, { status: 200, headers });
}
