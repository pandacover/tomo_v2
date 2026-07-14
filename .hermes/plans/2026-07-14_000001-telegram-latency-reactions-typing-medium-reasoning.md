# Telegram Latency, Reactions, Typing, and Medium Reasoning Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Reduce Telegram time-to-first-reply and eliminate avoidable failures by changing hosted reasoning from high to medium, making unsupported reaction intent nonfatal, keeping `typing...` active until the first successful Telegram bubble, and replacing stale-revision empty streams with a typed, safely rebased retry.

**Architecture:** Keep the four concerns at their proper boundaries. Model guidance and plan parsing own reaction intent; Telegram delivery owns the final allowlist and best-effort API call. A generation-scoped typing lease starts after durable ingress and refreshes below Telegram's five-second expiry until the first successful `sendMessage`. Stale sandbox revisions become an explicit terminal protocol event carrying only the non-sensitive current revision, allowing the host store to atomically rebase once rather than producing `invalid_result` or walking through five one-step retries.

**Tech Stack:** Python 3.11, synchronous worker threads, `httpx`, SQLite, versioned JSONL sandbox protocol, `unittest`, Railway, Daytona.

---

## Scope and measured baseline

The production trace captured on 2026-07-14 measured:

- `22.901s` Telegram-origin-to-visible-reply.
- `19.224s` from sandbox execution to first frame.
- One structured-output contract repair, which caused another provider call.
- Only `118ms` eligible queue delay and about `710ms` Daytona reconciliation/lookup/PTY setup.

This plan therefore does **not** optimize Daytona, remove debounce, weaken SQLite/checkpoint durability, or change bubble pacing. It addresses the measured model/contract costs and the separate Telegram UX gap.

Current behavior and relevant boundaries:

- `tomo_core/src/tomo_core/hosted_config.py` defaults `TOMO_XAI_REASONING_EFFORT` to `high`.
- `tomo_core/src/tomo_core/conversation/models.py` has the supported reaction set `👍 ❤️ 😂 🔥 🥰 👏 🤔 👀 🙏 🫡`.
- `tomo_core/src/tomo_core/conversation/parsing.py` currently constructs `ReactionIntent` during strict plan parsing; unsupported reactions therefore become `invalid_plan` and can trigger a full provider repair.
- `tomo_core/src/tomo_core/shared_gateway.py` sends one typing action when generation work starts. Telegram expires it after about five seconds.
- `tomo_core/src/tomo_core/telegram_router.py` knows when a message has been durably enqueued, before debounce and generation claim.
- `tomo_core/src/tomo_core/runtime.py` silently returns when `save_session(...)` rejects a stale revision.
- `tomo_core/src/tomo_core/sandbox_inbound.py` currently returns success when the runtime iterator ends without `RuntimeCompleted`, producing a stream without a terminal event.
- `tomo_core/src/tomo_core/sandbox_protocol.py` correctly rejects missing-terminal streams, but the host subsequently classifies the failure as `invalid_result` and retries without knowing the sandbox revision floor.

## Acceptance criteria

1. Hosted default and production configuration use `medium` reasoning; explicit `low`, `medium`, and `high` overrides remain valid.
2. The first-segment prompt lists the exact supported reaction set and permits `null`.
3. An unsupported, malformed, or unavailable decorative reaction never invalidates an otherwise valid plan, never triggers contract repair by itself, and never delays/discards text.
4. Valid allowlisted reactions still flow through the protocol and are attempted once; Telegram rejection remains nonfatal.
5. An installed user's durable message ingress triggers a best-effort typing action immediately after enqueue.
6. During a long generation, typing refreshes at a single named interval below five seconds, default `4.0s`.
7. Refreshing stops after the first successful Telegram `sendMessage`, and does not restart for later bubbles.
8. A failed first send does not count as first delivery; typing remains/restarts while the generation is still active.
9. Typing refresh work exits on completion, generation failure, cancellation/supersession, router shutdown, or typing API failure without leaking worker threads.
10. A stale sandbox session revision emits one explicit terminal stale event with the current numeric revision, never an empty stream.
11. The host atomically marks the stale attempt failed and sets the next host revision to at least `sandbox_current_revision + 1`, avoiding repeated one-by-one retries.
12. No new telemetry or errors contain message text, model output, memories, actor/chat/user IDs, OAuth material, credentials, reaction contents, or filesystem paths.
13. SQLite working-copy/checkpoint durability, typed Daytona not-found handling, revision fencing, and existing delivery reconciliation remain intact.

