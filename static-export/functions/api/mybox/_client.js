/**
 * MYBOX Open API 호출 공통부.
 *
 *   https://open-api.mybox.naver.com/v1/drive/...
 *   Authorization: Bearer {개인 액세스 토큰}
 *
 * 토큰은 개인 액세스 토큰(PAT)이고, OAuth 가 아니라 **계정 전체**에 접근
 * 한다. 그래서 이 모듈을 거치는 엔드포인트는 전부 관리자 전용이다.
 *
 * 그리고 이 토큰은 최대 180일에 만료된다. 만료되면 401 이 오는데, 그걸
 * 그냥 "실패"로 뭉개면 조용히 멈춘 채 아무도 모르는 상태가 된다 — 이
 * 저장소가 이미 두 번 겪은 실패 방식이라, 401 은 따로 구분해 올려 보낸다.
 */
const BASE = "https://open-api.mybox.naver.com";
const TIMEOUT_MS = 15_000;

export async function myboxGet(env, path) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: {
        Authorization: `Bearer ${env.MYBOX_TOKEN}`,
        Accept: "application/json",
      },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (err) {
    return { ok: false, status: 0, detail: String(err) };
  }

  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    // 본문이 JSON 이 아니면 그대로 들고 간다 — 원인 추적에 쓸모가 있다.
  }

  if (!response.ok) {
    return { ok: false, status: response.status, data, detail: text.slice(0, 300) };
  }
  return { ok: true, data };
}

/** myboxGet 의 실패를 그대로 돌려줄 Response 로 바꾼다. */
export function myboxError(result) {
  const hint =
    result.status === 401 ? "MYBOX token is expired or revoked — issue a new one (max 180 days)"
    : result.status === 403 ? "MYBOX refused this resource (shared or password folders are not supported)"
    : result.status === 404 ? "not found in MYBOX"
    : result.status === 429 ? "MYBOX rate limit"
    : result.status === 0 ? "could not reach MYBOX"
    : "MYBOX request failed";

  return Response.json(
    {
      error: hint,
      mybox_status: result.status,
      mybox_code: result.data?.code ?? null,
      detail: result.detail ?? null,
    },
    // 401 을 그대로 돌려주면 "관리자 비번이 틀렸다"와 구분이 안 된다.
    // 우리 쪽 인증은 이미 통과한 상태이므로 502 로 올린다.
    { status: result.status === 401 ? 502 : 502, headers: { "Cache-Control": "no-store" } },
  );
}
