# TurnRun Segment, Frame, and Tool Batching Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace Tomo's per-move completion loop with one coherent `TurnRun` runtime that streams validated frames from one model generation per knowledge segment, supports safe batched read-only tool rounds, preserves interruption and persistence guarantees, and keeps internal context retrieval socially silent.

**Architecture:** A `TurnRun` owns a turn-level `MovePlan`, hard execution budgets, and one or more model-generation `Segment`s. Each segment is one continuous provider stream that emits zero to three complete validated `Frame`s and may end with one native `ToolBatch`; only a verified tool observation, user interruption, approval, external event, or meaningful job milestone starts another segment. Railway remains the trusted Telegram sender and delivery fence, while the Daytona sandbox owns model streaming, context hydration, tool execution, and logical-turn persistence.

**Tech Stack:** Python 3.11, frozen dataclasses, `httpx` streaming/SSE, OpenAI-compatible chat-completions tool calls, `concurrent.futures`, SQLite, atomic JSON session persistence, Daytona SDK 0.195.0 process sessions, Telegram Bot API, `unittest`.

---

## 1. Why this migration exists

The current implementation is internally consistent but produces the wrong social behavior:

```text
InputBurst
  -> model call: select MovePlan
  -> for each move:
       fresh model call: write one standalone utterance
       emit one Telegram bubble
  -> persist one logical assistant row
```

`ConversationEngine.respond_iter()` therefore makes one independent completion per move. Supplying prior same-turn bubbles as transcript context does not make those completions one decoder trajectory, so acknowledgements, answers, and follow-up questions can sound like different speakers.

The target behavior is:

```text
ordinary conversation
  TurnRun
    -> silent ContextHydration
    -> Segment 0: one continuous model stream
         plan record (internal)
         Frame 0 -> validate -> emit
         Frame 1 -> validate -> emit
         provider stop
    -> persist one logical assistant turn

read-only tool interaction
  TurnRun
    -> silent ContextHydration
    -> Segment 0: optional useful pre-tool Frame + ToolBatch
    -> execute independent safe calls concurrently
    -> append verified observations
    -> Segment 1: result Frames
    -> persist one logical assistant turn
```

The governing boundary rule is:

> Start another model generation only when genuinely new information becomes available.

Generating “the next conversational move” is not a knowledge boundary.

## 2. Current repository facts

- `tomo_core/src/tomo_core/providers.py` exposes blocking `complete()` only. All hosted providers call `/chat/completions` with `httpx.post` and return content text; native tool-call payloads and streaming usage are discarded.
- `tomo_core/src/tomo_core/conversation/engine.py` selects once, then calls `complete()` once per `MovePlan.sequence` item.
- `tomo_core/src/tomo_core/conversation/prompts.py` explicitly asks each step to be a “standalone chat utterance.”
- `tomo_core/src/tomo_core/conversation/models.py` couples `UtteranceReady` to one `ConversationMove`.
- `tomo_core/src/tomo_core/sandbox_protocol.py` protocol v2 requires every `utterance` event to carry a move and requires completed move count to equal utterance count.
- `tomo_core/src/tomo_core/onboarding_store.py` persists a non-null `move` for every delivery event.
- `tomo_core/src/tomo_core/shared_gateway.py` already performs the authoritative activity check immediately before every Telegram send and paces visible bubbles.
- `tomo_core/src/tomo_core/sessions.py` already preserves inbound-message identity, provisional assistant generations, accepted generations, and the Daytona-volume `ENOSYS` fallback. These guarantees must remain intact.
- `InboundEnvelope.location` exists, but `sandbox_dispatch.burst_from_work()` does not yet ingest Telegram static/live-location payloads.
- No durable-memory search, cross-session search, runtime tool registry, approvals layer, or concrete search tools exist in `tomo_core` today.
- The tree was clean except for untracked `tomo_core/.venv/`; never stage or modify that directory.

## 3. Locked product and domain decisions

### 3.1 Canonical terms

- **`TurnRun`**: the complete interaction initiated by one input burst, potentially spanning tools and several model segments.
- **`Segment`**: one continuous model generation between knowledge boundaries.
- **`Frame`**: one complete, validated outward text unit emitted by a segment.
- **`Bubble`**: Telegram's representation of a delivered frame.
- **`MovePlan`**: compact turn-level conversational intent; it does not determine frame count or bubble count.
- **`ToolBatch`**: the native tool calls requested by one model segment and executed as one tool round.
- **`ContextSnapshot`**: silently hydrated context available before the first model segment.

### 3.2 Generation and delivery

1. Ordinary conversation uses exactly one normal model-generation segment.
2. One segment may emit multiple frames from the same provider stream.
3. A move is not a frame. Several moves may share one frame, and one move may span frames.
4. Completed frames may be delivered while the same provider stream continues.
5. Raw tokens, partial JSON, chain-of-thought, move labels, tool arguments, and tool observations never cross the user-visible event boundary.
6. Runtime validates every complete frame before emission. It never silently truncates or arbitrarily splits invalid text.
7. Runtime checks generation activity before provider work, after blocking boundaries, before each emitted frame, and again at Railway immediately before each Telegram send.
8. Already visible frames survive supersession and become chronological assistant context for the replacement generation.
9. One completed `TurnRun` persists as one logical assistant interaction even if it had several segments and bubbles.

### 3.3 Socially quiet internal work

1. Context hydration emits no frame and creates no visible “tool execution” event.
2. Durable-memory and cross-session searches, once implemented, are internal context sources by default.
3. If the user explicitly asks Tomo to check memory or old conversations, Tomo may acknowledge that naturally, but no runtime-generated telemetry is mandatory.
4. Short read-only tool work normally relies on Telegram typing state; it does not require “starting,” “searching,” or “finished” bubbles.
5. A pre-tool frame is allowed only when socially useful, such as setting an expectation for a noticeably long operation or confirming material parameters.
6. The result itself demonstrates completion; do not add a redundant completion announcement.
7. Never offer to book, purchase, send, delete, or otherwise mutate unless that capability is actually bound and the required confirmation path exists.

