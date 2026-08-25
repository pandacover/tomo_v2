import { DurableObject } from "cloudflare:workers";
import { getSandbox, parseSSEStream, type ExecEvent } from "@cloudflare/sandbox";
import type { Env } from "./env";
import { hexKey, logOps } from "./env";
import { CRON_OPS, issueAttachment, issueCron, sha256Hex } from "./hmac";
import {
  encodeAutomation,
  encodeInbound,
  envelopeFromCompact,
  parseDiagnosticLine,
  parseEventLine,
  requestIdFor,
  splitLines,
  type InboundBurst,
} from "./protocol";
import { TelegramApi, type CompactUpdate } from "./telegram";

const DATA_DIR = "/workspace/tomo-data";
const COMMAND = "/opt/tomo/.venv/bin/tomo-core sandbox-inbound";
const DEBOUNCE_MS = 700;
const PACE_MS = 1500;
const BUDGET_LINE = "I cannot think right now. Model budget hit. Try later.";

type SqlRow = Record<string, string | number | null>;

export class OwnerDO extends DurableObject<Env> {
  private ready: Promise<void>;
  private running = false;
  private abort: AbortController | null = null;
  private liveSandbox: SandboxHandle | null = null;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.ready = this.ctx.blockConcurrencyWhile(async () => this.init());
  }

  private sql(query: string, ...binds: (string | number | null)[]): SqlRow[] {
    return this.ctx.storage.sql.exec(query, ...binds).toArray() as SqlRow[];
  }

  private one(query: string, ...binds: (string | number | null)[]): SqlRow | null {
    return this.sql(query, ...binds)[0] ?? null;
  }

  private init(): void {
    this.ctx.storage.sql.exec(`
      CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS inbox (
        update_id INTEGER PRIMARY KEY,
        chat_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        message_id TEXT,
        payload TEXT NOT NULL,
        created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS chat_turn (
        chat_id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL,
        burst_id TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        active_generation_id TEXT,
        quiet_until INTEGER
      );
      CREATE TABLE IF NOT EXISTS generations (
        generation_id TEXT PRIMARY KEY,
        burst_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        chat_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        error_class TEXT
      );
      CREATE TABLE IF NOT EXISTS deliveries (
        generation_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        text TEXT NOT NULL,
        reply_to TEXT,
        status TEXT NOT NULL,
        telegram_message_id TEXT,
        PRIMARY KEY (generation_id, sequence)
      );
      CREATE TABLE IF NOT EXISTS cron_jobs (
        job_id TEXT PRIMARY KEY,
        destination TEXT NOT NULL,
        intent TEXT NOT NULL,
        constraints TEXT NOT NULL,
        schedule_kind TEXT NOT NULL,
        schedule_at INTEGER,
        schedule_every REAL,
        status TEXT NOT NULL,
        revision INTEGER NOT NULL,
        successful_runs INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT UNIQUE
      );
    `);
  }

  private tomoId(): string {
    return this.ctx.id.name || "tomo-unknown";
  }

  private origin(): string {
    return String(this.one("SELECT v FROM meta WHERE k = 'origin'")?.v || "");
  }

  async fetch(request: Request): Promise<Response> {
    await this.ready;
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/inbox") {
      const body = (await request.json()) as { compact: CompactUpdate; origin: string };
      this.ctx.storage.sql.exec(
        "INSERT INTO meta(k, v) VALUES ('origin', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        body.origin,
      );
      return await this.enqueue(body.compact);
    }
    if (url.pathname.startsWith("/v1/cron/")) return this.cron(request, url);
    return Response.json({ error: "not found" }, { status: 404 });
  }

  async alarm(): Promise<void> {
    await this.ready;
    this.ctx.waitUntil(this.handleAlarm());
  }

  private async handleAlarm(): Promise<void> {
    if (this.running) {
      await this.scheduleWake();
      return;
    }
    const dueCron = this.one(
      "SELECT * FROM cron_jobs WHERE status = 'active' AND schedule_at IS NOT NULL AND schedule_at <= ? ORDER BY schedule_at LIMIT 1",
      Date.now(),
    );
    const turn = this.one(
      "SELECT * FROM chat_turn WHERE burst_id IS NOT NULL AND active_generation_id IS NULL AND quiet_until IS NOT NULL AND quiet_until <= ?",
      Date.now(),
    );
    logOps({
      event: "owner_alarm",
      tomo_id: this.tomoId(),
      cron: dueCron ? 1 : 0,
      turn: turn ? 1 : 0,
    });
    try {
      if (dueCron) await this.runCron(dueCron);
      else if (turn) await this.runInteractive(String(turn.chat_id));
    } catch (error) {
      logOps({
        event: "owner_alarm_failed",
        tomo_id: this.tomoId(),
        error_class: error instanceof Error ? error.name : "Error",
      });
    }
    await this.scheduleWake();
  }

  private async scheduleWake(): Promise<void> {
    const quiet = this.one(
      "SELECT MIN(quiet_until) AS next FROM chat_turn WHERE burst_id IS NOT NULL AND active_generation_id IS NULL AND quiet_until IS NOT NULL",
    );
    const cron = this.one(
      "SELECT MIN(schedule_at) AS next FROM cron_jobs WHERE status = 'active' AND schedule_at IS NOT NULL",
    );
    const candidates = [quiet?.next, cron?.next].filter((value): value is number => typeof value === "number");
    if (candidates.length === 0) {
      await this.ctx.storage.deleteAlarm();
      return;
    }
    await this.ctx.storage.setAlarm(Math.min(...candidates));
  }

  private async enqueue(compact: CompactUpdate): Promise<Response> {
    if (this.one("SELECT 1 AS ok FROM inbox WHERE update_id = ?", compact.updateId)) {
      return Response.json({ ok: true, duplicate: true });
    }
    this.ctx.storage.sql.exec(
      "INSERT INTO inbox(update_id, chat_id, actor_id, message_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
      compact.updateId,
      compact.chatId,
      compact.actorId,
      compact.messageId,
      JSON.stringify(compact),
      Date.now(),
    );
    const turn = this.one("SELECT * FROM chat_turn WHERE chat_id = ?", compact.chatId);
    const now = Date.now();
    if (turn?.active_generation_id) {
      this.ctx.storage.sql.exec(
        "UPDATE generations SET status = 'superseded' WHERE generation_id = ? AND status = 'active'",
        turn.active_generation_id,
      );
      this.abort?.abort();
    }
    const burstId = turn?.burst_id ? String(turn.burst_id) : `${compact.chatId}:${compact.updateId}`;
    const revision = Number(turn?.revision || 0) + 1;
    this.ctx.storage.sql.exec(
      `INSERT INTO chat_turn(chat_id, actor_id, burst_id, revision, active_generation_id, quiet_until)
       VALUES (?, ?, ?, ?, NULL, ?)
       ON CONFLICT(chat_id) DO UPDATE SET
         actor_id=excluded.actor_id,
         burst_id=excluded.burst_id,
         revision=excluded.revision,
         active_generation_id=NULL,
         quiet_until=excluded.quiet_until`,
      compact.chatId,
      compact.actorId,
      burstId,
      revision,
      now + DEBOUNCE_MS,
    );
    await this.ctx.storage.setAlarm(now + DEBOUNCE_MS);
    this.ctx.waitUntil(new TelegramApi(this.env.TELEGRAM_BOT_TOKEN).sendTyping(compact.chatId));
    logOps({ event: "inbox_enqueued", tomo_id: this.tomoId(), update_id: compact.updateId });
    return Response.json({ ok: true });
  }

  private messagesForBurst(burstId: string, chatId: string): CompactUpdate[] {
    const firstUpdate = Number(burstId.split(":").pop());
    const rows = this.sql(
      "SELECT payload FROM inbox WHERE chat_id = ? AND update_id >= ? ORDER BY update_id",
      chatId,
      Number.isFinite(firstUpdate) ? firstUpdate : 0,
    );
    return rows.map((row) => JSON.parse(String(row.payload)) as CompactUpdate);
  }

  private async runInteractive(chatId: string): Promise<void> {
    const turn = this.one("SELECT * FROM chat_turn WHERE chat_id = ?", chatId);
    if (!turn?.burst_id || turn.active_generation_id) return;
    const burstId = String(turn.burst_id);
    const revision = Number(turn.revision);
    const generationId = `${burstId}:r${revision}`;
    this.ctx.storage.sql.exec(
      "INSERT INTO generations(generation_id, burst_id, revision, chat_id, kind, status, created_at) VALUES (?, ?, ?, ?, 'interactive', 'active', ?)",
      generationId,
      burstId,
      revision,
      chatId,
      Date.now(),
    );
    this.ctx.storage.sql.exec("UPDATE chat_turn SET active_generation_id = ? WHERE chat_id = ?", generationId, chatId);
    const messages = this.messagesForBurst(burstId, chatId);
    if (messages.length === 0) {
      this.finishGeneration(generationId, chatId, "failed", "empty_burst");
      return;
    }
    const visible = this.sql(
      "SELECT text FROM deliveries WHERE status IN ('sent', 'unknown') AND generation_id IN (SELECT generation_id FROM generations WHERE burst_id = ?) ORDER BY generation_id, sequence",
      burstId,
    ).map((row) => String(row.text));
    const burst: InboundBurst = {
      burst_id: burstId,
      generation_id: generationId,
      revision,
      visible_assistant_utterances: visible,
      accepted_generation_ids: [],
      messages: messages.map((item, index) => ({
        ordinal: index + 1,
        update_id: item.updateId,
        envelope: envelopeFromCompact(item, this.tomoId()),
      })),
    };
    const requestId = requestIdFor(generationId);
    const last = messages[messages.length - 1];
    await this.execute(generationId, chatId, last.actorId, requestId, {
      TOMO_INBOUND_JSON: encodeInbound(requestId, burst),
    }, last, { interactive: true, images: messages.flatMap((item) => item.photoFileId ? [item.photoFileId] : []) });
  }

  private async runCron(job: SqlRow): Promise<void> {
    const turn = this.one("SELECT * FROM chat_turn LIMIT 1");
    if (!turn) return;
    if (turn.active_generation_id) return;
    const chatId = String(turn.chat_id);
    const actorId = String(turn.actor_id);
    const destination = String(job.destination);
    const boundChat = destination.startsWith("telegram:") ? destination.slice("telegram:".length) : chatId;
    const revision = Number(turn.revision) + 1;
    const generationId = `cron:${job.job_id}:${Date.now()}`;
    const runId = `run:${generationId}`;
    this.ctx.storage.sql.exec("UPDATE chat_turn SET revision = ?, active_generation_id = ? WHERE chat_id = ?", revision, generationId, boundChat);
    this.ctx.storage.sql.exec(
      "INSERT INTO generations(generation_id, burst_id, revision, chat_id, kind, status, created_at) VALUES (?, ?, ?, ?, 'automation', 'active', ?)",
      generationId,
      `cron:${job.job_id}`,
      revision,
      boundChat,
      Date.now(),
    );
    const scheduled = new Date(Number(job.schedule_at) || Date.now()).toISOString();
    const turnPayload = {
      generation_id: generationId,
      revision,
      job_id: String(job.job_id),
      run_id: runId,
      actor_id: actorId,
      chat_id: boundChat,
      intent: String(job.intent),
      scheduled_for: scheduled,
      previous_outcome: null,
      trigger: "schedule",
      will_end_after_run: job.schedule_kind === "once" || job.schedule_every == null,
      connector: "telegram",
      constraints: JSON.parse(String(job.constraints || "[]")),
      successful_runs: Number(job.successful_runs || 0),
      facts: [],
    };
    const requestId = requestIdFor(generationId);
    await this.execute(generationId, boundChat, actorId, requestId, {
      TOMO_AUTOMATION_JSON: encodeAutomation(requestId, turnPayload),
    }, null, { interactive: false, images: [] });
    if (job.schedule_every != null) {
      this.ctx.storage.sql.exec(
        "UPDATE cron_jobs SET schedule_at = ?, successful_runs = successful_runs + 1 WHERE job_id = ?",
        Date.now() + Number(job.schedule_every) * 1000,
        String(job.job_id),
      );
    } else {
      this.ctx.storage.sql.exec(
        "UPDATE cron_jobs SET status = 'ended', successful_runs = successful_runs + 1, schedule_at = NULL WHERE job_id = ?",
        String(job.job_id),
      );
    }
  }

  private generationActive(generationId: string): boolean {
    return this.one("SELECT 1 AS ok FROM generations WHERE generation_id = ? AND status = 'active'", generationId) !== null;
  }

  private finishGeneration(generationId: string, chatId: string, status: string, errorClass: string | null): void {
    this.ctx.storage.sql.exec(
      "UPDATE generations SET status = ?, error_class = ? WHERE generation_id = ? AND status = 'active'",
      status,
      errorClass,
      generationId,
    );
    this.ctx.storage.sql.exec(
      "UPDATE chat_turn SET active_generation_id = NULL, burst_id = CASE WHEN burst_id IS NOT NULL THEN NULL ELSE burst_id END WHERE chat_id = ? AND active_generation_id = ?",
      chatId,
      generationId,
    );
  }

  private async execute(
    generationId: string,
    chatId: string,
    actorId: string,
    requestId: string,
    extraEnv: Record<string, string>,
    last: CompactUpdate | null,
    options: { interactive: boolean; images: string[] },
  ): Promise<void> {
    const started = Date.now();
    this.running = true;
    const abort = new AbortController();
    this.abort = abort;
    const telegram = new TelegramApi(this.env.TELEGRAM_BOT_TOKEN);
    const typing = this.keepTyping(chatId, abort.signal, telegram);
    const sandbox = getSandbox(this.env.Sandbox, this.tomoId(), {
      enableDefaultSession: false,
      normalizeId: true,
      sleepAfter: "10s",
      containerTimeouts: { instanceGetTimeoutMS: 120_000, portReadyTimeoutMS: 180_000 },
    });
    this.liveSandbox = sandbox;
    let errorClass: string | null = null;
    try {
      if (!this.env.OPENROUTER_API_KEY) {
        throw new Error("missing_openrouter_key");
      }
      logOps({ event: "exec_start", tomo_id: this.tomoId(), generation_id: generationId });
      await this.prepareGuest(sandbox);
      const env = await this.guestEnv(generationId, chatId, actorId, extraEnv, options);
      const timeout = 240_000 + Math.min(options.images.length, 8) * 75_000;
      const stream = await this.execGuestStream(sandbox, env, timeout);
      let carry = "";
      let firstFrame = true;
      let exitCode: number | null = null;
      let stderrPresent = false;
      const diagnosticCodes = new Set<string>();
      for await (const event of parseSSEStream<ExecEvent>(stream)) {
        if (event.type === "stdout") {
          const split = splitLines(event.data || "", carry);
          carry = split.rest;
          const flag = await this.handleLines(
            split.lines,
            generationId,
            chatId,
            requestId,
            telegram,
            last,
            firstFrame,
            diagnosticCodes,
          );
          firstFrame = flag.firstFrame;
          if (flag.errorClass) errorClass = flag.errorClass;
        } else if (event.type === "stderr") {
          stderrPresent = stderrPresent || Boolean(event.data);
        } else if (event.type === "complete") {
          exitCode = typeof event.exitCode === "number" ? event.exitCode : null;
        } else if (event.type === "error") {
          throw new Error(`sandbox_stream_${event.error || "failed"}`);
        }
      }
      if (carry.trim()) {
        const flag = await this.handleLines(
          [carry],
          generationId,
          chatId,
          requestId,
          telegram,
          last,
          firstFrame,
          diagnosticCodes,
        );
        if (flag.errorClass) errorClass = flag.errorClass;
      }
      if (exitCode === null && !errorClass) errorClass = "sandbox_stream_incomplete";
      if (exitCode !== null && exitCode !== 0 && !errorClass) {
        errorClass = exitCode === 124 ? "sandbox_timeout" : `sandbox_exit_${exitCode}`;
      }
      logOps({
        event: "exec_result",
        tomo_id: this.tomoId(),
        generation_id: generationId,
        exit_code: exitCode,
        error_class: errorClass,
        stderr_present: stderrPresent ? 1 : 0,
      });
      if (this.generationActive(generationId) && !abort.signal.aborted) {
        await this.checkpoint(sandbox);
      }
    } catch (error) {
      errorClass = sandboxLabel(error);
      logOps({
        event: "exec_failed",
        tomo_id: this.tomoId(),
        generation_id: generationId,
        error_class: errorClass,
      });
    } finally {
      abort.abort();
      await typing.catch(() => undefined);
      try {
        await sandbox.destroy();
      } catch {
        // destroy is best-effort stop for the $5 envelope
      }
      this.running = false;
      this.abort = null;
      this.liveSandbox = null;
      const delivered = this.one(
        "SELECT 1 AS ok FROM deliveries WHERE generation_id = ? AND status = 'sent'",
        generationId,
      );
      if (this.generationActive(generationId) && errorClass && errorClass !== "provider_budget" && !delivered) {
        await telegram.sendMessage(chatId, `I couldn't finish that turn (${errorClass}). Try again.`);
      }
      const status = this.generationActive(generationId) ? (errorClass ? "failed" : "completed") : "superseded";
      if (this.generationActive(generationId)) this.finishGeneration(generationId, chatId, status, errorClass);
      logOps({
        event: "generation_done",
        tomo_id: this.tomoId(),
        generation_id: generationId,
        duration_ms: Date.now() - started,
        error_class: errorClass,
      });
    }
  }

  private async handleLines(
    lines: string[],
    generationId: string,
    chatId: string,
    requestId: string,
    telegram: TelegramApi,
    last: CompactUpdate | null,
    firstFrame: boolean,
    diagnosticCodes: Set<string>,
  ): Promise<{ firstFrame: boolean; errorClass: string | null }> {
    let errorClass: string | null = null;
    for (const line of lines) {
      const diagnostic = parseDiagnosticLine(line);
      if (diagnostic) {
        if (!diagnosticCodes.has(diagnostic) && this.generationActive(generationId)) {
          diagnosticCodes.add(diagnostic);
          logOps({ event: "vision_diagnostic", tomo_id: this.tomoId(), generation_id: generationId, code: diagnostic });
          await telegram.sendMessage(chatId, `Vision diagnostic: ${diagnostic}`);
        }
        continue;
      }
      let event;
      try {
        event = parseEventLine(line, requestId, generationId);
      } catch {
        continue;
      }
      if (!event) continue;
      if (!this.generationActive(generationId)) continue;
      if (event.type === "error") {
        errorClass = event.code;
        if (event.code === "provider_budget") {
          const delivered = this.one(
            "SELECT 1 AS ok FROM deliveries WHERE generation_id = ? AND status = 'sent'",
            generationId,
          );
          if (!delivered) await telegram.sendMessage(chatId, BUDGET_LINE);
        }
        continue;
      }
      if (event.type === "stale") {
        errorClass = "stale";
        continue;
      }
      if (event.type === "reaction" && last?.messageId) {
        await telegram.react(chatId, last.messageId, event.emoji);
        continue;
      }
      if (event.type === "frame") {
        if (this.one("SELECT 1 AS ok FROM deliveries WHERE generation_id = ? AND sequence = ?", generationId, event.sequence)) {
          continue;
        }
        this.ctx.storage.sql.exec(
          "INSERT INTO deliveries(generation_id, sequence, text, reply_to, status) VALUES (?, ?, ?, ?, 'reserved')",
          generationId,
          event.sequence,
          event.text,
          firstFrame && last?.messageId ? last.messageId : null,
        );
        if (!this.generationActive(generationId)) {
          this.ctx.storage.sql.exec(
            "UPDATE deliveries SET status = 'suppressed' WHERE generation_id = ? AND sequence = ?",
            generationId,
            event.sequence,
          );
          continue;
        }
        if (!firstFrame) await new Promise((resolve) => setTimeout(resolve, PACE_MS));
        const send = await telegram.sendMessage(chatId, event.text, firstFrame ? last?.messageId : null);
        if ("messageId" in send) {
          this.ctx.storage.sql.exec(
            "UPDATE deliveries SET status = 'sent', telegram_message_id = ? WHERE generation_id = ? AND sequence = ?",
            send.messageId,
            generationId,
            event.sequence,
          );
        } else {
          this.ctx.storage.sql.exec(
            "UPDATE deliveries SET status = 'unknown' WHERE generation_id = ? AND sequence = ?",
            generationId,
            event.sequence,
          );
        }
        firstFrame = false;
      }
    }
    return { firstFrame, errorClass };
  }

  private async keepTyping(chatId: string, signal: AbortSignal, telegram: TelegramApi): Promise<void> {
    while (!signal.aborted) {
      await telegram.sendTyping(chatId);
      await new Promise<void>((resolve) => {
        if (signal.aborted) {
          resolve();
          return;
        }
        const timer = setTimeout(resolve, 4000);
        signal.addEventListener(
          "abort",
          () => {
            clearTimeout(timer);
            resolve();
          },
          { once: true },
        );
      });
    }
  }

  private async guestEnv(
    generationId: string,
    chatId: string,
    actorId: string,
    extra: Record<string, string>,
    options: { interactive: boolean; images: string[] },
  ): Promise<Record<string, string>> {
    const now = Math.floor(Date.now() / 1000);
    const origin = this.origin();
    const env: Record<string, string> = {
      TOMO_CORE_DATA_DIR: DATA_DIR,
      TOMO_INSTANCE_ID: this.tomoId(),
      TOMO_CORE_SOUL: "/opt/tomo/SOUL.md",
      OPENROUTER_API_KEY: this.env.OPENROUTER_API_KEY,
      TOMO_AGENT_MODEL: this.env.TOMO_AGENT_MODEL,
      TOMO_VISION_MODEL: this.env.TOMO_VISION_MODEL,
      TOMO_VISION_DIAGNOSTICS: "1",
      ...extra,
    };
    if (options.interactive) {
      if (origin.startsWith("https://")) {
        const cronKey = hexKey(this.env.CRON_CAPABILITY_KEY);
        env.TOMO_CRON_CONTROL_URL = origin;
        env.TOMO_CRON_CAPABILITY = await issueCron(cronKey, {
          owner_id: this.tomoId(),
          actor_id: actorId,
          destination: `telegram:${chatId}`,
          session_id: `telegram:actor:${actorId}`,
          issued_at: now,
          expires_at: now + 300,
          operations: CRON_OPS,
        });
        env.TOMO_CRON_OWNER_ID = this.tomoId();
        env.TOMO_CRON_ACTOR_ID = actorId;
        env.TOMO_CRON_DESTINATION = `telegram:${chatId}`;
        env.TOMO_CRON_SESSION_ID = `telegram:actor:${actorId}`;
      }
      if (options.images.length > 0) {
        const hashes = [];
        for (const fileId of options.images.slice(0, 8)) hashes.push(await sha256Hex(fileId));
        env.TOMO_ATTACHMENT_CONTROL_URL = "http://tomo.control";
        env.TOMO_ATTACHMENT_CAPABILITY = await issueAttachment(hexKey(this.env.ATTACHMENT_CAPABILITY_KEY), {
          owner_id: this.tomoId(),
          generation_id: generationId,
          file_hashes: hashes,
          issued_at: now,
          expires_at: now + 300,
        });
        env.TOMO_ATTACHMENT_OWNER_ID = this.tomoId();
        env.TOMO_ATTACHMENT_GENERATION_ID = generationId;
      }
    }
    return env;
  }

  private async prepareGuest(sandbox: SandboxHandle): Promise<void> {
    await this.withContainerRetry(() => sandbox.mkdir(DATA_DIR, { recursive: true }));
    await this.hydrate(sandbox);
  }

  private async execGuestStream(
    sandbox: SandboxHandle,
    env: Record<string, string>,
    timeout: number,
  ): Promise<ReadableStream> {
    return this.withContainerRetry(() =>
      sandbox.execStream(COMMAND, { env: { PYTHONUNBUFFERED: "1", ...env }, timeout }),
    );
  }

  private async withContainerRetry<T>(operation: () => Promise<T>): Promise<T> {
    let last: unknown;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        return await operation();
      } catch (error) {
        last = error;
        if (!retryableSandbox(error) || attempt === 4) throw error;
        await new Promise((resolve) => setTimeout(resolve, 4000 * (attempt + 1)));
      }
    }
    throw last;
  }

  private async hydrate(sandbox: SandboxHandle): Promise<void> {
    const object = await this.env.CHECKPOINTS.get(`owners/${this.tomoId()}/tomo.sqlite3`);
    if (!object) return;
    const bytes = new Uint8Array(await object.arrayBuffer());
    await writeSandboxFile(sandbox, `${DATA_DIR}/tomo.sqlite3`, bytes);
  }

  private async checkpoint(sandbox: ReturnType<typeof getSandbox>): Promise<void> {
    await sandbox.exec(
      "python3 -c \"import sqlite3,pathlib; p=pathlib.Path('/workspace/tomo-data/tomo.sqlite3'); c=sqlite3.connect(str(p)) if p.exists() else None; c and (c.execute('PRAGMA wal_checkpoint(FULL)'), c.close())\"",
      { timeout: 30_000 },
    );
    const bytes = await readSandboxFile(sandbox, `${DATA_DIR}/tomo.sqlite3`);
    if (!bytes) return;
    await this.env.CHECKPOINTS.put(`owners/${this.tomoId()}/tomo.sqlite3`, bytes);
  }

  private async cron(request: Request, url: URL): Promise<Response> {
    const operation = cronOp(url.pathname, request.method);
    if (!operation) return Response.json({ ok: false, error: "cron_not_found" }, { status: 404 });
    const ownerId = request.headers.get("x-tomo-owner-id") || "";
    if (ownerId !== this.tomoId()) return Response.json({ ok: false, error: "cron_unauthorized" }, { status: 401 });
    if (operation === "list") {
      const jobs = this.sql("SELECT * FROM cron_jobs").map(jobJson);
      return Response.json({ ok: true, jobs });
    }
    if (operation === "create") {
      const idempotency = request.headers.get("idempotency-key") || "";
      if (!idempotency) return Response.json({ ok: false, error: "missing_idempotency" }, { status: 400 });
      const existing = this.one("SELECT * FROM cron_jobs WHERE idempotency_key = ?", idempotency);
      if (existing) return Response.json({ ok: true, job: jobJson(existing) });
      const body = (await request.json()) as {
        intent?: string;
        constraints?: string[];
        schedule?: { kind?: string; at?: string; afterSeconds?: number; everySeconds?: number };
      };
      const jobId = (await sha256Hex(`${ownerId}\ncreate\n${idempotency}`)).slice(0, 32);
      const schedule = body.schedule || {};
      let scheduleAt: number | null = null;
      let every: number | null = null;
      let kind = schedule.kind || "once";
      if (kind === "delay") {
        kind = "once";
        scheduleAt = Date.now() + Number(schedule.afterSeconds || 0) * 1000;
      } else if (kind === "once") {
        scheduleAt = schedule.at ? Date.parse(schedule.at) : Date.now();
      } else if (kind === "interval") {
        every = Number(schedule.everySeconds || 0);
        scheduleAt = Date.now() + every * 1000;
      } else {
        scheduleAt = Date.now() + 60_000;
      }
      const destination = request.headers.get("x-tomo-destination") || "";
      this.ctx.storage.sql.exec(
        "INSERT INTO cron_jobs(job_id, destination, intent, constraints, schedule_kind, schedule_at, schedule_every, status, revision, successful_runs, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 1, 0, ?)",
        jobId,
        destination,
        body.intent || "",
        JSON.stringify(body.constraints || []),
        kind,
        scheduleAt,
        every,
        idempotency,
      );
      await this.scheduleWake();
      return Response.json({ ok: true, job: jobJson(this.one("SELECT * FROM cron_jobs WHERE job_id = ?", jobId)!) });
    }
    const match = url.pathname.match(/\/v1\/cron\/jobs\/([^/]+)(?:\/([^/]+))?$/);
    const jobId = match?.[1] ? decodeURIComponent(match[1]) : "";
    const job = this.one("SELECT * FROM cron_jobs WHERE job_id = ?", jobId);
    if (!job) return Response.json({ ok: false, error: "cron_not_found" }, { status: 404 });
    if (operation === "inspect") return Response.json({ ok: true, job: jobJson(job) });
    if (operation === "history") return Response.json({ ok: true, history: [] });
    if (operation === "pause") this.ctx.storage.sql.exec("UPDATE cron_jobs SET status='paused', revision=revision+1 WHERE job_id=?", jobId);
    if (operation === "resume") this.ctx.storage.sql.exec("UPDATE cron_jobs SET status='active', revision=revision+1 WHERE job_id=?", jobId);
    if (operation === "delete") this.ctx.storage.sql.exec("UPDATE cron_jobs SET status='cancelled', revision=revision+1, schedule_at=NULL WHERE job_id=?", jobId);
    if (operation === "run_now") this.ctx.storage.sql.exec("UPDATE cron_jobs SET schedule_at=? WHERE job_id=?", Date.now(), jobId);
    await this.scheduleWake();
    return Response.json({ ok: true, job: jobJson(this.one("SELECT * FROM cron_jobs WHERE job_id = ?", jobId)!) });
  }
}

