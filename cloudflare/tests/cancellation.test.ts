import assert from "node:assert/strict";
import test from "node:test";
import { cancelSupersededExecution } from "../src/cancellation.ts";

test("superseding a turn aborts stream consumption and destroys its sandbox", async () => {
  const controller = new AbortController();
  let destroys = 0;

  const result = await cancelSupersededExecution(controller, {
    async destroy() {
      destroys += 1;
    },
  });

  assert.equal(controller.signal.aborted, true);
  assert.equal(destroys, 1);
  assert.equal(result, "destroyed");
});

test("sandbox teardown failures are contained after the stream is aborted", async () => {
  const controller = new AbortController();

  const result = await cancelSupersededExecution(controller, {
    async destroy() {
      throw new Error("control plane unavailable");
    },
  });

  assert.equal(controller.signal.aborted, true);
  assert.equal(result, "destroy_failed");
});

test("an evicted turn without an in-memory sandbox still aborts safely", async () => {
  const controller = new AbortController();
  const result = await cancelSupersededExecution(controller, null);

  assert.equal(controller.signal.aborted, true);
  assert.equal(result, "no_sandbox");
});
