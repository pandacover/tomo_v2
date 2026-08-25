import assert from "node:assert/strict";
import test from "node:test";
import { nextIntervalAt, parseCronSchedule } from "../src/cron-policy.ts";

test("cron schedules validate one-shot, delay, and recurring inputs", () => {
  const now = Date.parse("2026-01-01T00:00:00Z");
  assert.deepEqual(parseCronSchedule({ kind: "delay", afterSeconds: 30 }, now), {
    kind: "once", scheduleAt: now + 30_000, everySeconds: null,
  });
  assert.deepEqual(parseCronSchedule({ kind: "interval", everySeconds: 60 }, now), {
    kind: "interval", scheduleAt: now + 60_000, everySeconds: 60,
  });
  assert.throws(() => parseCronSchedule({ kind: "interval", everySeconds: 0 }, now), /cron_invalid_schedule/);
  assert.throws(() => parseCronSchedule({ kind: "cron" }, now), /cron_expression_unsupported/);
});

test("recurring jobs advance from their schedule without replaying missed slots", () => {
  const scheduled = Date.parse("2026-01-01T00:00:00Z");
  assert.equal(nextIntervalAt(scheduled, 60, scheduled + 10_000), scheduled + 60_000);
  assert.equal(nextIntervalAt(scheduled, 60, scheduled + 190_000), scheduled + 240_000);
});
