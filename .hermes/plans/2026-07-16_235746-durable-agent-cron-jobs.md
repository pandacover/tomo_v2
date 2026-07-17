# Durable Agent Cron Jobs Implementation Plan

> **For Hermes:** implement this plan task-by-task with TDD, delegate bounded phases through OpenCode, then independently review public contracts, transactions, and the full suite.

**Goal:** add owner-scoped durable scheduled/recurring agent jobs that survive control-host and Daytona replacement, execute as explicit automation turns, deliver back to the originating Telegram conversation, and end safely according to lifecycle policy.

**Architecture:** the control host owns `cron.sqlite3`, schedule calculation, occurrence identity, leases, retries, revision fences, and delivery state. The existing shared Telegram gateway hosts one scheduler worker and routes claimed runs through the same per-owner Daytona dispatch and session-revision lane as interactive turns. Sandboxes receive a first-class automation source and short-lived owner-scoped capability for interactive cron tools; they never receive the dashboard API key or write the control-host database directly.

**Tech stack:** Python 3.11 stdlib (`sqlite3`, `zoneinfo`, `hmac`, `threading`), FastAPI/httpx, existing `ToolRegistry`, sandbox protocol, Daytona dispatch, Telegram gateway, `unittest`.

---

## Locked v1 behavior

- Every user-created job invokes Tomo. Deterministic messages are control notices only.
- Schedules: one-time UTC timestamp, fixed interval, or timezone-aware five-field cron expression.
- Timezone defaults to the creating context; cron matching uses IANA `zoneinfo`. Ambiguous local minutes fire once; nonexistent local minutes are skipped.
- Downtime coalesces missed occurrences into at most one catch-up run.
- One active run per job. Due ticks coalesce while that run is pending/leased/executing/delivering.
- Execution retries are bounded (3 attempts with persisted backoff); a recurring definition remains active after an exhausted run.
- Delivery retries are persisted separately (5 attempts with backoff).
- Lifecycles: one-shot, indefinite, and bounded by `ends_at` and/or `max_successful_runs`.
- A final normal run carries `will_end_after_run=true`; expiry without a normal run creates a lifecycle automation run. Permanent agent failure uses a control notice.
- Delete/cancel/update increments the job revision. A run captures that revision; stale output cannot deliver.
- “Self-destruct” means ended/tombstoned plus content-bounded audit after terminal delivery, not loss of dedupe evidence.
- Originating Telegram chat/session is the only v1 destination.
- Personal facts come from fresh runtime context/connections, not duplicated cron payload state.
- Automation turns get the unattended registry. Destructive or approval-gated capabilities are withheld; they must return an approval-needed outcome once such capabilities exist.

## Public domain contracts

`CronJob`, `JobIntent`, `ScheduleSpec`, `LifecyclePolicy`, `JobRun`, `RunOutcome`, `DeliveryAttempt`, `ControlNotice`, `AutomationTurn`, `JobProgress`.

Statuses:

```text
job: active | paused | ended | cancelled
run: pending | leased | executing | succeeded | retry_wait | failed | invalidated
delivery: pending | leased | delivered | retry_wait | exhausted | suppressed
```

## Task 1: schedule and domain model

**Files**
- Create `tomo_core/src/tomo_core/cron_models.py`
- Create `tomo_core/src/tomo_core/cron_schedule.py`
- Create `tomo_core/tests/test_cron_schedule.py`
- Create `tomo_core/tests/test_cron_models.py`

**TDD steps**
1. Write failing validation tests for IDs, owner/destination, intent bounds, lifecycle combinations, timezone names, and immutable run revision.
2. Write failing schedule tests for one-shot, interval, cron ranges/lists/steps, next occurrence, DST fold/gap, and catch-up coalescing.
3. Run `python -m unittest tests.test_cron_models tests.test_cron_schedule -v`; confirm red.
4. Implement frozen dataclasses/enums and a dependency-free five-field cron parser.
5. Re-run focused tests; confirm green.

## Task 2: durable repository and state machine

**Files**
- Create `tomo_core/src/tomo_core/cron_store.py`
- Create `tomo_core/tests/test_cron_store.py`

**Required repository methods**

```text
create/list/get/update/pause/resume/cancel job
request_manual_run
claim_due_run/renew_run_lease/complete_run/fail_run
claim_delivery/complete_delivery/fail_delivery
history/recover_expired_leases/redact_terminal_job
```

