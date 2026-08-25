import { DurableObject } from "cloudflare:workers";
import type { AllowlistEntry, Env } from "./env";
import { parseAllowlist } from "./env";

export class RegistryDO extends DurableObject<Env> {
  private ready: Promise<void>;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.ready = this.ctx.blockConcurrencyWhile(async () => {
      this.ctx.storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS allowlist (
          actor_id TEXT PRIMARY KEY,
          tomo_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bindings (
          actor_id TEXT PRIMARY KEY,
          tomo_id TEXT NOT NULL,
          chat_id TEXT NOT NULL
        );
      `);
      this.seed(parseAllowlist(this.env.TOMO_ALLOWLIST));
    });
  }

  private seed(entries: AllowlistEntry[]): void {
    for (const entry of entries) {
      this.ctx.storage.sql.exec(
        "INSERT INTO allowlist(actor_id, tomo_id) VALUES (?, ?) ON CONFLICT(actor_id) DO UPDATE SET tomo_id=excluded.tomo_id",
        entry.actor_id,
        entry.tomo_id,
      );
    }
  }

  async fetch(request: Request): Promise<Response> {
    await this.ready;
    const url = new URL(request.url);
    if (url.pathname === "/lookup" && request.method === "POST") {
      const body = (await request.json()) as { actor_id?: string; chat_id?: string };
      if (!body.actor_id || !body.chat_id) return Response.json({ error: "invalid" }, { status: 400 });
      this.seed(parseAllowlist(this.env.TOMO_ALLOWLIST));
      const allowed = this.ctx.storage.sql
        .exec("SELECT tomo_id FROM allowlist WHERE actor_id = ?", body.actor_id)
        .toArray() as Array<{ tomo_id: string }>;
      if (allowed.length === 0) return Response.json({ allowed: false });
      const tomoId = allowed[0].tomo_id;
      this.ctx.storage.sql.exec(
        "INSERT INTO bindings(actor_id, tomo_id, chat_id) VALUES (?, ?, ?) ON CONFLICT(actor_id) DO UPDATE SET chat_id=excluded.chat_id",
        body.actor_id,
        tomoId,
        body.chat_id,
      );
      return Response.json({ allowed: true, tomo_id: tomoId, chat_id: body.chat_id, actor_id: body.actor_id });
    }
    return Response.json({ error: "not found" }, { status: 404 });
  }
}
