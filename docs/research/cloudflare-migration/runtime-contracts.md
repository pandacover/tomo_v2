# Runtime contracts Railway and Daytona currently satisfy

## Purpose and source basis

This note inventories the production contracts that a Railway/Daytona replacement must satisfy. It deliberately does **not** choose Cloudflare products or propose migration code. The source of truth is the repository at commit `8eefc198acda4ce286e22d392828fd4a76c620d1`; the main worktree was also inspected read-only. Its uncommitted changes are dashboard presentation/refactoring changes and do not alter the auth, onboarding, control API, Telegram, cron, persistence, or runtime interfaces cited here.

The migration effort supplies these planning inputs:

- exactly three allowlisted owners;
- all three owners may have an interactive turn in flight simultaneously;
- each owner still gets secure state and execution isolation, but a literal per-owner container is not required;
- the packaged Tomo runtime and its tools are in scope; arbitrary user code, arbitrary shells, and user-installed packages are not;
- behavior and safety guarantees should survive even when their implementation changes;
- this is an empty-state relaunch, so copying existing Railway/Daytona data and keeping either service for rollback are not requirements.

## Current production topology

Railway is not only an HTTP host. The production start wrapper supervises two sibling processes: a FastAPI control server and, when the global bot token exists, one foreground `telegram-shared` process. If either child exits, the wrapper stops the other; termination is forwarded with a ten-second graceful-stop budget (`tomo_core/scripts/railway_core_start.py:31-57`, `tomo_core/scripts/railway_core_start.py:64-104`). The Railway manifests expose `/v1/health` and restart on failure (`railway.toml:1-5`, `tomo_core/railway.toml:1-5`).

The shared Telegram process constructs and owns the durable inbox/onboarding store, cron store, peer exchange store, signing keys, hosted SuperGrok token broker, Daytona registry/supervisor/dispatch, cron scheduler, peer worker, and Telegram router. The cron and peer services run as background threads beside the long poller (`tomo_core/src/tomo_core/cli.py:254-267`, `tomo_core/src/tomo_core/cli.py:297-358`). Therefore replacing Railway means replacing an always-on coordination service, not merely moving a web endpoint.

Daytona is the execution plane. Each Tomo has a deterministic sandbox and volume, reconciled from an immutable snapshot. The sandbox image is Python 3.11 with the locked Python package installed and `/opt/tomo/.venv/bin/tomo-core` as its entry command surface (`tomo_core/Dockerfile.daytona:1-15`, `tomo_core/pyproject.toml:1-17`). Every turn launches `tomo-core sandbox-inbound` in a PTY session and consumes a streamed, line-oriented protocol (`tomo_core/src/tomo_core/sandbox_dispatch.py:30-48`, `tomo_core/src/tomo_core/sandbox_dispatch.py:159-211`).

The two planes are intentionally asymmetric:

- the control host owns identities, routing, schedules, delivery, central durability, platform credentials, and correctness fences;
- the per-owner runtime owns conversation execution and personal data;
- only the control host sends Telegram messages or sees Telegram and refresh credentials; a sandbox receives one current access token for one command (`docs/daytona-railway.md:68-78`, `docs/daytona-railway.md:134-136`).

## Contracts the control plane must replace

### One trusted ingress and sender

There must be exactly one consumer for the shared bot token. It accepts private chats only and verifies that the sender is the actor bound to that chat. The dashboard never receives the token (`docs/onboarding-telegram.md:54-60`). The current listener uses Telegram long polling with a configurable 30-second default and a valid range of 1-60 seconds (`tomo_core/src/tomo_core/hosted_config.py:108-118`). A webhook is a valid replacement mechanism, but it must preserve the single-ingress, private-chat, actor-binding, ordering, and durable-acceptance properties.

The listener durably inserts an update before advancing its Telegram offset. `update_id` is unique, order is preserved per chat, and a row left in `processing` is recovered to `pending` at startup (`docs/daytona-railway.md:68-72`). The current store is `<TOMO_CORE_DATA_DIR>/onboarding.sqlite` (`tomo_core/src/tomo_core/onboarding_store.py:118-120`). These are correctness contracts, not SQLite-specific requirements.

Railway is the sole Telegram sender. Sandbox input contains neither the bot token nor a destination chat id. The host validates the event stream, rechecks the active generation, reserves delivery, sends only to the trusted installation chat, and stores Telegram's message id (`docs/daytona-railway.md:72-76`). The delivery semantic is duplicate-averse, not exactly-once: a send whose remote acceptance is ambiguous becomes `unknown` and is never blindly repeated (`docs/daytona-railway.md:74-76`).