### 3.4 Tool batching

Calls belong to one batch only when all arguments are resolved, no call consumes another call's output, each tool permits parallel read-only execution, approval requirements are compatible, and cancellation/failure semantics are safe. “Similar arguments” helps the model notice batching opportunities, but runtime safety and independence are the actual gates.

One segment may request several native tool calls. Runtime executes eligible calls concurrently and presents every success or safe failure observation together to the next segment. A dependent call waits for a later segment after the prerequisite observation. No automatic retry occurs inside a batch; a later model segment may request a retry and consumes normal budgets.

### 3.5 Initial hard budgets

```python
TurnBudget(
    max_model_segments=1,       # ordinary conversation
    max_tool_rounds=0,
    max_tool_calls=0,
    max_visible_segments=1,
    max_frames_per_segment=3,
    max_sentences_per_frame=3,
)

TurnBudget(
    max_model_segments=6,       # read-only tool TurnRun
    max_tool_rounds=5,          # separate counter; cannot exceed call count in v1
    max_tool_calls=5,
    max_visible_segments=3,
    max_frames_per_segment=3,
    max_sentences_per_frame=3,
)
```

Prompt targets are one to two sentences per frame and one to two frames per segment. Role-specific visible limits are zero or one pre-tool frame, zero or one progress frame, and one to three final-result frames.

Reserve the last model segment and the last visible segment for a grounded final response. When tool, time, token, or cost budget is exhausted, tools are removed from the final request and Tomo summarizes only verified results and failures already observed.

**Open value that must be selected before implementation Task 3:** `max_chars_per_frame`. The current Telegram transport ceiling is 4096, but the desired smaller product cap was not concluded. Do not silently choose a product value in code; record the approved value in `RuntimeConfig`, tests, and architecture docs first.

### 3.6 Contract-repair exception

Contract repair is not a conversational knowledge segment. The normal path still has one segment. If the first segment produces no deliverable frame and no tool call because its framed output is malformed, runtime may make one replacement repair generation under a separately counted `max_contract_repairs=1`. If any frame is already visible, do not start a repair generation that could contradict it; terminate as `completed_partial`, persist the valid visible frames, and surface no fabricated completion claim.

## 4. Explicitly deferred capabilities

The architecture must leave seams for these, but this implementation must not create fake tools or claim availability:

- concrete durable-memory search;
- cross-session/previous-conversation retrieval;
- Telegram static/live-location ingestion and freshness resolution;
- mutating tools, purchasing, booking, approvals, and idempotent side effects;
- durable background jobs and milestone notifications;
- automatic long-running foreground-to-background conversion;
- token/cost enforcement until provider usage/cost data is available and configured;
- removing protocol-v2 compatibility before every active sandbox is proven migrated.

A future `ContextSource` may add memory, session, or location facts to `ContextSnapshot` with provenance and freshness. Precedence is current explicit input, location deliberately attached to the current input, recent current-conversation context, durable memory, then older retrieved conversations. Conflicts that materially change execution require clarification.

## 5. Target types and boundaries

The exact naming may change during review, but responsibilities must not collapse back together.

```python
# tomo_core/src/tomo_core/conversation/models.py
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

class SegmentFinish(str, Enum):
    COMPLETE = "complete"
    TOOL_BATCH = "tool_batch"
    PARTIAL = "partial"

@dataclass(frozen=True)
class Frame:
    segment_index: int
    frame_index: int
    text: str

@dataclass(frozen=True)
class TurnBudget:
    max_model_segments: int
    max_tool_rounds: int
    max_tool_calls: int
    max_visible_segments: int
    max_frames_per_segment: int
    max_sentences_per_frame: int
    max_chars_per_frame: int
    max_contract_repairs: int = 1
    max_elapsed_seconds: float = 120.0

@dataclass(frozen=True)
class TurnUsage:
    model_segments: int = 0
    tool_rounds: int = 0
    tool_calls: int = 0
    visible_segments: int = 0
    contract_repairs: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None

@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, object]

@dataclass(frozen=True)
class ToolObservation:
    call_id: str
    name: str
    ok: bool
    content: str
    error_code: str | None = None

@dataclass(frozen=True)
class SegmentResult:
    index: int
    frames: tuple[Frame, ...]
    tool_calls: tuple[ToolCall, ...]
    finish: SegmentFinish

@dataclass(frozen=True)
class TurnRunResult:
    plan: MovePlan
    segments: tuple[SegmentResult, ...]
    frames: tuple[Frame, ...]
    usage: TurnUsage
    status: str

    @property
    def logical_text(self) -> str:
        return " ".join(frame.text for frame in self.frames)
```

```python
# tomo_core/src/tomo_core/context.py
from dataclasses import dataclass

@dataclass(frozen=True)
class ContextFact:
    value: object
    source: str
    observed_at: str | None = None
    confidence: str = "provided"
    sensitivity: str = "normal"

@dataclass(frozen=True)
class ContextSnapshot:
    history: tuple[dict[str, str], ...]
    visible_frames: tuple[str, ...]
    facts: tuple[ContextFact, ...] = ()

class ContextHydrator:
    def hydrate(self, request: ConversationRequest) -> ContextSnapshot:
        return ContextSnapshot(
            history=request.history,
            visible_frames=request.burst.visible_assistant_utterances,
        )
```

The first implementation has only current history and host-confirmed visible frames. It has no user-visible events and no placeholder memory/session/location network calls.

```python
# tomo_core/src/tomo_core/providers.py
@dataclass(frozen=True)
class ProviderTextDelta:
    text: str

@dataclass(frozen=True)
class ProviderToolCallReady:
    call_id: str
    name: str
    arguments_json: str

@dataclass(frozen=True)
class ProviderStreamCompleted:
    finish_reason: str
    input_tokens: int | None = None
    output_tokens: int | None = None

ProviderStreamEvent = ProviderTextDelta | ProviderToolCallReady | ProviderStreamCompleted

class ProviderAdapter(Protocol):
    name: str
    supports_tool_calls: bool

    def stream(
        self,
        messages: list[dict[str, object]],
        *,
        tools: tuple[dict[str, object], ...] = (),
        actor_id: str | None = None,
    ) -> Iterator[ProviderStreamEvent]: ...
```

