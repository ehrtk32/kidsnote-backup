/**
 * 업로드가 들어갈 MYBOX 폴더를 찾거나 만든다.
 *
 *   키즈노트 업로드/        원본
 *   키즈노트 업로드/web/    화면에 띄울 사본
 *
 * 폴더 id 를 설정으로 받지 않는 이유: production/preview 양쪽에 환경변수를
 * 두 개씩 넣어야 하고, 사용자가 MYBOX 앱에서 폴더를 지우면 조용히 고장난다.
 * 이름으로 찾아 없으면 만드는 편이 설정이 없고 스스로 복구된다.
 *
 * MYBOX API 에는 폴더 삭제도 이름 변경도 없다. 그래서 "있으면 쓴다"가
 * 안전하다 — 잘못 만들어도 덮어쓰거나 치울 방법이 없기 때문이다.
 */
import { myboxGet, myboxPost } from "./_client.js";

export const UPLOAD_FOLDER = "키즈노트 업로드";
export const WEB_SUBFOLDER = "web";

// 아이솔레이트가 사는 동안만 기억한다. 틀려도 다음 요청에서 다시 찾으므로
// 오래 들고 있을 이유가 없다.
let cached = null;

async function findChild(env, parentId, name) {
  const path = parentId
    ? `/v1/drive/folders/${encodeURIComponent(parentId)}/resources?count=1000`
    : "/v1/drive/resources?count=1000";
  const result = await myboxGet(env, path);
  if (!result.ok) return { error: result };
  const hit = (result.data?.resources || []).find(
    (item) => item.type === "folder" && item.name === name,
  );
  return { id: hit?.resourceId || "" };
}

async function findOrCreate(env, parentId, name) {
  const found = await findChild(env, parentId, name);
  if (found.error) return found;
  if (found.id) return found;

  const body = parentId ? { folderName: name, parentId } : { folderName: name };
  const made = await myboxPost(env, "/v1/drive/folders", body);
  if (!made.ok) return { error: made };
  return { id: made.data?.resourceId || "" };
}

export async function uploadFolders(env) {
  if (cached) return cached;

  const root = await findOrCreate(env, "", UPLOAD_FOLDER);
  if (root.error || !root.id) return { error: root.error || { ok: false, status: 502 } };

  const web = await findOrCreate(env, root.id, WEB_SUBFOLDER);
  if (web.error || !web.id) return { error: web.error || { ok: false, status: 502 } };

  cached = { originals: root.id, web: web.id };
  return cached;
}
