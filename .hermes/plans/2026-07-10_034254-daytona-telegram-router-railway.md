# Daytona Telegram Router + Railway Hosting Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make `/start <token>` provision exactly one persistent Daytona sandbox for that user, route every later shared-bot DM to that sandbox, run the Tomo agent there, and keep Railway/Daytona setup deterministic and recoverable.

**Architecture:** Keep the dashboard and the sole Telegram poller on Railway. Railway persists onboarding/routing state under `TOMO_CORE_DATA_DIR`, uses the Daytona SDK to reconcile a deterministic sandbox and volume per `tomo_id`, and invokes a versioned `sandbox-inbound` command for each DM. The sandbox returns validated bubble JSON; only the Railway listener owns the global bot token and sends Telegram messages. A Railway-side SuperGrok token broker bootstraps from a locally exported OAuth JSON secret, refreshes centrally, and passes only the current access token to each sandbox execution.

**Tech Stack:** Python 3.11, `unittest`, FastAPI, SQLite, Daytona Python SDK `0.195.0`, Telegram Bot API long polling, Railway volumes/process supervision, Next.js dashboard (existing onboarding BFF; no UI changes planned).

---

## 1. Locked decisions and assumptions

1. One global Telegram bot token has exactly one update consumer: Railway `telegram-shared`.
2. One Better Auth user maps to one stable `tomo_id`, one Daytona sandbox, and one Daytona volume.
3. A Telegram room is not an instance. This v1 remains private-DM only.
4. Daytona sandboxes never receive `TOMO_TELEGRAM_GLOBAL_BOT_TOKEN`; Railway is the only Telegram sender.
5. `/start <token>` consumes a hashed, single-use token, binds the chat, and immediately attempts `ensure_sandbox()` before sending the connected welcome.
6. Every later DM also calls `ensure_sandbox()` so a stopped, stale, or partially provisioned sandbox self-heals.
7. One global `tomo_core/SOUL.md` remains in the snapshot. Do not create per-user souls or edit the soul for deployment behavior.
8. The locally generated OAuth credential is an operator-level SuperGrok/Grok credential shared by hosted Tomo sandboxes. Per-end-user OAuth is a different product and is out of scope.
9. The refresh token stays on Railway. Daytona receives only a short-lived access token for an inbound turn.
10. The sandbox stays active (`auto_stop_interval=0`) in v1. Cost-based auto-stop can be added later without changing routing because every delivery reconciles/starts the sandbox.

## 2. Current repo state versus target

| Area | Current | Target |
|---|---|---|
| Dashboard onboarding | Authenticated BFF creates `/start` link | Keep unchanged |
| Install token | Hashed, 10-minute, single-use | Keep; provision after bind |
| Runtime selection | `RuntimeInstanceRegistry` creates in-process runtimes on Railway | Explicit local dispatch or Daytona dispatch; never silent hosted fallback |
| Telegram routing | Bound `chat_id` directly invokes in-process runtime | Bound `chat_id` resolves `tomo_id`, queues update, executes in matching Daytona sandbox |
| Sandbox support | None in `tomo_v2` | Daytona SDK adapter, registry, persistent volume, snapshot smoke, inbound command |
| OAuth | Local/actor token files or xAI API key | Railway token broker bootstrapped by base64 OAuth JSON; current access token injected per exec |
| Railway listener | Detached child can die while API stays healthy | One supervisor process monitors control API and shared poller; either child exit restarts service |
| Polling durability | Model call blocks polling; offset only in memory | SQLite inbox accepts updates quickly; worker retries and preserves per-chat ordering |
| Deployment artifact | Railpack package only | Railpack Railway core plus reproducible Daytona snapshot Dockerfile/builder |

## 3. Runtime flow

```text
dashboard (Railway)
  -> authenticated GET /api/onboarding/telegram
  -> POST core /v1/onboarding/telegram/install-link
  -> tg://...start=<single-use-token>

telegram-shared (Railway, sole poller)
  -> persist Telegram update in SQLite inbox
  -> worker consumes /start token and binds chat -> tomo_id
  -> DaytonaDispatch.ensure_sandbox(tomo_id)
       -> deterministic volume: tomo-data-<tomo hash>
       -> deterministic sandbox: tomo-<tomo hash>
       -> custom snapshot smoke: tomo-core sandbox-inbound --health
  -> connected welcome

later DM
  -> route chat_id -> installation.tomo_id
  -> ensure/reconcile sandbox
  -> Railway token broker returns current SuperGrok access token
  -> process.exec('/opt/tomo/.venv/bin/tomo-core sandbox-inbound', env={
       TOMO_INBOUND_JSON,
       TOMO_SUPERGROK_ACCESS_TOKEN,
       TOMO_CORE_DATA_DIR,
       TOMO_INSTANCE_ID
     })
  -> parse TOMO_SANDBOX_RESULT=<json>
  -> validate request_id and 1..4 bubbles
  -> Railway sends bubbles to the bound chat_id only
```