## Non-goals

- Do not remove the reaction allowlist.
- Do not promise that every allowlisted reaction is available in every Telegram chat.
- Do not add custom or paid Telegram reactions.
- Do not move active SQLite onto a Daytona named volume.
- Do not reduce model reasoning below `medium` in this change.
- Do not re-enable `TOMO_LATENCY_TRACE` during implementation unless the user explicitly authorizes another production trace.
- Do not commit, push, deploy, or build a Daytona snapshot unless the user separately authorizes execution and those side effects.

---

### Task 1: Record the baseline without disturbing the dirty worktree

**Objective:** Establish a trustworthy pre-change baseline and preserve unrelated user changes.

**Files:**
- Inspect only: repository status and relevant tests.

**Step 1: Recheck repository state**

Run from `tomo_core/`:

```bash
git status --short --branch
git diff --check
```

Expected: branch and existing modified/untracked files are visible; no files are staged or reverted by this task.

**Step 2: Run the focused baseline**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_hosted_config.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_conversation_*.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_sandbox_*.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_shared_gateway.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_telegram_router.py' -v
```

Expected: current tests pass before new red tests are added.

---

### Task 2: Change the hosted reasoning default from high to medium

**Objective:** Remove unnecessary high-reasoning latency while preserving explicit overrides.

**Files:**
- Modify: `tomo_core/src/tomo_core/hosted_config.py`
- Test: `tomo_core/tests/test_hosted_config.py`

**Step 1: Write failing default tests**

Change the local and Daytona default assertions to:

```python
self.assertEqual(config.xai_reasoning_effort, "medium")
```

Extend the override test to exercise all supported values:

```python
for effort in ("low", "medium", "high"):
    with self.subTest(effort=effort):
        config = HostedRuntimeConfig.from_env({**base, "TOMO_XAI_REASONING_EFFORT": effort})
        self.assertEqual(config.xai_reasoning_effort, effort)
```

**Step 2: Run the test to verify failure**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_hosted_config.py' -v
```

Expected: FAIL because the current fallback is `high`.

**Step 3: Make the minimal implementation**

In `HostedRuntimeConfig.from_env`, change only the fallback:

```python
xai_reasoning_effort=env.get("TOMO_XAI_REASONING_EFFORT", "medium")
```

Keep the existing validation and explicit override behavior.

**Step 4: Run the focused test**

Use the command from Step 2.

Expected: PASS.

---

### Task 3: Tell the model the exact reaction contract

**Objective:** Reduce unsupported reaction choices before parsing without making reaction selection mandatory.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Test: `tomo_core/tests/test_conversation_prompts.py`

**Step 1: Make the allowlist deterministically renderable**

Preserve one canonical ordered tuple and one membership set:

```python
REACTION_EMOJI_OPTIONS = ("👍", "❤️", "😂", "🔥", "🥰", "👏", "🤔", "👀", "🙏", "🫡")
REACTION_EMOJI_ALLOWLIST = frozenset(REACTION_EMOJI_OPTIONS)
```

No emoji is added or removed.

**Step 2: Write a failing prompt assertion**

In `test_conversation_prompts.py`, assert that the first-segment system prompt contains wording equivalent to:

```text
reaction must be exactly one of ["👍","❤️","😂","🔥","🥰","👏","🤔","👀","🙏","🫡"] or null
```

Also assert that later-segment prompts do not request another reaction or turn plan.

**Step 3: Run the prompt test to verify failure**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_conversation_prompts.py' -v
```

Expected: FAIL because the exact supported set is not currently exposed.

**Step 4: Render the canonical list into the first-segment prompt**

Import `REACTION_EMOJI_OPTIONS` and serialize it with compact JSON so prompt text and code cannot drift:

```python
reaction_options = json.dumps(REACTION_EMOJI_OPTIONS, ensure_ascii=False, separators=(",", ":"))
```

Use the generated value in the first-segment contract instruction. Keep `reaction: null` in the example.

**Step 5: Run the prompt test**

Expected: PASS.

---

### Task 4: Make unsupported reaction intent nonfatal at plan parsing

**Objective:** Preserve valid text planning when the decorative reaction field is unsupported or malformed.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`
- Test: `tomo_core/tests/test_conversation_framing.py`
- Test: `tomo_core/tests/test_conversation_parsing.py` if reaction parsing tests already live there
- Test: `tomo_core/tests/test_conversation_engine.py`

