/**
 * MYBOX 폴더 하나를 영상 목록으로 가져온다.
 *
 *   POST /api/mybox/sync   { passcode, folder, child, dry_run? }
 *
 * 폴더 안의 동영상 파일을 videos 표에 넣는다. id 가 파일 id 에서 나오므로
 * 같은 폴더를 다시 가져와도 중복이 생기지 않고 제목·순서만 갱신된다.
 *
 * 재생 URL 은 여기서 받지 않는다. MYBOX 가 주는 URL 은 1회용 10분짜리라
 * 저장하는 순간 쓸모가 없어진다 — 재생할 때 /api/videos/{id}/play 가 그때
 * 받아 쓴다.
 */
import {
  json,
  badRequest,
  readJson,
  requireAdmin,
  normalizeChild,
  normalizeText,
  videoSlug,
  CHILD_KEYS,
} from "../../_lib.js";
import { myboxGet, myboxError } from "./_client.js";

const MAX_FILES = 500;

// "10주차_0일_1.671804132E9_...mp4" 처럼 주차가 이름에 들어 있는 경우.
// 초음파 영상이 이 꼴이라 제목과 순서를 여기서 뽑는다.
const WEEK_PATTERN = /^(\d{1,2})주차[_\s]*(\d{1,2})?일?/;

function parseName(name) {
  const stem = name.replace(/\.[^.]+$/, "");
  const match = stem.match(WEEK_PATTERN);
  if (match) {
    const week = Number(match[1]);
    const day = Number(match[2] ?? 0);
    return {
      title: `${week}주차 ${day}일`,
      // 주차가 클수록 앞에 온다. 나머지 화면이 전부 최신 우선이라 맞춘다.
      seq: week * 100 + day,
    };
  }
  return { title: stem, seq: 0 };
}

export async function onRequestPost({ request, env }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  if (!env.MYBOX_TOKEN) {
    return json({ error: "server is missing MYBOX_TOKEN" }, 503);
  }

  const child = normalizeChild(body.child);
  if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);

  const folder = normalizeText(body.folder, 200);
  if (!folder) return badRequest("folder is required");

  // 폴더가 커도 한 번에 가져온다. cursor 가 끝날 때까지 돈다.
  const files = [];
  let cursor = "";
  for (let page = 0; page < 10; page += 1) {
    const params = new URLSearchParams({ count: "1000" });
    if (cursor) params.set("cursor", cursor);
    const result = await myboxGet(
      env,
      `/v1/drive/folders/${encodeURIComponent(folder)}/resources?${params}`,
    );
    if (!result.ok) return myboxError(result);

    for (const item of result.data?.resources || []) {
      // 동영상만 가져온다. 사진은 키즈노트 쪽에서 이미 내려온다.
      if (item.type === "file" && item.category === "video") files.push(item);
    }
    cursor = result.data?.responseMetaData?.cursor || "";
    if (!cursor || files.length >= MAX_FILES) break;
  }

  const videos = [];
  for (const file of files.slice(0, MAX_FILES)) {
    const { title, seq } = parseName(file.name || "");
    videos.push({
      id: await videoSlug(`mybox:${file.resourceId}`),
      child,
      source: "mybox",
      url: "",
      mybox_file_id: file.resourceId,
      title: normalizeText(title, 200) || file.name,
      seq,
      size: file.size ?? 0,
    });
  }

  if (body.dry_run) {
    return json({ ok: true, dry_run: true, count: videos.length, videos });
  }

  const now = Date.now();
  // D1 배치는 100개씩 끊는다.
  for (let i = 0; i < videos.length; i += 100) {
    const chunk = videos.slice(i, i + 100).map((v) =>
      env.DB
        .prepare(
          `INSERT INTO videos (id, child, source, url, mybox_file_id, title, video_date, seq, summary, created_at)
           VALUES (?, ?, 'mybox', '', ?, ?, '', ?, '', ?)
           ON CONFLICT(id) DO UPDATE SET
             child = excluded.child,
             title = excluded.title,
             seq = excluded.seq`,
        )
        .bind(v.id, v.child, v.mybox_file_id, v.title, v.seq, now),
    );
    if (chunk.length) await env.DB.batch(chunk);
  }

  return json({ ok: true, count: videos.length, child, folder });
}