## 4. Environment contract

### Railway dashboard only

- `BETTER_AUTH_SECRET`
- `BETTER_AUTH_API_KEY` when Better Auth Infrastructure is enabled
- `BETTER_AUTH_URL`
- `TOMO_DASHBOARD_DATA_DIR=/data`
- `TOMO_CONTROL_API_URL` without `/v1`
- `TOMO_CONTROL_API_KEY`

### Railway core only

- `TOMO_CORE_DATA_DIR=/data`
- `TOMO_CONTROL_API_KEY`
- `TOMO_TELEGRAM_GLOBAL_BOT_TOKEN`
- `TOMO_TELEGRAM_GLOBAL_BOT_USERNAME`
- `DAYTONA_API_KEY`
- `TOMO_DAYTONA_SNAPSHOT=tomo-core-<git-sha>`
- `TOMO_DAYTONA_SANDBOX_DATA_DIR=/home/daytona/.tomo_core`
- `TOMO_SUPERGROK_OAUTH_JSON_B64=<base64 of local ~/.grok/auth.json>`
- Optional: `DAYTONA_API_URL`, `DAYTONA_TARGET`, `TOMO_ROUTER_WORKERS=4`, `TOMO_SANDBOX_EXEC_TIMEOUT_SECONDS=120`

### Injected into a Daytona exec

- `TOMO_CORE_DATA_DIR=/home/daytona/.tomo_core`
- `TOMO_INSTANCE_ID=<tomo_id>`
- `TOMO_INBOUND_JSON=<versioned request>`
- `TOMO_SUPERGROK_ACCESS_TOKEN=<current access token>`
- `TOMO_CORE_SOUL=/opt/tomo/SOUL.md`

Never inject the Telegram bot token, Daytona API key, Railway control key, or OAuth refresh token into a sandbox.

---

### Task 1: Pin the Daytona SDK and isolate it behind an adapter

**Objective:** Add the inspected SDK version without letting Daytona classes leak through routing/business code.

**Files:**
- Modify: `tomo_core/pyproject.toml`
- Modify: `tomo_core/uv.lock`
- Create: `tomo_core/src/tomo_core/daytona_client.py`
- Create: `tomo_core/tests/test_daytona_client.py`

**Step 1: Write the failing adapter test**

Define a narrow protocol and fake SDK objects. Assert that `DaytonaClient.create_sandbox()` constructs the pinned SDK request with a deterministic name, snapshot, labels, `auto_stop_interval=0`, and a volume mounted at the configured data directory. Assert `exec()` forwards a static command plus an env mapping, never interpolating user text into the command.

Required public contract:

```python
@dataclass(frozen=True)
class SandboxHandle:
    id: str
    name: str

@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str

class DaytonaClientProtocol(Protocol):
    def get_sandbox(self, sandbox_id_or_name: str) -> SandboxHandle | None: ...
    def ensure_volume(self, name: str) -> str: ...
    def create_sandbox(self, *, name: str, snapshot: str, tomo_id: str,
                       volume_id: str, mount_path: str) -> SandboxHandle: ...
    def start_sandbox(self, handle: SandboxHandle) -> SandboxHandle: ...
    def delete_sandbox(self, handle: SandboxHandle) -> None: ...
    def exec(self, handle: SandboxHandle, *, command: str,
             env: dict[str, str], timeout: int) -> ExecResult: ...
```

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_daytona_client -v`

Expected: import failure because `tomo_core.daytona_client` does not exist.

**Step 3: Implement the minimal adapter**

- Add `"daytona==0.195.0"` to dependencies and run `uv lock` during implementation.
- Use `Daytona()`, `CreateSandboxFromSnapshotParams`, and `VolumeMount` only in `daytona_client.py`.
- Map SDK not-found errors to `None`; re-raise authentication, quota, and service errors as typed `DaytonaClientError(code, safe_message)` without secrets.
- Use `Daytona.volume.get(name, create=True)` and `sandbox.process.exec(command, env=env, timeout=timeout)`.
- Do not log env mappings or raw SDK request bodies.

**Step 4: Verify GREEN**

Run the focused test, then: `cd tomo_core && uv run python -m unittest discover -s tests -v`

**Step 5: Optional commit**

```bash
# only if the user requested commits
git add tomo_core/pyproject.toml tomo_core/uv.lock tomo_core/src/tomo_core/daytona_client.py tomo_core/tests/test_daytona_client.py
git commit -m "feat(core): add pinned daytona sdk adapter"
```

### Task 2: Define a versioned sandbox request/result protocol

**Objective:** Make Railway-to-sandbox communication strict, validated, and independent of Telegram send credentials.

**Files:**
- Create: `tomo_core/src/tomo_core/sandbox_protocol.py`
- Create: `tomo_core/tests/test_sandbox_protocol.py`

**Step 1: Write failing tests**

Cover:
- valid request round-trip;
- unsupported `version` rejected;
- missing/mismatched `request_id` rejected;
- result allows 1–4 non-empty bubbles only;
- bubble text over Telegram's 4096-character limit rejected;
- result cannot specify a destination chat/user;
- parser ignores ordinary stdout and reads only the final `TOMO_SANDBOX_RESULT=` line.

Required schemas:

```python
class SandboxInbound(BaseModel):
    version: Literal[1] = 1
    request_id: str
    tomo_id: str
    envelope: InboundEnvelopePayload

