// Cron-only watchdog. Secrets never leave the fixed GitHub dispatch endpoint.
const REPO = "zyc8717757/ai-news-auto-publisher";
const RAW = `https://raw.githubusercontent.com/${REPO}`;
const AI_RUN_LIMIT = 20;
const AUTO_RECOVERY_MIN_REQUESTS = 2;
const AUTO_RECOVERY_DELAY_MS = 15 * 60 * 1000;

function recoverableProductionFailure(state, issue, now) {
  if (!Array.isArray(state.ai_audit)) return false;
  let latest = null;
  for (const audit of state.ai_audit) {
    if (audit?.issue !== issue || audit.extra_test === true || audit.validation_only === true) continue;
    const at = Date.parse(audit.at);
    if (!Number.isFinite(at)) return false;
    if (!latest || at > latest.at) latest = { audit, at };
  }
  if (!latest || latest.audit.stage !== "provider_error"
      || !/^ai_provider_http_(429|500|502|503|504)$/.test(latest.audit.reason || "")) return false;

  const calls = state.ai_attempts ?? [];
  if (!Array.isArray(calls)) return false;
  let lastCallAt = latest.at;
  for (const value of calls) {
    const at = Date.parse(value);
    if (!Number.isFinite(at)) return false;
    lastCallAt = Math.max(lastCallAt, at);
  }
  if (now.getTime() - lastCallAt < AUTO_RECOVERY_DELAY_MS) return false;

  const counters = state.ai_issue_attempts;
  let used;
  if (counters && Object.prototype.hasOwnProperty.call(counters, issue)) {
    used = counters[issue];
  } else {
    // Legacy records lack a per-issue count. Treat all surviving calls as this issue's usage.
    if (calls.length === 0) return false;
    used = calls.length;
  }
  return Number.isSafeInteger(used) && used >= 0
    && AI_RUN_LIMIT - used >= AUTO_RECOVERY_MIN_REQUESTS;
}

async function latestDigestIsRules(read, issue, key) {
  const match = /^(\d{4}-\d{2}-\d{2}-(?:am|pm))-r([1-9]\d*)$/.exec(key || "");
  if (!match || match[1] !== issue) return false;
  const digest = await read(`news-data/digests/${encodeURIComponent(key)}.json`, true);
  return digest?.issue === issue && digest.revision === Number(match[2])
    && digest.mode === "rules" && Array.isArray(digest.stories) && digest.stories.length > 0;
}

async function dispatch(env, request, inputs) {
  const credential = env.GITHUB_DISPATCH_TOKEN.trim();
  if (!credential || /\s/.test(credential)) throw new Error("invalid_dispatch_credential");
  const response = await request(`https://api.github.com/repos/${REPO}/actions/workflows/news.yml/dispatches`, {
    method: "POST", redirect: "manual", signal: AbortSignal.timeout(20000),
    headers: { "User-Agent": "AI-News-Cloudflare-Watchdog", "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${credential}`, "Content-Type": "application/json",
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

export async function check(env, now = new Date(), request = (url, options) => globalThis.fetch(url, options)) {
  if (env.ENABLED !== "true") return "paused";
  if (!env.GITHUB_DISPATCH_TOKEN || !env.DISPATCH_STATE) throw new Error("configuration_missing");
  // Optional one-off live credential/egress proof. Preview cannot collect, call AI,
  // deploy Pages or send mail. Only a successful 204 leaves a verified marker.
  if (env.VERIFY_PREVIEW === "true" && !(await env.DISPATCH_STATE.get("preview_verified", "json"))) {
    await dispatch(env, request, { mode: "preview" });
    await env.DISPATCH_STATE.put("preview_verified", JSON.stringify({ at: now.toISOString(), status: "verified" }));
  }
  async function read(path, json) {
    const response = await request(`${RAW}/${path}?t=${now.getTime()}`, { redirect: "manual", signal: AbortSignal.timeout(20000) });
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
  const keys = Object.keys(state.issues).filter(k => new RegExp(`^${issue}-r[1-9]\\d*$`).test(k))
    .sort((a, b) => Number(b.split("-r")[1]) - Number(a.split("-r")[1]));
  const latestKey = keys[0];
  const current = state.issues[latestKey];
  const completed = current?.deployment === "ready" && ["sent", "disabled"].includes(current.email);
  if (completed) {
    if (!recoverableProductionFailure(state, issue, now)
        || !(await latestDigestIsRules(read, issue, latestKey))) return "completed";
  }
  if (["sending", "unknown"].includes(current?.email)) return "mail_needs_review";
  if (!completed && (current?.email_attempts || 0) >= 3) return "mail_attempt_limit";
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
      let previous;
      // Keep the five-minute checks, but share the account's scarce KV writes
      // responsibly: persist a heartbeat every 30 minutes, log every invocation.
      const record = async (status, reason) => {
        const now = new Date();
        if (previous?.heartbeat_interval_minutes === 30 && now.getTime() - Date.parse(previous.at) < 1800000) return;
        await env.DISPATCH_STATE.put("heartbeat", JSON.stringify({ at: now.toISOString(), scheduled_at: new Date(controller.scheduledTime).toISOString(), heartbeat_interval_minutes: 30, status, ...(reason ? {reason} : {}) }), { expirationTtl: 86400 });
      };
      try {
        previous = await env.DISPATCH_STATE.get("heartbeat", "json");
        // Write only the terminal result: two immediate writes to the same KV
        // key can exceed KV's per-key write rate even on the first invocation.
        const status = await check(env);
        await record(status);
        console.log(JSON.stringify({ status }));
      } catch (error) {
        const message = error?.message || "";
        const reason = /^(github_dispatch_http_\d{3}|public_state_http_\d{3}|configuration_missing|invalid_schedule|invalid_public_state|invalid_dispatch_credential)$/.test(message)
          ? message : ["TimeoutError", "AbortError"].includes(error?.name) ? "request_timeout"
          : /illegal invocation/i.test(message) ? "runtime_illegal_invocation"
          : error?.name === "TypeError" && /header/i.test(message) ? "invalid_header_value"
          : error?.name === "TypeError" ? "runtime_type_error" : "unexpected_failure";
        await record("watchdog_failed", reason);
        console.error(JSON.stringify({status: "watchdog_failed", reason}));
        throw new Error("watchdog_failed");
      }
    })());
  },
  fetch() { return new Response("Not found", { status: 404 }); },
};
