// 外部定时调度器：按 cron 时间向 GitHub 发送 repository_dispatch，
// 由 GitHub Actions 执行实际任务。这样定时能力不依赖 GitHub 自带的
// schedule（公共仓库 60 天无活动会被停用，且负载高峰会延迟或丢弃）。

// 注意：Cron Triggers 使用 UTC 时间。北京时间 = UTC + 8。
const CRON_TARGETS = {
  "17 0 * * *": { event: "daily-digest", label: "AI news daily" }, // 北京 08:17
  "17 23 * * *": { event: "cloudstudio-checkin", label: "Cloud Studio check-in" }, // 北京 07:17
  // 临时自检：链路验证通过后删除本行，并移除 wrangler.jsonc 里的对应 cron。
  "*/30 * * * *": { event: "selftest", label: "Scheduler self test" },
};

function dispatchUrl(env, endpoint) {
  const repo = env.GITHUB_REPO || "dcgitcode/ai-news-daily";
  return `https://api.github.com/repos/${repo}/${endpoint}`;
}

async function callGitHub(env, endpoint, body) {
  if (!env.GITHUB_DISPATCH_TOKEN) {
    throw new Error("Missing Cloudflare secret: GITHUB_DISPATCH_TOKEN");
  }

  const response = await fetch(dispatchUrl(env, endpoint), {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "ai-news-scheduler",
    },
    body: JSON.stringify(body),
  });

  // repository_dispatch 成功时返回 204 No Content。
  if (!response.ok) {
    const message = await response.text();
    throw new Error(`GitHub dispatch failed (${response.status}): ${message}`);
  }
}

function payloadFor(event) {
  return event === "daily-digest"
    ? { event_type: event, client_payload: { dry_run: false } }
    : { event_type: event };
}

export default {
  async scheduled(controller, env, ctx) {
    const target = CRON_TARGETS[controller.cron];
    if (!target) {
      console.log(`No target mapped for cron: ${controller.cron}`);
      return;
    }

    ctx.waitUntil(
      callGitHub(env, "dispatches", payloadFor(target.event)).then(
        () => console.log(`Dispatched ${target.event} (${target.label})`),
        (error) => console.error(`Dispatch failed: ${error.message}`)
      )
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/" || url.pathname === "/health") {
      return Response.json({
        status: "active",
        crons: Object.keys(CRON_TARGETS),
        repo: env.GITHUB_REPO || "dcgitcode/ai-news-daily",
      });
    }

    // 手动触发：POST /run/daily-digest 或 /run/cloudstudio-checkin
    // 需要请求头 X-Trigger-Token 与 Cloudflare secret 一致，避免被公开滥用。
    const match = url.pathname.match(/^\/run\/([a-zA-Z0-9_-]+)$/);
    if (match && request.method === "POST") {
      const token = request.headers.get("X-Trigger-Token");
      if (!env.TRIGGER_TOKEN || token !== env.TRIGGER_TOKEN) {
        return new Response("Unauthorized", { status: 401 });
      }

      const event = match[1];
      const known = Object.values(CRON_TARGETS).some((item) => item.event === event);
      if (!known) {
        return new Response(`Unknown event: ${event}`, { status: 404 });
      }

      try {
        await callGitHub(env, "dispatches", payloadFor(event));
        return Response.json({ ok: true, event });
      } catch (error) {
        return Response.json({ ok: false, error: error.message }, { status: 502 });
      }
    }

    return new Response("Not found", { status: 404 });
  },
};