The framed content protocol is JSON Lines in model content. Only complete lines are parsed:

```json
{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"answer directly","confidence":"high"}
{"type":"frame","text":"first complete bubble."}
{"type":"frame","text":"second complete bubble."}
```

The first segment requires exactly one `turn_plan` record before any frame. Later segments reuse the same plan and emit frame records only. Native provider `tool_calls`, not model-authored pseudo-tool JSON, define a `ToolBatch`.

## 6. Implementation sequence

### Task 1: Record the superseding architecture decision

**Objective:** Make the new segment/frame contract authoritative and explicitly supersede per-move realization.

**Files:**
- Modify: `tomo_core/docs/conversation-architecture.md`
- Create: `tomo_core/docs/adr/0001-turnrun-segment-frame-boundaries.md`
- Reference: `.hermes/plans/2026-07-11_010215-telegram-progressive-interruptible-turns.md`

**Step 1: Write the ADR**

Record status `Accepted`, context, decision, consequences, migration ordering, and these invariants:

```text
move != frame
segment != bubble
tool call != visible announcement
new model generation requires new information, except one undelivered contract repair
```

State that the older plan's “one move completion per utterance” is superseded while its input-burst, cancellation, fencing, and persistence decisions remain valid.

**Step 2: Update the architecture document**

Replace the current progressive section and 1-4 utterance invariant with `TurnRun -> Segment -> Frame -> Bubble`, the one-normal-segment rule, 1-3 frame hard maximum, tool-round boundaries, quiet context hydration, and deferred concrete context sources.

**Step 3: Review**

Run:

```bash
cd tomo_core
python -m unittest discover -s tests -v
```

Expected: existing baseline still passes because this task changes docs only.

**Optional commit, only if requested:**

```bash
git add tomo_core/docs/conversation-architecture.md tomo_core/docs/adr/0001-turnrun-segment-frame-boundaries.md
git commit -m "docs(core): define TurnRun segment boundaries"
```

### Task 2: Characterize streaming and native tool-call transport

**Objective:** Prove the configured xAI/SuperGrok endpoint's SSE and tool-call shapes before changing the runtime contract.

**Files:**
- Create: `tomo_core/tests/test_provider_streaming.py`
- Modify: `tomo_core/tests/test_grok_auth.py`
- Modify: `tomo_core/tests/test_oauth_provider.py`
- Modify: `tomo_core/tests/test_xai_api_supergrok_oauth.py`

**Step 1: Add fixture-based failing tests**

Build fake `httpx` stream responses containing:

```text
data: {"choices":[{"delta":{"content":"{\"type\":\"frame\""},"finish_reason":null}]}

data: {"choices":[{"delta":{"content":",\"text\":\"hi.\"}\n"},"finish_reason":null}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"search","arguments":"{\"q\":"}}]},"finish_reason":null}]}

data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"japan\"}"}}]},"finish_reason":"tool_calls"}]}

data: [DONE]
```

Assert arbitrary chunk boundaries, multiple tool calls by index, UTF-8 boundaries, malformed JSON, HTTP errors, missing `[DONE]`, and optional usage data.

**Step 2: Verify RED**

Run:

```bash
uv run python -m unittest tests.test_provider_streaming -v
```

Expected: FAIL because `ProviderAdapter.stream()` and stream event types do not exist.

**Step 3: Perform one privacy-safe live smoke outside unit tests**

Use the existing authenticated provider path with a harmless prompt and no logging of messages, output, token, actor ID, or credentials. Record only support booleans, event types, finish reason, and usage presence. If the endpoint differs from the fixtures, update the ADR and fixture shape before implementation. Do not add a permanent live-network test.

### Task 3: Select and centralize the frame character cap

**Objective:** Resolve the only unsettled response-contract value before code depends on it.

**Files:**
- Modify: `tomo_core/docs/adr/0001-turnrun-segment-frame-boundaries.md`
- Later consumed by: `tomo_core/src/tomo_core/models.py`

**Step 1: Obtain the product decision**

Select a hard `max_chars_per_frame` below or equal to Telegram's 4096-character transport limit. Record why it fits Tomo's 1-2 sentence target.

**Step 2: Add the exact accepted value to the ADR**

Do not use an undocumented magic number or silently inherit 4096 as the conversational product cap.

### Task 4: Add provider stream event models and SSE decoding

**Objective:** Expose content deltas, assembled native tool calls, completion, and usage without leaking provider JSON into the conversation layer.

**Files:**
- Modify: `tomo_core/src/tomo_core/providers.py`
- Test: `tomo_core/tests/test_provider_streaming.py`
- Test: `tomo_core/tests/test_grok_auth.py`
- Test: `tomo_core/tests/test_oauth_provider.py`

**Step 1: Implement strict stream event dataclasses**

Add `ProviderTextDelta`, `ProviderToolCallReady`, `ProviderStreamCompleted`, and the closed union. Validate nonblank tool names/IDs and nonnegative usage.

**Step 2: Implement one shared OpenAI-compatible stream helper**

Use `httpx.stream("POST", ..., json={..., "stream": True}, timeout=60)` and `response.iter_lines()`. Accumulate tool-call function name and arguments by provider index; emit `ProviderToolCallReady` only after the terminal finish reason.

**Step 3: Make all provider adapters delegate to the helper**

Preserve dynamic OAuth setup checks and `ProviderSetupRequired`. `GrokAuthProvider` and `OAuthBackedSuperGrokProvider` must use the same stream path as their blocking equivalents.

**Step 4: Keep a compatibility collector temporarily**

Implement `complete()` by collecting only text events from `stream()` and reject unexpected tool calls. Mark it internal compatibility, not the new engine path.

**Step 5: Verify GREEN**

Run:

