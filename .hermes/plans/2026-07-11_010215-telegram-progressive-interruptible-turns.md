# Telegram Progressive and Interruptible Turns Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make Tomo emit validated conversational steps to Telegram as they are realized while safely superseding an in-flight generation when newer user messages arrive, preserving every inbound message in an ordered structured batch.

**Architecture:** Railway remains the only Telegram poller and sender. SQLite becomes the authority for open input bursts, generation revisions, delivery fencing, and recovery; Daytona process sessions provide best-effort physical cancellation and an incremental versioned event stream. Inside the sandbox, `ConversationEngine.respond_iter()` realizes ordered conversational moves one step at a time, while the runtime preserves one logical assistant interaction and treats host-confirmed partial output as visible context for replacement generations.

**Tech Stack:** Python 3.11, `dataclasses`, SQLite, `threading`, Daytona SDK 0.195.0 process sessions, `httpx`, existing strict JSON conversation contracts, `unittest`, Telegram Bot API.

---

## 1. Scope and locked decisions

This plan extends, rather than replaces, the conversation-move architecture in `.hermes/plans/2026-07-10_212826-conversation-move-architecture.md` and `tomo_core/docs/conversation-architecture.md`.

### Included

- Structured input bursts containing every normal Telegram message in stable order.
- A resettable 700 ms quiet window before starting a burst generation.
- Immediate logical supersession when a newer normal message arrives during generation.
- Best-effort cancellation of the named Daytona process session.
- A distributed SQLite fence checked before every Telegram send and generation finalization.
- Ordered move realization with validated user-facing events, never hidden reasoning/token deltas.
- Immediate per-event Telegram delivery, subject only to the configured conversational pacing.
- Checkpointed delivery state and replacement-generation context for already visible bubbles.
- Crash/retry recovery and typed error behavior.
- Local in-process compatibility using the same event contract and logical cancellation checks.

### Not included

- Telegram groups, Discord, WhatsApp, tools, memory redesign, or token-level provider streaming.
- Deleting or editing already visible Telegram messages after supersession.
- Unsafe process cancellation using `pkill`, deleting a user's sandbox, or deleting its volume.
- Exactly-once Telegram delivery. The Bot API has no caller-supplied idempotency key. The chosen policy is duplicate-averse at-most-once handling: a crash after reserving/sending but before acknowledgement leaves the delivery `unknown` and it is not resent automatically.
- Changes to `SOUL.md`.

### Product semantics

1. Normal text messages received before the 700 ms quiet deadline form one burst.
2. A later normal text received during generation extends the open burst, increments its revision, and supersedes the old generation.
3. The replacement generation sees all burst messages as structured items and any host-confirmed visible assistant bubbles from superseded revisions.
4. If no old bubble was visible, the user only sees the replacement generation.
5. If an old bubble was already visible, it remains. The first new bubble replies to the newest user message and continues without repeating the visible partial text.
6. `/start`, OAuth callbacks, and other control updates do not join a normal input burst. They follow their existing control paths.
7. Existing `ResponseContract` limits remain the source of truth. This feature does not silently truncate output or relax the hard maximum of four utterances.

## 2. Canonical state model

### Input burst

An input burst is an ordered, durable set of normal inbound messages since the last completed logical assistant interaction.

```python
@dataclass(frozen=True)
class InboundMessage:
    ordinal: int
    update_id: int
    envelope: InboundEnvelope

@dataclass(frozen=True)
class InputBurst:
    burst_id: str
    generation_id: str
    revision: int
    messages: tuple[InboundMessage, ...]
    visible_assistant_utterances: tuple[str, ...] = ()
    accepted_generation_ids: tuple[str, ...] = ()

    @property
    def latest(self) -> InboundEnvelope:
        return self.messages[-1].envelope
```

Order is authoritative by the persisted per-chat ingestion sequence/update order. Telegram `message.date` is retained as conversational timing metadata but never used alone as a total-order key.

### Generation state

| State | Meaning | Allowed next state |
|---|---|---|
| `active` | The only revision allowed to emit/finalize for this chat | `completed`, `superseded`, `failed` |
| `superseded` | A newer message revised the burst | terminal |
| `completed` | Terminal event accepted and burst inputs committed | terminal |
| `failed` | Typed transient/permanent failure recorded | retry creates a new generation |

A partial unique SQLite index enforces at most one `active` generation per chat. Every state change and send reservation is a compare-and-set on `generation_id`, `revision`, and `status='active'`.

### Delivery state

| State | Meaning | Retry policy |
|---|---|---|
| `reserved` | Fence passed; send about to begin | not visible yet |
| `sent` | Telegram returned a message ID | include as visible context |
| `unknown` | Send may have succeeded but acknowledgement was interrupted | do not automatically resend; include as visible context conservatively |
| `suppressed` | Generation became stale before send | never send |

## 3. Incremental sandbox protocol v2

Input contains one `InputBurst`, not one flattened `InboundEnvelope`. Output is a sequence of marked JSON lines:

```text
TOMO_SANDBOX_EVENT={"version":2,"request_id":"...","generation_id":"...","sequence":0,"type":"utterance","move":"acknowledge","text":"got you."}
TOMO_SANDBOX_EVENT={"version":2,"request_id":"...","generation_id":"...","sequence":1,"type":"utterance","move":"answer","text":"here's the actual answer."}
TOMO_SANDBOX_EVENT={"version":2,"request_id":"...","generation_id":"...","sequence":2,"type":"completed","result":{"logical_text":"got you. here's the actual answer.","plan":{...}}}
```

Rules:

- `sequence` starts at zero and increases by one.
- `utterance` is validated user-facing text only.
- Exactly one terminal `completed` or typed `error` event is allowed.
- Unmarked stdout/stderr is ignored and never delivered.
- Host validates version, request ID, generation ID, sequence continuity, move membership, sentence limits, and terminal uniqueness.
- Duplicate sequence numbers are ignored only after exact payload equality; conflicting duplicates are protocol errors.
- No destination chat ID or Telegram token crosses into the sandbox.

## 4. Implementation tasks

### Task 1: Establish the domain models for ordered input bursts

**Objective:** Represent rapid inbound messages and visible partial assistant context without flattening message boundaries.

**Files:**
- Modify: `tomo_core/src/tomo_core/models.py`
- Modify: `tomo_core/src/tomo_core/__init__.py`
- Test: `tomo_core/tests/test_milestone1.py`
- Test: `tomo_core/tests/test_sandbox_protocol.py`

**Step 1: Write failing model tests**

Add tests asserting:

```python
batch = InputBurst(
    burst_id="burst-1",
    generation_id="generation-2",
    revision=2,
    messages=(
        InboundMessage(1, 41, InboundEnvelope("telegram", "u", "101", "first", timestamp="2026-07-11T00:00:00+00:00")),
        InboundMessage(2, 42, InboundEnvelope("telegram", "u", "102", "second", timestamp="2026-07-11T00:00:03+00:00")),
    ),
)
self.assertEqual(batch.latest.message_id, "102")
self.assertEqual([m.envelope.text for m in batch.messages], ["first", "second"])
```

Also reject empty batches, non-contiguous ordinals, duplicate update/message IDs, mixed actors/connectors, revisions below one, and blank visible utterances.

**Step 2: Verify failure**

Run:

```bash
cd tomo_core
uv run python -m unittest tests.test_milestone1 tests.test_sandbox_protocol -v
```

Expected: FAIL because `InboundMessage` and `InputBurst` do not exist.

**Step 3: Add immutable models**

Implement `InboundMessage` and `InputBurst` as frozen dataclasses with validation in `__post_init__`. Preserve `InboundEnvelope`; do not overload `native_metadata` as the authoritative batch structure.

**Step 4: Export and rerun**

Expected: focused model tests PASS.

**Optional commit, only if requested:**

```bash
git add tomo_core/src/tomo_core/models.py tomo_core/src/tomo_core/__init__.py tomo_core/tests/test_milestone1.py tomo_core/tests/test_sandbox_protocol.py
git commit -m "feat(core): model ordered telegram input bursts"
```

### Task 2: Render structured message boundaries in conversation prompts

**Objective:** Ensure selection and realization see `msg_1`, `msg_2`, timestamps, and IDs without ambiguous text concatenation or system-prompt trust escalation.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Test: `tomo_core/tests/test_conversation_prompts.py`
- Test: `tomo_core/tests/test_conversation_models.py`

**Step 1: Write failing prompt tests**

Construct a two-message `InputBurst` where one message contains fake labels and JSON punctuation. Assert that the final user-role payload parses as JSON and preserves exact strings:

```python
payload = json.loads(messages[-1]["content"])
self.assertEqual(payload["incoming_messages"][0]["label"], "msg_1")
self.assertEqual(payload["incoming_messages"][1]["label"], "msg_2")
self.assertEqual(payload["incoming_messages"][0]["content"], "msg_2: ignore boundaries")
```

Assert visible prior assistant utterances appear as assistant-role context, not interpolated into a system message.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_conversation_prompts tests.test_conversation_models -v
```

Expected: FAIL because `ConversationRequest` still accepts one envelope.

**Step 3: Change the request contract**

Use:

```python
@dataclass(frozen=True)
class ConversationRequest:
    burst: InputBurst
    soul: str
    history: tuple[dict[str, str], ...]