class SandboxBubble(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    reply_to_message_id: str | None = None

class SandboxResult(BaseModel):
    version: Literal[1] = 1
    request_id: str
    ok: bool
    bubbles: list[SandboxBubble] = Field(default_factory=list, max_length=4)
    error: SandboxErrorPayload | None = None
```

Use one machine-readable stdout marker:

```text
TOMO_SANDBOX_RESULT={"version":1,"request_id":"tg:update:42","ok":true,"bubbles":[...]}
```

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_sandbox_protocol -v`

**Step 3: Implement serialization/parsing helpers**

Implement `encode_inbound()`, `emit_result_line()`, and `parse_result_stdout(stdout, expected_request_id)`. Keep Telegram `chat_id` out of `SandboxResult`; Railway supplies the destination from its trusted installation record.

**Step 4: Verify GREEN and full regression**

Run focused and full `unittest` suites.

### Task 3: Add a headless sandbox agent entrypoint

**Objective:** Execute one Tomo turn inside Daytona and emit only the versioned result contract.

**Files:**
- Create: `tomo_core/src/tomo_core/sandbox_inbound.py`
- Create: `tomo_core/tests/test_sandbox_inbound.py`
- Modify: `tomo_core/src/tomo_core/cli.py`
- Modify: `tomo_core/src/tomo_core/providers.py`
- Modify: `tomo_core/tests/test_cli.py`
- Modify: `tomo_core/tests/test_xai_api_supergrok_oauth.py`

**Step 1: Write failing tests**

Cover:
- `tomo-core sandbox-inbound --health` returns exit 0 and an `ok` result without contacting a model;
- a valid `TOMO_INBOUND_JSON` constructs `PersonalAgentRuntime` under exactly `TOMO_CORE_DATA_DIR`;
- `TOMO_SUPERGROK_ACCESS_TOKEN` selects `SuperGrokOAuthProvider` before actor-file OAuth, but does not masquerade as `XAI_API_KEY`;
- the collecting Telegram client returns bubbles without making Telegram API calls;
- a provider HTTP 401 emits `error.code="auth_expired"`;
- malformed input emits a safe `invalid_request` result and nonzero exit;
- no result or exception string contains token values.

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_sandbox_inbound -v`

**Step 3: Implement the command**

- Add `sandbox-inbound` as a top-level CLI command.
- Parse `TOMO_INBOUND_JSON`; do not accept inbound JSON as a shell argument.
- Add a `CollectingTelegramClient` implementing `TelegramClient`; typing is a no-op and messages are collected.
- Build `RuntimeConfig(data_dir=$TOMO_CORE_DATA_DIR, soul_path=$TOMO_CORE_SOUL)`.
- Convert the protocol envelope into the existing `InboundEnvelope` and call `handle_telegram_text()`.
- Catch `httpx.HTTPStatusError` 401 separately as `auth_expired`; map all other failures to safe error codes.
- Always emit one marker line, including expected failures.

**Step 4: Verify GREEN**

Run focused tests, `tests.test_cli`, provider tests, then the full suite.

### Task 4: Bootstrap and refresh the hosted SuperGrok OAuth credential on Railway

**Objective:** Turn a locally generated OAuth JSON blob into a safe Railway-side token broker and inject only current access tokens into Daytona.

**Files:**
- Create: `tomo_core/src/tomo_core/hosted_auth.py`
- Create: `tomo_core/tests/test_hosted_auth.py`
- Create: `tomo_core/scripts/export_grok_auth.py`
- Create: `tomo_core/tests/test_export_grok_auth.py`
- Modify: `tomo_core/src/tomo_core/grok_auth.py`

**Step 1: Write failing tests**

Use synthetic credentials only. Cover:
- strict base64 and JSON validation;
- recursive normalization of `access_token`/`accessToken`, `refresh_token`/`refreshToken`, and common expiry fields;
- bootstrap writes `/data/hosted-auth/supergrok.json` atomically with mode `0600`;
- an unchanged env fingerprint does not overwrite a refreshed on-disk token;
- a changed env fingerprint intentionally reseeds the token;
- token within 120 seconds of expiry refreshes via `https://auth.x.ai/oauth2/token` using the public client id already used by `OAuthManager`;
- missing refresh token produces `HostedAuthError("refresh_unavailable")`;
- a refresh response that omits `refresh_token` preserves the old one;
- exceptions and logs never contain access/refresh tokens;
- export script rejects missing/invalid local files and prints one base64 line for a valid file.

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_hosted_auth tests.test_export_grok_auth -v`

**Step 3: Implement the broker**

Required API:

```python
class HostedSuperGrokTokenBroker:
    def __init__(self, data_dir: Path, bootstrap_b64: str,
                 token_url: str = "https://auth.x.ai/oauth2/token") -> None: ...
    def access_token(self, *, force_refresh: bool = False) -> str: ...
```

Use a process lock plus atomic temp-file replace. Store a SHA-256 fingerprint of the bootstrap blob, never the blob itself in metadata/logs. The export script must read `~/.grok/auth.json` by default and never modify it.

**Step 4: Verify GREEN**

Run focused tests and full suite.

### Task 5: Persist and reconcile sandbox/volume ownership

**Objective:** Make sandbox identity recoverable after Railway restarts and snapshot upgrades.

**Files:**
- Create: `tomo_core/src/tomo_core/sandbox_registry.py`
- Create: `tomo_core/tests/test_sandbox_registry.py`

**Step 1: Write failing tests**

Cover create/update/read for:

```python
@dataclass(frozen=True)
class SandboxRecord:
    tomo_id: str
    sandbox_id: str
    sandbox_name: str
    volume_name: str
    snapshot: str
    status: Literal["provisioning", "ready", "error"]
    last_error_code: str | None
    updated_at: int
```

Also test migration from a missing table, no cross-`tomo_id` reads, and redaction of error details.

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_sandbox_registry -v`

**Step 3: Implement SQLite registry**

Store `daytona_sandboxes` in `${TOMO_CORE_DATA_DIR}/onboarding.sqlite` or a dedicated `${TOMO_CORE_DATA_DIR}/daytona.sqlite`; choose one and keep it canonical. Prefer the existing onboarding DB connection helpers only if they can be shared without circular imports. Names must be deterministic and bounded:

```python
def sandbox_name(tomo_id: str) -> str:
    return f"tomo-{sha256(tomo_id.encode()).hexdigest()[:20]}"

def volume_name(tomo_id: str) -> str:
    return f"tomo-data-{sha256(tomo_id.encode()).hexdigest()[:20]}"
```

Never use raw email, Better Auth user id, or Telegram chat id in Daytona names/labels.

**Step 4: Verify GREEN**

Run focused and full suites.

### Task 6: Implement idempotent Daytona provisioning

**Objective:** Ensure one healthy sandbox plus one persistent volume per `tomo_id`, including stale-record and failed-smoke recovery.

**Files:**
- Create: `tomo_core/src/tomo_core/daytona_supervisor.py`
- Create: `tomo_core/tests/test_daytona_supervisor.py`

**Step 1: Write failing tests**

Cover these vertical slices:

1. No record and no remote sandbox: create volume, create sandbox, smoke, mark ready.
2. Same `tomo_id` twice: return the same sandbox; do not create twice.
3. Different `tomo_id`s: distinct names, volumes, and records.
4. Recorded stopped sandbox: start, smoke, retain same volume.
5. Stale recorded id but deterministic remote name exists: reconcile and update record.
6. Create conflict: fetch deterministic name and continue instead of creating an orphan.
7. Smoke failure after create: mark error, best-effort delete the failed sandbox, retain the volume, and raise a safe typed error.
8. Snapshot changed: deliberately replace/recreate the sandbox while reusing the volume.
9. Concurrent calls for one `tomo_id`: one create only via a per-id lock.

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_daytona_supervisor -v`

**Step 3: Implement minimal supervisor**

Required constructor and method:

```python
class DaytonaSandboxSupervisor:
    def __init__(self, client: DaytonaClientProtocol, registry: SandboxRegistry,
                 snapshot: str, sandbox_data_dir: str,
                 exec_timeout_seconds: int = 120) -> None: ...
    def ensure_sandbox(self, tomo_id: str) -> SandboxHandle: ...
```

Smoke command must be static:

```text
/opt/tomo/.venv/bin/tomo-core sandbox-inbound --health
```

Validate the smoke with `parse_result_stdout`; do not treat exit code 0 alone as readiness.

**Step 4: Verify GREEN**

Run focused and full suites.

### Task 7: Deliver a Telegram turn through the correct Daytona sandbox

**Objective:** Route one trusted installation/envelope to one sandbox, retry once on expired auth, and return validated bubbles.

**Files:**
- Create: `tomo_core/src/tomo_core/sandbox_dispatch.py`
- Create: `tomo_core/tests/test_sandbox_dispatch.py`

**Step 1: Write failing tests**

Cover:
- `ensure_worker(installation)` delegates using only `installation.tomo_id`;
- `deliver_telegram()` ensures the sandbox before every exec;
- request id is `telegram:update:<update_id>` and expected result id must match;
- current access token comes from the Railway broker and is present only in exec env;
- command is static and user text exists only inside JSON env;
- valid bubbles are returned but not sent by the sandbox;
- `auth_expired` forces one broker refresh and exactly one retry;
- second auth failure stops retrying;
- timeout, malformed stdout, and nonzero exit become safe `SandboxDispatchError` codes;
- a malicious result cannot redirect output to another chat.

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_sandbox_dispatch -v`

**Step 3: Implement dispatch**

Required protocol for gateway injection:

```python
class TelegramRuntimeDispatch(Protocol):
    def ensure_worker(self, installation: TelegramInstallation) -> None: ...
    def deliver_telegram(self, installation: TelegramInstallation,
                         update_id: int, envelope: InboundEnvelope) -> list[SandboxBubble]: ...
```

Add an `InProcessTelegramDispatch` adapter around `RuntimeInstanceRegistry` for explicit local development only. Do not choose it when any Daytona env variable is present but incomplete.

**Step 4: Verify GREEN**

Run focused and full suites.

### Task 8: Provision on `/start` and route later DMs through dispatch

**Objective:** Replace the hosted in-process runtime path while preserving local smoke mode.

**Files:**
- Modify: `tomo_core/src/tomo_core/shared_gateway.py`
- Modify: `tomo_core/tests/test_shared_gateway.py`
- Modify: `tomo_core/src/tomo_core/models.py`

**Step 1: Rewrite tests before production code**

Required behavior:
- `/start <token>` binds the chat, sends an immediate short setup message, calls `ensure_worker()` for the returned installation, then sends connected only after success;
- provision failure keeps the valid binding and sends retry guidance without exposing provider details;
- a later DM from that bound chat calls `deliver_telegram()` with the stored `tomo_id`;
- bubbles are sent by Railway to `installation.chat_id` only;
- first bubble replies to the triggering Telegram message; later bubbles preserve their returned reply ids only when valid;
- unknown chat gets dashboard onboarding guidance;
- private `actor_id` uses Telegram `from.id`, while destination uses `chat.id`;
- two chats bound to two users can never select each other's `tomo_id`;
- groups remain ignored.

Extend `InboundEnvelope` with an optional `update_id` or pass update id separately; do not add `room_id` in this DM-only slice.

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_shared_gateway -v`

**Step 3: Implement dispatch-based gateway**

Change the dataclass field from `instances: RuntimeInstanceRegistry` to `dispatch: TelegramRuntimeDispatch`. Keep onboarding token consumption in the trusted host. Catch only typed dispatch errors and return safe user copy.

**Step 4: Verify GREEN**

Run shared gateway, onboarding store, instances, and full tests.

### Task 9: Add a durable Telegram inbox with per-chat ordered workers

**Objective:** Keep long polling responsive while slow model turns run, and recover accepted updates after a Railway restart.

**Files:**
- Modify: `tomo_core/src/tomo_core/onboarding_store.py`
- Modify: `tomo_core/tests/test_onboarding_store.py`
- Create: `tomo_core/src/tomo_core/shared_router.py`
- Create: `tomo_core/tests/test_shared_router.py`

**Step 1: Write failing persistence tests**

Add a `telegram_inbox` table with `update_id` primary key, `chat_id`, compact JSON payload, `status`, `attempts`, `available_at`, `last_error_code`, and timestamps. Test:
- duplicate update id enqueues once;
- claim order is ascending per chat;
- only one processing row per chat;
- different chats may be processed concurrently;
- success deletes or marks done;
- transient failure retries with bounded exponential delay;
- invalid/unknown updates are marked done rather than retried forever;
- interrupted `processing` rows are reset to pending on startup;
- payload/error fields never contain environment secrets.

**Step 2: Verify RED**

Run onboarding and router focused tests.

**Step 3: Implement the router**

- Poller thread only fetches and enqueues updates.
- A bounded worker pool (`TOMO_ROUTER_WORKERS`, default 4) claims work.
- SQLite claim must prevent concurrent work for the same `chat_id`, preserving session order.
- `SharedTelegramGateway.process_update()` remains the single update behavior implementation.
- Graceful shutdown stops fetching, finishes active jobs up to a timeout, then leaves remaining rows pending.
- A rare crash after Telegram `sendMessage` but before marking done may duplicate a reply; document this at-least-once boundary instead of pretending Bot API sends are idempotent.

**Step 4: Verify GREEN**

Run focused and full suites.

### Task 10: Make hosted dispatch selection explicit and fail closed

**Objective:** Ensure Railway cannot silently run a user's Tomo in the control container when Daytona is partially configured.

**Files:**
- Modify: `tomo_core/src/tomo_core/cli.py`
- Modify: `tomo_core/tests/test_cli.py`
- Create: `tomo_core/src/tomo_core/hosted_config.py`
- Create: `tomo_core/tests/test_hosted_config.py`

**Step 1: Write failing tests**

Cover:
- all required Daytona vars -> build `DaytonaSandboxDispatch`;
- no Daytona vars + explicit local/static mode -> build `InProcessTelegramDispatch`;
- any Daytona var present but API key/snapshot/OAuth bootstrap missing -> startup exit 2 with names of missing vars, no secret values;
- production Railway (`RAILWAY_ENVIRONMENT` set) refuses implicit in-process fallback;
- invalid timeout/worker count/data path fails before polling;
- CLI passes the durable router into the shared gateway loop;
- each update failure is isolated and does not kill polling.

**Step 2: Verify RED**

Run config and CLI focused tests.

**Step 3: Implement one config loader**

Use a typed `HostedRuntimeConfig.from_env()` shared by CLI, Railway preflight, supervisor, and docs. Remove scattered Daytona `os.getenv()` reads. Local mode must be selected intentionally with `TOMO_HOSTED_RUNTIME=local`; production default is `daytona`.

**Step 4: Verify GREEN**

Run focused and full suites.

### Task 11: Supervise both Railway processes instead of detaching the listener

**Objective:** Make Railway restart the service if either the control API or shared Telegram poller dies.

**Files:**
- Modify: `tomo_core/scripts/railway_core_start.py`
- Create: `tomo_core/tests/test_railway_core_start.py`
- Keep: `tomo_core/railway.toml`
- Keep: root `railway.toml`

**Step 1: Write failing process-supervision tests**

Using fake `subprocess.Popen`, assert:
- env preflight runs before children;
- shared poller starts in foreground as a child, not via `telegram-shared restart`;
- control API binds `0.0.0.0:$PORT`;
- if either child exits, the other receives terminate, then kill after timeout;
- SIGTERM/SIGINT are forwarded;
- nonzero child exit makes the supervisor exit nonzero so Railway restarts it;
- with no bot token in explicit local control-only mode, only control starts;
- command lines and logs never print secret values.

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_railway_core_start -v`

**Step 3: Implement supervision**

Use `Popen` for both long-lived commands and a small wait loop. Do not rely on shell `&`, `nohup`, detached PID state, or a custom wrapper. Keep the control API health check at `/v1/health`; the parent process guarantees the listener cannot fail silently while health remains green.

**Step 4: Verify GREEN**

Run focused, CLI, and full suites.

### Task 12: Add a reproducible Tomo Daytona snapshot artifact

**Objective:** Build a custom snapshot from the exact repo state without private git clones or interactive shell setup.

**Files:**
- Create: `tomo_core/Dockerfile.daytona`
- Create: `tomo_core/scripts/create_daytona_snapshot.py`
- Create: `tomo_core/tests/test_create_daytona_snapshot.py`
- Modify: `tomo_core/.dockerignore` if created; otherwise create it

**Step 1: Write failing builder tests**

Test the script's pure request builder with a fake Daytona snapshot service:
- requires an explicit immutable snapshot name;
- uses `Image.from_dockerfile("Dockerfile.daytona")` and `CreateSnapshotParams`;
- refuses an existing name unless `--replace` is given;
- waits for/prints active status but never prints API credentials;
- non-active/failing snapshot exits nonzero.

**Step 2: Verify RED**

Run: `cd tomo_core && uv run python -m unittest tests.test_create_daytona_snapshot -v`

**Step 3: Create the image**

`Dockerfile.daytona` should:

```dockerfile
FROM python:3.11-slim
WORKDIR /opt/tomo
RUN python -m pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY SOUL.md ./SOUL.md
RUN uv sync --frozen --no-dev
ENV TOMO_CORE_SOUL=/opt/tomo/SOUL.md
RUN /opt/tomo/.venv/bin/tomo-core --help
```

Do not bake OAuth, API keys, Telegram tokens, `.tomo_core`, `.env`, or test caches. The builder command should be:

```bash
cd tomo_core
DAYTONA_API_KEY=... uv run python scripts/create_daytona_snapshot.py --name "tomo-core-$(git rev-parse --short HEAD)"
```

**Step 4: Verify locally**

Run the builder unit test. If Docker is available, build the image and run:

```bash
docker build -f tomo_core/Dockerfile.daytona tomo_core -t tomo-core-daytona:test
docker run --rm tomo-core-daytona:test /opt/tomo/.venv/bin/tomo-core sandbox-inbound --health
```

Expected: exit 0 with a valid `TOMO_SANDBOX_RESULT=` line.

### Task 13: Document a short, secret-safe operator setup

**Objective:** Make setup repeatable without mixing dashboard, Railway core, and Daytona responsibilities.

**Files:**
- Create: `docs/daytona-railway.md`
- Modify: `docs/onboarding-telegram.md`
- Modify: `tomo_core/README.md`
- Modify: `tomo_core/HANDOFF.md`

**Step 1: Write the exact runbook**

The runbook must have five sections only:

1. **local auth bootstrap**
   ```bash
   grok login
   cd tomo_core
   uv run python scripts/export_grok_auth.py ~/.grok/auth.json
   ```
   Paste the single output line into Railway's sealed `TOMO_SUPERGROK_OAUTH_JSON_B64` variable. Warn not to commit it or paste it into chat/logs.

2. **create snapshot** using the immutable git-sha name.

3. **configure Railway core** using the core env list above and mount `/data`.

4. **configure Railway dashboard** with only Better Auth + control client vars and its own `/data` volume.

5. **verify** health, `/start`, sandbox/volume creation, DM reply, restart recovery.

Add a small troubleshooting table for `missing hosted env`, `sandbox smoke failed`, `auth_expired`, `Daytona conflict`, `listener exited`, and `snapshot changed`. Every recovery must preserve the per-user volume.

**Step 2: Consistency checks**

Search docs for stale claims that shared hosted messages run in `RuntimeInstanceRegistry`, that `XAI_API_KEY` is required, or that dashboard sign-in provisions a sandbox. Update only affected statements.

**Step 3: Verify commands against CLI help**

Run:

```bash
cd tomo_core
uv run tomo-core --help
uv run tomo-core sandbox-inbound --health
uv run python scripts/create_daytona_snapshot.py --help
uv run python scripts/export_grok_auth.py --help
```

### Task 14: Run regressions and live end-to-end acceptance

**Objective:** Prove isolation, routing, restart recovery, OAuth handoff, and deployment behavior with real services.

**Files:**
- Test only; do not modify production code unless a failing acceptance check first gains a regression test.

**Step 1: Automated suite**

```bash
cd tomo_core
uv sync
uv run python -m unittest discover -s tests -v
cd ../dashboard
bun run lint
bun run build
```

Expected: all Python tests pass; dashboard lint/build pass.

**Step 2: Local contract smoke**

Run `sandbox-inbound --health`, then a static-provider synthetic inbound. Parse its marker using `parse_result_stdout` and verify one trusted result.

**Step 3: Daytona snapshot smoke**

Create a disposable sandbox from the new snapshot with a disposable volume. Run health, then a static inbound. Verify the session file lands on the mounted data path. Delete the sandbox and recreate it with the same volume; verify the session remains.

**Step 4: Railway preflight**

Deploy the core service with `/data`; confirm `/v1/health` returns 200 and logs show exactly one shared poller. Stop the poller child deliberately and verify Railway restarts the service.

**Step 5: `/start` acceptance**

From a fresh authenticated dashboard user:
- click `text tomo` and press Telegram Start;
- verify one installation row, one Daytona sandbox, and one volume;
- verify the connected welcome is sent only after snapshot smoke succeeds;
- repeat `/start` with the same token and verify no second sandbox is created.

**Step 6: Routing isolation acceptance**

Use two Telegram accounts bound to two Better Auth users. Send distinct marker messages. Verify:
- different deterministic `tomo_id`, sandbox, and volume;
- each response returns to its source chat only;
- each sandbox volume contains only its own session marker;
- no sandbox environment contains the Telegram bot token or OAuth refresh token.

**Step 7: Recovery acceptance**

- restart Railway: next DM reuses the same sandbox and volume;
- stop one sandbox: next DM restarts/reconciles it;
- deploy a new snapshot name: sandbox is recreated while volume persists;
- temporarily use an expired access token fixture in staging: broker refreshes and retries exactly once;
- inspect Railway and Daytona logs for token substrings and confirm none appear.

**Step 8: Final invariants**

- [ ] no changes to `tomo_core/SOUL.md`
- [ ] dashboard never talks directly to Daytona
- [ ] only Railway owns the Telegram token and sends messages
- [ ] `/start` provisions before connected welcome
- [ ] every DM resolves `chat_id -> installation -> tomo_id -> sandbox`
- [ ] one user cannot select another user's sandbox/output destination
- [ ] OAuth refresh token never leaves Railway
- [ ] incomplete Daytona config fails closed
- [ ] Railway restarts when the poller dies
- [ ] full Python suite and dashboard build are green

## 5. Files likely to change

### New

- `tomo_core/src/tomo_core/daytona_client.py`
- `tomo_core/src/tomo_core/sandbox_protocol.py`
- `tomo_core/src/tomo_core/sandbox_inbound.py`
- `tomo_core/src/tomo_core/hosted_auth.py`
- `tomo_core/src/tomo_core/sandbox_registry.py`
- `tomo_core/src/tomo_core/daytona_supervisor.py`
- `tomo_core/src/tomo_core/sandbox_dispatch.py`
- `tomo_core/src/tomo_core/shared_router.py`
- `tomo_core/src/tomo_core/hosted_config.py`
- corresponding `tomo_core/tests/test_*.py`
- `tomo_core/Dockerfile.daytona`
- `tomo_core/.dockerignore`
- `tomo_core/scripts/create_daytona_snapshot.py`
- `tomo_core/scripts/export_grok_auth.py`
- `docs/daytona-railway.md`

### Modified

- `tomo_core/pyproject.toml`
- `tomo_core/uv.lock`
- `tomo_core/src/tomo_core/cli.py`
- `tomo_core/src/tomo_core/providers.py`
- `tomo_core/src/tomo_core/grok_auth.py`
- `tomo_core/src/tomo_core/models.py`
- `tomo_core/src/tomo_core/shared_gateway.py`
- `tomo_core/src/tomo_core/onboarding_store.py`
- `tomo_core/scripts/railway_core_start.py`
- focused existing tests
- `docs/onboarding-telegram.md`
- `tomo_core/README.md`
- `tomo_core/HANDOFF.md`

### Deliberately unchanged

- `dashboard/src/app/api/onboarding/telegram/route.ts`
- `dashboard/src/lib/auth.ts`
- `tomo_core/SOUL.md`
- dashboard visual design

## 6. Risks and tradeoffs

- **Operator-level OAuth is shared:** all user sandboxes consume one model credential. If each user must bring their own SuperGrok account, redesign auth ownership before implementation.
- **OAuth schema variation:** local Grok auth JSON may be nested. Normalize common keys and fail with a safe actionable error; never assume a single flat schema without tests.
- **At-least-once Telegram boundary:** a crash after `sendMessage` but before inbox completion can duplicate a reply. Telegram has no send idempotency key; prefer rare duplicates over silent loss.
- **Long-running sandbox cost:** `auto_stop_interval=0` satisfies always-on behavior but may be expensive. Reconciliation supports later auto-stop without changing routing.
- **Snapshot upgrades:** code changes do not affect existing sandboxes until `TOMO_DAYTONA_SNAPSHOT` changes. Use immutable git-sha snapshot names and preserve volumes during replacement.
- **SDK drift:** the plan targets Daytona `0.195.0`, inspected on 2026-07-10. Keep it pinned and update adapter tests before upgrading.
- **SQLite on Railway:** the core must have exactly one writer service and a persistent `/data` volume. Multiple replicas require a shared database/queue and leader election; keep replicas at one in v1.

## 7. Open question that does not block v1

Should sandboxes auto-stop after inactivity to reduce cost? The proposed default is always-on because that matches the request. If cost matters, set a nonzero Daytona auto-stop interval later; `ensure_sandbox()` already makes wake-up transparent.