### Per-chat ordering, burst coalescing, and supersession

Normal text is coalesced into an ordered burst after a resettable quiet window; the default is 0.7 seconds. Progressive bubbles are paced by a configurable delay whose default is 1.5 seconds (`docs/daytona-railway.md:20-23`, `tomo_core/src/tomo_core/hosted_config.py:113-119`).

A later user message revises the burst, marks the old generation `superseded`, and requests best-effort process cancellation. Cancellation is an optimization: every provider step, send, and finalization is fenced against durable generation identity and revision. Visible text already sent - or whose acknowledgement is unknown - is included in the replacement generation's context (`docs/daytona-railway.md:70-97`). A new execution design must therefore offer a cheap, strongly checked `is this generation still active?` boundary even if it cannot terminate compute immediately.

Retryable control-update failures use five attempts with 1, 2, 4, 8, and 16 second backoff. Exhaustion completes the control update so later updates in that chat are not blocked (`docs/daytona-railway.md:78-78`).

### Control API and internal authentication

The control API provides health, Telegram install links, attachment resolution, cron operations, and peer operations. Dashboard-to-control calls use a shared `x-api-key`; runtime-to-control cron, peer, and attachment calls use short-lived signed capabilities bound to owner and operation context (`tomo_core/src/tomo_core/control_api.py:128-188`, `tomo_core/src/tomo_core/control_api.py:190-239`). The public control URL must be HTTPS in hosted Daytona mode because sandboxes call back into it (`tomo_core/src/tomo_core/hosted_config.py:98-106`).

Telegram install links are one-time, expire after ten minutes, and bind a private chat to one `tomo_id` only when the user sends `/start <token>` (`docs/onboarding-telegram.md:16-24`). The current dashboard route forwards any authenticated Better Auth user id to the control API, and the control endpoint creates a link for any correctly API-key-authenticated request (`dashboard/src/app/api/onboarding/telegram/route.ts:5-22`, `tomo_core/src/tomo_core/control_api.py:219-226`). The repository therefore has no explicit three-owner admission cap. The target architecture must add an allowlist or equivalent admission rule at a trusted server-side boundary; a UI-only restriction would not satisfy the target.

### Central durable state

The Railway volume is a multi-database correctness boundary:

| State | Current durable location | Ownership/contract |
| --- | --- | --- |
| Telegram installs, tokens, inbox, generations, offsets, and delivery receipts | `onboarding.sqlite` | One control-plane authority; transactional ordering, fencing, recovery, and dedupe (`tomo_core/src/tomo_core/onboarding_store.py:118-120`, `docs/daytona-railway.md:68-101`). |
| Cron definitions, occurrences, leases, attempts, receipts, and tombstones | `cron.sqlite3` | Always-on scheduler authority; sandboxes never open it (`tomo_core/src/tomo_core/cron_store.py:44-46`, `tomo_core/docs/adr/0003-control-host-durable-agent-cron.md:6-27`). |
| Peer identities, relationships, grants, requests, leases, confirmations, and audit state | `peer.sqlite3` | Owner-scoped control state and recovery (`tomo_core/src/tomo_core/peer_store.py:87-89`, `tomo_core/src/tomo_core/cli.py:262-267`). |
| Sandbox reconciliation records | `sandbox_registry.sqlite` | Deterministic owner-to-runtime and volume identity (`tomo_core/src/tomo_core/sandbox_registry.py:24-26`, `tomo_core/src/tomo_core/sandbox_registry.py:106-141`). |
| Mutable SuperGrok OAuth payload and bootstrap fingerprint | `hosted-auth/supergrok.json` plus adjacent private files | Central token refresh; credential-sensitive, atomic private writes (`tomo_core/src/tomo_core/hosted_auth.py:68-99`, `tomo_core/src/tomo_core/hosted_auth.py:166-175`). |
| Capability signing keys | files below the core data directory | Stable across restarts so issued host/runtime capabilities can be verified (`tomo_core/src/tomo_core/cli.py:259-262`, `tomo_core/src/tomo_core/cli.py:297-324`). |

The implementation assumes a singleton control deployment. SQLite leases defend against duplicate workers and restart recovery, but are explicitly not a distributed scheduler (`tomo_core/docs/adr/0003-control-host-durable-agent-cron.md:53-57`). A replacement can distribute the system only if it establishes a new single-writer/transactional authority for the same invariants.

## Contracts the execution plane must replace

### Owner isolation and concurrency