**TDD steps**
1. Test restart persistence, owner isolation, create idempotency, revision CAS, and list/history ordering.
2. Test duplicate ticks create one `(job_id, scheduled_for, trigger)` occurrence.
3. Test lease exclusivity and expired lease recovery across two repository instances.
4. Test pause/resume catch-up, manual run without schedule advancement, success counting, bounded lifecycle, lifecycle-only expiry, and one-shot ending.
5. Test stale revision suppression after update/cancel and terminal redaction only after delivery terminality.
6. Implement schema creation/migrations and transaction boundaries using `BEGIN IMMEDIATE`, WAL, foreign keys, busy timeout, and UTC ISO timestamps.
7. Run `python -m unittest tests.test_cron_store -v`.

## Task 3: honest mutating tool execution

**Files**
- Modify `tomo_core/src/tomo_core/tools.py`
- Modify `tomo_core/src/tomo_core/tool_execution.py`
- Modify `tomo_core/tests/test_tools.py`
- Modify `tomo_core/tests/test_tool_execution.py`
- Modify `tomo_core/src/tomo_core/conversation/prompts.py` and its tests if capability wording is hard-coded

**TDD steps**
1. Replace the blanket registry rejection test with policy tests: read-only parallel batches remain concurrent; any mutating or serial tool must be the only call in a batch.
2. Prove mixed/multiple mutating batches invoke zero tools and return a stable validation code.
3. Prove cancellation checks happen before and after a serial side effect and the exact prepared call is executed once.
4. Extend `ToolSpec` with explicit approval/unattended metadata only where needed; do not mark cron mutations read-only.
5. Keep personal search behavior unchanged and run focused tool/conversation tests.

## Task 4: owner-scoped cron capability API and sandbox tools

**Files**
- Create `tomo_core/src/tomo_core/cron_capability.py`
- Create `tomo_core/src/tomo_core/cron_api.py`
- Create `tomo_core/src/tomo_core/cron_tools.py`
- Modify `tomo_core/src/tomo_core/control_api.py`
- Modify `tomo_core/src/tomo_core/sandbox_dispatch.py`
- Modify `tomo_core/src/tomo_core/sandbox_inbound.py`
- Modify `tomo_core/src/tomo_core/runtime.py`
- Create `tomo_core/tests/test_cron_capability.py`
- Create `tomo_core/tests/test_cron_api.py`
- Create `tomo_core/tests/test_cron_tools.py`

**Contract**
- A `0600` HMAC key is atomically created under the control data directory and shared by the control API and gateway processes.
- Short-lived signed claims bind owner, originating actor/chat/session, allowed cron operations, and expiry.
- The control URL comes from explicit hosted config/public-domain derivation; fail closed when unavailable.
- API routes never accept owner/destination authority from model arguments.
- Tool names: `cron_create`, `cron_list`, `cron_inspect`, `cron_pause`, `cron_resume`, `cron_run_now`, `cron_update`, `cron_delete`, `cron_history`.
- Tool-call idempotency derives from generation ID + tool name + canonical arguments, so provider repairs cannot duplicate mutations.
- Normal interactive turns receive cron tools. Automation turns receive only unattended-safe tools, excluding cron mutations.

**Tests**
- tamper/expiry/cross-owner/cross-chat rejection; no key/token logging;
- every mutation is owner-scoped and idempotent;
- actual runtime registry contains cron tools on interactive hosted turns and withholds them on automation turns;
- control API dashboard key cannot substitute for an owner capability and vice versa.

## Task 5: first-class automation turn and conversation continuity

**Files**
- Modify `tomo_core/src/tomo_core/models.py`
- Modify `tomo_core/src/tomo_core/sessions.py`
- Modify `tomo_core/src/tomo_core/sqlite_personal_data.py`
- Modify `tomo_core/src/tomo_core/sandbox_protocol.py`
- Modify `tomo_core/src/tomo_core/sandbox_inbound.py`
- Modify `tomo_core/src/tomo_core/runtime.py`
- Modify `tomo_core/tests/test_sessions.py`
- Modify `tomo_core/tests/test_sqlite_personal_data.py`
- Modify `tomo_core/tests/test_sandbox_protocol.py`
- Modify `tomo_core/tests/test_sandbox_inbound.py`
- Add `tomo_core/tests/test_runtime_automation.py`

**TDD steps**
1. Add explicit `source=user|automation` plus structured automation context (`job_id`, `run_id`, due time, prior outcome, lifecycle flag) to the work protocol.
2. Migrate personal SQLite messages to permit role `automation`; persist a unique run identity.
3. Project automation records to the model as clearly labelled system-originated event data, never as a claimed user utterance.
4. Persist resulting assistant frames into the originating Telegram session so the next user reply sees them.
5. Suppress reply-to IDs, reactions, and user-authoritative memory governance for automation events.
6. Prove protocol round-trip, duplicate-run append suppression, session revision fencing, and current memory hydration.

