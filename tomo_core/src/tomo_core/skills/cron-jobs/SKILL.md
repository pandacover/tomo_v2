---
name: cron-jobs
description: cron-jobs: use for creating, inspecting, changing, running, pausing, resuming, deleting, or explaining owner-scoped scheduled Tomo work.
---

# Cron Jobs

Cron jobs are durable owner-scoped intentions, not frozen notification strings. Use the exposed cron tools for scheduling authority and never simulate a schedule, tool result, or delivery.

## Tools

Use cron_create to create, cron_list to list, cron_inspect to inspect, cron_update to revise, cron_run_now to request an immediate run, cron_pause and cron_resume to control lifecycle, cron_delete to cancel/delete, and cron_history to inspect bounded run history. Inspect before retrying after an ambiguous mutation observation. The rule is simple: never claim success without a tool observation. If cron tools are unavailable, say scheduling is unavailable.

## Schedules

Accept one of these forms:

- once with an ISO timestamp in at.
- interval with positive everySeconds and optional startsAt anchor.
- delay with positive afterSeconds for a one-shot relative reminder.
- standard five-field cron expression with an IANA timezone. When both day-of-month and day-of-week are restricted, standard OR semantics apply. Ambiguous local minutes fire once and nonexistent local minutes are skipped.

Ask only for materially missing schedule, timezone, or lifecycle details. Resolve the user's local timezone whenever a civil time would otherwise be ambiguous. The timezone field belongs to cron schedules; once timestamps and interval anchors must be timezone-aware ISO timestamps. Never invent a default that changes the intended time.

For "in N" one-shot reminders, MUST use delay and never infer wall-clock time. startsAt is itself the first interval occurrence. An interval without startsAt first fires one interval after creation.

## Intent And Lifecycle

Persist the user intent and constraints, not stale mutable domain facts. Update, pause, resume, and delete require the current revision, advance it, and fence older work. Run-now requires the current revision and creates one manual occurrence without changing the job definition or revision. Use endsAt and maxSuccessfulRuns for bounded lifecycles. A job may be one-shot, indefinite, or bounded by either limit.

Due runs rebuild current context from intent, relevant current memory, previous outcome and progress, current time, and available owner-authorized connections. Automation may use only explicitly unattended-safe operations. Approval-gated or consequential work stops with an argument-free approval-needed outcome rather than guessing or acting.

## Delivery

Execution and Telegram delivery are separate durable lifecycles. Ordered text frames are persisted before sending, and later frames wait for the configured pace. Definite delivery failures may retry independently. If a timeout or response ambiguity may mean Telegram accepted the message, the outcome is unknown and must never be blindly replayed. Never create or run another occurrence to compensate for uncertain delivery, and never claim delivery without an observation that establishes it.
