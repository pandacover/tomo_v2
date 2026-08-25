export interface CompactUpdate {
  updateId: number;
  chatId: string;
  actorId: string;
  messageId: string | null;
  text: string;
  date: number;
  photoFileId: string | null;
  photoMeta: Record<string, string | number>;
  replyTo: {
    messageId: string;
    authorRole: "user" | "assistant" | "unknown";
    text: string | null;
  } | null;
  raw: Record<string, unknown>;
}

export function compactPrivateMessage(update: Record<string, unknown>): CompactUpdate | null {
  const updateId = update.update_id;
  const message = update.message;
  if (typeof updateId !== "number" || message === null || typeof message !== "object") return null;
  const msg = message as Record<string, unknown>;
  const chat = msg.chat;
  const from = msg.from;
  if (chat === null || typeof chat !== "object" || from === null || typeof from !== "object") return null;
  const chatObj = chat as Record<string, unknown>;
  const fromObj = from as Record<string, unknown>;
  if (chatObj.type !== "private") return null;
  const chatId = String(chatObj.id ?? "");
  const actorId = String(fromObj.id ?? "");
  if (!chatId || !actorId) return null;
  const photos = Array.isArray(msg.photo) ? msg.photo : [];
  let photoFileId: string | null = null;
  let photoMeta: Record<string, string | number> = {};
  let best = -1;
  for (const item of photos) {
    if (item === null || typeof item !== "object") continue;
    const photo = item as Record<string, unknown>;
    const area = Number(photo.width || 0) * Number(photo.height || 0);
    if (typeof photo.file_id === "string" && area >= best) {
      best = area;
      photoFileId = photo.file_id;
      photoMeta = {};
      for (const key of ["width", "height", "file_size", "file_unique_id"] as const) {
        if (key in photo && (typeof photo[key] === "number" || typeof photo[key] === "string")) {
          photoMeta[key] = photo[key] as number | string;
        }
      }
    }
  }
  const text = typeof msg.text === "string" ? msg.text : typeof msg.caption === "string" ? msg.caption : "";
  if (!text && !photoFileId) return null;
  let replyTo: CompactUpdate["replyTo"] = null;
  const reply = msg.reply_to_message;
  if (reply !== null && typeof reply === "object") {
    const parent = reply as Record<string, unknown>;
    if (parent.message_id !== undefined) {
      const sender = parent.from;
      let authorRole: "user" | "assistant" | "unknown" = "unknown";
      if (sender !== null && typeof sender === "object") {
        authorRole = (sender as { is_bot?: boolean }).is_bot ? "assistant" : "user";
      }
      const replyText =
        typeof parent.text === "string" ? parent.text : typeof parent.caption === "string" ? parent.caption : null;
      replyTo = { messageId: String(parent.message_id), authorRole, text: replyText };
    }
  }
  return {
    updateId,
    chatId,
    actorId,
    messageId: msg.message_id === undefined ? null : String(msg.message_id),
    text,
    date: typeof msg.date === "number" ? msg.date : 0,
    photoFileId,
    photoMeta,
    replyTo,
    raw: update,
  };
}

export class TelegramApi {
  private token: string;

  constructor(token: string) {
    this.token = token;
  }

  async sendMessage(chatId: string, text: string, replyTo?: string | null): Promise<{ messageId: string } | { uncertain: boolean }> {
    const body: Record<string, unknown> = { chat_id: chatId, text };
    if (replyTo) body.reply_to_message_id = Number(replyTo);
    try {
      const response = await fetch(`https://api.telegram.org/bot${this.token}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) return { uncertain: false };
      const payload = (await response.json()) as { ok?: boolean; result?: { message_id?: number } };
      if (!payload.ok || payload.result?.message_id === undefined) return { uncertain: true };
      return { messageId: String(payload.result.message_id) };
    } catch {
      return { uncertain: true };
    }
  }

  async sendTyping(chatId: string): Promise<void> {
    try {
      await fetch(`https://api.telegram.org/bot${this.token}/sendChatAction`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, action: "typing" }),
      });
    } catch {
      // typing is best-effort
    }
  }

  async react(
    chatId: string,
    messageId: string,
    emoji: string,
  ): Promise<{ ok: true } | { ok: false; code: string }> {
    const numericMessageId = Number(messageId);
    if (!chatId || !Number.isSafeInteger(numericMessageId) || numericMessageId <= 0) {
      return { ok: false, code: "invalid_target" };
    }
    try {
      const response = await fetch(`https://api.telegram.org/bot${this.token}/setMessageReaction`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          chat_id: chatId,
          message_id: numericMessageId,
          reaction: [{ type: "emoji", emoji }],
          is_big: false,
        }),
      });
      let payload: { ok?: boolean; result?: boolean; error_code?: number };
      try {
        payload = (await response.json()) as { ok?: boolean; result?: boolean; error_code?: number };
      } catch {
        return { ok: false, code: response.ok ? "invalid_response" : `http_${response.status}` };
      }
      if (response.ok && payload.ok === true && payload.result === true) return { ok: true };
      if (typeof payload.error_code === "number") return { ok: false, code: `telegram_${payload.error_code}` };
      return { ok: false, code: response.ok ? "telegram_rejected" : `http_${response.status}` };
    } catch {
      return { ok: false, code: "transport" };
    }
  }

  async fetchFile(fileId: string): Promise<{ bytes: Uint8Array; mime: string } | null> {
    const meta = await fetch(`https://api.telegram.org/bot${this.token}/getFile`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ file_id: fileId }),
    });
    if (!meta.ok) return null;
    const payload = (await meta.json()) as { ok?: boolean; result?: { file_path?: string } };
    if (!payload.ok || !payload.result?.file_path) return null;
    const file = await fetch(`https://api.telegram.org/file/bot${this.token}/${payload.result.file_path}`);
    if (!file.ok) return null;
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (bytes.byteLength > 10 * 1024 * 1024) return null;
    const contentType = file.headers.get("content-type") || "";
    const mime = contentType.startsWith("image/") ? contentType : photoMime(payload.result.file_path);
    return { bytes, mime };
  }
}

function photoMime(filePath: string): string {
  if (/\.png$/i.test(filePath)) return "image/png";
  if (/\.webp$/i.test(filePath)) return "image/webp";
  return "image/jpeg";
}

export async function equalSecret(left: string, right: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(left)),
    crypto.subtle.digest("SHA-256", encoder.encode(right)),
  ]);
  const leftBytes = new Uint8Array(a);
  const rightBytes = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < leftBytes.length; i += 1) diff |= leftBytes[i] ^ rightBytes[i];
  return diff === 0;
}