## Task 6: scheduler orchestration, execution, and delivery ledger

**Files**
- Create `tomo_core/src/tomo_core/cron_service.py`
- Create `tomo_core/src/tomo_core/cron_worker.py`
- Create `tomo_core/tests/test_cron_service.py`
- Create `tomo_core/tests/test_cron_worker.py`

**TDD scenarios**
1. one-shot reminder: due claim → automation turn → outcome → persisted delivery → Telegram send → ended tombstone;
2. recurring health check: next occurrence advances, previous outcome enters next run context;
3. restart at due time: one catch-up only;
4. duplicate workers/ticks: one lease and one delivery;
5. sandbox replacement/failure: same run ID retries, then a control notice on exhaustion;
6. successful execution plus Telegram failure: execution is not repeated; delivery alone retries;
7. cancel/update during execution: returned frames are persisted as suppressed, never sent;
8. bounded final run and expiry-without-run each produce exactly one lifecycle-aware outcome;
9. expired run/delivery leases recover after process death.

The worker must use injected dispatch and Telegram interfaces in tests. It must not own an untracked feature-specific thread.

## Task 7: shared gateway lifecycle wiring

**Files**
- Modify `tomo_core/src/tomo_core/shared_gateway.py`
- Modify `tomo_core/src/tomo_core/onboarding_store.py` only if the existing generation lane needs explicit automation metadata
- Modify `tomo_core/src/tomo_core/telegram_router.py`
- Modify `tomo_core/src/tomo_core/hosted_config.py`
- Modify `tomo_core/src/tomo_core/cli.py`
- Modify `tomo_core/tests/test_shared_gateway.py`
- Modify `tomo_core/tests/test_onboarding_store.py`
- Modify `tomo_core/tests/test_hosted_config.py`
- Modify `tomo_core/tests/test_cli.py`

**Requirements**
- Start exactly one cron worker from the active shared Telegram gateway process; stop/join it on shutdown.
- Use the same absolute `TOMO_CORE_DATA_DIR` as the control API.
- Serialize scheduled and interactive sandbox turns per owner and preserve generation/session revision fencing.
- Store destination before scheduling; a missing route is a failed/suppressed delivery, never reported as delivered.
- Add operator CLI inspection/run-once commands without exposing tokens or raw intent across owners.
- Tests prove cross-process visibility: API-created job is claimed by a separately opened gateway repository.

## Task 8: contract, failure, and regression coverage

**Files**
- Add `tomo_core/tests/test_cron_end_to_end.py`
- Update existing protocol/gateway/control/runtime tests affected by public contracts

**Verification matrix**

| Requirement | Public evidence |
|---|---|
| survives restart/deploy | close/reopen two repository instances and claim due run |
| survives sandbox replacement | first dispatch fails, retry uses same run ID and succeeds |
| no fake user message | stored role/source assertion and provider prompt assertion |
| no duplicate occurrence/delivery | competing claims plus second poll assertions |
| owner authorization | API/tool cross-owner denial |
| separate execution/delivery | delivery retry does not increment execution attempts |
| lifecycle end ping | final and expiry-only scenario assertions |
| stale output fencing | cancel/update while dispatch blocked |
| unattended safety | automation registry excludes mutating/approval tools |
| context continuity | next interactive prompt includes prior automation assistant output |

Run focused modules after each phase, then:

```bash
cd tomo_core
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py'
python -m compileall -q src/tomo_core tests
```

## Task 9: docs and final review

**Files**
- Update `tomo_core/CONTEXT.md` only where implementation confirms the agreed vocabulary
- Create `tomo_core/docs/adr/0002-control-host-durable-agent-cron.md`
- Update `tomo_core/docs/conversation-architecture.md`
- Update `tomo_core/README.md`

Document scheduler ownership, automation source, capability trust boundary, retry/lease state machine, data paths, user controls, and the fact that external connections improve contextual accuracy later.

Run a final code review against this plan. Inspect SQL transaction/CAS ordering, lease expiry, cancellation fencing, token redaction, tool registry binding, startup/shutdown, and exact Telegram send behavior manually. Do not commit, push, snapshot, deploy, or touch unrelated dashboard/cache files unless separately requested.

## Deferred intentionally

- dashboard cron UI;
- email/Discord/arbitrary cross-chat destinations;
- deterministic registered action handlers, webhooks, scripts, and shell jobs;
- catch-up of every missed occurrence;
- authoritative medication/inventory schema;
- arbitrary external connection implementation;
- distributed scheduler beyond SQLite leases and the current singleton shared gateway deployment.
