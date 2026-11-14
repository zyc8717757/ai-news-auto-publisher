import test from "node:test";
import assert from "node:assert/strict";
import worker, { check, dueIssue } from "./worker.mjs";

test("Beijing cutoff and previous-day mapping", () => {
  assert.equal(dueIssue(new Date("2026-10-11T00:29Z"), "08:30", "20:30"), "2026-10-10-pm");
  assert.equal(dueIssue(new Date("2026-10-11T00:30Z"), "08:30", "20:30"), "2026-10-11-am");
  assert.equal(dueIssue(new Date("2026-10-11T12:30Z"), "08:30", "20:30"), "2026-10-11-pm");
});

test("native fetch retains its receiver and stdin newline cannot poison auth header", async t => {
  let previews = 0;
  t.mock.method(globalThis, "fetch", async function(url, options) {
    assert.equal(this, globalThis);
    assert.equal(options.redirect, "manual");
    if (url.includes("api.github.com")) {
      assert.equal(options.headers.Authorization, "Bearer TEST_ONLY");
      previews++;
      return new Response(null, { status: 204 });
    }
    if (url.includes("settings.yaml")) return new Response("morning_time: '08:30'\nevening_time: '20:30'\n");
    return Response.json({ schema_version: 1, issues: { "2026-10-11-am-r1": { deployment: "ready", email: "sent" } } });
  });
  const env = { ENABLED: "true", VERIFY_PREVIEW: "true", GITHUB_DISPATCH_TOKEN: " TEST_ONLY\r\n",
    DISPATCH_STATE: { get: async () => null, put: async () => {} } };
  assert.equal(await check(env, new Date("2026-10-11T05:00Z")), "completed");
  assert.equal(previews, 1);
});

test("dispatch redirects are refused without forwarding credentials", async () => {
  let calls = 0, writes = 0;
  const env = { ENABLED: "true", VERIFY_PREVIEW: "true", GITHUB_DISPATCH_TOKEN: "TEST_ONLY",
    DISPATCH_STATE: { get: async () => null, put: async () => writes++ } };
  await assert.rejects(check(env, new Date(), async (url, options) => {
    calls++;
    assert.equal(url, "https://api.github.com/repos/zyc8717757/ai-news-auto-publisher/actions/workflows/news.yml/dispatches");
    assert.equal(options.redirect, "manual");
    return new Response(null, { status: 307, headers: { location: "https://example.com" } });
  }), /github_dispatch_http_307/);
  assert.equal(calls, 1);
  assert.equal(writes, 0);
});

test("scheduled event leaves a credential-free durable heartbeat", async () => {
  let pending, saved;
  worker.scheduled({ scheduledTime: Date.parse("2026-10-11T06:10Z") },
    { ENABLED: "false", DISPATCH_STATE: { get: async () => null, put: async (key, value) => { saved = [key, JSON.parse(value)]; } } },
    { waitUntil(promise) { pending = promise; } });
  await pending;
  assert.equal(saved[0], "heartbeat");
  assert.equal(saved[1].scheduled_at, "2026-10-11T06:10:00.000Z");
  assert.equal(saved[1].status, "paused");
  assert.ok(Number.isFinite(Date.parse(saved[1].at)));
});

test("request failure leaves a single sanitized terminal record", async t => {
  const records = []; let pending;
  t.mock.method(globalThis, "fetch", async () => { throw Object.assign(new Error("PRIVATE_MARKER"), { name: "TimeoutError" }); });
  worker.scheduled({ scheduledTime: Date.now() }, {
    ENABLED: "true", GITHUB_DISPATCH_TOKEN: "TEST_ONLY",
    DISPATCH_STATE: { get: async () => null, put: async (key, value) => records.push(JSON.parse(value)) },
  }, { waitUntil(promise) { pending = promise; } });
  await assert.rejects(pending, /^Error: watchdog_failed$/);
  assert.equal(records.length, 1);
  assert.equal(records[0].status, "watchdog_failed");
  assert.equal(records.at(-1).reason, "request_timeout");
  assert.equal(JSON.stringify(records).includes("PRIVATE_MARKER"), false);
});

