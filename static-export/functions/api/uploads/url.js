/**
 * 업로드할 자리를 하나 내준다.
 *
 *   POST /api/uploads/url   { passcode, file_name, file_size, web? }
 *     → { upload_url }
 *
 * 파일 바이트는 **브라우저가 MYBOX 로 직접** 보낸다. 여기로 흘리지 않는
 * 이유는 셋이다 — Workers 요청 본문이 100MB 에서 막히고, 폰에서 올리는
 * 수십 MB 를 중계하면 느려지고, 우리가 들고 있을 이유가 없다.
 * MYBOX 업로드 호스트가 `access-control-allow-origin: *` 를 주므로 된다.
 *
 * 그래서 이 엔드포인트가 사실상 **쓰기 권한 그 자체**다. 발급된 URL 을
 * 가진 사람은 누구나 사용자의 개인 드라이브에 파일을 쓸 수 있다. 댓글·
 * 좋아요와 달리 반드시 관리자만 부를 수 있어야 하는 이유다.
 */
import { json, badRequest, readJson, requireAdmin, normalizeText } from "../../_lib.js";
import { myboxPost, myboxError } from "../mybox/_client.js";
import { uploadFolders } from "../mybox/_folders.js";

// 폰 사진은 10MB 안쪽, 영상은 길면 1GB 도 간다. 상한을 아주 크게 두면
// 실수로 올린 거대한 파일이 드라이브를 채우는데 지울 방법이 없다.
const MAX_BYTES = 2 * 1024 * 1024 * 1024;

export async function onRequestPost({ request, env }) {
  const body = await readJson(request);
  if (!body) return badRequest("body must be JSON");

  const blocked = await requireAdmin(request, env, body);
  if (blocked) return blocked;

  if (!env.MYBOX_TOKEN) {
    return json({ error: "server is missing MYBOX_TOKEN" }, 503);
  }

  const fileName = normalizeText(body.file_name, 200);
  if (!fileName) return badRequest("file_name is required");

  const fileSize = Number(body.file_size);
  if (!Number.isInteger(fileSize) || fileSize <= 0 || fileSize > MAX_BYTES) {
    return badRequest(`file_size must be 1..${MAX_BYTES}`);
  }

  const folders = await uploadFolders(env);
  if (folders.error) return myboxError(folders.error);

  const result = await myboxPost(env, "/v1/drive/files", {
    fileName,
    fileSize,
    // 웹용 사본은 하위 폴더로 내려 원본과 섞이지 않게 한다.
    parentId: body.web ? folders.web : folders.originals,
  });
  if (!result.ok) return myboxError(result);

  const uploadUrl = result.data?.uploadUrl || "";
  if (!uploadUrl) return json({ error: "MYBOX did not return an upload url" }, 502);

  return json({ ok: true, upload_url: uploadUrl });
}