```bash
uv run python -m unittest tests.test_provider_streaming tests.test_grok_auth tests.test_oauth_provider tests.test_xai_api_supergrok_oauth -v
```

Expected: all provider tests pass; auth/setup behavior is unchanged.

### Task 5: Introduce TurnRun, segment, frame, and budget models

**Objective:** Decouple conversational intent, generation segments, visible frames, and tool calls.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/conversation/__init__.py`
- Modify: `tomo_core/src/tomo_core/models.py`
- Test: `tomo_core/tests/test_conversation_models.py`

**Step 1: Write failing validation tests**

Cover negative indices, blank frames, more than three frames per segment, invalid budgets, budget usage exceeding limits, duplicate tool IDs, invalid finish/status values, and logical-text ordering across segments.

**Step 2: Verify RED**

```bash
uv run python -m unittest tests.test_conversation_models -v
```

**Step 3: Add the models from Section 5**

Rename `ResponseContract` fields to frame vocabulary or provide a short-lived compatibility property. Change the production default hard maximum from four utterances to three frames. Add the approved character cap.

**Step 4: Deprecate per-frame move metadata**

Replace `UtteranceReady(sequence, move, text)` with:

```python
@dataclass(frozen=True)
class FrameReady:
    sequence: int
    frame: Frame
```

Keep `MovePlan` only on `TurnRunStarted`/`TurnRunCompleted`. Do not infer one move for each frame.

**Step 5: Verify GREEN**

Expected: new model tests pass; failures in old engine/protocol tests are expected until later migration tasks.

### Task 6: Parse framed JSON Lines incrementally

**Objective:** Convert provider text deltas into complete validated plan/frame records without exposing partial tokens.

**Files:**
- Create: `tomo_core/src/tomo_core/conversation/framing.py`
- Create: `tomo_core/tests/test_conversation_framing.py`
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`

**Step 1: Write failing parser tests**

Test arbitrary delta boundaries, two records in one delta, final line with/without newline, escaped newlines inside frame text, duplicate/missing plan, frame-before-plan, extra keys, markdown, internal labels, banned dashes, sentence limit, character limit, too many frames, trailing garbage, and incomplete final JSON.

**Step 2: Verify RED**

```bash
uv run python -m unittest tests.test_conversation_framing -v
```

**Step 3: Implement `SegmentFrameParser`**

```python
class SegmentFrameParser:
    def __init__(self, *, first_segment: bool, budget: TurnBudget) -> None:
        self.first_segment = first_segment
        self.budget = budget
        self.buffer = ""
        self.plan: MovePlan | None = None
        self.frames: list[str] = []

    def feed(self, delta: str) -> tuple[MovePlan | Frame, ...]:
        self.buffer += delta
        ready: list[MovePlan | Frame] = []
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                ready.append(self._parse_line(line))
        return tuple(ready)

    def finish(self) -> tuple[MovePlan | Frame, ...]:
        ready = () if not self.buffer.strip() else (self._parse_line(self.buffer),)
        self.buffer = ""
        if self.first_segment and self.plan is None:
            raise ConversationOutputError("missing_turn_plan")
        return ready
```

Reuse one strict `validate_frame_text()` function for parser, runtime, sandbox protocol, and delivery. Do not split or truncate text.

**Step 4: Verify GREEN**

Expected: all framing tests pass.

### Task 7: Replace selection/step prompts with one segment prompt

**Objective:** Generate turn intent and all ordinary frames in one continuous model call.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Modify: `tomo_core/src/tomo_core/conversation/moves.py`
- Test: `tomo_core/tests/test_conversation_prompts.py`
- Test: `tomo_core/tests/test_conversation_scenarios.py`

**Step 1: Write failing prompt tests**

Assert the first-segment prompt:

- requests one internal `turn_plan` JSON line followed by zero to three frame JSON lines;
- describes moves as turn-level purposes, never bubble sections;
- targets one to two sentences and one to two frames;
- states the approved hard character/sentence/frame budgets;
- forbids action claims without observations;
- forbids automatic tool announcements and redundant completion messages;
- tells the model to batch independent related tool calls in one assistant response;
- allows zero visible frames before a tool call;
- says memory/session hydration is silent unless the user explicitly asked to consult it;
- does not promise unavailable booking or mutation capabilities;
- preserves plain text for a single text-only burst and structured boundaries for multi-message/attachment bursts.

**Step 2: Verify RED**

```bash
uv run python -m unittest tests.test_conversation_prompts tests.test_conversation_scenarios -v
```

**Step 3: Implement `build_segment_messages()`**

The first segment includes SOUL, current `ContextSnapshot`, user burst, allowed tool schemas, budgets, and all move procedures as planning vocabulary. Later segments include the fixed turn plan, prior visible frames, prior assistant native tool calls, and tool observations.

Delete `build_move_selection_messages()` and `build_step_realization_messages()` only after all callers migrate. Keep user content exclusively in user-role messages and tool observations in tool-role messages.

**Step 4: Verify GREEN**

Expected: prompt/scenario tests pass and no prompt instructs standalone per-move generation.

### Task 8: Add silent current-context hydration

**Objective:** Establish the future context-source seam without implementing unavailable memory, session-search, or location providers.

**Files:**
- Create: `tomo_core/src/tomo_core/context.py`
- Create: `tomo_core/tests/test_context.py`
- Modify: `tomo_core/src/tomo_core/runtime.py`

**Step 1: Write failing tests**

Assert hydration contains current history and host-confirmed visible frames in chronological order, emits no runtime event, makes no provider/tool call, and does not duplicate current-burst user messages.

**Step 2: Implement `ContextHydrator` and `ContextSnapshot`**

Use the exact minimal implementation in Section 5. Do not add empty “memory_search” tools, database tables, vector dependencies, or Telegram location parsing.

**Step 3: Verify**

```bash
uv run python -m unittest tests.test_context tests.test_runtime_conversation_moves -v
```

### Task 9: Implement the one-segment ordinary TurnRun

