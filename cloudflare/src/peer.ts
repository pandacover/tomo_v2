import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env";

export class PeerDO extends DurableObject<Env> {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/v1/peer-agent/relationships" && request.method === "GET") {
      return Response.json({ ok: true, relationships: [] });
    }
    if (url.pathname === "/v1/peer-agent/requests" && request.method === "POST") {
      return Response.json({ ok: false, error: "peer_unavailable" });
    }
    if (url.pathname.startsWith("/v1/peer-agent/requests/") && request.method === "GET") {
      return Response.json({ ok: false, error: "peer_not_found" }, { status: 404 });
    }
    if (url.pathname.startsWith("/v1/peer-agent/threads/") && request.method === "GET") {
      return Response.json({ ok: false, error: "peer_not_found" }, { status: 404 });
    }
    return Response.json({ error: "not found" }, { status: 404 });
  }
}
