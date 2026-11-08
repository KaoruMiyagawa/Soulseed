import test from "node:test";
import assert from "node:assert/strict";
import { quotaAllowsStart, quotaMessage } from "../src/explorationQuota.ts";

const exhausted = { limit: 8, used: 8, remaining: 0, available: false,
  reason: "exploration_daily_quota_exhausted", day: "2030-01-01", resets_at: "2030-01-02T00:00:00+00:00" };

test("quota exhaustion disables new starts and formats UTC reset in user timezone", () => {
  assert.equal(quotaAllowsStart(exhausted), false);
  const message = quotaMessage(exhausted, "Asia/Shanghai");
  assert.match(message, /8 \/ 8/);
  assert.match(message, /剩余 0/);
  assert.match(message, /UTC 2030-01-02 00:00/);
  assert.match(message, /8:00:00|08:00:00/);
  assert.notEqual(message, quotaMessage(exhausted, "America/New_York"));
});

test("unknown ledger is not represented as available; fresh day enables start", () => {
  assert.equal(quotaAllowsStart(null), false);
  assert.equal(quotaAllowsStart({ ...exhausted, used: null, remaining: null }), false);
  assert.match(quotaMessage(null), /无法核验/);
  assert.equal(quotaAllowsStart({ ...exhausted, used: 0, remaining: 8, available: true }), true);
});
