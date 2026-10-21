/**
 * GitHub Actions 스케줄러 대역.
 *
 * 이 저장소의 `schedule:` 트리거는 2026-10-08 이후 한 번도 발사되지 않았다.
 * 설정은 멀쩡하고(기본 브랜치에 유효한 cron, 워크플로 state=active, Actions
 * 활성), 같은 파일을 쓰는 업스트림 redchupa/kidsnote-backup 은 정상으로 돈다.
 * disable→enable 토글도 듣지 않았다. 원인은 끝내 GitHub 쪽이라 손이 닿지
 * 않으므로, 트리거만 밖으로 꺼낸다.
 *
 * workflow_dispatch 는 멀쩡하다 — 수동 실행은 계속 성공했다. 그래서 여기서
 * 하는 일은 그 엔드포인트를 정해진 시각에 두드리는 것뿐이고, 워크플로 내용은
 * 아무것도 바뀌지 않는다.
 */

const REPO = "ehrtk32/kidsnote-backup";
const SITE = "https://seoi-kidsnote.pages.dev";

// cron 표현식 → 두드릴 워크플로. GitHub 쪽 cron 과 같은 값을 쓴다. 자녀
// 알림장이 올라오는 시각(KST 03:15 / 09:15 / 15:15 / 21:15)을 그대로
// 유지하기 위해서다. Cloudflare cron 도 UTC 다.
const JOBS = {
  "30 10 * * *": "kidsnote-session-refresh.yml",   // KST 19:30
  "0 11 * * *": "kidsnote-to-notion.yml",          // KST 20:00
};

// 결과물이 실제로 갱신됐는지 보는 cron. 미러와 배포가 끝난 뒤(KST 22:00)라야
// 오늘 것을 본다.
const FRESHNESS_CRON = "0 13 * * *";

// 마지막 export 가 이 시간을 넘기면 파이프라인이 죽은 것으로 본다. 미러는
// 6시간마다 도니 24시간이면 네 번을 내리 놓친 셈이다.
const STALE_HOURS = 24;

const UA = "kidsnote-cron (+https://github.com/ehrtk32/kidsnote-backup)";

/**
 * 경보 전달 지점.
 *
 * 채널이 아직 정해지지 않아서 지금은 로그로만 남긴다. Cloudflare 대시보드의
 * Workers Logs 와 Cron Events 에서 보인다. Discord/Slack 웹훅이 정해지면
 * 이 함수 안에서 fetch 한 번 하면 되고, 호출부는 건드릴 필요가 없다.
 *
 * 탐지를 지금 넣어 두는 이유: 이번 사고의 본질은 "cron 이 멈췄다"가 아니라
 * "12일 동안 아무도 몰랐다" 쪽이다. 전달은 한 줄이지만 탐지는 설계다.
 */
async function notify(level, message, detail) {
  const line = `[${level}] ${message}${detail ? ` ${JSON.stringify(detail)}` : ""}`;
  if (level === "error") console.error(line);
  else console.log(line);
}