**Objective:** Make ordinary conversation use one model stream that can progressively emit several validated frames.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/engine.py`
- Modify: `tomo_core/src/tomo_core/conversation/__init__.py`
- Test: `tomo_core/tests/test_conversation_engine.py`

**Step 1: Replace per-move tests with failing coherence tests**

Use a scripted streaming provider. Assert:

- one provider stream call total for ordinary conversation;
- the plan and two frames arrive from that same call;
- frame 0 is yielded before the provider supplies frame 1;
- frame count is independent of selected move count;
- no separate selection call occurs;
- no repeated acknowledgement is generated by runtime;
- provider setup guidance still becomes one safe frame without structured parsing;
- activity becoming false closes consumption and suppresses later frames/completion.

**Step 2: Verify RED**

```bash
uv run python -m unittest tests.test_conversation_engine -v
```

**Step 3: Implement a first-segment runner**

Consume provider events incrementally. Feed only text deltas into `SegmentFrameParser`; parse native tool calls separately. Yield `TurnRunStarted` once the plan record is complete, then one `FrameReady` per validated frame. Emit `TurnRunCompleted` only after provider completion and budget reconciliation.

**Step 4: Implement bounded invalid-output behavior**

- No frame/tool observed: allow one replacement repair stream.
- Any valid frame emitted: abort malformed remainder, return `completed_partial`, and do not fabricate another frame.
- A second invalid replacement fails with the existing safe `ConversationOutputError` path.

**Step 5: Keep `respond()` only as a compatibility collector**

It collects `FrameReady` events into one `TurnRunResult`; production progressive flow uses the iterator.

**Step 6: Verify GREEN**

Expected: engine tests prove one normal stream and progressive complete-frame delivery.

### Task 10: Define read-only tool contracts and the actual bound registry

**Objective:** Add a tool-capability seam that cannot claim tools unavailable to the active runtime.

**Files:**
- Create: `tomo_core/src/tomo_core/tools.py`
- Create: `tomo_core/tests/test_tools.py`
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/instances.py`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py`

**Step 1: Write failing registry tests**

Assert duplicate names are rejected, schemas are valid objects, mutation/approval-required tools are rejected in v1, and the exact registry passed to `PersonalAgentRuntime` is the registry whose schemas reach `provider.stream()`.

**Step 2: Implement contracts**

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, object]
    read_only: bool = True
    parallel_safe: bool = True
    internal_context: bool = False

@dataclass(frozen=True)
class BoundTool:
    spec: ToolSpec
    invoke: Callable[[dict[str, object]], object]

class ToolRegistry:
    def __init__(self, tools: tuple[BoundTool, ...] = ()) -> None: ...
    def schemas(self) -> tuple[dict[str, object], ...]: ...
    def resolve(self, name: str) -> BoundTool: ...
```

The default registry is empty. Tests bind fake read-only tools. Do not report `supports_tool_calls=True` as equivalent to a nonempty bound registry.

**Step 3: Trace every construction path**

Pass the registry through local `RuntimeInstanceRegistry`, CLI runtime creation, and sandbox `build_runtime()`. Add assertions that the active runtime receives exactly the intended registry.

**Step 4: Verify**

```bash
uv run python -m unittest tests.test_tools tests.test_cli tests.test_sandbox_inbound -v
```

### Task 11: Execute safe independent calls as one ToolBatch

**Objective:** Batch independent related calls while preserving deterministic observation order and safe failure semantics.

**Files:**
- Create: `tomo_core/src/tomo_core/tool_execution.py`
- Create: `tomo_core/tests/test_tool_execution.py`

**Step 1: Write failing tests**

Cover two parallel-safe calls overlapping in time, stable observation order matching provider call order, one success plus one failure, unknown tool, invalid JSON arguments, mutation rejection, duplicate call IDs, batch larger than remaining call budget, cancellation before submission, and cancellation while workers finish.

**Step 2: Implement validation before execution**

Parse every arguments string as one JSON object. Resolve all tools. Reject the entire batch before starting if any call is unknown, malformed, not read-only, not parallel-safe, duplicated, or exceeds budget. V1 has no output-reference syntax; arguments containing unresolved runtime references are invalid.

**Step 3: Execute concurrently**

Use a bounded `ThreadPoolExecutor(max_workers=min(len(calls), remaining_tool_calls))`. Preserve provider order in returned observations regardless of completion order.

**Step 4: Normalize results safely**

A failure observation contains tool name, call ID, `ok=False`, and a safe error code. Never place exception text, credentials, paths, or traceback content in provider messages or logs. Do not automatically retry.

**Step 5: Verify GREEN**

```bash
uv run python -m unittest tests.test_tool_execution -v
```

### Task 12: Add multi-segment tool TurnRun orchestration and budgets

**Objective:** Continue generation only after verified observations and stop safely at hard execution limits.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/engine.py`
- Create: `tomo_core/tests/test_turn_run_tools.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`

**Step 1: Write failing end-to-end fake-tool tests**

Test:

1. Segment 0 emits no frame and requests one tool; Segment 1 emits final frames.
2. Segment 0 emits one useful pre-tool frame and two independent calls; both execute in one round; Segment 1 sees both observations.
3. A call dependent on an earlier result is requested only in Segment 1 and executes in a second round.
4. One partial batch failure reaches Segment 1 as a verified failure observation.
5. Five total calls are enforced even when distributed across batches.
6. Model segment six cannot request tools and must summarize verified partial results.
7. The third visible segment is reserved for the final result; interim frames are rejected/suppressed before consuming it.
8. A short tool run emits zero forced execution announcements.
9. The model cannot offer booking when no booking tool schema is bound.
10. Tool results never appear in Telegram except through a later validated frame.

**Step 2: Implement the loop**

```python
while True:
    segment = self._run_segment(state)
    state = state.record(segment)
    if segment.finish is SegmentFinish.COMPLETE:
        return state.complete()
    observations = self.tool_executor.execute_batch(
        segment.tool_calls,
        remaining_calls=state.remaining_tool_calls,
        is_active=is_active,
    )
    state = state.observe(observations)
