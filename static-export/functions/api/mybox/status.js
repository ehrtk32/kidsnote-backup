/**
 * MYBOX 연동 상태.
 *
 *   GET /api/mybox/status
 *
 * 토큰 값은 절대 내보내지 않고, **쓸 수 있는 상태인지**만 알려 준다.
 * 인증을 걸지 않은 이유: 돌려주는 것이 "연결됨/만료됨" 두 글자뿐이고,
 * cron-worker 가 하루 한 번 이걸 찔러 토큰 만료를 미리 잡아야 하기
 * 때문이다. 관리자 비번을 Worker 에 넣는 것보다 이쪽이 낫다.
 *
 * PAT 는 최대 180일이다. 만료되면 401 이 오고, 아무도 안 보면 그대로
 * 멈춘다 — 키즈노트 쿠키(30일)와 GitHub 스케줄에서 이미 겪은 방식이다.
 */
import { json } from "../../_lib.js";
import { myboxGet } from "./_client.js";

export async function onRequestGet({ env }) {
  if (!env.MYBOX_TOKEN) {
    return json({ configured: false, ok: false, reason: "no token" });
  }

  // 루트 한 페이지만 읽는 가장 싼 호출. 1건만 요청한다.
  const result = await myboxGet(env, "/v1/drive/resources?count=1");
  if (result.ok) {
    return json({ configured: true, ok: true });
  }

  return json({
    configured: true,
    ok: false,
    status: result.status,
    reason: result.status === 401
      ? "token expired or revoked"
      : result.status === 429
        ? "rate limited"
        : "request failed",
  });
}
