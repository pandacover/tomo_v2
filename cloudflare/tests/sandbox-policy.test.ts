import assert from "node:assert/strict";
import test from "node:test";
import { retryableSandbox, sandboxFailureCode } from "../src/sandbox-policy.ts";

test("only SDK-classified platform failures are retried", () => {
  const transient = { retryable: true, overloaded: false, toString: () => "temporary platform failure" };
  const sdkClassifier = (error: unknown) => error === transient;
  assert.equal(retryableSandbox(transient, sdkClassifier), true);
  assert.equal(retryableSandbox(new Error("status 500"), sdkClassifier), false);
  const broad = new Error("HTTP_ERROR_STATUS_500");
  broad.name = "SandboxError";
  assert.equal(retryableSandbox(broad, sdkClassifier), false);
});

test("sandbox failures are reduced to stable privacy-safe codes", () => {
  const neverTransient = () => false;
  assert.equal(sandboxFailureCode(new Error("status 500 with internal detail"), neverTransient), "sandbox_failure");
  assert.equal(sandboxFailureCode(new Error("sandbox_revision_mismatch"), neverTransient), "sandbox_revision_mismatch");
});