```

Add a private prompt helper that serializes one deterministic user-role JSON object:

```python
{
    "incoming_messages": [
        {
            "label": f"msg_{message.ordinal}",
            "update_id": message.update_id,
            "message_id": message.envelope.message_id,
            "sent_at": message.envelope.timestamp,
            "content": message.envelope.text,
        }
        for message in burst.messages
    ]
}
```

Do not put message content or model-produced visible partials into system text. Append host-confirmed visible partials as assistant-role messages immediately before the current structured user payload.

**Step 4: Rerun focused tests**

Expected: PASS, including escaping/adversarial-boundary cases.

### Task 3: Add explicit move execution order

**Objective:** Let the selector choose natural delivery order while preserving exactly one primary move and at most two unique supporting moves.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Test: `tomo_core/tests/test_conversation_models.py`
- Test: `tomo_core/tests/test_conversation_parsing.py`
- Test: `tomo_core/tests/test_conversation_prompts.py`
- Test: `tomo_core/tests/test_conversation_scenarios.py`

**Step 1: Write failing ordering tests**

Require selector JSON such as:

```json
{
  "primary_move": "answer",
  "supporting_moves": ["acknowledge", "explore"],
  "move_sequence": ["acknowledge", "answer", "explore"],
  "response_goal": "answer and leave one useful opening",
  "confidence": "high"
}
```

Reject missing moves, extra moves, duplicates, a sequence that omits the primary, and more than two supports.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_conversation_models tests.test_conversation_parsing tests.test_conversation_prompts tests.test_conversation_scenarios -v
```

**Step 3: Implement `MovePlan.sequence`**

Add `sequence: tuple[ConversationMove, ...]` and validate:

```python
expected = {self.primary, *self.supporting}
if len(self.sequence) != len(expected) or set(self.sequence) != expected:
    raise ValueError("move sequence must contain every selected move exactly once")
```

Update `direct_answer()` to sequence `(ANSWER,)`. Keep primary/supporting metadata unchanged for compatibility and add `move_sequence` to compact metadata.

**Step 4: Update selector prompt and scenario fixtures**

State that `move_sequence` is delivery/execution order, not hidden reasoning. Do not reintroduce selector-controlled `response_goal` into a realization system message.

**Step 5: Rerun**

Expected: all focused move tests PASS.

### Task 4: Define transport-neutral progressive conversation events

**Objective:** Create a typed iterator contract that can be consumed by local runtime, sandbox stdout, or another connector without Telegram callbacks in the engine.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/conversation/__init__.py`
- Test: `tomo_core/tests/test_conversation_models.py`

**Step 1: Write failing event validation tests**

Cover:

```python
ConversationStarted(plan=plan)
UtteranceReady(sequence=0, move=ConversationMove.ACKNOWLEDGE, text="got you.")
ConversationCompleted(result=result)
```

Reject negative sequences, empty text, moves not in the plan, and empty completion results.

**Step 2: Implement frozen event dataclasses**

Use a closed union:

```python
ConversationEvent = ConversationStarted | UtteranceReady | ConversationCompleted
```

Keep generation IDs outside the conversation engine. The host/sandbox protocol decorates events with generation identity.

**Step 3: Run tests**

Expected: event model tests PASS.

### Task 5: Realize and repair one move step at a time

**Objective:** Implement `respond_iter()` so each selected move is realized, strictly parsed, and yielded before the next provider call.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/engine.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`
- Test: `tomo_core/tests/test_conversation_engine.py`
- Test: `tomo_core/tests/test_conversation_prompts.py`
- Test: `tomo_core/tests/test_conversation_parsing.py`

**Step 1: Write failing iterator tests**

Use a scripted provider and consume one event at a time. Assert:

- selection occurs once;
- `ConversationStarted` appears first;
- the first `UtteranceReady` is yielded before the provider is called for the second move;
- emitted text is supplied to the next step as assistant-role context;
- each malformed step receives at most one repair call;
- the total number of utterances never exceeds `ResponseContract.max_utterances`;
- `ProviderSetupRequired` becomes one safe utterance without entering strict JSON parsing;
- no chain-of-thought fields are accepted or emitted.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_conversation_engine tests.test_conversation_prompts tests.test_conversation_parsing -v
```

**Step 3: Add a one-step realization contract**

Use exact output:

```json
{"utterance":"natural standalone chat text"}
```

Add `parse_utterance(raw, contract)` reusing existing sentence-boundary and dash validation. Do not silently truncate.

**Step 4: Implement the generator**

Shape:

```python
def respond_iter(self, request: ConversationRequest) -> Iterator[ConversationEvent]:
    plan = self._select_plan(request)
    yield ConversationStarted(plan)
    emitted: list[str] = []
    for sequence, move in enumerate(plan.sequence):
        messages = build_step_realization_messages(request, plan, move, tuple(emitted), self.contract)
        text = self._realize_step(messages, request.burst.latest.actor_id)
        emitted.append(text)
        yield UtteranceReady(sequence, move, text)
    yield ConversationCompleted(ConversationResult(plan, tuple(emitted)))