test("delayed cron chooses execution-time period instead of stale planned tick", async t => {
  const now = Date.parse("2026-10-11T05:00:00Z");
  t.mock.timers.enable({ apis: ["Date"], now });
  let pending, issue, heartbeat;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    if (url.includes("api.github.com")) {
      issue = JSON.parse(options.body).inputs.issue;
      return new Response(null, { status: 204 });
    }
    if (url.includes("settings.yaml")) return new Response("morning_time: '08:30'\nevening_time: '20:30'\n");
    return Response.json({ schema_version: 1, issues: {} });
  });
  worker.scheduled({ scheduledTime: Date.parse("2026-10-10T12:30Z") }, {
    ENABLED: "true", GITHUB_DISPATCH_TOKEN: "TEST_ONLY",
    DISPATCH_STATE: { get: async () => null, put: async (key, value) => { if (key === "heartbeat") heartbeat = JSON.parse(value); } },
  }, { waitUntil(promise) { pending = promise; } });
  await pending;
  assert.equal(issue, "2026-10-11-am");
  assert.equal(heartbeat.at, "2026-10-11T05:00:00.000Z");
  assert.equal(heartbeat.scheduled_at, "2026-10-10T12:30:00.000Z");
});

test("completed/ambiguous mail never dispatched; missing issue dispatched with cooldown", async () => {
  const values = new Map(); let mail = "sent", dispatches = 0;
  const env = { ENABLED: "true", GITHUB_DISPATCH_TOKEN: "TEST_ONLY",
    DISPATCH_STATE: { get: async k => values.has(k) ? JSON.parse(values.get(k)) : null,
      put: async (k, v) => values.set(k, v) } };
  const request = async (url, options) => {
    if (url.includes("api.github.com")) { dispatches++; return new Response(null, { status: 204 }); }
    assert.equal(options.headers?.Authorization, undefined);
    if (url.includes("settings.yaml")) return new Response("morning_time: '08:30'\nevening_time: '20:30'\n");
    return Response.json({ schema_version: 1, issues: mail ? { "2026-10-11-am-r1": { deployment: "ready", email: mail } } : {} });
  };
  const now = new Date("2026-10-11T05:00Z");
  assert.equal(await check(env, now, request), "completed");
  mail = "unknown"; assert.equal(await check(env, now, request), "mail_needs_review");
  mail = null; assert.equal(await check(env, now, request), "dispatched");
  assert.equal(await check(env, now, request), "cooldown");
  assert.equal(dispatches, 1);
  mail = "sent"; env.VERIFY_PREVIEW = "true";
  assert.equal(await check(env, now, request), "completed");
  assert.equal(dispatches, 2);
  assert.equal(JSON.parse(values.get("preview_verified")).status, "verified");
  assert.equal(await check(env, now, request), "completed");
  assert.equal(dispatches, 2);
});

function recoveryState(now, { email = "sent", emailAttempts = 1, used = 18 } = {}) {
  const issue = "2026-10-11-am";
  const at = new Date(now.getTime() - 16 * 60 * 1000).toISOString();
  return {
    schema_version: 1,
    issues: { [`${issue}-r1`]: { deployment: "ready", email, email_attempts: emailAttempts } },
    ai_issue_attempts: { [issue]: used },
    ai_attempts: Array(used).fill(at),
    ai_audit: [{
      at,
      issue,
      stage: "provider_error",
      reason: "ai_provider_http_503",
      extra_test: false,
    }],
  };
}

async function watchdogResult(state, digestMode, now) {
  const dispatches = [], writes = [];
  const env = {
    ENABLED: "true",
    GITHUB_DISPATCH_TOKEN: "TEST_ONLY",
    DISPATCH_STATE: {
      get: async () => null,
      put: async (key, value) => writes.push([key, JSON.parse(value)]),
    },
  };
  const request = async (url, options) => {
    if (url.includes("api.github.com")) {
      dispatches.push(JSON.parse(options.body).inputs);
      return new Response(null, { status: 204 });
    }
    assert.equal(options.headers?.Authorization, undefined);
    if (url.includes("settings.yaml")) return new Response("morning_time: '08:30'\nevening_time: '20:30'\n");
    if (url.includes("news-data/state.json")) return Response.json(state);
    if (url.includes("news-data/digests/2026-10-11-am-r1.json")) {
      return Response.json({ issue: "2026-10-11-am", revision: 1, mode: digestMode, stories: [{}] });
    }
    throw new Error("unexpected_public_read");
  };
  const status = await check(env, now, request);
  return { status, dispatches, writes };
}

test("completed rules digest can dispatch delayed recovery without a send operation", async () => {
  const now = new Date("2026-10-11T05:00:00Z");
  const state = recoveryState(now, { emailAttempts: 3 });
  const result = await watchdogResult(state, "rules", now);
  assert.equal(result.status, "dispatched");
  assert.deepEqual(result.dispatches, [{ mode: "run", issue: "2026-10-11-am" }]);
  assert.equal(result.writes.length, 1);
  assert.equal(result.writes[0][0], "2026-10-11-am");
  assert.equal(state.ai_issue_attempts["2026-10-11-am"], 18);
  assert.equal(state.ai_audit.length, 1);
});

