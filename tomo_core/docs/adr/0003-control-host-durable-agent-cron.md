# ADR 0003: Control-host durable agent cron jobs

- Status: accepted
- Date: 2026-07-17

## Context

Scheduled work must survive control-host restarts and replaceable Daytona sandboxes without turning a user intent into a frozen notification string. A due occurrence must wake Tomo with current personal context, while execution and Telegram delivery remain independently retryable and owner-scoped.

## Decision

The always-on control host owns scheduling authority. `<TOMO_CORE_DATA_DIR>/cron.sqlite3` stores job definitions, schedule anchors, immutable run occurrences, leases, retry state, delivery attempts, receipts, lifecycle state, and content-free terminal tombstones. Sandboxes never open this database.

Each occurrence follows this path:

```text
due schedule
-> control-host run claim and lease
-> owner/chat-bound automation generation reservation
-> first-class automation turn in the owner's runtime
-> durable RunOutcome and ordered delivery rows
-> separately claimed Telegram sends
-> receipt, delivery retry, or terminal uncertain delivery
-> next occurrence or terminal tombstone
```

Shared-gateway startup owns one bounded-cadence scheduler lane, independent of Telegram worker traffic, and joins it during shutdown. SQLite leases allow recovery after process death. Missed occurrences coalesce, and only one active run per job is allowed.

## Scheduling and lifecycle

Schedules are a UTC one-time timestamp, a fixed interval, or a timezone-aware standard five-field cron expression. Cron day-of-month and day-of-week use standard OR behavior when both are restricted. Ambiguous local minutes fire once; nonexistent local minutes are skipped.

Jobs may be one-shot, indefinite, or bounded by an end time and/or successful-run count. A normal final run receives `will_end_after_run`; expiry that has no normal occurrence creates one lifecycle automation run. Execution exhaustion does not disable an otherwise recurring job. Cancellation and terminal delivery remove intent, constraints, destination, and output while retaining IDs, timestamps, statuses, attempts, and dedupe evidence.

Job revisions and per-claim opaque lease tokens fence execution and delivery. Update, pause, resume, cancellation, or replacement invalidates work captured under an older revision. A delivery admission and a revision mutation are linearized in SQLite: after a send enters `sending`, revision mutation must retry after that send reaches a terminal state; if mutation commits first, send admission fails. New user input also supersedes an active automation generation; unsent frames are discarded and the occurrence is deferred without consuming an execution attempt.

## Trust and safety boundary

Interactive turns receive a short-lived HMAC capability bound to the verified Tomo owner, Telegram actor, destination, and session. The key is exactly 32 bytes, created atomically with mode `0600` at `<TOMO_CORE_DATA_DIR>/cron-capability.key`. Hosted startup requires an HTTPS `TOMO_CONTROL_PUBLIC_URL`, or derives it from `RAILWAY_PUBLIC_DOMAIN`. Local mode defaults to `http://127.0.0.1:8787`.

The control API compares request owner, actor, destination, and session context with verified claims before owner-scoped work. Request/response sizes are bounded, capability tokens are redacted from sandbox diagnostics, and the dashboard API key cannot substitute for an owner capability. Automation turns receive no cron mutation capability. Their tool registry is restricted to explicitly unattended-safe operations; approval-gated or consequential operations stop with an argument-free approval-needed outcome rather than guess or act.

Job context is rebuilt for every run from intent, relevant current memories, previous outcome/progress, current time, and available owner-authorized connections. Mutable domain facts are not copied into cron state. Connected facts must retain source, observation time, freshness/confidence, and uncertainty.

## Telegram delivery

Execution may produce at most three ordered text frames. The control host stores them before any send. Delivery has its own lease, attempt counter, backoff, and terminal status, so Telegram failure never repeats successful agent execution. Definite rejection can retry independently; a timeout or response ambiguity that may have followed an accepted Telegram send becomes terminal `unknown` and is never blindly replayed. Later frames become eligible only after the configured `TOMO_TELEGRAM_DELIVERY_PACE_SECONDS` delay. Sends target the stored originating chat without reply-to metadata and revalidate its current Tomo binding.

## User controls

Owner-scoped tools and API routes support create, list, inspect, update, run now, pause, resume, cancel/delete, and history. Mutation idempotency is derived from generation identity, tool name, and canonical arguments.

## Consequences

- Restart recovery is durable on the control host; Daytona remains replaceable execution infrastructure.
- SQLite leases support the current singleton deployment and defend against duplicate workers, but this is not a distributed scheduler.
- Dashboard UI, arbitrary destinations, registered scripts/webhooks, replay of every missed occurrence, and domain-specific medication/inventory state remain deferred.
- Deploying Daytona code changes still requires a new immutable snapshot and separately approved rollout.