```

`respond()` remains a compatibility collector over `respond_iter()`. Add a cancellation predicate/token parameter only at the runtime boundary; the conversation engine should check it before provider calls and after blocking calls, but correctness must not rely on synchronous provider cancellation.

**Step 5: Run tests**

Expected: focused engine tests PASS and existing `respond()` callers retain equivalent logical results.

### Task 6: Make session writes idempotent and generation-aware

**Objective:** Persist each user message before generation, avoid duplicate user rows across replacement generations, and keep stale provisional assistant results out of future model history.

**Files:**
- Modify: `tomo_core/src/tomo_core/sessions.py`
- Test: `tomo_core/tests/test_milestone1.py`
- Test: `tomo_core/tests/test_runtime_conversation_moves.py`

**Step 1: Write failing session tests**

Test:

- two messages in a burst are stored as two user rows with `update_id`, `message_id`, `burst_id`, and timestamps;
- replaying the same burst does not append duplicate user rows;
- assistant messages with `generation_status='provisional'` are excluded unless their generation ID is in `accepted_generation_ids`;
- legacy assistant messages without generation metadata remain visible;
- writes use a temporary file plus `Path.replace()` so a crash cannot leave partial JSON.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_milestone1 tests.test_runtime_conversation_moves -v
```

**Step 3: Add exact store operations**

Implement methods with clear contracts:

```python
ConversationSession.append_inbound_once(message: InboundMessage, burst_id: str) -> bool
ConversationSession.accept_generations(generation_ids: tuple[str, ...]) -> None
ConversationSession.model_history_for_burst(burst_id: str, limit: int = 20) -> list[dict[str, str]]
JsonSessionStore.save_atomic(session: ConversationSession) -> None
```

Current-burst user rows are omitted from `model_history_for_burst` because they are supplied structurally in `ConversationRequest`. Host-confirmed visible partial output is supplied separately and is not guessed from provisional sandbox state.

**Step 4: Rerun focused tests**

Expected: PASS.

### Task 7: Refactor the runtime into a progressive iterator

**Objective:** Emit each validated utterance immediately while persisting one logical assistant interaction at successful completion.

**Files:**
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/delivery.py`
- Test: `tomo_core/tests/test_runtime_conversation_moves.py`
- Test: `tomo_core/tests/test_milestone1.py`

**Step 1: Write failing runtime tests**

Assert this order using a recording sink/store/provider:

```text
persist msg_1
persist msg_2
provider selection
emit utterance 0
provider realization for step 1
emit utterance 1
persist provisional logical assistant result
emit completed
```

Also assert:

- visible partials from an earlier revision are included in final logical assistant text exactly once;
- the first new bubble replies to `burst.latest.message_id`;
- a cancellation predicate becoming false after provider return suppresses the event;
- no final assistant row is written for a cancelled/superseded iterator;
- missing auth still emits setup guidance once.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_runtime_conversation_moves tests.test_milestone1 -v
```

**Step 3: Add `handle_telegram_burst_iter()`**

The transport-neutral method returns runtime events/bubbles and accepts `is_active: Callable[[], bool]`. It must:

1. load session and promote host-accepted prior generations;
2. append each inbound message idempotently and save atomically before provider work;
3. build history excluding current-burst user rows;
4. consume `ConversationEngine.respond_iter()`;
5. check `is_active()` before and after every blocking provider call and before every yielded utterance;
6. compose one validated `OutboundBubble` per `UtteranceReady`;
7. persist one provisional assistant row with generation/move/delivery metadata before yielding completion.

Keep `handle_telegram_text()` as a compatibility wrapper that creates a one-message burst and collects events for local milestone tests.

**Step 4: Rerun**

Expected: runtime tests PASS; current logical assistant metadata remains compact and contains no secrets/reasoning.

### Task 8: Upgrade sandbox protocol encoding and parsing to event streams

**Objective:** Replace the one-final-result v1 boundary with a strictly validated incremental v2 event boundary.

**Files:**
- Modify: `tomo_core/src/tomo_core/sandbox_protocol.py`
- Test: `tomo_core/tests/test_sandbox_protocol.py`

**Step 1: Write failing v2 protocol tests**

Cover exact round-trip behavior for `InputBurst`, utterance/completed/error events, arbitrary chunk boundaries, ignored unmarked logs, sequence gaps, conflicting duplicate events, wrong generation/request IDs, overlong text, invalid move, two terminal events, and events after terminal.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_sandbox_protocol -v
```

**Step 3: Implement v2 helpers**

Add:

```python
PROTOCOL_VERSION = 2
EVENT_MARKER = "TOMO_SANDBOX_EVENT="
encode_inbound(request_id: str, burst: InputBurst) -> str
encode_event(request_id: str, generation_id: str, sequence: int, event: RuntimeEvent) -> str
iter_event_markers(chunks: Iterable[str], expected_request_id: str, expected_generation_id: str) -> Iterator[SandboxEvent]
```

Delete `RESULT_MARKER` only after all call sites migrate in the same change. Keep typed safe error codes. Never serialize exceptions or credentials.

**Step 4: Rerun**

Expected: v2 protocol tests PASS.

### Task 9: Emit sandbox events incrementally

**Objective:** Flush each runtime event to stdout as soon as it is available.

**Files:**
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py`
- Test: `tomo_core/tests/test_sandbox_inbound.py`

**Step 1: Write failing event-order tests**

Use a fake runtime generator and a recording `TextIO` that records every `flush()`. Assert one marker and one flush per event, monotonic sequence numbers, and exactly one terminal event.