```

Before each segment, calculate available tools and visible-frame allowance. Remove tools from the final reserved segment. A provider stop with neither frames nor tools is invalid.

**Step 3: Enforce elapsed-time and optional usage budgets**

Use monotonic time. Track provider-reported token usage when present. Token/cost caps remain disabled unless configured; do not pretend to enforce unavailable cost data.

**Step 4: Verify GREEN**

```bash
uv run python -m unittest tests.test_turn_run_tools -v
```

### Task 13: Migrate runtime persistence to frames and segment metadata

**Objective:** Persist one logical TurnRun while retaining visible-partial and provisional-generation behavior.

**Files:**
- Modify: `tomo_core/src/tomo_core/runtime.py`
- Modify: `tomo_core/src/tomo_core/sessions.py`
- Test: `tomo_core/tests/test_runtime_conversation_moves.py`
- Test: `tomo_core/tests/test_sessions.py`

**Step 1: Write failing runtime tests**

Assert inbound messages save before hydration/provider work; each frame is yielded immediately; cancellation suppresses unsent frames; completed logical text includes old visible partials exactly once; a tool TurnRun with several segments stores one assistant row; and malformed partial completion stores only verified visible frames.

**Step 2: Replace runtime utterance events**

Use `RuntimeFrameReady` and `RuntimeCompleted`. `_compose_progressive_bubble()` consumes a `Frame`; first newly generated frame replies to the newest burst message.

**Step 3: Persist compact metadata only**

Store:

```python
{
    "generation_id": burst.generation_id,
    "generation_status": "provisional",
    "turn_run": {
        "status": result.status,
        "primary_move": result.plan.primary.value,
        "supporting_moves": [move.value for move in result.plan.supporting],
        "response_goal": result.plan.response_goal,
        "segment_count": len(result.segments),
        "frame_count": len(result.frames),
        "tool_rounds": result.usage.tool_rounds,
        "tool_calls": result.usage.tool_calls,
    },
    "delivery_bubbles": [bubble.text for bubble in delivered],
}
```

Do not persist raw tool arguments, observations, provider chunks, hidden plan output, credentials, or exception messages. Preserve `save_atomic()` and its narrow `ENOSYS` behavior unchanged.

**Step 4: Verify**

```bash
uv run python -m unittest tests.test_runtime_conversation_moves tests.test_sessions -v
```

### Task 14: Introduce sandbox event protocol v3 with dual v2 reading

**Objective:** Transport frames without per-bubble move coupling while allowing an old sandbox to coexist during staged rollout.

**Files:**
- Modify: `tomo_core/src/tomo_core/sandbox_protocol.py`
- Test: `tomo_core/tests/test_sandbox_protocol.py`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py`
- Test: `tomo_core/tests/test_sandbox_inbound.py`

**Step 1: Split protocol constants**

```python
INBOUND_PROTOCOL_VERSION = 2
EVENT_PROTOCOL_VERSION = 3
LEGACY_EVENT_PROTOCOL_VERSION = 2
LEGACY_RESULT_PROTOCOL_VERSION = 1
```

Keep inbound v2 because its `InputBurst` shape remains valid. New sandboxes emit event v3; the host parser accepts event v2 and v3 during migration.

**Step 2: Define v3 events**

```json
{"version":3,"request_id":"...","generation_id":"...","sequence":0,"type":"frame","segment_index":0,"frame_index":0,"text":"..."}
{"version":3,"request_id":"...","generation_id":"...","sequence":1,"type":"completed","result":{"logical_text":"...","frames":[...],"plan":{...},"usage":{...},"status":"completed"}}
```

Tool calls and observations remain sandbox-internal and never cross this protocol.

**Step 3: Write failing compatibility tests**

Cover strict v3 frame ordering, segment/frame indices, content validation, completion reconciliation, v2 utterance mapping into a frame with optional `legacy_move`, ANSI prefix handling, gaps, conflicting duplicates, wrong IDs, terminal uniqueness, and no output after terminal.

**Step 4: Implement `SandboxFrameEvent`**

The parser returns one host-facing frame type for both v2 and v3. V2's move is optional legacy provenance only; v3 does not require it.

**Step 5: Verify**

```bash
uv run python -m unittest tests.test_sandbox_protocol tests.test_sandbox_inbound -v
```

Expected: strict protocol tests pass and existing narrow ANSI handling remains intact.

### Task 15: Migrate Railway delivery rows from moves to frames

**Objective:** Preserve distributed delivery fencing while storing segment/frame coordinates and optional legacy move provenance.

**Files:**
- Modify: `tomo_core/src/tomo_core/onboarding_store.py`
- Test: `tomo_core/tests/test_onboarding_store.py`
- Test: `tomo_core/tests/test_interruptible_telegram_turns.py`

**Step 1: Write failing fresh/migration tests**

Build both a fresh database and an existing database whose `telegram_delivery_events.move` is `NOT NULL`. Assert all rows survive migration and v3 rows can store `move=NULL`, `segment_index`, and `frame_index`.

**Step 2: Rebuild the table transactionally**

Use a create-copy-drop-rename migration:

```sql
create table telegram_delivery_events_v3(
  generation_id text not null,
  sequence integer not null,
  segment_index integer not null default 0,
  frame_index integer not null default 0,
  move text,
  text text not null,
  reply_to_message_id text,
  status text not null check(status in ('reserved','sent','unknown','suppressed')),
  telegram_message_id text,
  created_at real not null,
  updated_at real not null,
  primary key(generation_id, sequence)
);
```

Copy legacy rows with `segment_index=0` and `frame_index=sequence`. Do not use an empty-string fake move.

**Step 3: Change the reserve seam**

```python
reserve_delivery(
    generation_id,
    revision,
    sequence,
    segment_index,
    frame_index,
    text,
    reply_to_message_id,
    legacy_move=None,
) -> bool
```

All activity, reservation, sent/unknown/suppressed, recovery, and visible-context semantics remain unchanged.

**Step 4: Verify**

```bash
uv run python -m unittest tests.test_onboarding_store tests.test_interruptible_telegram_turns -v
```