function cronOp(pathname: string, method: string): string | null {
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

function jobJson(job: SqlRow): Record<string, unknown> {
  return {
    jobId: job.job_id,
    intent: job.intent,
    constraints: JSON.parse(String(job.constraints || "[]")),
    schedule: {
      kind: job.schedule_kind,
      at: job.schedule_at ? new Date(Number(job.schedule_at)).toISOString() : null,
      everySeconds: job.schedule_every,
      expression: null,
      timezoneName: "UTC",
      startsAt: null,
    },
    lifecycle: { endsAt: null, maxSuccessfulRuns: null },
    status: job.status,
    revision: job.revision,
  };
}

type SandboxHandle = ReturnType<typeof getSandbox>;

function sandboxLabel(error: unknown): string {
  const name = error instanceof Error ? error.name : "Error";
  const detail = error instanceof Error ? error.message : "";
  const code = error && typeof error === "object" && "code" in error ? String((error as { code?: unknown }).code || "") : "";
  const raw = [name, code, detail].filter(Boolean).join("_");
  return raw.replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 60) || "Error";
}

function retryableSandbox(error: unknown): boolean {
  const name = error instanceof Error ? error.name : "";
  const code = error && typeof error === "object" && "code" in error ? String((error as { code?: unknown }).code || "") : "";
  const detail = error instanceof Error ? error.message : "";
  const blob = `${name} ${code} ${detail}`.toLowerCase();
  return (
    name === "ContainerUnavailableError" ||
    name === "RPCTransportError" ||
    name === "SandboxError" ||
    code === "CONTAINER_UNAVAILABLE" ||
    code === "RPC_TRANSPORT_ERROR" ||
    blob.includes("container_starting") ||
    blob.includes("container unavailable") ||
    blob.includes("http_error_status_500") ||
    blob.includes("status 500")
  );
}

function bytesToB64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function b64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

async function writeSandboxFile(sandbox: SandboxHandle, path: string, bytes: Uint8Array): Promise<void> {
  await sandbox.mkdir(DATA_DIR, { recursive: true });
  await sandbox.writeFile(path, bytesToB64(bytes), { encoding: "base64" });
}

async function readSandboxFile(sandbox: SandboxHandle, path: string): Promise<Uint8Array | null> {
  try {
    const exists = await sandbox.exists(path);
    if (!exists.exists) return null;
    const result = await sandbox.readFile(path, { encoding: "base64" });
    if (!result.success || !result.content) return null;
    return b64ToBytes(result.content);
  } catch {
    return null;
  }
}
