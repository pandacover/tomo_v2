import type { Sandbox } from "@cloudflare/sandbox";

export interface AllowlistEntry {
  actor_id: string;
  tomo_id: string;
}

export interface Env {
  Sandbox: DurableObjectNamespace<Sandbox>;
  REGISTRY: DurableObjectNamespace;
  OWNER: DurableObjectNamespace;
  PEER: DurableObjectNamespace;
  CHECKPOINTS: R2Bucket;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  OPENROUTER_API_KEY: string;
  CRON_CAPABILITY_KEY: string;
  ATTACHMENT_CAPABILITY_KEY: string;
  PEER_CAPABILITY_KEY: string;
  TOMO_AGENT_MODEL: string;
  TOMO_VISION_MODEL: string;
  TOMO_ALLOWLIST: string;
}

export function parseAllowlist(raw: string): AllowlistEntry[] {
  const parsed = JSON.parse(raw) as unknown;
  if (!Array.isArray(parsed)) throw new Error("TOMO_ALLOWLIST must be a JSON array");
  return parsed.map((entry) => {
    if (
      entry === null ||
      typeof entry !== "object" ||
      typeof (entry as AllowlistEntry).actor_id !== "string" ||
      typeof (entry as AllowlistEntry).tomo_id !== "string"
    ) {
      throw new Error("TOMO_ALLOWLIST entries need actor_id and tomo_id");
    }
    return { actor_id: (entry as AllowlistEntry).actor_id, tomo_id: (entry as AllowlistEntry).tomo_id };
  });
}

export function hexKey(value: string): Uint8Array {
  const hex = value.trim();
  if (!/^[0-9a-fA-F]{64}$/.test(hex)) throw new Error("capability key must be 32 bytes hex");
  const out = new Uint8Array(32);
  for (let i = 0; i < 32; i += 1) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

export function logOps(fields: Record<string, string | number | null | undefined>): void {
  const body: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined && value !== null) body[key] = value;
  }
  console.log(JSON.stringify(body));
}