**Step 1: Write parser tests that are red today**

Cover these turn-plan values:

```python
("👍", "👍")                 # supported, retained
(None, None)                  # absent intent
("🪿", None)                 # unsupported emoji, omitted
("not-an-emoji", None)       # arbitrary text, omitted
({"emoji": "👍"}, None)      # malformed type, omitted
```

For every unsupported case, feed a valid frame after the plan and assert parsing completes without `ConversationOutputError("invalid_plan")`.

**Step 2: Write an engine-level repair regression**

Use a scripted provider whose first response contains an unsupported reaction plus a valid frame. Assert:

```python
self.assertEqual(result.usage.contract_repairs, 0)
self.assertEqual([frame.text for frame in result.frames], ["hello back."])
self.assertIsNone(result.plan.reaction)
```

This proves the fix reduces latency rather than merely moving the exception.

**Step 3: Run focused tests to verify failure**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_conversation_framing.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_conversation_engine.py' -v
```

Expected: unsupported reactions currently produce `invalid_plan` and/or a repair.

**Step 4: Normalize reaction independently from required plan fields**

Add a narrow helper in `conversation/parsing.py`:

```python
def _parse_optional_reaction(value: object) -> ReactionIntent | None:
    if value is None:
        return None
    try:
        return ReactionIntent(value)
    except (TypeError, ValueError):
        return None
```

Use it only for `payload.get("reaction")`. Keep primary move, supporting moves, sequence, goal, confidence, plan keys, and frame validation strict.

**Step 5: Run focused tests**

Expected: PASS with zero repair for reaction-only mismatch.

---

### Task 5: Preserve the Telegram-side reaction safety boundary

**Objective:** Ensure only allowlisted reactions reach Telegram and all reaction API failures remain decorative/nonfatal.

**Files:**
- Modify if needed: `tomo_core/src/tomo_core/runtime.py`
- Modify if needed: `tomo_core/src/tomo_core/sandbox_protocol.py`
- Modify: `tomo_core/src/tomo_core/shared_gateway.py`
- Test: `tomo_core/tests/test_sandbox_protocol.py`
- Test: `tomo_core/tests/test_shared_gateway.py`

**Step 1: Add boundary tests**

Assert:

- `RuntimeReactionReady` and sandbox reaction decoding continue rejecting unsupported emoji.
- A supported reaction is attempted once.
- `react_to_message` raising an HTTP or generic exception does not prevent the first text bubble.
- A stale/superseded generation does not react.

**Step 2: Run focused tests**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_sandbox_protocol.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_shared_gateway.py' -v
```

Expected: existing behavior may already satisfy part of this task; only the new explicit boundary cases should be red.

**Step 3: Keep strict transport validation**

Do not loosen `ReactionIntent` itself. The parser from Task 4 is the only place that downgrades unsupported model intent to `None`. Keep strict construction in:

```python
RuntimeReactionReady.__post_init__
encode_event(... RuntimeReactionReady ...)
_event_from_message(... reaction ...)
```

Before calling Telegram in `shared_gateway.py`, recheck membership in `REACTION_EMOJI_ALLOWLIST`; skip unsupported values without raising. Retain the existing `try/except` around the Telegram reaction API.

**Step 4: Run focused tests**

Expected: PASS; text delivery remains independent from reaction delivery.

---

### Task 6: Add a generation-scoped typing lease

**Objective:** Refresh Telegram typing safely during blocking provider/sandbox work without spreading thread lifecycle logic through the gateway.

**Files:**
- Create: `tomo_core/src/tomo_core/typing_status.py`
- Create: `tomo_core/tests/test_typing_status.py`
- Modify: `tomo_core/src/tomo_core/telegram_bot.py`

**Step 1: Write deterministic failing lease tests**

Test a `TypingLease` using `threading.Event` barriers, not real four-second sleeps. Required cases:

