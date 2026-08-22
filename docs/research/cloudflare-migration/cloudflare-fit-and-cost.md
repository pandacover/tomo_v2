# Cloudflare product fit and the $5 operating envelope

Status: resolved research finding
Date: 2026-08-23

## Answer

Cloudflare can host Tomo's control plane, per-owner durable state, dynamic schedules, queued/retriable work, images, and three isolated agent runtimes. It is **not** a lift-and-shift into plain Workers, and a Cloudflare-only bill of exactly $5 is not guaranteed.

The lowest-rewrite migration is:

1. Accept Telegram webhooks in a TypeScript Worker and durably record/deduplicate each update before returning success.
2. Route each owner to an owner-keyed Durable Object (DO). Use DO SQLite for the inbox, personal state, debounce state, schedule state, generation/revision fences, and delivery claims.
3. Use a Workflow for a long or retriable turn. Run the packaged Python agent in one scale-to-zero Sandbox/Container per owner. Treat its filesystem as disposable.
4. Persist canonical data and checkpoints in DO SQLite (or D1 if a shared/queryable database is deliberately chosen), store image payloads and container backups in R2, and send final/streamed events back through authenticated Worker/DO endpoints.
5. Use one DO alarm per owner to implement dynamic user cron jobs; store all of that owner's jobs in SQLite and schedule the alarm for the next due job.

This preserves the current important guarantees—per-owner isolation, one turn per owner, durable inboxes, stale-generation rejection, single delivery, and dynamic schedules—without forcing the existing Python package into the restricted Python Workers runtime.

The $5/month target is credible only if the account is on **Workers Paid Standard**, all non-container usage remains inside included allowances, and owner containers sleep aggressively. Three always-on containers exceed $5. Cloudflare charges usage over the included amounts, so $5 is an operating target rather than a hard ceiling.

## Confirmed product fit

The facts in this section are confirmed by current first-party documentation. They do not confirm what is enabled on this particular Cloudflare account.

### Ingress and Telegram

