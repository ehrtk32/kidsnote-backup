/**
 * 영상 목록.
 *
 *   GET  /api/videos?child=seoi      목록 (공개)
 *   POST /api/videos                 추가 (관리자 비번 필요)
 *
 * 영상이 Notion 이 아니라 여기 있는 이유: 용량이 노션 무료 플랜의 5 MiB 를
 * 넘어서 외부 스토리지에 두기 때문이고, 또 dist/ 는 cron 이 6시간마다 통째로
 * 다시 만들기 때문이다. 사람이 추가한 것은 dist/ 에 남길 수 없다.
 */
import {
  json,
  badRequest,
  readJson,
  requireAdmin,
  normalizeChild,
  normalizeUrl,
  normalizeDate,
  normalizeText,
  videoSlug,
  fetchLinkTitle,
  titleFromUrl,
  CHILD_KEYS,
} from "../_lib.js";

const MAX_TITLE = 200;
const MAX_SUMMARY = 500;

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const childParam = url.searchParams.get("child");

  let statement;
  if (childParam) {
    const child = normalizeChild(childParam);
    if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);
    statement = env.DB
      .prepare(
        `SELECT id, child, source, url, title, video_date, seq, summary, created_at
           FROM videos WHERE child = ?
          ORDER BY video_date DESC, seq DESC, created_at DESC`,
      )
      .bind(child);
  } else {
    statement = env.DB.prepare(
      `SELECT id, child, source, url, title, video_date, seq, summary, created_at
         FROM videos
        ORDER BY video_date DESC, seq DESC, created_at DESC`,
    );
  }

  const { results } = await statement.all();
  return json({
    count: results.length,
    videos: results.map((row) => ({
      id: row.id,
      child: row.child,
      source: row.source || "link",
      url: row.url,
      title: row.title,
      // 프런트가 쓰는 표시 형식은 글과 같은 'YYYY.MM.DD' 다.
      date: row.video_date ? row.video_date.replaceAll("-", ".") : "",
      sort_key: row.video_date,
      summary: row.summary,
    })),
  });
}

export async function onRequestPost({ request, env }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  const child = normalizeChild(body.child);
  if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);

  const url = normalizeUrl(body.url);
  if (!url) return badRequest("url must be a http(s) link");

  const videoDate = normalizeDate(body.date);
  if (videoDate === null) return badRequest("date must be YYYY-MM-DD");

  // 제목은 선택이다. 비워 두면 공유 링크가 스스로 말하는 제목(og:title 또는
  // <title>)을 한 번 읽어 쓴다. 그마저 없으면 URL 의 마지막 조각을 쓴다 —
  // 목록에서 무엇인지 구분은 돼야 하므로 빈 제목으로 두지는 않는다.
  let title = normalizeText(body.title, MAX_TITLE);
  if (!title) {
    title = normalizeText(await fetchLinkTitle(url), MAX_TITLE)
      || normalizeText(titleFromUrl(url), MAX_TITLE)
      || "영상";
  }

  const summary = normalizeText(body.summary, MAX_SUMMARY);
  const id = await videoSlug(url);
  const now = Date.now();

  // 같은 링크를 다시 넣으면 중복이 생기는 대신 내용이 갱신된다. id 가 URL 에서
  // 나오므로 이게 자연스러운 동작이다.
  await env.DB
    .prepare(
      `INSERT INTO videos (id, child, source, url, title, video_date, summary, created_at)
       VALUES (?, ?, 'link', ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         child = excluded.child,
         title = excluded.title,
         video_date = excluded.video_date,
         summary = excluded.summary`,
    )
    .bind(id, child, url, title, videoDate, summary, now)
    .run();

  return json({
    ok: true,
    video: {
      id,
      child,
      source: "link",
      url,
      title,
      date: videoDate ? videoDate.replaceAll("-", ".") : "",
      sort_key: videoDate,
      summary,
    },
  }, 201);
}