- `start()` performs one immediate best-effort pulse.
- A controlled timer wake causes another pulse.
- a pulse exception is swallowed and terminates or suppresses further pulses without escaping to generation work.
- `close()` is idempotent and joins the heartbeat thread.
- no pulse begins after `close()` returns.
- `pause_for_first_delivery()` fences new pulses before the first `sendMessage` attempt.
- `resume_after_failed_delivery()` sends/restarts typing only while the generation remains active.

Expose dependencies for tests rather than patching `time.sleep` globally:

```python
class TypingLease:
    def __init__(
        self,
        send_typing: Callable[[str], None],
        actor_id: str,
        *,
        interval_seconds: float = 4.0,
        is_active: Callable[[], bool] = lambda: True,
        wait: Callable[[threading.Event, float], bool] | None = None,
    ) -> None: ...
```

Use a named constant:

```python
TELEGRAM_TYPING_REFRESH_SECONDS = 4.0
```

**Step 2: Run the new test to verify failure**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_typing_status.py' -v
```

Expected: FAIL because the module does not exist.

**Step 3: Implement the minimal lease**

Implementation requirements:

- one daemon heartbeat thread per active generation;
- one `threading.Event` for closure;
- one lock/condition that tracks an in-flight pulse;
- `start()` is idempotent;
- each loop checks both the close event and `is_active()` before calling Telegram;
- exceptions from `send_typing` are swallowed and do not call user/model code;
- `close()` signals first, then joins with a bounded timeout;
- do not log actor/chat IDs or exception text.

The first-delivery fence should prevent a typing request from being launched after delivery begins. If the first send fails, resume only after confirming `is_active()`.

**Step 4: Bound typing API calls**

In `TelegramBotClient.send_typing`, set a short explicit HTTP timeout for this best-effort action so lease cleanup cannot wait on the general Telegram timeout indefinitely:

```python
httpx.post(..., timeout=2.0).raise_for_status()
```

Do not change message-send timeout semantics in this task.

**Step 5: Run the lease tests repeatedly**

```bash
for i in 1 2 3 4 5; do uv run --no-sync python -m unittest discover -s tests -p 'test_typing_status.py' -v || exit 1; done
```

Expected: all runs pass without timing flakes or live threads.

---

### Task 7: Start typing immediately after durable Telegram ingress

**Objective:** Cover debounce and queue time rather than waiting for the generation worker to claim the message.

**Files:**
- Modify: `tomo_core/src/tomo_core/telegram_router.py:112-148`
- Modify: `tomo_core/tests/test_telegram_router.py`

**Step 1: Extend the router fake client**

Add recording and optional failure support:

```python
self.typing_actor_ids = []
self.typing_error = None

def send_typing(self, actor_id):
    self.typing_actor_ids.append(actor_id)
    if self.typing_error:
        raise self.typing_error
```

**Step 2: Add failing ingress tests**

Assert:

- an installed normal message calls `send_typing(chat_id)` only after `EnqueueResult.enqueued` is true;
- duplicate update delivery does not create another initial pulse;
- uninstalled messages routed to control work do not pulse;
- `/start`, callback queries, and ignored groups do not pulse;
- a typing API exception does not prevent offset advancement after durable enqueue;
- superseding user messages may issue a new immediate pulse for the new revision.

**Step 3: Run the router test to verify failure**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_telegram_router.py' -v
```

Expected: new typing assertions fail.

**Step 4: Add best-effort ingress typing**

Immediately after `enqueue_update(...)` returns:

```python
if kind == "message" and result.enqueued:
    try:
        self.client.send_typing(compact.chat_id)
    except Exception:
        pass
```

Do not pulse before the durable write, and do not let a chat-action failure affect acknowledgement.

**Step 5: Run the router test**

Expected: PASS.

---

### Task 8: Keep typing active until the first successful bubble

**Objective:** Tie the heartbeat to generation ownership and actual Telegram delivery semantics.

**Files:**
- Modify: `tomo_core/src/tomo_core/shared_gateway.py:150-280`
- Modify: `tomo_core/tests/test_shared_gateway.py`

**Step 1: Add lifecycle tests using events**

Add generation-work tests that block the fake dispatch iterator and assert:

- the lease starts when active work begins;
- at least one refresh occurs while no frame has arrived;
- the first successful `send_message` closes the lease;
- later bubbles do not restart it;
- a failed first send does not set `first_send_accepted` and permits typing to resume;
- dispatch error, empty/terminal error, inactive generation, and supersession close the lease;
- `process_update` does not return while its lease thread remains alive;
- stale generation A closing cannot close or disable generation B's lease.