test("validation-only audit does not hide a delayed production provider failure", async () => {
  const now = new Date("2026-10-11T05:00:00Z");
  const state = recoveryState(now);
  const validationAt = new Date(now.getTime() - 16 * 60 * 1000).toISOString();
  state.ai_audit.push({
    at: validationAt,
    issue: "2026-10-11-am",
    stage: "output_rejected",
    reason: "ai_output_invalid",
    extra_test: false,
    validation_only: true,
  });
  state.ai_audit.push({
    at: validationAt,
    issue: "2026-10-11-am",
    stage: "output_rejected",
    reason: "ai_output_invalid",
    extra_test: true,
  });
  const result = await watchdogResult(state, "rules", now);
  assert.equal(result.status, "dispatched");
  assert.deepEqual(result.dispatches, [{ mode: "run", issue: "2026-10-11-am" }]);
});

test("legacy budget uses the surviving global attempt count conservatively", async () => {
  const now = new Date("2026-10-11T05:00:00Z");
  const state = recoveryState(now);
  delete state.ai_issue_attempts;
  const result = await watchdogResult(state, "rules", now);
  assert.equal(result.status, "dispatched");
  assert.deepEqual(result.dispatches, [{ mode: "run", issue: "2026-10-11-am" }]);
});

test("completed issue recovery remains gated by digest mode, failure type, budget and mail state", async () => {
  const now = new Date("2026-10-11T05:00:00Z");
  const cases = [
    { name: "AI digest", state: recoveryState(now), digestMode: "ai", status: "completed" },
    { name: "terminal provider failure", state: recoveryState(now), reason: "ai_provider_http_400", status: "completed" },
    { name: "evidence rejection", state: recoveryState(now), stage: "output_rejected", reason: "ai_output_invalid", status: "completed" },
    { name: "recent last request", state: recoveryState(now), status: "completed", recentCall: true },
    { name: "recent production audit", state: recoveryState(now), status: "completed", recentAudit: true },
    { name: "one request remaining", state: recoveryState(now, { used: 19 }), status: "completed" },
    { name: "legacy near cap", state: recoveryState(now, { used: 19 }), legacy: true, status: "completed" },
    { name: "legacy unknown budget", state: recoveryState(now), legacy: true, noCalls: true, status: "completed" },
    { name: "unknown email", state: recoveryState(now, { email: "unknown" }), status: "mail_needs_review" },
  ];
  for (const scenario of cases) {
    if (scenario.stage) scenario.state.ai_audit[0].stage = scenario.stage;
    if (scenario.reason) scenario.state.ai_audit[0].reason = scenario.reason;
    if (scenario.legacy) delete scenario.state.ai_issue_attempts;
    if (scenario.noCalls) scenario.state.ai_attempts = [];
    if (scenario.recentCall) scenario.state.ai_attempts = [new Date(now.getTime() - 14 * 60 * 1000).toISOString()];
    if (scenario.recentAudit) scenario.state.ai_audit[0].at = new Date(now.getTime() - 14 * 60 * 1000).toISOString();
    const result = await watchdogResult(scenario.state, scenario.digestMode || "rules", now);
    assert.equal(result.status, scenario.status, scenario.name);
    assert.deepEqual(result.dispatches, [], scenario.name);
    assert.equal(result.writes.length, 0, scenario.name);
  }
});

test("288 daily checks use only 48 heartbeat writes on a fresh namespace", async t => {
  t.mock.method(console, "log", () => {});
  const start = Date.parse("2026-10-12T00:00:00Z");
  t.mock.timers.enable({ apis: ["Date"], now: start });
  let stored = null, writes = 0, pending;
  const env = { ENABLED: "false", DISPATCH_STATE: {
    get: async () => stored,
    put: async (key, value) => { stored = JSON.parse(value); writes++; },
  } };
  for (let i = 0; i < 288; i++) {
    t.mock.timers.setTime(start + i * 300000);
    worker.scheduled({ scheduledTime: Date.now() }, env, { waitUntil(p) { pending = p; } });
    await pending;
  }
  assert.equal(writes, 48);
  assert.equal(stored.heartbeat_interval_minutes, 30);
  assert.equal(stored.at, "2026-10-12T23:30:00.000Z");
});
