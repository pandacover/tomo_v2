import type { CompactUpdate } from "./telegram";

const EVENT_MARKER = "TOMO_SANDBOX_EVENT=";
const LATENCY_MARKER = "TOMO_SANDBOX_LATENCY_V1=";
const DIAGNOSTIC_MARKER = "TOMO_SANDBOX_DIAGNOSTIC=";

export interface InboundBurst {
  burst_id: string;
  generation_id: string;
  revision: number;
  visible_assistant_utterances: string[];
  accepted_generation_ids: string[];
  messages: Array<{
    ordinal: number;
    update_id: number;
    envelope: Record<string, unknown>;
  }>;
}

export function encodeInbound(requestId: string, burst: InboundBurst): string {
  return JSON.stringify({ version: 2, type: "inbound", request_id: requestId, burst });
}

export function encodeAutomation(requestId: string, turn: Record<string, unknown>): string {
  return JSON.stringify({ version: 6, type: "automation", request_id: requestId, turn });
}

export function envelopeFromCompact(
  compact: CompactUpdate,
  tomoId: string,
): Record<string, unknown> {
  const attachments = compact.photoFileId
    ? [
        {
          kind: "image",
          file_id: compact.photoFileId,
          url: null,
          path: null,
          mime_type: "image/jpeg",
          metadata: compact.photoMeta,
        },
      ]
    : [];
  const reply =
    compact.replyTo === null
      ? null
      : {
          message_id: compact.replyTo.messageId,
          author_role: compact.replyTo.authorRole,
          text: compact.replyTo.text,
          timestamp: null,
          attachments: [],
          availability: compact.replyTo.text ? "available" : "unavailable",
          truncated: false,
        };
  return {
    connector: "telegram",
    actor_id: compact.actorId,
    message_id: compact.messageId || String(compact.updateId),
    text: compact.text,
    timestamp: new Date(compact.date * 1000).toISOString(),
    attachments,
    location: null,
    native_metadata: { update_id: compact.updateId, from_id: compact.actorId, tomo_id: tomoId },
    reply_context: reply,
  };
}

export type SandboxEvent =
  | { type: "frame"; sequence: number; text: string }
  | { type: "reaction"; sequence: number; emoji: string; target_message_id: string }
  | { type: "completed"; sequence: number }
  | { type: "error"; sequence: number; code: string }
  | { type: "stale"; sequence: number };

export function parseEventLine(line: string, requestId: string, generationId: string): SandboxEvent | null {
  if (line.includes(LATENCY_MARKER)) return null;
  const position = line.indexOf(EVENT_MARKER);
  if (position < 0) return null;
  const message = JSON.parse(line.slice(position + EVENT_MARKER.length)) as Record<string, unknown>;
  const sequence = message.sequence;
  if (message.request_id !== requestId || message.generation_id !== generationId) {
    if (message.type === "error") {
      const error = message.error as { code?: string } | undefined;
      return { type: "error", sequence: typeof sequence === "number" ? sequence : 0, code: error?.code || "runtime_failed" };
    }
    throw new Error("sandbox event binding mismatch");
  }
  if (typeof sequence !== "number") throw new Error("sandbox event sequence missing");
  if (message.type === "frame" && typeof message.text === "string") {
    return { type: "frame", sequence, text: message.text };
  }
  if (message.type === "reaction" && typeof message.emoji === "string" && typeof message.target_message_id === "string") {
    return { type: "reaction", sequence, emoji: message.emoji, target_message_id: message.target_message_id };
  }
  if (message.type === "completed") return { type: "completed", sequence };
  if (message.type === "error") {
    const error = message.error as { code?: string } | undefined;
    return { type: "error", sequence, code: error?.code || "runtime_failed" };
  }
  if (message.type === "stale") return { type: "stale", sequence };
  throw new Error("unsupported sandbox event");
}

export function splitLines(chunk: string, carry: string): { lines: string[]; rest: string } {
  const combined = carry + chunk;
  const parts = combined.split(/\r?\n/);
  const rest = parts.pop() ?? "";
  return { lines: parts, rest };
}

export function consumeLogSnapshot(
  snapshot: string,
  offset: number,
  carry: string,
): { lines: string[]; offset: number; carry: string } {
  if (!Number.isInteger(offset) || offset < 0 || offset > snapshot.length) {
    throw new Error("sandbox log cursor is invalid");
  }
  const split = splitLines(snapshot.slice(offset), carry);
  return { lines: split.lines, offset: snapshot.length, carry: split.rest };
}

export function parseDiagnosticLine(line: string): string | null {
  if (!line.startsWith(DIAGNOSTIC_MARKER)) return null;
  const code = line.slice(DIAGNOSTIC_MARKER.length);
  return /^(?:attachment_[a-z0-9_]+|unsupported_image|vision_[a-z0-9_]+)$/.test(code) ? code : null;
}

export function requestIdFor(generationId: string): string {
  const safe = generationId.replace(/[^A-Za-z0-9._:-]/g, "-").slice(0, 100);
  return `telegram-generation-${safe}`.slice(0, 128);
}