Do not assert exact wall-clock durations. Drive lease wakeups with injected events or a lease factory.

**Step 2: Run the gateway tests to verify failure**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_shared_gateway.py' -v
```

Expected: current one-shot typing behavior fails refresh/cleanup assertions.

**Step 3: Inject a lease factory**

Add an optional constructor dependency for tests while preserving the production default:

```python
typing_lease_factory: Callable[..., TypingLease] = TypingLease
```

**Step 4: Own the lease in `_process_generation_work`**

Use this lifecycle:

```python
lease = self.typing_lease_factory(
    self.client.send_typing,
    work.chat_id,
    is_active=lambda: self.store.is_generation_active(work.generation_id, work.revision),
)
lease.start()
try:
    # iterate reaction/frame/terminal events
    # immediately before first Telegram send attempt: lease.pause_for_first_delivery()
    # on send exception and active generation: lease.resume_after_failed_delivery()
    # on successful first send: lease.close(); first_send_accepted = True
finally:
    lease.close()
```

The success boundary is `TelegramBotClient.send_message` returning successfully, not delivery reservation, send attempt, sandbox completion, or SQLite acknowledgement.

**Step 5: Preserve first-send telemetry semantics**

Keep the existing distinction among:

- `first_send_attempted`;
- `first_send_accepted`;
- `first_frame_delivered`/acknowledged.

Do not add message content or IDs to traces. Typing itself does not need production telemetry.

**Step 6: Run gateway and router tests repeatedly**

Expected: PASS with no flakiness.

---

### Task 9: Represent stale sandbox revisions as an explicit terminal event

**Objective:** Remove the empty-stream `invalid_result` path and carry the revision floor needed for safe recovery.

**Files:**
- Modify: `tomo_core/src/tomo_core/personal_data.py`
- Modify: `tomo_core/src/tomo_core/sqlite_personal_data.py`
- Modify: any in-memory/fake `PersonalDataStore` implementations found by symbol search
- Modify: `tomo_core/src/tomo_core/runtime.py:131-150`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py:56-101`
- Modify: `tomo_core/src/tomo_core/sandbox_protocol.py`
- Test: `tomo_core/tests/test_sqlite_personal_data.py`
- Test: `tomo_core/tests/test_runtime.py`
- Test: `tomo_core/tests/test_sandbox_inbound.py`
- Test: `tomo_core/tests/test_sandbox_protocol.py`

**Step 1: Add a read-only session revision query**

Define a store method with no content exposure:

```python
def current_session_revision(self, owner_id: str, session_key: str) -> int | None: ...
```

SQLite implementation should select only `sessions.current_revision` for the owner/session pair. Add tests for missing session, initialized session, and advanced revision.

**Step 2: Introduce a typed internal stale outcome**

In `runtime.py`:

```python
class StaleSessionRevisionError(RuntimeError):
    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__("stale_session_revision")
```

When the initial `save_session(...)` returns `False`, query the current revision and raise this typed error. If the revision cannot be read, raise a generic safe runtime failure rather than guessing.

**Step 3: Write red runtime tests**

Assert stale save rejection raises the typed outcome containing only the numeric current revision and emits no model/provider calls, frames, reactions, or memory changes.

**Step 4: Add a protocol-v5 terminal stale event**

Define:

```python
@dataclass(frozen=True)
class SandboxStaleEvent:
    sequence: int
    current_revision: int
```

Bump `PROTOCOL_VERSION` to `5`, continue accepting legacy event protocol versions `2`, `3`, and `4`, and add an event payload:

```json
{"version":5,"type":"stale","request_id":"...","generation_id":"...","sequence":0,"current_revision":12}
```

Validation requirements:

- exact keys only;
- non-negative integer revision, booleans rejected;
- event is terminal;
- stale cannot follow completion/error or be followed by another event;
- version 4 never accepts `stale`;
- no owner/session/chat/message data crosses this event.

