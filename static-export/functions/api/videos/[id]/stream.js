/**
 * MYBOX 영상 중계.
 *
 *   GET /api/videos/{id}/stream
 *
 * MYBOX 가 주는 다운로드 URL 은 바이트는 mp4 인데 `content-type` 이
 * `application/octet-stream` 이라, 브라우저의 <video> 가 재생을 거부한다
 * (MediaError 4 = SRC_NOT_SUPPORTED). 그래서 여기서 한 번 거쳐 타입만
 * 바로잡아 흘려 보낸다. 본문은 그대로 통과시킨다.
 *
 * Range 를 그대로 넘기는 것이 중요하다. 넘기지 않으면 탐색(seek)이 안 되고,
 * 60MB 짜리를 처음부터 다 받아야 한다. MYBOX 쪽은 Range 를 지원한다
 * (content-range 로 확인).
 *
 * 10분짜리 URL 은 요청마다 새로 받는다. 저장해 두면 만료된 뒤 재생이
 * 깨지는데, 그 증상이 "가끔 안 나옴"이라 원인을 찾기 어렵다.
 */
import { json } from "../../../_lib.js";
import { myboxGet } from "../../mybox/_client.js";

const TYPE_BY_EXT = {
  mp4: "video/mp4",
  m4v: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
  avi: "video/x-msvideo",
  mkv: "video/x-matroska",
};

function guessType(name) {
  const ext = String(name || "").split(".").pop()?.toLowerCase();
  return TYPE_BY_EXT[ext] || "video/mp4";
}

export async function onRequestGet({ request, env, params }) {
  const id = String(params.id ?? "").trim();
  if (!id) return json({ error: "id is required" }, 400);

  const row = await env.DB
    .prepare("SELECT source, mybox_file_id, title FROM videos WHERE id = ?")
    .bind(id)
    .first();

  if (!row || row.source !== "mybox") return json({ error: "not found" }, 404);
  if (!env.MYBOX_TOKEN) return json({ error: "server is missing MYBOX_TOKEN" }, 503);

  const issued = await myboxGet(
    env,
    `/v1/drive/files/${encodeURIComponent(row.mybox_file_id)}/download`,
  );
  if (!issued.ok || !issued.data?.downloadUrl) {
    return json({ error: "could not issue a download url" }, 502);
  }

  const range = request.headers.get("Range");
  const upstream = await fetch(issued.data.downloadUrl, {
    headers: range ? { Range: range } : {},
  });

  if (!upstream.ok && upstream.status !== 206) {
    return json({ error: "upstream refused", status: upstream.status }, 502);
  }

  const headers = new Headers();
  headers.set("Content-Type", guessType(row.title));
  headers.set("Accept-Ranges", "bytes");
  // 상류 URL 은 **1회용**이라(두 번째 사용에 403) 발급 횟수를 캐시로 줄일 수
  // 없다. 줄일 수 있는 것은 브라우저가 같은 구간을 다시 달라고 하는 횟수다.
  // no-store 로 두면 메타데이터·탐색·버퍼링이 서로의 결과를 못 쓰고 매번
  // 새로 받는다 — 썸네일 18개짜리 목록 한 번에 /stream 이 101번 불렸다.
  //
  // 응답에 서명된 URL 은 들어 있지 않고 바이트만 흐르므로 캐시해도 샐 것이
  // 없다. 공유 캐시까지 가지 않게 private 으로 둔다.
  headers.set("Cache-Control", "private, max-age=600");
  for (const key of ["Content-Length", "Content-Range"]) {
    const value = upstream.headers.get(key);
    if (value) headers.set(key, value);
  }

  return new Response(upstream.body, { status: upstream.status, headers });
}
