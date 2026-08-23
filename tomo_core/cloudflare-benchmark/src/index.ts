import { getSandbox, type Sandbox } from "@cloudflare/sandbox";

export { Sandbox } from "@cloudflare/sandbox";

interface Env {
  Sandbox: DurableObjectNamespace<Sandbox>;
  BENCHMARK_TOKEN: string;
}

type Scenario = "text" | "image" | "image-max" | "wait";

interface RunRequest {
  scenario?: Scenario;
  owners?: number;
  delaySeconds?: number;
  sleepCycle?: boolean;
}

interface OutputChunk {
  stream: "stdout" | "stderr";
  atMs: number;
  bytes: number;
}

const RESULT_MARKER = "TOMO_BENCH_RESULT=";
const ALLOWED_SCENARIOS = new Set<Scenario>(["text", "image", "image-max", "wait"]);

function json(payload: unknown, status = 200): Response {
  return Response.json(payload, {
    status,
    headers: { "cache-control": "no-store" },
  });
}

async function secureEqual(left: string, right: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [leftHash, rightHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(left)),
    crypto.subtle.digest("SHA-256", encoder.encode(right)),
  ]);
  const leftBytes = new Uint8Array(leftHash);
  const rightBytes = new Uint8Array(rightHash);
  let difference = 0;
  for (let index = 0; index < leftBytes.length; index += 1) {
    difference |= leftBytes[index] ^ rightBytes[index];
  }
  return difference === 0;
}

async function authorized(request: Request, env: Env): Promise<boolean> {
  const header = request.headers.get("authorization") ?? "";
  const prefix = "Bearer ";
  if (!header.startsWith(prefix) || !env.BENCHMARK_TOKEN) return false;
  return secureEqual(header.slice(prefix.length), env.BENCHMARK_TOKEN);
}

function parseRequest(value: unknown): Required<RunRequest> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("request body must be a JSON object");
  }
  const body = value as Record<string, unknown>;
  const scenario = body.scenario ?? "text";
  const owners = body.owners ?? 3;
  const delaySeconds = body.delaySeconds ?? (scenario === "wait" ? 120 : 0.05);
  const sleepCycle = body.sleepCycle ?? false;
  if (typeof scenario !== "string" || !ALLOWED_SCENARIOS.has(scenario as Scenario)) {
    throw new Error("scenario must be text, image, image-max, or wait");
  }
  if (!Number.isInteger(owners) || typeof owners !== "number" || owners < 1 || owners > 3) {
    throw new Error("owners must be an integer from 1 to 3");
  }
  if (typeof delaySeconds !== "number" || !Number.isFinite(delaySeconds) || delaySeconds < 0 || delaySeconds > 720) {
    throw new Error("delaySeconds must be between 0 and 720");
  }
  if (typeof sleepCycle !== "boolean") {
    throw new Error("sleepCycle must be a boolean");
  }
  return { scenario: scenario as Scenario, owners, delaySeconds, sleepCycle };
}

function parseBenchmarkResult(stdout: string): unknown {
  const line = stdout
    .split("\n")
    .reverse()
    .find((candidate) => candidate.startsWith(RESULT_MARKER));
  if (!line) throw new Error("benchmark process did not emit a result");
  return JSON.parse(line.slice(RESULT_MARKER.length));
}

async function runOwner(
  env: Env,
  runId: string,
  ownerIndex: number,
  request: Required<RunRequest>,
): Promise<unknown> {
  const ownerId = `owner-${ownerIndex + 1}`;
  const sandboxId = `${runId}-${ownerId}`;
  const sandbox = getSandbox(env.Sandbox, sandboxId, {
    enableDefaultSession: false,
    normalizeId: true,
    sleepAfter: "30s",
    containerTimeouts: {
      instanceGetTimeoutMS: 120_000,
      portReadyTimeoutMS: 180_000,
    },
  });
  async function executeOnce(cycle: "cold" | "after-sleep"): Promise<unknown> {
    const outputChunks: OutputChunk[] = [];
    const started = performance.now();
    const result = await sandbox.exec(
      "/opt/tomo/.venv/bin/python /opt/tomo/benchmark.py",
      {
        env: {
          TOMO_BENCH_OWNER_ID: ownerId,
          TOMO_BENCH_SCENARIO: request.scenario,
          TOMO_BENCH_DELAY_SECONDS: String(request.delaySeconds),
        },
        stream: true,
        onOutput(stream, data) {
          outputChunks.push({
            stream,
            atMs: Math.round(performance.now() - started),
            bytes: new TextEncoder().encode(data).byteLength,
          });
        },
        timeout: Math.max(180_000, (request.delaySeconds + 90) * 1_000),
      },
    );
    return {
      cycle,
      elapsedMs: Math.round(performance.now() - started),
      firstOutputMs: outputChunks[0]?.atMs ?? null,
      outputChunks,
      command: {
        success: result.success,
        exitCode: result.exitCode,
        stderr: result.stderr.slice(0, 2_000),
      },
      benchmark: parseBenchmarkResult(result.stdout),
    };
  }

  const ownerStarted = performance.now();
  try {
    const runs = [await executeOnce("cold")];
    if (request.sleepCycle) {
      await new Promise((resolve) => setTimeout(resolve, 35_000));
      runs.push(await executeOnce("after-sleep"));
    }
    return {
      ownerId,
      sandboxId,
      elapsedMs: Math.round(performance.now() - ownerStarted),
      runs,
    };
  } finally {
    await sandbox.destroy();
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true });
    if (url.pathname !== "/run" || request.method !== "POST") {
      return json({ error: "use POST /run" }, 404);
    }
    if (!(await authorized(request, env))) return json({ error: "unauthorized" }, 401);

    let benchmarkRequest: Required<RunRequest>;
    try {
      benchmarkRequest = parseRequest(await request.json());
    } catch (error) {
      const message = error instanceof Error ? error.message : "invalid request";
      return json({ error: message }, 400);
    }

    const runId = `bench-${crypto.randomUUID()}`;
    const started = performance.now();
    try {
      const owners = await Promise.all(
        Array.from({ length: benchmarkRequest.owners }, (_, index) =>
          runOwner(env, runId, index, benchmarkRequest),
        ),
      );
      return json({
        runId,
        scenario: benchmarkRequest.scenario,
        sleepCycle: benchmarkRequest.sleepCycle,
        ownerCount: benchmarkRequest.owners,
        concurrentElapsedMs: Math.round(performance.now() - started),
        owners,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "benchmark failed";
      console.error(JSON.stringify({ event: "benchmark_failed", runId, message }));
      return json({ error: message, runId }, 500);
    }
  },
};