**Step 5: Run protocol tests to verify failure, then implement**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_sandbox_protocol.py' -v
```

Expected before implementation: FAIL because v5/stale is unknown. Expected after implementation: PASS while legacy fixtures remain readable.

**Step 6: Emit stale from the sandbox entrypoint**

Catch `StaleSessionRevisionError` before the generic exception handler and write exactly one `SandboxStaleEvent` at the next sequence. Return a non-error process status after emitting the terminal event; the host event determines retry policy.

Also change the generic iterator-exhaustion path: if no `RuntimeCompleted` or typed terminal event was emitted, emit/raise a safe `runtime_missing_terminal` error instead of returning `0` with an invalid stream.

**Step 7: Replace the old cancellation test expectation**

The existing `test_cancellation_after_frames_does_not_synthesize_completion` currently expects a missing-terminal parse failure. Replace it with an assertion that every `run_once` path emits exactly one explicit terminal outcome. Do not synthesize a successful completion from partial frames.

**Step 8: Run all focused storage/runtime/protocol tests**

Expected: PASS.

---

### Task 10: Atomically rebase stale work instead of retrying one revision at a time

**Objective:** Convert `SandboxStaleEvent` into one bounded host retry above the sandbox revision floor.

**Files:**
- Modify: `tomo_core/src/tomo_core/sandbox_dispatch.py`
- Modify: `tomo_core/src/tomo_core/shared_gateway.py`
- Modify: `tomo_core/src/tomo_core/telegram_router.py`
- Modify: `tomo_core/src/tomo_core/onboarding_store.py:482-536`
- Test: `tomo_core/tests/test_sandbox_dispatch.py`
- Test: `tomo_core/tests/test_shared_gateway.py`
- Test: `tomo_core/tests/test_telegram_router.py`
- Test: `tomo_core/tests/test_onboarding_store.py`

**Step 1: Add a typed host retry error**

Extend the existing retry error without embedding identifiers:

```python
class StaleRevisionTelegramUpdateError(RetryableTelegramUpdateError):
    def __init__(self, current_revision: int) -> None:
        super().__init__("stale_session_revision")
        self.current_revision = current_revision
```

**Step 2: Write red gateway/dispatch tests**

Assert a parsed `SandboxStaleEvent(current_revision=12)`:

- sends no Telegram bubble or reaction;
- closes the typing lease;
- raises `StaleRevisionTelegramUpdateError(12)`;
- is not converted to `invalid_result`;
- is not logged with identifiers or content.

**Step 3: Extend `fail_generation` with an optional revision floor**

Add:

```python
def fail_generation(..., minimum_next_revision: int | None = None) -> bool:
```

Inside the existing `BEGIN IMMEDIATE` transaction, when scheduling a retry set:

```python
next_revision = max(int(turn["revision"]) + 1, minimum_next_revision or 0)
```

Then update `telegram_chat_turns.revision = next_revision`, clear `active_generation_id`, and preserve the existing retry timing. Validate that the floor is a non-negative integer and reject booleans.

Do not mutate an inactive/superseded generation and do not lower a newer host revision.

**Step 4: Add store tests**

Cover:

- host revision `3`, sandbox current `12` -> next revision `13`;
- host revision already `20`, sandbox current `12` -> next revision `21`;
- stale result for an inactive generation changes nothing;
- normal retry behavior remains `revision + 1`;
- max-attempt terminal behavior still completes the inbox rather than reopening it.

**Step 5: Teach the router the typed retry**

In `process_next`, catch `StaleRevisionTelegramUpdateError` before the base retry error and call:

```python
self.store.fail_generation(
    work.generation_id,
    exc.error_code,
    now=now,
    max_attempts=self.max_attempts,
    minimum_next_revision=exc.current_revision + 1,
)
```

The next `claim_next_work` should create one generation at the rebased revision.

**Step 6: Add an end-to-end host retry test**

Simulate stale current revision `12`, then a successful second sandbox stream. Assert exactly two generation attempts, revisions `1` then `13`, one visible text delivery, no duplicate reaction, and no `invalid_result`.

**Step 7: Run focused tests**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_onboarding_store.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_telegram_router.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_sandbox_dispatch.py' -v
uv run --no-sync python -m unittest discover -s tests -p 'test_shared_gateway.py' -v
```

Expected: PASS.

---

### Task 11: Run complete regression and package verification

**Objective:** Prove the combined change preserves all current behavior and packages every sandbox-side file.

**Files:**
- Verify: all changed source and tests.
- Inspect: `tomo_core/Dockerfile.daytona`, package manifest/build configuration.

**Step 1: Run syntax/diff checks**

