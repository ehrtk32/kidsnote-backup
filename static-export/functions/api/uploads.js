/**
 * 올린 사진 목록과 기록.
 *
 *   GET  /api/uploads?child=seoi
 *   POST /api/uploads   { passcode, child, web_id, file_id?, ... }
 *
 * 바이트는 브라우저가 MYBOX 로 직접 보낸다(`uploads/url` 참고). 여기로는
 * **올렸다는 사실만** 온다. 그래서 POST 는 가볍고, 전송이 끊겨도 여기까지
 * 왔다면 사진은 확실히 MYBOX 에 있다.
 *
 * `file_id`(원본)는 비어 있을 수 있다. 웹용 사본을 먼저 올리고 원본을
 * 나중에 올리기 때문이다 — 폰에서 중간에 끊기면 사진은 보이는데 보관본만
 * 없는 상태가 되고, 그건 사진이 아예 없는 것보다 낫다.
 */
import {
  json, badRequest, readJson, requireAdmin,
  normalizeChild, normalizeText, CHILD_KEYS,
} from "../_lib.js";

function row(r) {
  return {
    id: r.id,
    child: r.child,
    // 화면에는 사본을 띄운다. 원본 주소를 그대로 주면 폰에서 몇 MB 씩 받는다.
    src: `/api/uploads/${encodeURIComponent(r.id)}/image`,
    caption: r.caption,
    file_name: r.file_name,
    width: r.width,
    height: r.height,
    taken_at: r.taken_at,
    created_at: r.created_at,
    // 보관본이 아직 없는 사진. 관리자 화면에서 다시 올리라고 알린다.
    original_missing: !r.mybox_file_id,
  };
}

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const childParam = url.searchParams.get("child");

  let statement;
  const columns = `id, child, mybox_file_id, caption, file_name,
                   width, height, taken_at, created_at`;
  if (childParam) {
    const child = normalizeChild(childParam);
    if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);
    statement = env.DB
      .prepare(`SELECT ${columns} FROM uploads WHERE child = ? ORDER BY created_at DESC`)
      .bind(child);
  } else {
    statement = env.DB.prepare(
      `SELECT ${columns} FROM uploads ORDER BY created_at DESC`,
    );
  }

  const { results } = await statement.all();
  return json({ count: results.length, uploads: results.map(row) });
}

export async function onRequestPost({ request, env }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  const child = normalizeChild(body.child);
  if (!child) return badRequest(`child must be one of ${CHILD_KEYS.join(", ")}`);

  const webId = normalizeText(body.web_id, 300);
  if (!webId) return badRequest("web_id is required");

  const id = `upload-${webId.slice(-12).replace(/[^A-Za-z0-9]/g, "")}`;
  const now = Date.now();

  await env.DB
    .prepare(
      `INSERT INTO uploads
         (id, child, mybox_file_id, mybox_web_id, file_name, caption,
          width, height, size, taken_at, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         mybox_file_id = excluded.mybox_file_id,
         caption = excluded.caption`,
    )
    .bind(
      id, child,
      normalizeText(body.file_id, 300) || "",
      webId,
      normalizeText(body.file_name, 200) || "",
      normalizeText(body.caption, 300) || "",
      Number(body.width) || 0,
      Number(body.height) || 0,
      Number(body.size) || 0,
      normalizeText(body.taken_at, 32) || "",
      now,
    )
    .run();

  return json({ ok: true, id }, 201);
}