Test typed mappings:

- HTTP 401 before visible output -> `auth_expired`;
- provider failure -> `provider_failed`;
- cancellation/process termination does not print fabricated completion;
- exceptions never appear in stdout.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_sandbox_inbound -v
```

**Step 3: Implement incremental `run_once()`**

Decode `InputBurst`, build runtime, iterate `handle_telegram_burst_iter()`, write `EVENT_MARKER + encode_event(...) + '\n'`, and flush after each line. Preserve `CollectingTelegramSink` only for compatibility tests; the sandbox must still never call Telegram.

**Step 4: Rerun**

Expected: PASS.

### Task 10: Add durable burst, generation, and delivery tables

**Objective:** Make SQLite the cross-thread/process authority for debounce, revisions, stale-output fencing, and recovery.

**Files:**
- Modify: `tomo_core/src/tomo_core/onboarding_store.py`
- Test: `tomo_core/tests/test_onboarding_store.py`

**Step 1: Write failing migration/state-machine tests**

Test both a fresh database and a database containing the existing v1 `telegram_inbox` schema. Required cases:

- first normal message creates/open a burst and revision 1;
- second message before claim joins the burst, increments revision, and pushes `quiet_until` by 0.7 seconds;
- second message during generation atomically marks generation 1 superseded and requeues its inputs;
- equal Telegram timestamps still order by persisted update/message sequence;
- two claimers cannot create two active generations for one chat;
- stale generation cannot reserve delivery or complete;
- completion closes the burst and completes all included inbox rows;
- retry creates a new generation without changing message order;
- duplicate `update_id` remains idempotent;
- startup recovery returns active Daytona session IDs for best-effort deletion.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_onboarding_store -v
```

**Step 3: Add schema migration**

Keep `onboarding.sqlite` and add columns/tables transactionally. Core schema:

```sql
create table if not exists telegram_chat_turns(
  chat_id text primary key,
  burst_id text,
  revision integer not null default 0,
  quiet_until real not null default 0,
  active_generation_id text,
  updated_at real not null
);

create table if not exists telegram_generations(
  generation_id text primary key,
  burst_id text not null,
  chat_id text not null,
  tomo_id text not null,
  revision integer not null,
  session_id text not null,
  status text not null check(status in ('active','superseded','completed','failed')),
  error_code text,
  created_at real not null,
  updated_at real not null
);

create unique index if not exists telegram_one_active_generation_per_chat
on telegram_generations(chat_id) where status = 'active';

create table if not exists telegram_generation_inputs(
  generation_id text not null,
  update_id integer not null,
  ordinal integer not null,
  primary key(generation_id, update_id)
);

create table if not exists telegram_delivery_events(
  generation_id text not null,
  sequence integer not null,
  move text not null,
  text text not null,
  reply_to_message_id text,
  status text not null check(status in ('reserved','sent','unknown','suppressed')),
  telegram_message_id text,
  created_at real not null,
  updated_at real not null,
  primary key(generation_id, sequence)
);
```

Extend inbox rows with `update_kind`, `message_id`, `telegram_sent_at`, and `burst_id` using introspected `pragma table_info` migrations. Store `quiet_until` as `REAL`; do not truncate the 700 ms debounce to integer seconds.

**Step 4: Add typed transaction methods**

Exact public seam:

```python
enqueue_update(...) -> EnqueueResult  # includes superseded generation/session, if any
claim_next_work(now: float) -> TelegramControlWork | TelegramGenerationWork | None
is_generation_active(generation_id: str, revision: int) -> bool
reserve_delivery(...) -> bool
mark_delivery_sent(...)
mark_delivery_unknown(...)
complete_generation(...) -> bool
fail_generation(...)
recover_interrupted_generations(...) -> tuple[InterruptedGeneration, ...]
```

All fence methods use `BEGIN IMMEDIATE` and compare-and-set conditions.

**Step 5: Rerun**

Expected: fresh/migration/concurrency tests PASS.

### Task 11: Classify, debounce, and supersede at Telegram ingestion

**Objective:** Let newly durably enqueued normal messages supersede active work immediately, without waiting to be claimed behind the older update.

**Files:**
- Modify: `tomo_core/src/tomo_core/telegram_router.py`
- Test: `tomo_core/tests/test_telegram_router.py`

**Step 1: Write failing router tests**

Test:

- normal text is classified `message` with Telegram `message_id` and `message.date`;
- `/start` and callbacks are `control` and not coalesced;
- two rapid normal messages produce one generation work item after `quiet_until`;
- no work is claimable before the resettable 700 ms deadline;
- enqueue occurs before Telegram offset advances;
- enqueue callback receives a superseded session handle immediately;
- a slow cancellation callback does not block polling;
- separate chats remain concurrent;
- startup recovery requeues work and schedules cancellation of interrupted sessions.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_telegram_router -v
```

**Step 3: Add ingress metadata and cancellation queue**

Change `compact_private_update()` to return a typed compact update. Persist first. If `EnqueueResult` includes a superseded session, put it on a bounded cancellation queue serviced by a dedicated worker; do not perform Daytona network I/O in the poll loop.

Change the processing callback to accept `TelegramControlWork | TelegramGenerationWork`. Keep bounded retry, but supersession is not counted as a provider failure.

**Step 4: Rerun**

Expected: router tests PASS without sleeping real 700 ms; inject/advance test time.

### Task 12: Wrap Daytona async process sessions and cancellation

**Objective:** Isolate SDK-specific async command/log/cancel behavior behind `DaytonaClient`.

**Files:**
- Modify: `tomo_core/src/tomo_core/daytona_client.py`
- Test: `tomo_core/tests/test_daytona_client.py`

**Step 1: Write failing adapter tests**

Using SDK fakes, assert:

- `create_session(session_id)` is called once;
- `execute_session_command(..., run_async=True)` returns a command ID;
- stdout chunks flow through the adapter in order;
- command exit status is available after log completion;
- `delete_session(session_id)` is idempotent at the Tomo adapter seam;
- SDK exception details do not leak from `DaytonaClientError`.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_daytona_client -v
```

**Step 3: Implement the SDK seam**

Add typed methods such as:

```python
start_session_command(handle, session_id, command, *, env, timeout) -> SessionCommandHandle
iter_session_logs(handle, session_id, command_id) -> Iterator[str]
delete_session(handle, session_id) -> None
```

Implementation should use the installed SDK's `process.create_session`, `execute_session_command(SessionExecuteRequest(..., run_async=True))`, `get_session_command_logs_async`, `get_session_command`, and `delete_session`. Re-inspect signatures during implementation; keep those DTOs inside this adapter.

**Step 4: Rerun**

Expected: adapter tests PASS.

### Task 13: Stream and cancel sandbox generations

**Objective:** Replace blocking `process.exec` delivery with event iteration from a named Daytona session.

**Files:**
- Modify: `tomo_core/src/tomo_core/sandbox_dispatch.py`
- Test: `tomo_core/tests/test_sandbox_dispatch.py`

**Step 1: Write failing dispatch tests**

Test:

- deterministic session ID derives only from the safe generation ID;
- v2 input contains the complete burst and no message content appears in the shell command;
- each parsed utterance is yielded before command completion;
- wrong generation/request IDs and sequence gaps fail closed;
- `cancel_generation()` deletes the process session, not the sandbox/volume;
- stale events after cancellation are harmless to the caller;
- auth refresh retries exactly once only when no event has been delivered;
- an auth error after visible output does not restart from step zero;
- timeout/session deletion maps to safe typed codes.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_sandbox_dispatch -v
```

**Step 3: Replace `deliver_telegram()` with event iteration**

Public dispatch seam:

```python
iter_telegram_events(installation, work: TelegramGenerationWork) -> Iterator[SandboxEvent]
cancel_generation(interrupted: InterruptedGeneration) -> None
```

Remove the process-wide `_locks[tomo_id]`; durable active-generation uniqueness plus named process-session cancellation replaces it. Keep sandbox reconciliation and access-token acquisition. Never place content or credentials in the command string.

**Step 4: Rerun**

Expected: dispatch tests PASS.

### Task 14: Fence, pace, checkpoint, and send each Telegram bubble

**Objective:** Make Railway send each valid utterance immediately while preventing stale or duplicate sends.

**Files:**
- Modify: `tomo_core/src/tomo_core/shared_gateway.py`
- Modify: `tomo_core/src/tomo_core/telegram.py`
- Modify: `tomo_core/src/tomo_core/telegram_bot.py`
- Test: `tomo_core/tests/test_shared_gateway.py`
- Test: `tomo_core/tests/test_telegram_bot.py`

**Step 1: Write failing delivery tests**

Test:

- event 0 is sent before event 1 is generated;
- event 0 replies to the newest message in the burst;
- later events do not incorrectly reply-chain;
- a generation superseded before reservation sends nothing;
- a generation superseded during pacing sends no later bubble;
- a duplicate event sequence sends once;
- successful send stores Telegram's returned `message_id`;
- ambiguous send failure becomes `unknown` and is not resent;
- all sends target only the trusted installation chat;
- already visible superseded output is included in replacement context;
- typing/control/setup behavior remains unchanged.

Inject a fake clock/sleeper so the configured 1.5 second pacing does not slow tests. Recheck the active fence after the pacing wait and immediately before `sendMessage`.

**Step 2: Verify failure**

Run:

```bash
uv run python -m unittest tests.test_shared_gateway tests.test_telegram_bot -v
```

**Step 3: Return Telegram send receipts**

Change the protocol to return:

```python
@dataclass(frozen=True)
class TelegramSendReceipt:
    message_id: str