```bash
git diff --check
uv run --no-sync python -m compileall -q src tests
```

Expected: exit `0`.

**Step 2: Run the complete test suite**

```bash
uv run --no-sync python -m unittest discover -s tests -p 'test_*.py'
```

Expected: all tests pass; baseline was `390/390`, and the total should increase by the new tests.

**Step 3: Run concurrency-sensitive tests repeatedly**

```bash
for i in 1 2 3 4 5; do
  uv run --no-sync python -m unittest discover -s tests -p 'test_typing_status.py' || exit 1
  uv run --no-sync python -m unittest discover -s tests -p 'test_shared_gateway.py' || exit 1
done
```

Expected: all ten invocations pass with no hang.

**Step 4: Build and inspect the wheel**

Use the repository's existing build command, currently expected to be:

```bash
uv build
```

Inspect the archive and verify the new `typing_status.py` plus changed sandbox protocol/runtime modules are present exactly once. Do not publish.

**Step 5: Review only the intended diff**

```bash
git diff -- src/tomo_core tests
```

Confirm no credentials, content-bearing logs, unrelated refactors, or formatting churn entered the diff.

**Step 6: Optional commit checkpoint**

Only if the user explicitly requests commits:

```bash
git add <exact changed files>
git commit -m "fix: improve telegram response latency and typing"
```

Do not stage unrelated dirty-worktree files.

---

### Task 12: Perform a compatibility-safe Daytona and Railway rollout

**Objective:** Deploy host and sandbox protocol changes without an old/new protocol mismatch, then verify real Telegram UX.

**Files / systems:**
- `tomo_core/Dockerfile.daytona`
- Daytona immutable snapshot
- Railway `control-api` service variables/deployment
- Production health endpoint: `https://core-production-535d.up.railway.app/v1/health`

**Step 1: Confirm deployment authorization and sensitive boundaries**

Before login, snapshot creation, Railway variable writes, or deployment, explain:

- purpose: release the tested host/sandbox protocol and UX changes;
- read scope: deployment metadata, service logs, health status;
- write scope: one immutable Daytona snapshot, `TOMO_DAYTONA_SNAPSHOT`, `TOMO_XAI_REASONING_EFFORT=medium`, and Railway redeployment;
- sensitive boundary: never print API keys, OAuth JSON, Telegram token, user/chat IDs, message text, memories, or reaction contents.

Do not proceed without user authorization.

**Step 2: Build a new immutable Daytona snapshot**

The snapshot must include the protocol-v5, runtime, parsing, and prompt changes because the sandbox runs those modules. Name it from the authorized commit, for example:

```text
tomo-core-<short-commit>
```

Verify snapshot state is `active` before changing Railway.

**Step 3: Apply production variables together**

Set:

```text
TOMO_DAYTONA_SNAPSHOT=tomo-core-<short-commit>
TOMO_XAI_REASONING_EFFORT=medium
```

Do not rely only on the new code default because production currently has an explicit `high` override.

**Step 4: Redeploy and verify metadata**

Verify the deployment reaches `SUCCESS` and references the intended commit and snapshot. Avoid the previously unreliable local watcher loop; use direct status checks.

**Step 5: Verify health**

```bash
curl -fsS https://core-production-535d.up.railway.app/v1/health
```

Expected: HTTP `200` and `{"ok":"true"}`.

**Step 6: Run an isolated snapshot smoke test**

In a temporary sandbox, run one static/scripted turn and verify:

- protocol v5 completion parses;
- unsupported reaction becomes `null` without repair;
- stale revision emits one terminal stale event;
- no Telegram API is called from the sandbox;
- exit/result is successful as appropriate.

Delete only the temporary sandbox afterward; do not touch named production user volumes.

**Step 7: Reconcile existing sandboxes**

Allow the established next-message reconciliation path to update existing user sandboxes while retaining their volumes. Verify the active snapshot marker and health after one controlled turn.

**Step 8: Live Telegram acceptance test**

With an authorized test account/message:

1. Send a normal installed-user message.
2. Observe typing begins after ingress, remains continuously visible through a turn longer than five seconds, and disappears when the first bubble arrives.
3. Confirm later bubbles do not restart typing.
4. Send a message likely to produce no reaction; text must arrive normally.
5. Exercise a supported reaction if possible; rejection by chat policy must not affect text.
6. Confirm logs contain no `invalid_plan` for reaction-only mismatch, no empty-stream `invalid_result`, and no sensitive values.

