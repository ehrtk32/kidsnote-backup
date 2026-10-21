/** Pages Functions 공용 헬퍼. functions/ 밑에서 _ 로 시작하는 파일은 라우트가 되지 않는다. */

// APP_JS 의 children[].key 와 같은 값. 둘째의 key 가 이름이 아닌 이유는
// export_static_site.py 의 주석을 볼 것 — 태명이라 바뀐다.
export const CHILD_KEYS = ["seoi", "second"];
export const DEFAULT_CHILD = "seoi";

const MAX_FAILS = 5;
const BLOCK_MS = 15 * 60 * 1000;

export function json(data, status = 200) {
  return Response.json(data, {
    status,
    // 쓰기가 열린 API 라 캐시가 끼면 방금 추가한 항목이 안 보인다.
    headers: { "Cache-Control": "no-store" },
  });
}

export function badRequest(message) {
  return json({ error: message }, 400);
}

/** 클라이언트 IP. Cloudflare 가 항상 채워 준다. */
export function clientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

/**
 * 길이가 같을 때 조기 반환하지 않는 비교.
 *
 * 6자리 숫자를 상대로 타이밍 공격이 현실적인 위협은 아니지만, 비밀을 비교
 * 하면서 `===` 를 쓰는 습관을 남기지 않는 편이 낫다.
 */
function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

const PASSCODE_KEY = "admin_passcode";
const PBKDF2_ITERATIONS = 150_000;

const toB64 = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const fromB64 = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));

/**
 * 비번을 PBKDF2 로 늘려 저장형 문자열로 만든다.
 *
 * 6자리 숫자는 경우의 수가 100만뿐이라 단순 해시로는 D1 이 새는 순간 바로
 * 역산된다. 늘려 둔다고 안전해지지는 않지만, 오프라인 역산 비용이 올라가고
 * 온라인 시도는 아래 잠금이 막는다.
 */
export async function hashPasscode(passcode) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(passcode), "PBKDF2", false, ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" }, key, 256,
  );
  return `pbkdf2$${PBKDF2_ITERATIONS}$${toB64(salt)}$${toB64(bits)}`;
}

async function verifyPasscode(passcode, stored) {
  const [scheme, iterations, salt, expected] = String(stored).split("$");
  if (scheme !== "pbkdf2") return false;
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(passcode), "PBKDF2", false, ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: fromB64(salt), iterations: Number(iterations), hash: "SHA-256" },
    key, 256,
  );
  return constantTimeEqual(toB64(bits), expected);
}

/** 저장된 비번 해시. 아직 한 번도 바꾸지 않았으면 null. */
export async function storedPasscode(env) {
  const row = await env.DB
    .prepare("SELECT value FROM settings WHERE key = ?")
    .bind(PASSCODE_KEY)
    .first();
  return row?.value ?? null;
}

export async function savePasscode(env, passcode) {
  await env.DB
    .prepare(
      `INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
       ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
    )
    .bind(PASSCODE_KEY, await hashPasscode(passcode), Date.now())
    .run();
}

/**
 * 관리자 비번 확인. 통과하면 null, 막히면 그대로 돌려줄 Response.
 *
 * 비번은 settings 표의 해시가 우선이고, 그 행이 없을 때만 ADMIN_PASSCODE
 * 시크릿을 쓴다. 즉 시크릿은 최초 1회 부트스트랩용이고, 한 번 변경하면
 * 그 뒤로는 쓰이지 않는다.
 *
 * 실패가 쌓인 IP 는 잠시 막는다. 막힌 동안에는 비번이 맞아도 통과시키지
 * 않는다 — 맞았는지 알려 주는 것 자체가 탐색을 도와주기 때문이다.
 */
export async function requireAdmin(request, env, body) {
  const stored = await storedPasscode(env);
  if (!stored && !env.ADMIN_PASSCODE) {
    return json({ error: "server has no admin passcode configured" }, 500);
  }

  const ip = clientIp(request);
  const now = Date.now();

  const row = await env.DB
    .prepare("SELECT fails, blocked_until FROM auth_attempts WHERE ip = ?")
    .bind(ip)
    .first();

  if (row && row.blocked_until > now) {
    const retryAfter = Math.ceil((row.blocked_until - now) / 1000);
    return json(
      { error: "too many attempts", retry_after_seconds: retryAfter },
      429,
    );
  }

  const candidate = String(body?.passcode ?? "");
  const ok = stored
    ? await verifyPasscode(candidate, stored)
    : constantTimeEqual(candidate, env.ADMIN_PASSCODE);

  if (ok) {
    if (row) {
      await env.DB.prepare("DELETE FROM auth_attempts WHERE ip = ?").bind(ip).run();
    }
    return null;
  }

  const fails = (row?.fails ?? 0) + 1;
  const blockedUntil = fails >= MAX_FAILS ? now + BLOCK_MS : 0;
  await env.DB
    .prepare(
      `INSERT INTO auth_attempts (ip, fails, blocked_until, updated_at)
       VALUES (?, ?, ?, ?)
       ON CONFLICT(ip) DO UPDATE SET
         fails = CASE WHEN excluded.blocked_until > 0 THEN 0 ELSE excluded.fails END,
         blocked_until = excluded.blocked_until,
         updated_at = excluded.updated_at`,
    )
    .bind(ip, fails, blockedUntil, now)
    .run();

  return json({ error: "wrong passcode" }, 401);
}

/** 요청 본문을 JSON 으로. 깨져 있으면 null. */
export async function readJson(request) {
  try {
    const body = await request.json();
    return body && typeof body === "object" ? body : null;
  } catch {
    return null;
  }
}

export function normalizeChild(value) {
  const child = String(value ?? "").trim() || DEFAULT_CHILD;
  return CHILD_KEYS.includes(child) ? child : null;
}

/**
 * URL 검사. http(s) 만 받는다.
 *
 * 이 값은 나중에 iframe src 로 들어간다. javascript: 나 data: 를 통과시키면
 * 관리자 비번을 아는 사람이 사이트에 스크립트를 심을 수 있다.
 */
export function normalizeUrl(value) {
  const raw = String(value ?? "").trim();
  if (!raw || raw.length > 2000) return null;
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  return parsed.toString();
}

/** 'YYYY-MM-DD' 또는 빈 문자열. */
export function normalizeDate(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return null;
  return Number.isNaN(Date.parse(raw)) ? null : raw;
}

export function normalizeText(value, maxLength) {
  return String(value ?? "").trim().slice(0, maxLength);
}

/**
 * URL 로부터 안정적인 id.
 *
 * export_static_site.py 의 video_slug() 와 같은 규칙(sha256 앞 12자리)이다.
 * 같은 링크를 두 번 넣으면 새로 생기지 않고 덮어쓴다.
 */
export async function videoSlug(url) {
  const bytes = new TextEncoder().encode(url);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hex = [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `video-${hex.slice(0, 12)}`;
}