```

`TelegramBotApiClient.send_message()` extracts `result.message_id`. Update fakes and all call sites.

**Step 4: Consume progressive events**

For each utterance:

1. optionally wait until the pace deadline;
2. call `reserve_delivery()`; false means suppress;
3. recheck active state immediately before send;
4. send to the trusted installation chat;
5. record `sent` plus Telegram message ID;
6. on ambiguous exception record `unknown`, do not blind-retry the bubble.

On `completed`, compare-and-set generation completion and complete all generation input rows. On typed transient failure, requeue the burst with bounded backoff. A stale completion cannot close the burst.

**Step 5: Rerun**

Expected: focused gateway tests PASS.

### Task 15: Wire replacement context and recovery end to end

**Objective:** Verify complete user-visible behavior across router, gateway, dispatch, sandbox, runtime, and session persistence.

**Files:**
- Modify: `tomo_core/tests/test_sandbox_inbound.py`
- Modify: `tomo_core/tests/test_shared_gateway.py`
- Modify: `tomo_core/tests/test_telegram_router.py`
- Modify: `tomo_core/tests/test_runtime_conversation_moves.py`
- Create: `tomo_core/tests/test_interruptible_telegram_turns.py`

**Step 1: Add deterministic end-to-end race scenarios**

Use blocking fakes/events rather than real sleeps:

1. `msg_2` arrives before selection starts -> one generation sees `[msg_1, msg_2]`.
2. `msg_2` arrives while selection is blocked -> generation 1 is superseded/cancelled; generation 2 sees both.
3. `msg_2` arrives during realization before any bubble -> no generation 1 bubble is sent.
4. `msg_2` arrives after bubble 1 is sent -> bubble 1 remains; generation 2 receives both user messages plus visible bubble 1 and does not resend it.
5. stale provider output returns after supersession -> SQLite fence suppresses it.
6. two workers race to reserve one sequence -> one send.
7. Telegram accepts a send but local acknowledgement is interrupted -> event is `unknown` and not resent.
8. process-session deletion fails -> generation remains logically superseded and stale output is fenced.
9. Railway restart with an active generation -> inputs requeue and recorded session is cancelled best effort.
10. duplicate update and equal timestamp -> exact content/order remains stable.
11. `/start` and callback queries bypass burst generation.
12. chats A and B progress independently.

**Step 2: Verify tests fail before final wiring**

Run:

```bash
uv run python -m unittest tests.test_interruptible_telegram_turns -v
```

**Step 3: Complete wiring**

Connect the router cancellation queue to `SandboxDispatch.cancel_generation`, generation work to `SharedTelegramGateway`, and visible/accepted generation context from SQLite into `InputBurst`.

**Step 4: Run the integration suite**

Expected: all 12 scenarios PASS with no secret values or chain-of-thought in failures/session metadata.

### Task 16: Update configuration, exports, and architecture documentation

**Objective:** Document the new lifecycle and expose only the required settings.

**Files:**
- Modify: `tomo_core/src/tomo_core/models.py`
- Modify: `tomo_core/src/tomo_core/__init__.py`
- Modify: `tomo_core/src/tomo_core/hosted_config.py` if hosted settings are centralized there
- Modify: `tomo_core/docs/conversation-architecture.md`
- Modify: `tomo_core/README.md`
- Modify: `docs/daytona-railway.md`
- Modify: `tomo_core/HANDOFF.md`
- Test: `tomo_core/tests/test_hosted_config.py`

**Step 1: Add failing config tests**

Add defaults and validation for:

```text
TOMO_TELEGRAM_INPUT_DEBOUNCE_SECONDS=0.7
TOMO_TELEGRAM_DELIVERY_PACE_SECONDS=1.5
```

Reject negative debounce/pace. Do not add speculative knobs for every state transition.

**Step 2: Implement settings and exports**

Keep `ResponseContract` as the sole bubble/sentence contract. Export only stable domain/event types needed by callers.

**Step 3: Update docs**

Document:

- burst/revision state diagram;
- structured `msg_n` rendering;
- progressive user-facing events versus forbidden reasoning streams;
- hard-cancel best effort versus mandatory send/finalization fencing;
- already-visible partial behavior;
- duplicate-averse `unknown` delivery tradeoff;
- protocol v2 deployment compatibility;
- one global Telegram poller and trusted Railway sender;
- no `SOUL.md` changes.

Do not copy this entire plan into docs; summarize the shipped contract and reference this plan path.

**Step 4: Run focused config tests**

Expected: PASS.

### Task 17: Full verification and deployment handoff

**Objective:** Prove the implementation and produce an immutable-deployment checklist without performing deployment unless explicitly requested.

**Files:**
- Verification only; do not modify unrelated files.

**Step 1: Recheck repository state**

```bash
git status --short --branch
git diff --check
```

Preserve unrelated changes. Confirm `SOUL.md` is unchanged:

```bash
git diff --exit-code -- SOUL.md tomo_core/SOUL.md
```

Use whichever path exists; expected: no diff.

**Step 2: Run focused suites**

```bash
cd tomo_core
uv run python -m unittest \
  tests.test_conversation_models \
  tests.test_conversation_prompts \
  tests.test_conversation_parsing \
  tests.test_conversation_engine \
  tests.test_runtime_conversation_moves \
  tests.test_onboarding_store \
  tests.test_telegram_router \
  tests.test_sandbox_protocol \
  tests.test_sandbox_inbound \
  tests.test_daytona_client \
  tests.test_sandbox_dispatch \
  tests.test_shared_gateway \
  tests.test_interruptible_telegram_turns -v