Keep `TOMO_LATENCY_TRACE=0`. Measure visible wall-clock time externally; do not claim a numerical speedup without a fresh authorized trace or benchmark.

**Step 9: Rollback criteria**

Rollback to the previous snapshot/config if any of these occur:

- protocol parse errors between host and sandbox;
- duplicate or missing Telegram text;
- typing continues after first delivery or leaks threads;
- stale retries loop or revisions decrease;
- checkpoint validation/reconciliation regresses;
- health becomes non-200.

Rollback variables:

```text
TOMO_DAYTONA_SNAPSHOT=tomo-core-bc7227c
TOMO_XAI_REASONING_EFFORT=high
```

Redeploy and reverify health. Keep the database and named volumes unchanged.

---

## Files likely to change

Production code:

- `tomo_core/src/tomo_core/hosted_config.py`
- `tomo_core/src/tomo_core/conversation/models.py`
- `tomo_core/src/tomo_core/conversation/prompts.py`
- `tomo_core/src/tomo_core/conversation/parsing.py`
- `tomo_core/src/tomo_core/runtime.py`
- `tomo_core/src/tomo_core/personal_data.py`
- `tomo_core/src/tomo_core/sqlite_personal_data.py`
- `tomo_core/src/tomo_core/typing_status.py` (new)
- `tomo_core/src/tomo_core/telegram_bot.py`
- `tomo_core/src/tomo_core/telegram_router.py`
- `tomo_core/src/tomo_core/shared_gateway.py`
- `tomo_core/src/tomo_core/sandbox_inbound.py`
- `tomo_core/src/tomo_core/sandbox_protocol.py`
- `tomo_core/src/tomo_core/sandbox_dispatch.py`
- `tomo_core/src/tomo_core/onboarding_store.py`

Tests:

- `tomo_core/tests/test_hosted_config.py`
- `tomo_core/tests/test_conversation_prompts.py`
- `tomo_core/tests/test_conversation_framing.py`
- `tomo_core/tests/test_conversation_engine.py`
- `tomo_core/tests/test_runtime.py`
- `tomo_core/tests/test_sqlite_personal_data.py`
- `tomo_core/tests/test_typing_status.py` (new)
- `tomo_core/tests/test_telegram_router.py`
- `tomo_core/tests/test_shared_gateway.py`
- `tomo_core/tests/test_sandbox_inbound.py`
- `tomo_core/tests/test_sandbox_protocol.py`
- `tomo_core/tests/test_sandbox_dispatch.py`
- `tomo_core/tests/test_onboarding_store.py`

Before editing, search every protocol/store interface implementation and test fake so the new revision query and v5 event are added consistently rather than guessed.

## Risks and tradeoffs

- **Reasoning quality:** `medium` should reduce latency but may alter answer quality. Preserve the env override so production can return to `high` without reverting code.
- **Typing threads:** the code volume is small, but cancellation and first-delivery races make this concurrency-sensitive. A dedicated lease with deterministic tests is safer than ad hoc thread creation in the gateway.
- **Telegram action failures:** typing is best-effort. Its timeout and exceptions must never block or fail the reply path.
- **Reaction availability:** allowlisted does not mean enabled in every chat. Telegram rejection remains normal and silent.
- **Protocol rollout:** v5 must remain backward-readable so a host deployment can tolerate old v4 sandbox output during reconciliation. A new snapshot is required before exercising stale-v5 output.
- **Revision rebasing:** only a monotonic upward floor is allowed. Never lower host or sandbox revisions and never bypass SQLite generation fencing.
- **Partial frames:** a stale event should occur before provider execution. Any stale/error after visible frames must still reconcile delivery records and must not resend known-visible bubbles.
- **Latency attribution:** reaction repair elimination and medium reasoning target the measured model-bound delay, but exact gains vary by prompt/provider. Do not present an estimate as measured production performance.

## Difficulty estimate

- Reasoning default: small.
- Reaction prompt and nonfatal normalization: small.
- Typing heartbeat: small code change, medium concurrency/testing risk.
- Stale-revision terminal event and atomic rebase: medium due to protocol compatibility and persistence fencing.
- Combined release: medium overall, with the stale-revision and typing lifecycle tests carrying most of the risk.