Each `tomo_id` deterministically maps to one sandbox name and one volume name. Reconciliation serializes per owner, retains the volume across sandbox replacement, resumes stopped sandboxes, smoke-tests created/resumed instances, and refuses to create a replacement if it cannot first remove a conflicting writer (`tomo_core/src/tomo_core/daytona_supervisor.py:32-113`, `docs/daytona-railway.md:99-101`). Daytona auto-stop is disabled (`tomo_core/src/tomo_core/daytona_client.py:82-89`).

Interactive, scheduled, and peer turns all share the same per-owner execution lock, so one owner never has two runtime commands racing. Locks are keyed by `tomo_id`, so three different owners can execute simultaneously (`tomo_core/src/tomo_core/sandbox_dispatch.py:136-141`, `tomo_core/src/tomo_core/sandbox_dispatch.py:325-357`, `tomo_core/src/tomo_core/sandbox_dispatch.py:389-435`). For the target workload, the execution plane must admit up to three cross-owner turns while serializing all work belonging to any one owner.

A literal container is replaceable. The non-replaceable boundary is that one owner's runtime cannot read or mutate another owner's personal state, credentials, active-generation state, or tool authority. Current personal data is owner-scoped at the repository interface, and hosted v1 additionally relies on per-user sandbox/volume isolation (`tomo_core/docs/adr/0002-portable-personal-data-and-search.md:7-24`, `tomo_core/docs/adr/0002-portable-personal-data-and-search.md:37-49`).

### Runtime duration and process behavior

An ordinary sandbox command has a 120-second budget. Each input image adds 75 seconds, capped at eight images, so a valid image-heavy interactive turn may occupy its owner execution slot for **720 seconds (12 minutes)** (`tomo_core/src/tomo_core/sandbox_dispatch.py:30-48`). Scheduled turns use 120 seconds; peer turns are bounded by their remaining expiry and at most 60 seconds (`tomo_core/src/tomo_core/sandbox_dispatch.py:325-374`, `tomo_core/src/tomo_core/sandbox_dispatch.py:389-445`). These are required workload envelopes unless product requirements deliberately shorten them later.

The current adapter requires process-like behavior: start a Python command with per-command environment variables, stream output while it runs, observe its exit code, impose a timeout, and best-effort kill a named session when superseded. The PTY is intentionally 999 columns because wrapping the one-line protocol corrupts messages (`tomo_core/src/tomo_core/daytona_client.py:13-17`, `tomo_core/src/tomo_core/daytona_client.py:101-170`, `tomo_core/src/tomo_core/daytona_client.py:172-186`). A replacement need not expose a shell to users, but it must provide an equivalent packaged-runtime invocation, streaming/capture channel, timeout, and cancellation boundary - or the runtime/transport must be rewritten to eliminate those assumptions.

The host passes only narrowly scoped command environment: inbound JSON, owner instance id, personal data directory, SOUL path, configured model/effort, one current SuperGrok access token, and short-lived capabilities/metadata for attachments, cron, and peers (`tomo_core/src/tomo_core/sandbox_dispatch.py:174-191`). Secrets must not leak into protocol errors or completion summaries.

### Personal data durability

The runtime persistence interface owns sessions, messages, accepted generations, memories, governance settings, pending actions, deletion tombstones, and search. SQLite v1 requires FTS5 and uses `bm25`; canonical versioned JSONL is the portability contract for another adapter (`tomo_core/docs/adr/0002-portable-personal-data-and-search.md:7-17`, `tomo_core/docs/adr/0002-portable-personal-data-and-search.md:26-35`). Accepted sessions and memories are retained indefinitely unless explicitly deleted (`tomo_core/docs/adr/0002-portable-personal-data-and-search.md:37-44`).

Daytona's mounted filesystem is not trusted for live SQLite writes. Each owner works on a local SQLite copy, and every committed mutation closes/backs up that database and synchronously fsyncs a framed checkpoint into alternating slots on the durable mounted volume. Success is not reported until checkpointing succeeds (`tomo_core/docs/adr/0002-portable-personal-data-and-search.md:19-24`, `docs/daytona-railway.md:99-101`). The replaceable detail is SQLite/checkpoint framing; the required behavior is owner-scoped durable commit before success, searchable state, and no concurrent writer.

Because this migration is an empty-state relaunch, the JSONL import/export and old checkpoint transfer paths do not need to be exercised for cutover. They remain useful compatibility seams if the new persistence adapter continues serving the existing Python runtime.

### Protocol and output

