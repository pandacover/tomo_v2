import assert from "node:assert/strict";
import test from "node:test";
import { imageRevisionMatches, observedImageRevision } from "../src/provenance.ts";

test("guest provenance accepts only an exact source revision", () => {
  const revision = "a".repeat(40);
  assert.equal(imageRevisionMatches({ exitCode: 0, stdout: `${revision}\n` }, revision), true);
  assert.equal(imageRevisionMatches({ exitCode: 0, stdout: `${"b".repeat(40)}\n` }, revision), false);
  assert.equal(observedImageRevision({ exitCode: 1, stdout: revision }), null);
  assert.equal(observedImageRevision({ exitCode: 0, stdout: "unexpected" }), null);
});
