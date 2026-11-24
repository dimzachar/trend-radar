// Daily timer for Trend Radar (Cloudflare Worker; the digest site itself is static assets).
//
// Cloudflare's Cron Trigger starts the GitHub workflow at a fixed time. GitHub's own
// `schedule:` is best effort and can run late or skip.
//
// Secret: GH_TOKEN (fine-grained, this repository only, "Actions: Read and write").

const WORKFLOW = "radar.yml";

async function startRun(env, why) {
  const resp = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "trend-radar-timer",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  if (resp.status !== 204) {
    throw new Error(`GitHub answered ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
  }
  console.log(`run started: ${why}`);
}

export default {
  async scheduled(event, env, ctx) {
    if (!env.GH_TOKEN) throw new Error("GH_TOKEN secret is not set");
    ctx.waitUntil(startRun(env, `planned ${event.cron}`));
  },
  // Only reached for paths that are not a file in public/.
  async fetch() {
    return new Response("Not found", { status: 404 });
  },
};