/** workflow_dispatch 를 한 번 쏜다. 성공은 204 No Content 다. */
async function dispatchWorkflow(env, workflow) {
  const url =
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`;

  // 5xx 와 네트워크 오류만 재시도한다. 4xx 는 다시 보내도 같은 답이 온다.
  let lastStatus = 0;
  let lastBody = "";
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          // GitHub API 는 User-Agent 가 없으면 403 을 준다.
          "User-Agent": UA,
        },
        // 입력을 비워 두면 워크플로에 정의된 기본값이 쓰인다. force_refresh
        // 가 꺼진 채로 가는 게 중요하다 — 켜지면 노션 페이지를 전부 아카이브
        // 하고 다시 받는다.
        body: JSON.stringify({ ref: "main" }),
      });
    } catch (err) {
      lastStatus = 0;
      lastBody = String(err);
      continue;
    }

    if (response.status === 204) {
      await notify("info", `dispatched ${workflow}`, { attempt });
      return true;
    }

    lastStatus = response.status;
    lastBody = (await response.text()).slice(0, 200);

    if (response.status < 500) break;
  }

  // 401 은 거의 항상 토큰 만료다. 이 워커가 잡아내야 하는 가장 중요한
  // 실패인데, 아무것도 안 하면 "성공한 척 조용히 멈추는" 지금 상태와
  // 똑같아진다.
  const hint =
    lastStatus === 401 ? "token expired or revoked"
    : lastStatus === 403 ? "token lacks permission for this repo"
    : lastStatus === 404 ? "workflow or repo not found (renamed? branch?)"
    : lastStatus === 0 ? "network error"
    : "unexpected status";

  await notify("error", `dispatch failed: ${workflow} — ${hint}`, {
    status: lastStatus,
    body: lastBody,
  });
  return false;
}

/**
 * 사이트가 실제로 갱신되고 있는지 본다.
 *
 * dispatch 가 204 를 받아도 그 뒤 파이프라인이 죽어 있을 수 있다. 쿠키 만료,
 * 노션 오류, 배포 실패 — 전부 dispatch 입장에선 성공이다. 결과물을 직접
 * 확인하는 게 유일하게 믿을 수 있는 신호다.
 */
async function checkFreshness() {
  let data;
  try {
    const response = await fetch(`${SITE}/data/posts.json`, {
      headers: { "User-Agent": UA },
      cf: { cacheTtl: 0 },
    });
    if (!response.ok) {
      await notify("error", "freshness check failed", { status: response.status });
      return;
    }
    data = await response.json();
  } catch (err) {
    await notify("error", "freshness check failed", { error: String(err) });
    return;
  }

  const exportedAt = Date.parse(data.exported_at || "");
  if (Number.isNaN(exportedAt)) {
    await notify("error", "freshness check: no usable exported_at");
    return;
  }

  const ageHours = (Date.now() - exportedAt) / 3_600_000;
  const detail = {
    exported_at: data.exported_at,
    age_hours: Math.round(ageHours * 10) / 10,
    count: data.count,
  };

  if (ageHours > STALE_HOURS) {
    await notify("error", `site is stale (> ${STALE_HOURS}h)`, detail);
  } else {
    await notify("info", "site is fresh", detail);
  }
}

/**
 * MYBOX 개인 액세스 토큰이 아직 살아 있는지 본다.
 *
 * 그 토큰은 최대 180일이고, 만료되면 401 이 온다. 아무도 안 보면 연동이
 * 조용히 멈춘 채로 남는다 — 키즈노트 쿠키(30일)와 GitHub 스케줄에서
 * 이미 두 번 겪은 방식이라, 신선도 확인에 같이 태운다.
 */
async function checkMyboxToken() {
  let payload;
  try {
    const response = await fetch(`${SITE}/api/mybox/status`, {
      headers: { "User-Agent": UA, accept: "application/json" },
      cf: { cacheTtl: 0 },
    });
    const type = response.headers.get("content-type") || "";
    if (!type.includes("application/json")) return;   // 아직 배포 전
    payload = await response.json();
  } catch (err) {
    await notify("error", "mybox check failed", { error: String(err) });
    return;
  }

  if (!payload.configured) return;    // 토큰을 아직 안 넣었다면 할 말이 없다
  if (payload.ok) {
    await notify("info", "mybox token ok");
  } else {
    await notify("error", "mybox token is not usable", payload);
  }
}

export default {
  async scheduled(controller, env, ctx) {
    if (controller.cron === FRESHNESS_CRON) {
      await checkFreshness();
      await checkMyboxToken();
      return;
    }

    const workflow = JOBS[controller.cron];
    if (!workflow) {
      // cron 을 추가해 놓고 JOBS 에 매핑을 빠뜨리면 조용히 아무 일도 안
      // 일어난다. 그 경우를 보이게 만든다.
      await notify("error", "no job mapped for cron", { cron: controller.cron });
      return;
    }

    await dispatchWorkflow(env, workflow);
  },

  // 브라우저로 열었을 때 상태를 보여 준다. 배포가 살아 있는지, cron 매핑이
  // 뭔지 확인하는 용도. 토큰은 노출하지 않는다.
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return Response.json({
        ok: true,
        repo: REPO,
        jobs: JOBS,
        stale_after_hours: STALE_HOURS,
        token_configured: Boolean(env.GH_DISPATCH_TOKEN),
        now: new Date().toISOString(),
      });
    }
    return new Response("kidsnote-cron\n", {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
