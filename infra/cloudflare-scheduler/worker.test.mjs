import test from "node:test";
import assert from "node:assert/strict";
import { check, dueIssue } from "./worker.mjs";

test("Beijing cutoff and previous-day mapping", () => {
  assert.equal(dueIssue(new Date("2026-10-11T00:29Z"), "08:30", "20:30"), "2026-10-10-pm");
  assert.equal(dueIssue(new Date("2026-10-11T00:30Z"), "08:30", "20:30"), "2026-10-11-am");
  assert.equal(dueIssue(new Date("2026-10-11T12:30Z"), "08:30", "20:30"), "2026-10-11-pm");
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
});
