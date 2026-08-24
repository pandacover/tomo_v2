import type { Env } from "./env";
import { hexKey, logOps } from "./env";
import { verifyAttachment, verifyCron } from "./hmac";
import { OwnerDO } from "./owner";
import { PeerDO } from "./peer";
import { RegistryDO } from "./registry";
import { compactPrivateMessage, equalSecret, TelegramApi } from "./telegram";

export { Sandbox } from "@cloudflare/sandbox";
export { OwnerDO, RegistryDO, PeerDO };

function json(payload: unknown, status = 200): Response {
  return Response.json(payload, { status, headers: { "cache-control": "no-store" } });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health" || url.pathname === "/v1/health") return json({ ok: true });

    if (url.pathname === "/telegram/webhook" && request.method === "POST") {
      const header = request.headers.get("x-telegram-bot-api-secret-token") || "";
      if (!env.TELEGRAM_WEBHOOK_SECRET || !(await equalSecret(header, env.TELEGRAM_WEBHOOK_SECRET))) {
        return json({ error: "unauthorized" }, 401);
      }
      let update: Record<string, unknown>;
      try {
        update = (await request.json()) as Record<string, unknown>;
      } catch {
        return json({ error: "invalid" }, 400);
      }
      const compact = compactPrivateMessage(update);
      if (!compact) return json({ ok: true, ignored: true });
      const registry = env.REGISTRY.get(env.REGISTRY.idFromName("global"));
      const lookup = await registry.fetch("https://tomo.internal/lookup", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ actor_id: compact.actorId, chat_id: compact.chatId }),
      });
      const binding = (await lookup.json()) as { allowed?: boolean; tomo_id?: string };
      if (!binding.allowed || !binding.tomo_id) {
        logOps({ event: "stranger_dropped", actor_present: 1 });
        return json({ ok: true, dropped: true });
      }
      const owner = env.OWNER.get(env.OWNER.idFromName(binding.tomo_id));
      const persisted = await owner.fetch("https://tomo.internal/inbox", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ compact, origin: url.origin }),
      });
      if (!persisted.ok) return json({ error: "persist_failed" }, 500);
      return json({ ok: true });
    }

    if (url.pathname === "/v1/attachments/resolve" && request.method === "POST") {
      const token = bearer(request);
      const ownerId = request.headers.get("x-tomo-owner-id") || "";
      const generationId = request.headers.get("x-tomo-generation-id") || "";
      const body = (await request.json().catch(() => null)) as { fileId?: string } | null;
      if (!token || !ownerId || !generationId || !body?.fileId) return json({ error: "invalid capability" }, 401);
      try {
        await verifyAttachment(hexKey(env.ATTACHMENT_CAPABILITY_KEY), token, body.fileId, ownerId, generationId);
      } catch {
        return json({ error: "invalid capability" }, 401);
      }
      const file = await new TelegramApi(env.TELEGRAM_BOT_TOKEN).fetchFile(body.fileId);
      if (!file) return json({ error: "attachment service unavailable" }, 503);
      return new Response(file.bytes.slice(), {
        headers: { "content-type": file.mime, "cache-control": "no-store" },
      });
    }

    if (url.pathname.startsWith("/v1/cron/")) {
      const token = bearer(request);
      const ownerId = request.headers.get("x-tomo-owner-id") || "";
      const actorId = request.headers.get("x-tomo-actor-id") || "";
      const destination = request.headers.get("x-tomo-destination") || "";
      const sessionId = request.headers.get("x-tomo-session-id") || "";
      const operation = cronOperation(url.pathname, request.method);
      if (!token || !operation) return json({ error: "cron_unauthorized" }, 401);
      try {
        await verifyCron(hexKey(env.CRON_CAPABILITY_KEY), token, operation, {
          owner_id: ownerId,
          actor_id: actorId,
          destination,
          session_id: sessionId,
        });
      } catch {
        return json({ error: "cron_unauthorized" }, 401);
      }
      const owner = env.OWNER.get(env.OWNER.idFromName(ownerId));
      return owner.fetch(new Request(`https://tomo.internal${url.pathname}${url.search}`, request));
    }

    if (url.pathname.startsWith("/v1/peer-agent/")) {
      const ownerId = request.headers.get("x-tomo-owner-id") || "default";
      const peer = env.PEER.get(env.PEER.idFromName(ownerId));
      return peer.fetch(request);
    }

    return json({ error: "not found" }, 404);
  },
};

function bearer(request: Request): string {
  const header = request.headers.get("authorization") || "";
  return header.startsWith("Bearer ") ? header.slice(7) : "";
}

function cronOperation(pathname: string, method: string): string | null {
  if (pathname === "/v1/cron/jobs" && method === "POST") return "create";
  if (pathname === "/v1/cron/jobs" && method === "GET") return "list";
  const match = pathname.match(/^\/v1\/cron\/jobs\/([^/]+)(?:\/([^/]+))?$/);
  if (!match) return null;
  if (!match[2] && method === "GET") return "inspect";
  if (!match[2] && method === "PATCH") return "update";
  if (match[2] === "pause") return "pause";
  if (match[2] === "resume") return "resume";
  if (match[2] === "run-now") return "run_now";
  if (match[2] === "delete") return "delete";
  if (match[2] === "history") return "history";
  return null;
}