The control plane accepts only protocol-v2 event lines with strict request id, generation id, sequence continuity, event type, and shape validation. Events are ordered utterances followed by one completion or a typed safe error (`docs/daytona-railway.md:72-76`). Current runtime defaults allow at most three frames per segment, three sentences per frame, and 800 characters per frame; tool turns may use up to six model segments, five tool rounds/calls, and three visible segments (`tomo_core/src/tomo_core/models.py:324-363`). The transport maximum remains 4096 characters per Telegram bubble (`tomo_core/src/tomo_core/sandbox_protocol.py:26-28`).

## Cron and unattended execution

Cron is agent execution, not stored notification text. The control host stores job intent and wakes the owner's current runtime with current personal context (`tomo_core/docs/adr/0003-control-host-durable-agent-cron.md:6-27`). Supported schedules are a UTC one-time instant, fixed interval, or timezone-aware five-field cron expression. Missed occurrences coalesce, and one job has at most one active run (`tomo_core/docs/adr/0003-control-host-durable-agent-cron.md:27-35`).

The scheduler lane is independent of interactive Telegram workers. Its current polling cadence is 0.2 seconds and execution lease is 180 seconds (`tomo_core/src/tomo_core/cron_service.py:23-41`). Job revisions and opaque lease tokens fence execution and delivery; user input can supersede active automation without consuming an execution attempt (`tomo_core/docs/adr/0003-control-host-durable-agent-cron.md:33-41`). Execution output is stored before delivery, and Telegram delivery has a separate lease/retry lifecycle so a send failure never reruns successful model work. Ambiguous sends become terminal `unknown` (`tomo_core/docs/adr/0003-control-host-durable-agent-cron.md:45-47`).

For only three owners, high availability and automated disaster recovery are not target requirements, but durable scheduling across service restarts, owner scoping, one active occurrence, mutation fencing, and independent delivery retry remain required behavior.

## Images

V1 accepts Telegram photos only. A burst may contain at most eight distinct images; each compressed image is limited to 10 MiB, decoded input to 20 megapixels, and normalized JPEG output to a 2048-pixel longest edge. Raw bytes, data URLs, Telegram file ids, capabilities, and provider credentials are not retained (`tomo_core/docs/image-understanding.md:1-11`, `tomo_core/src/tomo_core/vision.py:210-228`).

The control host fetches the Telegram file and returns it through a no-store endpoint only after verifying a capability bound to owner, generation, and file-id hash. The sandbox capability lasts five minutes and permits at most the first eight distinct image ids in that burst (`tomo_core/src/tomo_core/control_api.py:179-217`, `tomo_core/src/tomo_core/sandbox_dispatch.py:300-323`). The sandbox downloads with a ten-second HTTP timeout and repeats the 10 MiB limit (`tomo_core/src/tomo_core/attachment_reader.py:23-59`). It normalizes locally and submits a tool-free, structured vision request; failures degrade to an explicit unavailable observation rather than failing the whole turn (`tomo_core/src/tomo_core/vision.py:142-207`).

Any redesign may avoid proxying raw bytes through the runtime, but it must preserve file authorization, size/type/decompression limits, untrusted-evidence treatment, non-retention, and graceful unavailable results.

## Model authentication and secret placement

Railway holds the Daytona API key, Telegram bot token, optional direct xAI key, and a base64 SuperGrok OAuth bootstrap containing the refresh token. The mutable SuperGrok payload is refreshed centrally when it is within 120 seconds of expiry; refresh HTTP calls have a 60-second timeout (`tomo_core/src/tomo_core/hosted_auth.py:19-26`, `tomo_core/src/tomo_core/hosted_auth.py:68-99`, `tomo_core/src/tomo_core/hosted_auth.py:126-164`). A sandbox receives only the current access token and retries once with forced refresh when the runtime reports `auth_expired` before emitting output (`tomo_core/src/tomo_core/sandbox_dispatch.py:164-222`).

The target keeps SuperGrok OAuth as the primary model-auth path, with direct xAI API support optional. Whether Cloudflare can safely run this broker unchanged - and whether the OAuth terms and token behavior permit the new execution placement - is a separate research/architecture decision. The invariant here is centralized refresh-token custody, atomic durable refresh state, and least-privilege per-turn access-token injection.

## Dashboard boundary

The dashboard is a server-rendered Next 16 application, not a static bundle. It has server API routes, server-side calls to the control API, Better Auth, password-reset email, and a local SQLite auth database opened through Bun SQLite or Node's `node:sqlite` (`dashboard/package.json:5-20`, `dashboard/src/lib/auth.ts:1-75`). Its environment includes a writable data directory, `auth.sqlite`, Better Auth public URL/secret/API key, control API URL, and the dashboard-to-control secret (`dashboard/src/lib/env.ts:1-24`).

