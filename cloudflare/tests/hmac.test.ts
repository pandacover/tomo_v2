import assert from "node:assert/strict";
import test from "node:test";
import { CRON_OPS, issueAttachment, issueCron, sha256Hex, verifyAttachment, verifyCron } from "../src/hmac.ts";

const KEY = Uint8Array.from(
  "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f".match(/.{2}/g)!.map((byte) => parseInt(byte, 16)),
);
const NOW = 1_700_000_000;
const PYTHON_CRON =
  "v1.eyJ2IjoidjEiLCJvd25lcl9pZCI6InRvbW8tcGFuZGFjb3ZlciIsImFjdG9yX2lkIjoiNTk5NTM0OTIxOSIsImRlc3RpbmF0aW9uIjoidGVsZWdyYW06NTk5NTM0OTIxOSIsInNlc3Npb25faWQiOiJ0ZWxlZ3JhbTphY3Rvcjo1OTk1MzQ5MjE5IiwiaXNzdWVkX2F0IjoxNzAwMDAwMDAwLCJleHBpcmVzX2F0IjoxNzAwMDAwMzAwLCJvcGVyYXRpb25zIjpbImNyZWF0ZSIsImRlbGV0ZSIsImhpc3RvcnkiLCJpbnNwZWN0IiwibGlzdCIsInBhdXNlIiwicmVzdW1lIiwicnVuX25vdyIsInVwZGF0ZSJdfQ.qQ3_HSlzJCCsaXWuBkBVb2aIWEcL8JXUmqDEjLT4tMY";
const PYTHON_ATTACHMENT =
  "v1.eyJ2IjoidjEiLCJvd25lcl9pZCI6InRvbW8tcGFuZGFjb3ZlciIsImdlbmVyYXRpb25faWQiOiJnZW4tMSIsImZpbGVfaGFzaGVzIjpbIjhlZDVjMDViYzEzMGJiYWEyZDYwMDU3NzdkNTEyZGRiMWI3YjljMGI2ZTNkMzk5MTVlZjE4OGYyZGQ2YTljMjciXSwiaXNzdWVkX2F0IjoxNzAwMDAwMDAwLCJleHBpcmVzX2F0IjoxNzAwMDAwMzAwfQ.lm3aIzgNzfKx7iJzHFh9Tnx2gflWsHz0BId3-KagrZU";

test("cron tokens match the Python issuer", async () => {
  const token = await issueCron(KEY, {
    owner_id: "tomo-pandacover",
    actor_id: "5995349219",
    destination: "telegram:5995349219",
    session_id: "telegram:actor:5995349219",
    issued_at: NOW,
    expires_at: NOW + 300,
    operations: CRON_OPS,
  });
  assert.equal(token, PYTHON_CRON);
  const claim = await verifyCron(
    KEY,
    PYTHON_CRON,
    "create",
    {
      owner_id: "tomo-pandacover",
      actor_id: "5995349219",
      destination: "telegram:5995349219",
      session_id: "telegram:actor:5995349219",
    },
    NOW + 1,
  );
  assert.equal(claim.owner_id, "tomo-pandacover");
});

test("attachment tokens match the Python issuer", async () => {
  const hash = await sha256Hex("AgFILE");
  assert.equal(hash, "8ed5c05bc130bbaa2d6005777d512ddb1b7b9c0b6e3d39915ef188f2dd6a9c27");
  const token = await issueAttachment(KEY, {
    owner_id: "tomo-pandacover",
    generation_id: "gen-1",
    file_hashes: [hash],
    issued_at: NOW,
    expires_at: NOW + 300,
  });
  assert.equal(token, PYTHON_ATTACHMENT);
  await verifyAttachment(KEY, PYTHON_ATTACHMENT, "AgFILE", "tomo-pandacover", "gen-1", NOW + 1);
});

test("cron verify rejects a mismatched owner header", async () => {
  await assert.rejects(
    () =>
      verifyCron(
        KEY,
        PYTHON_CRON,
        "create",
        {
          owner_id: "other",
          actor_id: "5995349219",
          destination: "telegram:5995349219",
          session_id: "telegram:actor:5995349219",
        },
        NOW + 1,
      ),
    /forbidden_capability/,
  );
});
