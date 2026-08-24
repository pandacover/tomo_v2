function b64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function fromB64url(value: string): Uint8Array {
  if (!value || /[^A-Za-z0-9_-]/.test(value)) throw new Error("invalid encoding");
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - (value.length % 4)) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

function asBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

async function hmac(key: Uint8Array, data: Uint8Array): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey("raw", asBuffer(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", cryptoKey, asBuffer(data)));
}

function timingEqual(left: Uint8Array, right: Uint8Array): boolean {
  if (left.length !== right.length) return false;
  let diff = 0;
  for (let i = 0; i < left.length; i += 1) diff |= left[i] ^ right[i];
  return diff === 0;
}

export interface CronClaim {
  owner_id: string;
  actor_id: string;
  destination: string;
  session_id: string;
  issued_at: number;
  expires_at: number;
  operations: string[];
}

export interface AttachmentClaim {
  owner_id: string;
  generation_id: string;
  file_hashes: string[];
  issued_at: number;
  expires_at: number;
}

export async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function issueCron(key: Uint8Array, claim: CronClaim): Promise<string> {
  if (claim.expires_at - claim.issued_at > 3600) throw new Error("invalid_capability");
  const payload = {
    v: "v1",
    owner_id: claim.owner_id,
    actor_id: claim.actor_id,
    destination: claim.destination,
    session_id: claim.session_id,
    issued_at: claim.issued_at,
    expires_at: claim.expires_at,
    operations: [...claim.operations].sort(),
  };
  const encoded = b64url(new TextEncoder().encode(JSON.stringify(payload)));
  const signature = b64url(await hmac(key, new TextEncoder().encode(encoded)));
  return `v1.${encoded}.${signature}`;
}

export async function verifyCron(
  key: Uint8Array,
  token: string,
  operation: string,
  headers: { owner_id: string; actor_id: string; destination: string; session_id: string },
  now = Math.floor(Date.now() / 1000),
): Promise<CronClaim> {
  const parts = token.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") throw new Error("invalid_capability");
  const expected = await hmac(key, new TextEncoder().encode(parts[1]));
  const signature = fromB64url(parts[2]);
  if (!timingEqual(expected, signature)) throw new Error("invalid_capability");
  const payload = JSON.parse(new TextDecoder().decode(fromB64url(parts[1]))) as CronClaim & { v: string };
  if (payload.v !== "v1") throw new Error("invalid_capability");
  if (payload.expires_at <= now) throw new Error("expired_capability");
  if (payload.issued_at > now + 30 || payload.expires_at - payload.issued_at > 3600) throw new Error("invalid_capability");
  if (!payload.operations.includes(operation)) throw new Error("forbidden_capability");
  if (
    payload.owner_id !== headers.owner_id ||
    payload.actor_id !== headers.actor_id ||
    payload.destination !== headers.destination ||
    payload.session_id !== headers.session_id
  ) {
    throw new Error("forbidden_capability");
  }
  return payload;
}

export async function issueAttachment(key: Uint8Array, claim: AttachmentClaim): Promise<string> {
  if (claim.expires_at - claim.issued_at > 300) throw new Error("invalid_capability");
  const payload = {
    v: "v1",
    owner_id: claim.owner_id,
    generation_id: claim.generation_id,
    file_hashes: claim.file_hashes,
    issued_at: claim.issued_at,
    expires_at: claim.expires_at,
  };
  const encoded = b64url(new TextEncoder().encode(JSON.stringify(payload)));
  const signature = b64url(await hmac(key, new TextEncoder().encode(encoded)));
  return `v1.${encoded}.${signature}`;
}

export async function verifyAttachment(
  key: Uint8Array,
  token: string,
  fileId: string,
  ownerId: string,
  generationId: string,
  now = Math.floor(Date.now() / 1000),
): Promise<AttachmentClaim> {
  const parts = token.split(".");
  if (parts.length !== 3 || parts[0] !== "v1") throw new Error("invalid_capability");
  const expected = await hmac(key, new TextEncoder().encode(parts[1]));
  if (!timingEqual(expected, fromB64url(parts[2]))) throw new Error("invalid_capability");
  const payload = JSON.parse(new TextDecoder().decode(fromB64url(parts[1]))) as AttachmentClaim & { v: string };
  if (payload.v !== "v1") throw new Error("invalid_capability");
  if (payload.expires_at <= now) throw new Error("expired_capability");
  if (payload.issued_at > now + 30 || payload.expires_at - payload.issued_at > 300) throw new Error("invalid_capability");
  if (payload.owner_id !== ownerId || payload.generation_id !== generationId) throw new Error("forbidden_capability");
  const fileHash = await sha256Hex(fileId);
  if (!payload.file_hashes.includes(fileHash)) throw new Error("forbidden_capability");
  return payload;
}

export const CRON_OPS = ["create", "list", "inspect", "update", "pause", "resume", "run_now", "delete", "history"];
