import assert from "node:assert/strict";
import test from "node:test";
import { encodeInbound, parseDiagnosticLine, parseEventLine, requestIdFor, splitLines } from "../src/protocol.ts";

test("inbound envelope is protocol v2", () => {
  const payload = JSON.parse(
    encodeInbound("telegram-generation-1", {
      burst_id: "1:1",
      generation_id: "1:1:r1",
      revision: 1,
      visible_assistant_utterances: [],
      accepted_generation_ids: [],
      messages: [
        {
          ordinal: 1,
          update_id: 1,
          envelope: { connector: "telegram", actor_id: "1", message_id: "1", text: "hi" },
        },
      ],
    }),
  );
  assert.equal(payload.version, 2);
  assert.equal(payload.type, "inbound");
  assert.equal(payload.burst.messages[0].envelope.text, "hi");
});

test("parses frame and error events and ignores latency", () => {
  const frame = parseEventLine(
    'TOMO_SANDBOX_EVENT={"version":5,"request_id":"r1","generation_id":"g1","sequence":0,"type":"frame","segment_index":0,"frame_index":0,"text":"hello"}',
    "r1",
    "g1",
  );
  assert.deepEqual(frame, { type: "frame", sequence: 0, text: "hello" });
  const error = parseEventLine(
    'TOMO_SANDBOX_EVENT={"version":5,"request_id":"r1","generation_id":"g1","sequence":1,"type":"error","error":{"code":"provider_budget"}}',
    "r1",
    "g1",
  );
  assert.equal(error?.type, "error");
  if (error?.type === "error") assert.equal(error.code, "provider_budget");
  const unbound = parseEventLine(
    'TOMO_SANDBOX_EVENT={"version":5,"request_id":"unknown","generation_id":"unknown","sequence":0,"type":"error","error":{"code":"missing_access_token"}}',
    "r1",
    "g1",
  );
  assert.equal(unbound?.type, "error");
  if (unbound?.type === "error") assert.equal(unbound.code, "missing_access_token");
  assert.equal(parseEventLine("TOMO_SANDBOX_LATENCY_V1=phase=sandbox_runtime_entry outcome=ok elapsed_ms=0", "r1", "g1"), null);
});

test("splitLines carries partial chunks", () => {
  const first = splitLines("a\nb\nhe", "");
  assert.deepEqual(first.lines, ["a", "b"]);
  const second = splitLines("llo\n", first.rest);
  assert.deepEqual(second.lines, ["hello"]);
  assert.equal(requestIdFor("chat:1:r2").startsWith("telegram-generation-"), true);
});

test("parses only fixed sandbox diagnostic codes", () => {
  assert.equal(parseDiagnosticLine("TOMO_SANDBOX_DIAGNOSTIC=attachment_auth_failed"), "attachment_auth_failed");
  assert.equal(parseDiagnosticLine("TOMO_SANDBOX_DIAGNOSTIC=vision_provider_timeout"), "vision_provider_timeout");
  assert.equal(parseDiagnosticLine("TOMO_SANDBOX_DIAGNOSTIC=unsupported_image"), "unsupported_image");
  assert.equal(parseDiagnosticLine("TOMO_SANDBOX_DIAGNOSTIC=secret=value"), null);
});
