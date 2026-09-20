// 外部定时调度器：按 cron 时间向 GitHub 发送 repository_dispatch，
// 由 GitHub Actions 执行实际任务。这样定时能力不依赖 GitHub 自带的
// schedule（公共仓库 60 天无活动会被停用，且负载高峰会延迟或丢弃）。

// 注意：Cron Triggers 使用 UTC 时间。北京时间 = UTC + 8。
const CRON_TARGETS = {
  "17 0 * * *": { event: "daily-digest", label: "AI news daily" }, // 北京 08:17
};

// 签到随机延迟：cron 在 UTC 23:17-23:47（北京 07:17-07:47）每分钟触发，
// Worker 用"当天日期哈希"算出 0-30 的偏移，只在对应那一分钟派发一次。
// 用日期哈希而非真随机的目的：同一天内结果固定，cron 逐分钟重复触发
// 不会造成重复派发，天然幂等。
const CHECKIN_CRON = "17-47 23 * * *";
const CHECKIN_EVENT = "cloudstudio-checkin";

function checkinTargetMinute(now) {
  const day = now.toISOString().slice(0, 10); // UTC 日期 YYYY-MM-DD
  // FNV-1a + 雪崩混淆：日期字符串只差最后一位，
  // 简单哈希会让偏移逐日顺序递增，必须充分打散。
  let h = 2166136261;
  for (const ch of day) {
    h ^= ch.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  h ^= h >>> 15;
  h = Math.imul(h, 2246822507);
  h ^= h >>> 13;
  return 17 + (h >>> 0) % 31; // UTC 分钟 17-47，对应北京 07:17-07:47
}

// /run 手动端点允许的事件
const KNOWN_EVENTS = ["daily-digest", CHECKIN_EVENT];

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
    const now = new Date();

    // 签到：随机延迟派发，见 CHECKIN_CRON 注释
    if (controller.cron === CHECKIN_CRON) {
      if (now.getUTCMinutes() !== checkinTargetMinute(now)) {
        return; // 还没轮到今天派发的那一分钟
      }
      ctx.waitUntil(
        callGitHub(env, "dispatches", payloadFor(CHECKIN_EVENT)).then(
          () => console.log(`Dispatched ${CHECKIN_EVENT} (jittered check-in)`),
          (error) => console.error(`Dispatch failed: ${error.message}`)
        )
      );
      return;
    }

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
        crons: [...Object.keys(CRON_TARGETS), CHECKIN_CRON],
        checkin: "UTC 23:17-23:47, jittered dispatch once per day",
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
      if (!KNOWN_EVENTS.includes(event)) {
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