### Task 16: Deliver v3 frames through local and Daytona dispatch

**Objective:** Carry the new frame event through every active runtime binding and keep Railway as the only sender.

**Files:**
- Modify: `tomo_core/src/tomo_core/shared_gateway.py`
- Modify: `tomo_core/src/tomo_core/sandbox_dispatch.py`
- Modify: `tomo_core/src/tomo_core/sandbox_inbound.py`
- Test: `tomo_core/tests/test_shared_gateway.py`
- Test: `tomo_core/tests/test_sandbox_dispatch.py`

**Step 1: Write failing binding tests**

Assert in-process and Daytona dispatch both yield `SandboxFrameEvent`; the host reserves and checks activity before every send; pacing remains 1.5 seconds; first new frame replies to the latest user message; stale frames become suppressed; and completion cannot finalize a superseded generation.

**Step 2: Update in-process mapping**

Map `RuntimeFrameReady` directly to `SandboxFrameEvent`. Do not synthesize move labels.

**Step 3: Update Daytona event iteration**

Keep request/generation/sequence validation, two-attempt auth refresh only before any yielded event, named process-session cleanup, and privacy-safe diagnostics.

**Step 4: Update the shared gateway**

Replace utterance branches with frame branches and call the migrated `reserve_delivery()`. Check `is_generation_active()` after reservation and immediately before Telegram send exactly as today.

**Step 5: Verify**

```bash
uv run python -m unittest tests.test_shared_gateway tests.test_sandbox_dispatch -v
```

### Task 17: Prove cancellation at every new boundary

**Objective:** Ensure streaming and concurrent tools do not weaken supersession correctness.

**Files:**
- Modify: `tomo_core/tests/test_runtime_conversation_moves.py`
- Modify: `tomo_core/tests/test_shared_gateway.py`
- Modify: `tomo_core/tests/test_sandbox_dispatch.py`
- Modify: `tomo_core/tests/test_interruptible_telegram_turns.py`

**Step 1: Add deterministic race tests**

Cover supersession:

- during provider SSE before the first complete frame;
- after frame 0 is emitted but before frame 1;
- after a tool batch starts but before observations return;
- after observations but before the next segment;
- after host reservation but before Telegram send;
- after Telegram send but before host acknowledgement;
- before terminal completion persistence.

**Step 2: Assert replacement context**

Only `sent` and conservatively `unknown` frame texts enter replacement visible context, in generation/sequence order. Suppressed or never-complete frame buffers do not.

**Step 3: Assert physical cancellation is optional**

Even if provider stream closure, worker cancellation, or Daytona session deletion fails, stale output cannot send or complete because SQLite activity checks remain authoritative.

**Step 4: Verify**

```bash
uv run python -m unittest tests.test_interruptible_telegram_turns tests.test_runtime_conversation_moves tests.test_shared_gateway tests.test_sandbox_dispatch -v
```

### Task 18: Remove obsolete per-move realization paths

**Objective:** Prevent accidental fallback to the multi-speaker architecture after all callers migrate.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/engine.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`
- Modify: `tomo_core/src/tomo_core/conversation/models.py`
- Modify: `tomo_core/src/tomo_core/providers.py`
- Modify: affected tests under `tomo_core/tests/`

**Step 1: Search for obsolete symbols**

```bash
rg "build_step_realization_messages|parse_utterance|UtteranceReady|SandboxUtteranceEvent|move_sequence.*utterance|standalone chat utterance" tomo_core/src tomo_core/tests
```

Expected before cleanup: only deliberate compatibility references remain.

**Step 2: Delete obsolete production paths**

Remove separate selection-call and per-move realization functions. Keep `MovePlan.sequence` only if still useful as internal purpose ordering; never use it to set frame count.

**Step 3: Decide `complete()` compatibility lifetime**

If no production caller requires blocking `complete()`, remove it from `ProviderAdapter`; otherwise retain only a tested collector with a deprecation comment.

**Step 4: Verify no stale semantic coupling**

Repeat the search. Expected: no production prompt or loop maps one move to one frame.

### Task 19: Run focused, full, and privacy checks

**Objective:** Prove the migration satisfies behavior, safety, and existing guarantees before deployment.

**Files:**
- No source changes unless a failing test exposes a defect.

**Step 1: Run focused suites**

```bash
cd tomo_core
uv run python -m unittest \
  tests.test_provider_streaming \
  tests.test_conversation_models \
  tests.test_conversation_framing \
  tests.test_conversation_prompts \
  tests.test_conversation_engine \
  tests.test_context \
  tests.test_tools \
  tests.test_tool_execution \
  tests.test_turn_run_tools \
  tests.test_runtime_conversation_moves \
  tests.test_sandbox_protocol \
  tests.test_sandbox_inbound \
  tests.test_onboarding_store \
  tests.test_interruptible_telegram_turns \
  tests.test_sandbox_dispatch \
  tests.test_shared_gateway -v
