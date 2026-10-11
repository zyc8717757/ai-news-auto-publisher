// Cron-only watchdog. Secrets never leave the fixed GitHub dispatch endpoint.
const REPO = "zyc8717757/ai-news-auto-publisher";
const RAW = `https://raw.githubusercontent.com/${REPO}`;

async function dispatch(env, request, inputs) {
  const response = await request(`https://api.github.com/repos/${REPO}/actions/workflows/news.yml/dispatches`, {
    method: "POST", redirect: "error", signal: AbortSignal.timeout(20000),
    headers: { "User-Agent": "AI-News-Cloudflare-Watchdog", "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28" },
    body: JSON.stringify({ ref: "main", inputs }),
  });
  if (response.status !== 204) throw new Error(`github_dispatch_http_${response.status}`);
}

export function dueIssue(now, morning, evening) {
  const local = new Date(now.getTime() + 8 * 3600000);
  const day = local.toISOString().slice(0, 10);
  const time = local.toISOString().slice(11, 16);
  if (time >= evening) return `${day}-pm`;
  if (time >= morning) return `${day}-am`;
  return `${new Date(local.getTime() - 86400000).toISOString().slice(0, 10)}-pm`;
}

export async function check(env, now = new Date(), request = fetch) {
  if (env.ENABLED !== "true") return "paused";
  if (!env.GITHUB_DISPATCH_TOKEN || !env.DISPATCH_STATE) throw new Error("configuration_missing");
  // Optional one-off live credential/egress proof. Preview cannot collect, call AI,
  // deploy Pages or send mail. Only a successful 204 leaves a verified marker.
  if (env.VERIFY_PREVIEW === "true" && !(await env.DISPATCH_STATE.get("preview_verified", "json"))) {
    await dispatch(env, request, { mode: "preview" });
    await env.DISPATCH_STATE.put("preview_verified", JSON.stringify({ at: now.toISOString(), status: "verified" }));
  }
  async function read(path, json) {
    const response = await request(`${RAW}/${path}?t=${now.getTime()}`, { redirect: "error", signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error(`public_state_http_${response.status}`);
    return json ? response.json() : response.text();
  }
  const settings = await read("main/config/settings.yaml", false);
  if (/^paused:\s*true\s*$/m.test(settings)) return "paused";
  const morning = /^morning_time:\s*['"]?(\d{2}:\d{2})/m.exec(settings)?.[1];
  const evening = /^evening_time:\s*['"]?(\d{2}:\d{2})/m.exec(settings)?.[1];
  if (!morning || !evening || morning >= evening) throw new Error("invalid_schedule");
  const issue = dueIssue(now, morning, evening);
  const state = await read("news-data/state.json", true);
  if (state.schema_version !== 1 || !state.issues) throw new Error("invalid_public_state");
  const keys = Object.keys(state.issues).filter(k => k.startsWith(`${issue}-r`))
    .sort((a, b) => Number(b.split("-r")[1]) - Number(a.split("-r")[1]));
  const current = state.issues[keys[0]];
  if (current?.deployment === "ready" && ["sent", "disabled"].includes(current.email)) return "completed";
  if (["sending", "unknown"].includes(current?.email)) return "mail_needs_review";
  if ((current?.email_attempts || 0) >= 3) return "mail_attempt_limit";
  const previous = await env.DISPATCH_STATE.get(issue, "json");
  if (previous && (previous.count >= 4 || now.getTime() - previous.at < 1800000)) return "cooldown";
  // Reserve before dispatch. KV is eventually consistent; the GitHub concurrency
  // group and frozen-period/mail idempotency remain the final duplicate guard.
  await env.DISPATCH_STATE.put(issue, JSON.stringify({ count: (previous?.count || 0) + 1, at: now.getTime() }),
    { expirationTtl: 259200 });
  await dispatch(env, request, { mode: "run", issue });
  return "dispatched";
}

export default {
  async scheduled(controller, env, ctx) {
    // A delayed platform event must repair the latest due period at execution
    // time, rather than repeatedly checking the period of its old planned tick.
    ctx.waitUntil((async () => {
      const record = async (status, reason) => env.DISPATCH_STATE.put("heartbeat", JSON.stringify({ at: new Date().toISOString(), scheduled_at: new Date(controller.scheduledTime).toISOString(), status, ...(reason ? {reason} : {}) }), { expirationTtl: 86400 });
      try {
        if (!(await env.DISPATCH_STATE.get("heartbeat"))) await record("started");
        const status = await check(env);
        await record(status);
        console.log(JSON.stringify({ status }));
      } catch (error) {
        const message = error?.message || "";
        const reason = /^(github_dispatch_http_\d{3}|public_state_http_\d{3}|configuration_missing|invalid_schedule|invalid_public_state)$/.test(message)
          ? message : ["TimeoutError", "AbortError"].includes(error?.name) ? "request_timeout" : "unexpected_failure";
        await record("watchdog_failed", reason);
        console.error(JSON.stringify({status: "watchdog_failed", reason}));
        throw new Error("watchdog_failed");
      }
    })());
  },
  fetch() { return new Response("Not found", { status: 404 }); },
};