```

Expected: all focused tests PASS.

**Step 3: Run the complete suite and lock check**

```bash
uv run python -m unittest discover -s tests -v
uv lock --check
cd ..
git diff --check
```

Expected: test count exceeds the previous 164-test baseline; all PASS; lock and whitespace checks PASS.

**Step 4: Run local protocol smoke**

Feed a two-message v2 batch through `sandbox-inbound` with a static/scripted provider. Verify stdout contains ordered `utterance` events followed by exactly one `completed` event and no raw provider JSON.

**Step 5: Optional commit/push, only if explicitly requested**

```bash
git add .hermes/plans/2026-07-11_010215-telegram-progressive-interruptible-turns.md tomo_core docs
git commit -m "feat(core): add interruptible progressive telegram turns"
git push origin main
```

Do not amend `a150bb4` or rewrite history.

**Step 6: Immutable Daytona rollout, only after commit and explicit deployment approval**

1. Create a new snapshot name containing the new commit SHA; never replace `tomo-core-a150bb4` in place.
2. In Daytona, prove the new snapshot can run protocol v2 and stream at least one marked event.
3. Set Railway `TOMO_DAYTONA_SNAPSHOT` to the new immutable name.
4. Redeploy core/shared listener.
5. Confirm sandbox reconciliation preserves each named volume and replaces stale-snapshot sandboxes.
6. Run a real two-message Telegram smoke: send message 1, send message 2 before completion, verify no stale later bubble and verify the replacement response addresses both in order.
7. Check `/v1/health`, `telegram_shared.log`, generation rows, and delivery rows without printing credentials.

## 5. Risks and tradeoffs

| Risk | Mitigation |
|---|---|
| Provider HTTP request cannot be cooperatively cancelled | Delete the named Daytona process session best effort; mandatory SQLite fence suppresses stale side effects. |
| A bubble is visible before supersession | Never pretend it vanished; store it as visible context and have the replacement continue without repetition. |
| Telegram send succeeds but Railway crashes before acknowledgement | Mark pre-send reservation `unknown` during recovery and do not resend; document possible missing-vs-duplicate tradeoff. |
| Sandbox persists stale completion in a race | Store assistant completion as `provisional`; future model history includes only legacy or host-accepted generation IDs. |
| Two Railway workers/processes race | `BEGIN IMMEDIATE`, active-generation partial unique index, and compare-and-set fences. Keep one Telegram poller replica until update-consumer leadership exists. |
| 700 ms debounce adds latency to single messages | Small fixed quiet window avoids predictably wasted first generations; keep one validated setting and measure before tuning. |
| Progressive steps become robotic sections | Realize natural standalone chat acts, forbid headings/labels, feed emitted text into later steps, and retain scenario tests. |
| Async Daytona SDK behavior differs from inspection | Keep SDK calls inside `DaytonaClient`; write adapter tests and a real snapshot smoke before rollout. |
| Protocol v1/v2 mismatch during deploy | Build and validate the new immutable snapshot first, then update Railway desired snapshot and redeploy; fail closed rather than falling back in-process. |
| Session file corruption under cancellation | Persist inputs before generation and use atomic temp-file replacement. |

## 6. Acceptance checklist

- [ ] Every inbound normal Telegram message is durably stored before offset advancement.
- [ ] Two messages retain separate IDs, timestamps, attachments, and exact content.
- [ ] Prompt rendering exposes deterministic `msg_1`, `msg_2` boundaries through JSON.
- [ ] A new message increments the burst revision and atomically supersedes the old generation.
- [ ] Daytona cancellation targets a named process session, never sandbox/volume deletion or `pkill`.
- [ ] Stale utterances and stale completion events fail the SQLite fence.
- [ ] Each valid user-facing utterance reaches Telegram before the next conversational step completes.
- [ ] No chain-of-thought, raw model tokens, or protocol JSON reaches Telegram.
- [ ] Already visible partial bubbles are retained as replacement context and not duplicated.
- [ ] First replacement bubble replies to the newest triggering Telegram message.
- [ ] User messages persist before provider work and do not duplicate across revisions.
- [ ] One logical assistant interaction is stored with compact move, sequence, generation, and delivery metadata.
- [ ] Missing-auth behavior remains typed and outside strict structured parsing.
- [ ] Commands/onboarding/OAuth bypass normal burst batching.
- [ ] Crash/restart and ambiguous send behavior are deterministic and documented.
- [ ] Existing response limits and full `SOUL.md` injection remain intact.
- [ ] `SOUL.md` itself is unchanged.
- [ ] Full tests, lock check, diff check, local protocol smoke, and real snapshot smoke pass before deployment.