A Worker is a suitable HTTPS webhook endpoint. Telegram sends webhook updates as HTTPS POST requests, retries unsuccessful requests, permits a `secret_token` whose value is sent in `X-Telegram-Bot-Api-Secret-Token`, and does not allow `getUpdates` while a webhook is configured ([Telegram Bot API: `setWebhook`](https://core.telegram.org/bots/api#setwebhook)). The ingress Worker should verify that header, validate and deduplicate the update, durably enqueue/record it, and return quickly rather than keeping Telegram's request open for the agent turn.

Workers Paid has no request-count limit, includes 10 million requests per month, and then bills $0.30 per additional million. It includes 30 million CPU milliseconds, then bills $0.02 per additional million ([Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/)). An HTTP Worker can have unlimited wall time while the client remains connected, but `waitUntil()` receives only up to 30 seconds after the response or client disconnects ([Workers limits](https://developers.cloudflare.com/workers/platform/limits/)). Therefore a detached, potentially long turn needs a durable handoff, not an unbounded post-response task.

### Three concurrent packaged turns and owner isolation

Cloudflare Containers and Sandbox reached general availability on 2026-04-13 ([GA changelog](https://developers.cloudflare.com/changelog/post/2026-04-13-containers-sandbox-ga/)). Container account limits are far above three instances; the smallest `lite` instance has 1/16 vCPU, 256 MiB memory, and 2 GB disk ([Container limits](https://developers.cloudflare.com/containers/platform-details/limits/)). The unanswered question is whether Tomo and its Python dependency graph fit and perform acceptably in that smallest shape.

Sandbox supplies a Linux/Python execution environment and supports streaming process output ([Sandbox overview](https://developers.cloudflare.com/sandbox/)). Each sandbox runs in a separate virtual machine with filesystem, process, and network isolation; Cloudflare still requires the application to authenticate users and choose separate sandboxes where appropriate ([Sandbox security](https://developers.cloudflare.com/sandbox/concepts/security/)). Mapping one authenticated owner to one sandbox/container therefore supports three concurrent owner turns and a stronger boundary than sharing one process, provided the owner-to-instance mapping is enforced in the control plane.

Containers have no fixed maximum runtime, but their local disk is ephemeral when an instance sleeps or is replaced. The default `sleepAfter` is ten minutes ([Container architecture](https://developers.cloudflare.com/containers/platform-details/architecture/), [Container FAQ](https://developers.cloudflare.com/containers/faq/)). Canonical personal data, turn fences, schedule state, and OAuth state must consequently live outside the container. Sandbox can back up a directory to R2, but its backup documentation warns about application-consistency concerns during writes; backups are recovery artifacts, not a substitute for transactional canonical storage ([Sandbox backup and restore](https://developers.cloudflare.com/sandbox/guides/backup-restore/), [Sandbox backups API](https://developers.cloudflare.com/sandbox/api/backups/)).

### Long model calls, retries, and streaming

Workflows are the best Cloudflare primitive for durable long-turn orchestration. A paid Workflow step has unlimited wall-clock duration, 30 seconds of CPU by default configurable to five minutes, and a non-streaming result limit of 1 MB. An instance can hold 1 GB of state, sleep for up to 365 days, and use up to 10,000 steps by default (configurable to 25,000) ([Workflows limits](https://developers.cloudflare.com/workflows/reference/limits/)). Workflows persist completed steps and resume from the last successful step after retry ([Workflows guide](https://developers.cloudflare.com/workflows/get-started/guide/)).

Streaming bytes should not be stored as a Workflow step result. A container process can stream logs/output, and Workers/R2 APIs accept streams ([Sandbox commands](https://developers.cloudflare.com/sandbox/api/commands/), [R2 Workers API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)). Tomo can stream progress over an authenticated HTTP/WebSocket/SSE path while independently persisting semantic events and the final result. If the client disconnects, durable execution continues in the Workflow; a later client can replay persisted events.

Workflow retries make side effects at-least-once from the application's perspective. Telegram delivery and final-state publication still need an atomic delivery claim and generation/revision check in the owner's DO before execution. This keeps stale or retried work from producing duplicate or superseded replies.

A Queue is useful as an optional load buffer, not as the sole long-turn runtime. Queues deliver at least once and do not guarantee order, so consumers must deduplicate by an application key ([delivery guarantees](https://developers.cloudflare.com/queues/reference/delivery-guarantees/), [how Queues works](https://developers.cloudflare.com/queues/reference/how-queues-works/)). Paid Queues allow a 15-minute consumer wall time, 100-message batches, 128 KB messages, and retention up to 14 days ([Queues limits](https://developers.cloudflare.com/queues/platform/limits/)). A turn that may exceed 15 minutes belongs in a Workflow/container, with the queue carrying only a small reference.

### Durable SQLite-shaped state, coordination, and fencing

An owner-keyed SQLite-backed DO is the closest match for Tomo's current per-owner SQLite and single-owner coordination model. Each DO has private, transactional, strongly consistent SQLite storage; the SQL API supports FTS5 and JSON functions and also exposes alarms and point-in-time recovery ([DO SQLite storage](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)). A paid SQLite DO can hold 10 GB; accounts can create unlimited objects and up to 500 DO classes ([DO limits](https://developers.cloudflare.com/durable-objects/platform/limits/)). Cloudflare encrypts DO data at rest with AES-256 and in transit with TLS ([DO data security](https://developers.cloudflare.com/durable-objects/reference/data-security/)).

DOs are globally unique and single-threaded, which makes them a good owner-scoped serialization and fencing seam ([rules of Durable Objects](https://developers.cloudflare.com/durable-objects/best-practices/rules-of-durable-objects/)). Awaiting external network calls can still allow other events to interleave. The DO should reserve a generation/lease transactionally, start the slow work outside the critical section, and require the matching generation/revision on every callback, completion, and delivery claim.

D1 also supports SQLite and FTS5. Paid limits include 50,000 databases, 10 GB per database, 1 TB total storage, 1,000 queries per Worker invocation, and a 2 MB row/BLOB limit ([D1 limits](https://developers.cloudflare.com/d1/platform/limits/), [D1 FTS5](https://developers.cloudflare.com/d1/sql-api/sql-statements/)). D1 is a viable durable data store or shared dashboard/query surface, but it is single-threaded per database and does not itself provide a per-owner actor. If D1 is canonical, keep owner coordination/fencing in a DO.

### Dynamic cron jobs

Cron Triggers are configuration-level UTC schedules managed by Wrangler, the dashboard, or the API. Changes can take up to 15 minutes to propagate, and a paid account is limited to 250 triggers ([Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/), [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)). They do not fit arbitrary runtime-created user jobs.

Each DO has one programmatically managed alarm. Alarm handlers are retried at least once with exponential backoff, starting at two seconds, for up to six retries ([DO alarms](https://developers.cloudflare.com/durable-objects/api/alarms/)). Store all jobs for an owner in that owner's DO, set its alarm to the nearest due time, transactionally claim due jobs, and reset the alarm to the next deadline. Application-level idempotency remains required.

### Secrets and OAuth refresh tokens

Worker secrets are encrypted bindings exposed only to Worker code and are appropriate for deployment-level values such as the Telegram bot token, webhook secret, signing key, and envelope-encryption key ([Worker secrets](https://developers.cloudflare.com/workers/configuration/secrets/)). Secrets Store is currently beta, limited to 100 secrets and one store per account, and is managed through Cloudflare rather than ordinary application writes ([Secrets Store limits](https://developers.cloudflare.com/secrets-store/manage-secrets/)). It is therefore a poor fit for mutable per-owner OAuth refresh tokens.

Store per-owner OAuth records in the owner's DO/D1 record, envelope-encrypted with a Worker secret, and update the refreshed token transactionally. Inject only the minimum short-lived credential into the owner's container. Sandbox supports outbound allow/deny policy and credential injection outside the sandbox boundary ([Sandbox outbound traffic](https://developers.cloudflare.com/sandbox/guides/outbound-traffic/)). Cloudflare's at-rest encryption is confirmed; application-layer envelope encryption, key rotation, and token redaction are Tomo design decisions.

### Images and object storage

R2 is appropriate for inbound image bytes, generated artifacts, and sandbox backups. Workers can stream R2 objects without loading an entire file into the 128 MB Worker isolate ([R2 Workers API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/), [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)). R2 includes 10 GB-month, one million Class A operations, ten million Class B operations, and free Internet egress before storage/operation overages ([R2 pricing](https://developers.cloudflare.com/r2/pricing/)). Objects can be up to 5 TiB, with a 5 GiB single-part upload maximum ([R2 limits](https://developers.cloudflare.com/r2/platform/limits/)).

Cloudflare Images is optional rather than required. Its free plan permits 5,000 unique transformations per month; additional new transformations fail unless Images is upgraded ([Images pricing](https://developers.cloudflare.com/images/pricing/)). Tomo can keep its existing Pillow processing inside the Python container and use R2 for transport/storage, avoiding a separate Images dependency.

### Python versus TypeScript

Use TypeScript Workers for ingress, DO coordination, and Cloudflare bindings; keep the existing agent package in a Python Sandbox/Container for the first migration.

Python Workers are open beta and run in Pyodide ([Python Workers](https://developers.cloudflare.com/workers/languages/python/)). Packages must be pure Python or have a Pyodide/PyEmscripten wheel; HTTP clients must be asynchronous ([Python packages](https://developers.cloudflare.com/workers/languages/python/packages/)). Their filesystem is ephemeral and in-memory, while `threading` and `multiprocessing` can be imported but do not function ([Python standard library](https://developers.cloudflare.com/workers/languages/python/stdlib/)). Tomo currently depends on native/local-process assumptions including `sqlite3` files, threading, subprocesses, Pillow, and synchronous streaming. A direct port is therefore not viable. A later Python-Worker or TypeScript rewrite may reduce container cost, but it requires a dependency/bundle/memory spike and a deliberate persistence/runtime refactor.

### Dashboard hosting

Cloudflare Workers can host Next.js through the OpenNext adapter, including the App Router, route handlers, SSR, and streaming for most applications ([Next.js on Workers](https://developers.cloudflare.com/workers/framework-guides/web-apps/nextjs/)). Static asset requests are free and unlimited; Pages Functions are billed as Worker requests ([Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [Pages Functions pricing](https://developers.cloudflare.com/pages/functions/pricing/)).

The present dashboard uses native `better-sqlite3`. Workers' Node.js compatibility table lists `node:sqlite` as a non-functional stub, so this persistence/auth path is not a zero-change deployment ([Workers Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/)). Moving the dashboard to Cloudflare is feasible after replacing local SQLite with a D1/DO-compatible adapter and validating OpenNext. Whether that is preferable to Vercel remains a separate hosting decision; it is not required for the core Cloudflare fit conclusion.

## Confirmed paid-plan allowances

Workers Paid Standard has a **$5/month account minimum**, is separate from other Cloudflare plans, and includes Workers, Pages Functions, Durable Objects, D1, R2, Queues, Workflows, and initial Container usage ([Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [Containers pricing](https://developers.cloudflare.com/containers/pricing/)). Relevant included monthly usage is:

| Product | Included with Workers Paid Standard | Overage most relevant here |
| --- | ---: | ---: |
| Workers | 10M requests; 30M CPU-ms | $0.30/M requests; $0.02/M CPU-ms |
| Durable Objects | 1M requests; 400k GB-s; SQLite: 25B rows read, 50M rows written, 5 GB-month | Usage-based |
| D1 | 25B rows read; 50M rows written; 5 GB-month | Usage-based |
| Queues | 1M operations (a normal delivered message is typically 3 operations) | $0.40/M operations |
| Workflows | 500k steps; 1 GB-month state; shares Workers request/CPU allowances | Usage-based |
| R2 | 10 GB-month; 1M Class A; 10M Class B | Usage-based; Internet egress free |
| Containers | 25 GiB-hours memory; 375 vCPU-min active CPU; 200 GB-hours disk | $0.0000025/GiB-sec memory; $0.000020/vCPU-sec; $0.00000007/GB-sec disk |

Workflows step/storage billing began on 2026-08-10; idle time and time waiting on external APIs do not consume Workflow CPU ([Workflows pricing](https://developers.cloudflare.com/workflows/reference/pricing/)). The small three-user control plane is expected to stay well below the non-container allowances, but that is an assumption until measured and those allowances may be shared with other applications on the account.

## Cost calculations and assumptions

These are calculations from the published Container rates, not Cloudflare guarantees. A month is modeled as 720 hours. They exclude taxes and any overage from Workers, DO, D1, R2, Queues, Workflows, or network regions; they also assume the account's included usage is otherwise unused.

The `lite` shape is 0.25 GiB memory, 1/16 vCPU, and 2 GB disk. Its memory, disk, and fully saturated CPU inclusions each cover exactly **100 aggregate active instance-hours/month**:

- memory: 25 GiB-hours / 0.25 GiB = 100 hours;
- disk: 200 GB-hours / 2 GB = 100 hours;
- CPU: 375 vCPU-min / (1/16 × 60) = 100 fully busy hours.

Across three owners, that is about 33.3 active container-hours per owner per month before a container overage. The default ten-minute idle delay can itself consume up to 1/6 hour per wake-up, so 100 aggregate hours corresponds to at most 600 fully separated wake windows before actual turn duration is counted. Shorter sleep timing and clustering adjacent work reduce idle waste.

| Scenario | Base + memory + disk | If CPU is saturated for every active second |
| --- | ---: | ---: |
| 75 aggregate `lite` hours | $5.00 | $5.00 |
| 100 aggregate `lite` hours | $5.00 | $5.00 |
| 150 aggregate `lite` hours | about $5.14 | about $5.36 |
| 300 aggregate `lite` hours | about $5.55 | about $6.45 |
| Three `lite` instances always on (2,160 aggregate hours) | about $10.67 | about $19.94 |

If `lite` is too small and Tomo requires the 1 GiB/4 GB `basic` shape, the included memory and disk cover only 25 aggregate instance-hours. Three always-on `basic` containers are approximately $26.34 before CPU and approximately $64.77 at full CPU. This makes the `lite` memory/performance test the pivotal cost experiment.

The $5 target is therefore achievable for low internal usage with scale-to-zero execution; it is not credible for three always-on runtimes. Published pricing automatically introduces overage charges after inclusions, and this research did not confirm an account-level hard spend cap. Instrument active container hours, CPU, memory, wake-ups, Worker/DO requests, Workflow steps, R2 storage/operations, and shared account consumption; alert before the included container thresholds.

## Unknown account entitlements and usage

The following cannot be inferred from “I have a $5/month Cloudflare subscription” and must be checked in the account:

- whether that subscription is **Workers Paid Standard**, rather than a zone/domain plan such as Pro; Workers Paid is a separate subscription;
- whether Containers, Sandbox, R2, Workflows, D1, DO, and Queues are enabled in the target account/region and whether any onboarding or billing setup remains;
- whether other projects already consume the account-level included Workers, DO, D1, R2, Queue, Workflow, or Container usage;
- the account's actual billing controls and whether a hard spend ceiling exists;
- whether Tomo's current Docker image fits `lite` (256 MiB), its cold-start behavior, and the measured active time/CPU for real model turns;
- outbound model-provider, Telegram, and any other third-party charges, which are outside the Cloudflare-only calculation.

## Decisions and fog exposed

1. **Execution runtime:** adopt one scale-to-zero Sandbox/Container per owner for the first migration, or fund a larger rewrite to Python Workers/TypeScript. The former is the recommended migration path; `lite` fit remains fog.
2. **Canonical personal store:** choose owner DO SQLite (recommended for locality and fencing), owner D1, or a temporary container-SQLite-plus-R2 checkpoint design. The last option has weaker transactional durability during runtime replacement.
3. **Long-turn protocol:** define Workflow step boundaries, signed container callbacks, persisted progress events, cancellation, retry, generation checks, and exactly-once-visible Telegram delivery.
4. **Scheduling:** map the existing cron lease/revision semantics onto one per-owner DO alarm and specify overdue, retry, timezone, and schedule-edit behavior.
5. **OAuth custody:** decide the envelope-encryption/key-rotation scheme, refresh transaction, audit/redaction policy, and short-lived credential injection boundary.
6. **Dashboard:** choose Cloudflare Workers/OpenNext after replacing native SQLite, or Vercel with a remote state adapter. This choice does not block the backend migration.
7. **Cost proof:** run three real concurrent owner turns on `lite`; measure RSS, cold start, active CPU/time, streaming, idle sleep, restore, image processing, and monthly wake frequency before promising a $5 ceiling.
8. **Account proof:** verify Workers Paid Standard and current shared usage/entitlements in the Cloudflare dashboard before provisioning.

## Resolution

Resolve the research ticket as **conditional yes**: Cloudflare has the primitives needed for Tomo and can target the existing $5 Workers Paid minimum at three low-activity internal users, using a Worker + per-owner DO + Workflow + scale-to-zero Sandbox/Container + R2 architecture. Do not claim a hard $5 cap. The two gates for migration planning are (1) confirming the subscription is Workers Paid Standard and (2) proving the packaged Python runtime fits the `lite` container with aggregate active time near or below 100 hours/month.