```

Expected: all focused tests pass.

**Step 2: Run the complete suite**

```bash
uv run python -m unittest discover -s tests -v
```

Expected: all tests pass with no skips hiding TurnRun behavior.

**Step 3: Run repository checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; `tomo_core/.venv/` remains untracked and unstaged; no credentials, generated commands, bytecode, caches, or probes are present.

**Step 4: Inspect privacy-safe diagnostics**

Logs may contain counts, lengths, event types, status, exception class, errno, and traceback basename/function/line. They must not contain message/history/frame text, tool arguments/results, IDs, credentials, paths containing secrets, or connection strings.

### Task 20: Stage protocol-compatible production deployment

**Objective:** Roll out host and sandbox changes without an incompatible v2/v3 window.

**Files:**
- Reference: `docs/daytona-railway.md`
- Reference: `tomo_core/scripts/create_daytona_snapshot.py`

**Step 1: Deploy a dual-reader host first**

The Railway host must accept both v2 utterance and v3 frame events while continuing to send inbound protocol v2. Verify healthy listener startup and old-snapshot replies before changing the desired sandbox snapshot.

**Step 2: Build an immutable snapshot from the reviewed commit**

Create a new unique snapshot name; never overwrite `tomo-core-20260711-5`. Smoke `sandbox-inbound --health`, one ordinary framed stream, and one fake/local tool TurnRun inside the image.

**Step 3: Update Railway's desired snapshot and redeploy**

Set `TOMO_DAYTONA_SNAPSHOT` to the new immutable snapshot. Wait for Railway `SUCCESS` and verify the deployed commit and configured snapshot without printing credentials or user identifiers.

**Step 4: Reconcile the target sandbox**

Confirm the target registry row and actual Daytona sandbox use the new snapshot. Preserve the mounted volume and `TOMO_DATA_DIR`/sandbox data-dir behavior.

**Step 5: Run real Telegram acceptance checks**

Use fresh conversations and verify:

1. A normal two-frame reply feels like one voice and comes from one model stream.
2. Frames arrive progressively and retain 1.5-second Telegram pacing.
3. A rapid interruption after frame 0 suppresses unsent frames and continues using the visible frame as context.
4. A short read-only tool run has no mechanical execution announcement.
5. A batched read-only test executes independent calls in one round and summarizes both verified observations.
6. No booking/action offer appears when no mutating capability is bound.
7. No protocol JSON, tool payload, move label, or diagnostic text reaches Telegram.

Production correctness is not established until these real checks pass.

**Step 6: Keep v2 support temporarily**

Do not remove the v2 event reader until every relevant sandbox is reconciled and a registry query proves no active sandbox can emit v2. Removal is a separate cleanup change.

## 7. Acceptance criteria

### Ordinary conversation

- One ordinary TurnRun uses exactly one normal provider stream call.
- One stream can produce one to three complete frames.
- Frame 0 can reach Telegram before frame 1 finishes generating.
- MovePlan metadata is created once and does not determine frame count.
- No “multiple people” repetition from independent per-move completions remains.

### Tools

- A model segment may request several independent bound read-only tools in one batch.
- Eligible calls execute concurrently and observations preserve request order.
- Dependent calls wait for a later segment after a verified observation.
- Partial failures are represented safely and do not erase successes.
- Hard limits enforce six model segments, five tool calls, five tool rounds, and three visible segments, with the final segment/visible segment reserved.
- No runtime-generated start/finish announcement exists.
- Unbound or mutating capabilities cannot be offered as executable actions.

### Context

- Current conversation and visible partials hydrate silently.
- Concrete memory search, previous-conversation search, and Telegram location retrieval remain unimplemented and are not advertised.
- Future internal context sources have a documented silent-by-default contract and provenance/freshness precedence.

### Validation and repair

- Frames target one to two sentences and hard-fail above three sentences or the approved character cap.
- Invalid frames are never truncated, split, or delivered.
- At most one replacement repair is allowed only before any frame/tool becomes visible/active.
- A malformed remainder after visible output finishes as a grounded partial result rather than generating a contradictory repair.

### Interruption and persistence

- Generation/revision fences remain authoritative before every send and completion.
- Visible sent/unknown frames survive supersession in chronological context.
- One logical assistant row represents the completed TurnRun.
- Raw tool payloads and provider chunks are not persisted in assistant metadata.
- Existing atomic session persistence and Daytona `ENOSYS` fallback remain covered.

### Protocol and deployment

- Event v3 carries frames and segment/frame indices without mandatory move metadata.
- Host reads both v2 and v3 during rollout; inbound remains v2.
- ANSI-prefix handling and strict request/generation/sequence validation remain intact.
- Railway sends Telegram messages; the sandbox never receives the bot token or destination authority.
- A new immutable Daytona snapshot, Railway success, target-sandbox reconciliation, and fresh Telegram checks are required.

## 8. Risks and tradeoffs

1. **Provider streaming shape may differ across OAuth/API paths.** Mitigation: Task 2 fixture characterization plus one privacy-safe live smoke before the engine migration.
2. **JSON Lines can be malformed after an earlier valid frame is visible.** Mitigation: validate complete records only, never expose partial data, permit repair only before visible output, and persist a partial grounded result otherwise.
3. **Concurrent tools cannot always be force-cancelled.** Mitigation: v1 tools are read-only; cancellation suppresses observations and later segments, while host fencing prevents stale delivery.
4. **Tool batching based only on semantic similarity would be unsafe.** Mitigation: model proposes a batch, but runtime requires resolved arguments, bound read-only tools, parallel safety, and budget compatibility.
5. **Protocol migration spans Railway and immutable sandboxes.** Mitigation: keep inbound v2, deploy a dual event reader first, then roll out v3 snapshots.
6. **SQLite table rebuild can lose delivery history if done incorrectly.** Mitigation: migration tests from a real legacy schema, one transaction, row-count/content assertions, and no destructive rollout without backup.
7. **A separate plan record consumes stream latency before frame 0.** Tradeoff: it preserves compact turn-level intent without a second model call. Keep it one small JSON line and measure time-to-first-frame in live acceptance.
8. **Token/cost caps cannot be honest without provider usage/pricing data.** Mitigation: collect optional usage now; enable enforcement only when explicit configured caps and pricing exist.
9. **No side-effect tools in v1.** This is intentional. Booking/purchase requires approvals, idempotency, durable checkpoints, and confirmation semantics beyond this read-only TurnRun migration.

## 9. Implementation handoff rules

- Before implementation, rerun `git status --short --branch`; preserve unrelated work and never stage `tomo_core/.venv/`.
- Delegate product coding through `/opencode` using `openai/gpt-5.6-terra`, variant `low`, as required for this repository.
- Give each delegated task its exact files, tests, and constraints from this plan.
- Independently inspect every delegated diff, trace changed symbols to all callers, and run the focused tests before proceeding.
- Use TDD: observe each new test fail for the intended reason before adding production code.
- Do not commit or push unless the user explicitly requests it. Optional commit checkpoints may follow the task boundaries above.
- Do not deploy until all focused/full tests pass and the host/sandbox compatibility order is ready.
