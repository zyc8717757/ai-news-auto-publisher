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

test("request failure leaves start proof and a sanitized reason", async t => {
  const records = []; let pending;
  t.mock.method(globalThis, "fetch", async () => { throw Object.assign(new Error("PRIVATE_MARKER"), { name: "TimeoutError" }); });
  worker.scheduled({ scheduledTime: Date.now() }, {
    ENABLED: "true", GITHUB_DISPATCH_TOKEN: "TEST_ONLY",
    DISPATCH_STATE: { get: async () => null, put: async (key, value) => records.push(JSON.parse(value)) },
  }, { waitUntil(promise) { pending = promise; } });
  await assert.rejects(pending, /^Error: watchdog_failed$/);
  assert.equal(records[0].status, "started");
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
