import assert from "node:assert/strict";
import test from "node:test";
import { compactPrivateMessage, TelegramApi } from "../src/telegram.ts";

test("keeps private text DMs and drops groups", () => {
  const dm = compactPrivateMessage({
    update_id: 9,
    message: {
      message_id: 1,
      date: 1700000000,
      text: "hi",
      chat: { id: 5995349219, type: "private" },
      from: { id: 5995349219 },
    },
  });
  assert.equal(dm?.actorId, "5995349219");
  assert.equal(dm?.text, "hi");
  assert.equal(
    compactPrivateMessage({
      update_id: 10,
      message: {
        message_id: 2,
        date: 1700000000,
        text: "nope",
        chat: { id: -100, type: "group" },
        from: { id: 1 },
      },
    }),
    null,
  );
});

test("uses the largest photo and caption", () => {
  const compact = compactPrivateMessage({
    update_id: 11,
    message: {
      message_id: 3,
      date: 1700000000,
      caption: "look",
      chat: { id: 1, type: "private" },
      from: { id: 1 },
      photo: [
        { file_id: "small", width: 10, height: 10 },
        { file_id: "large", width: 100, height: 80, file_size: 12 },
      ],
    },
  });
  assert.equal(compact?.photoFileId, "large");
  assert.equal(compact?.text, "look");
  assert.equal(compact?.photoMeta.width, 100);
});

test("treats Telegram photo downloads with a generic content type as JPEG", async () => {
  const originalFetch = globalThis.fetch;
  let request = 0;
  globalThis.fetch = async () => {
    request += 1;
    if (request === 1) {
      return Response.json({ ok: true, result: { file_path: "photos/file_1.jpg" } });
    }
    return new Response(new Uint8Array([0xff, 0xd8, 0xff]), {
      headers: { "content-type": "application/octet-stream" },
    });
  };
  try {
    const result = await new TelegramApi("token").fetchFile("file-id");
    assert.equal(result?.mime, "image/jpeg");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
