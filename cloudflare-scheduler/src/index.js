const WORKFLOW_DISPATCH_URL =
  "https://api.github.com/repos/dcgitcode/ai-news-daily/actions/workflows/daily.yml/dispatches";

async function dispatchDailyNews(env) {
  if (!env.GITHUB_DISPATCH_TOKEN) {
    throw new Error("Missing Cloudflare secret: GITHUB_DISPATCH_TOKEN");
  }

  const response = await fetch(WORKFLOW_DISPATCH_URL, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      ref: "main",
      inputs: { dry_run: "false" },
    }),
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(`GitHub workflow dispatch failed (${response.status}): ${message}`);
  }
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(dispatchDailyNews(env));
  },

  async fetch() {
    return new Response("AI News scheduler is active.", { status: 200 });
  },
};