The Telegram onboarding API route authenticates a dashboard session, calls the control server-side install-link endpoint with `x-api-key`, and returns a no-store handoff page (`dashboard/src/app/api/onboarding/telegram/route.ts:5-56`). Peer dashboard reads/mutations are also server-only, authenticated control calls (`dashboard/src/lib/peer-client.ts:6-44`).

Therefore moving the dashboard off Railway requires one of these later decisions: host a compatible stateful Next server, replace its auth persistence/runtime dependencies, or split static presentation from server/auth functions. "Cloudflare Pages versus Vercel" cannot be decided from static asset hosting alone because the current dashboard contract includes durable auth state and server routes.

## Concise replacement requirements matrix

| Area | Required behavior at destination | Current mechanism | Replaceable? |
| --- | --- | --- | --- |
| Admission | Exactly three server-side allowlisted owners | No explicit cap exists | Mechanism undecided; requirement new |
| Ingress | One authenticated Telegram ingress; private bound actors only | Singleton long poller | Yes, including webhook conversion |
| Acceptance | Persist update before acknowledgement/offset advance; dedupe by update id | `onboarding.sqlite` transaction | Storage mechanism yes; ordering invariant no |
| Chat work | Per-chat order, 0.7 s burst debounce, five bounded retries | Router workers plus SQLite | Mechanism yes |
| Supersession | New input fences old compute, writes, sends, and completion | Revisioned generation rows plus best-effort PTY kill | Mechanism yes; fence no |
| Delivery | Host-only Telegram send; ordered pacing; ambiguous send becomes `unknown` | Durable reservations/receipts plus 1.5 s pace | Mechanism yes; semantics no |
| Owner concurrency | One runtime turn per owner; three owners may run concurrently | Per-`tomo_id` locks and sandboxes | Container no; isolation/serialization yes |
| Runtime duration | 120 s ordinary/cron; up to 720 s with eight images; peer <=60 s | Daytona PTY commands | Requires explicit platform fit decision |
| Runtime package | Execute packaged Python 3.11 Tomo and stream strict events | Immutable Daytona snapshot | Image/build mechanism yes |
| Personal state | Owner-scoped durable commit before success; sessions/memory/search | Local SQLite plus alternating volume checkpoints | Adapter/storage yes; durability contract no |
| Central state | Durable onboarding, generation, cron, peer, delivery, and signing state | Four SQLite databases and key files on Railway volume | Storage design undecided |
| Cron | Restart-safe schedules, one active occurrence, leases/revisions, execution/delivery split | Always-on 0.2 s scheduler lane | Mechanism yes |
| Model auth | Central refresh-token custody; one current access token per command | Railway SuperGrok broker | Placement/design undecided |
| Images | Owner/generation/file-scoped resolution; <=8, <=10 MiB each, safe normalization and non-retention | Five-minute capability and control-host proxy | Mechanism yes; limits/safety no |
| Dashboard | Next server routes, Better Auth, durable auth database, control proxy | Railway Next/Bun service and `auth.sqlite` | Host/runtime/persistence undecided |
| Operations | Health endpoint, safe logs, supervised recovery; manual recovery acceptable | Railway restart policy and logs | Mechanism yes |

## Newly exposed decisions and fog

These questions are now sharp enough for later Wayfinder tickets or for tickets already on the map:

1. **Where will long Python turns run?** The candidate must support three concurrent owner-isolated executions, a 120-720 second envelope, streamed or captured protocol output, cancellation/fencing, Pillow/SQLite FTS5, and the packaged runtime without granting arbitrary user execution.
2. **What is the new durable ownership model?** Decide how Telegram/generation, cron, peer, signing-key, and per-owner personal state map to transactional/single-writer primitives while retaining owner isolation and restart recovery.
3. **How will Telegram ingress be converted?** Decide webhook versus another singleton consumer and specify acknowledgement, durable enqueue, ordering, dedupe, retry, and secret verification.
4. **Where is the three-owner allowlist enforced?** The existing authenticated onboarding flow admits any dashboard account; define the authoritative admission source and behavior for users outside it.
5. **Where will SuperGrok OAuth refresh run?** Verify platform and provider compatibility, secret storage, atomic refreshed-token persistence, and safe injection into isolated turns.
6. **Where will the dashboard's server and auth state live?** Cloudflare versus Vercel depends on the chosen Next runtime and replacement for local `auth.sqlite`, not just frontend hosting.

Still-foggy items should remain off the ticket frontier until the runtime and persistence decisions above are answered: the exact split among Cloudflare services, whether the Python runtime is retained or selectively ported, how peer-worker execution shares capacity with three interactive owners, and which operational metrics are necessary to police the $5/month target.
