# Advisory MovePlan Contract Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Preserve Tomo’s conversational planning when the model produces a valid `turn_plan`, while allowing valid frames and native tool calls to proceed when advisory plan metadata is missing or harmlessly malformed.

**Architecture:** Add one deep output-contract module that owns the model-facing JSONL grammar, advisory plan normalization, source classification, and safe synthesized plans. `SegmentFrameParser` will resolve plans before frame or memory-control records; `ConversationEngine` will resolve tool-only first segments before tool execution. Frames, memory controls, native tool calls, budgets, side-effect checks, malformed JSON, and provider completion remain strict.

**Tech Stack:** Python 3.11, dataclasses/enums, streamed JSON Lines, `unittest`/pytest, existing fixed-schema latency telemetry, Daytona sandbox protocol.

---

## Scope and invariants

### In scope

- Put the canonical first/later-segment output grammar at the end of each system prompt.
- Stop asking the model to author `move_sequence`; derive it from primary + supporting moves.
- Normalize harmless `turn_plan` drift.
- Synthesize a safe plan when a first segment begins with a frame, memory control, or native tool call.
- Preserve `TurnRunStarted → MemoryControlReady/FrameReady/tool execution` ordering.
- Record `plan_model`, `plan_normalized`, or `plan_synthesized` as privacy-safe integer telemetry.
- Keep the existing one-repair budget for genuinely invalid output.

### Explicitly out of scope

- No provider `response_format` or JSON-schema mode. An enclosing structured object would complicate progressive frame delivery and native tool-call continuations.
- No additional provider retries or larger repair budget.
- No heuristic extraction from prose, Markdown, code fences, or malformed JSON.
- No loosening of frame, memory, tool, action, generation, or delivery fencing.
- No changes to bubble count policy or SOUL.

### Accepted behavior matrix

| Model output | Desired behavior |
|---|---|
| Valid plan + valid frames | Preserve model plan and frames; source `model` |
| Plan with missing/invalid advisory values + valid frames | Normalize plan and preserve frames; source `normalized` |
| Frame before plan | Emit synthesized direct-answer plan, then frame; source `synthesized` |
| Memory control before plan | Emit synthesized direct-answer plan, then validate control normally |
| Native tool call with no plan or frame | Emit synthesized tool-assisted plan before reaction/tool execution |
| Empty `stop` completion | `missing_frame`; repair as today |
| Malformed JSON/prose | `invalid_json`; repair/fail as today |
| Invalid frame | Existing safe frame error; repair/fail as today |
| Invalid memory control | Existing safe memory error; repair/fail as today |
| Malformed/unavailable/mismatched native tool call | Existing safe tool error; repair/fail as today |
| Duplicate or late plan | Keep strict initially; do not mutate an already-emitted plan |

---

## Task 1: Establish a clean baseline and protect the dirty worktree

**Objective:** Record the pre-change test baseline and ensure implementation touches only the contract slice.

**Files:**
- Read only: `tomo_core/src/tomo_core/conversation/*.py`
- Read only: `tomo_core/tests/test_conversation_*.py`
- Read only: `tomo_core/src/tomo_core/latency_trace.py`
- Read only: `tomo_core/src/tomo_core/sandbox_protocol.py`

**Step 1: Re-check the worktree**

Run:

```bash
git status --short --branch
```

Expected: many unrelated modified/untracked files. Do not reset, clean, stash, or reformat them.

**Step 2: Run the focused baseline**

Run:

```bash
cd tomo_core && uv run pytest -q \
  tests/test_conversation_parsing.py \
  tests/test_conversation_framing.py \
  tests/test_conversation_prompts.py \
  tests/test_conversation_engine.py \
  tests/test_latency_trace.py \
  tests/test_sandbox_protocol.py
```

Expected: all selected tests pass before edits. Record the count for the final comparison.

**Step 3: Confirm the intended edit set**

Expected production files:

```text
tomo_core/src/tomo_core/conversation/contract.py       # new
tomo_core/src/tomo_core/conversation/framing.py
tomo_core/src/tomo_core/conversation/prompts.py
tomo_core/src/tomo_core/conversation/engine.py
tomo_core/src/tomo_core/latency_trace.py
tomo_core/src/tomo_core/sandbox_protocol.py
```

Expected tests:

```text
tomo_core/tests/test_conversation_contract.py          # new
tomo_core/tests/test_conversation_framing.py
tomo_core/tests/test_conversation_prompts.py
tomo_core/tests/test_conversation_engine.py
tomo_core/tests/test_latency_trace.py
tomo_core/tests/test_sandbox_protocol.py
```

Do not commit unless the user explicitly requests it.

---

## Task 2: Create the advisory plan resolver as a deep module

**Objective:** Put plan normalization, source classification, safe defaults, and model-facing plan examples behind one small interface.

**Files:**
- Create: `tomo_core/src/tomo_core/conversation/contract.py`
- Create: `tomo_core/tests/test_conversation_contract.py`
- Modify only if needed for exports: `tomo_core/src/tomo_core/conversation/__init__.py`

**Step 1: Write failing source-classification tests**

Add tests equivalent to:

```python
from tomo_core.conversation.contract import PlanSource, resolve_advisory_plan
from tomo_core.conversation.models import ConversationMove, MoveConfidence


def test_complete_canonical_plan_is_model_owned():
    resolution = resolve_advisory_plan({
        "primary_move": "challenge",
        "supporting_moves": ["acknowledge", "explore"],
        "response_goal": "challenge the assumption",
        "confidence": "high",
        "reaction": None,
    })
    assert resolution.source is PlanSource.MODEL
    assert resolution.plan.primary is ConversationMove.CHALLENGE
    assert resolution.plan.supporting == (
        ConversationMove.ACKNOWLEDGE,
        ConversationMove.EXPLORE,
    )
    assert resolution.plan.sequence == (
        ConversationMove.CHALLENGE,
        ConversationMove.ACKNOWLEDGE,
        ConversationMove.EXPLORE,
    )


def test_sequence_is_runtime_derived_not_model_authored():
    resolution = resolve_advisory_plan({
        "primary_move": "answer",
        "supporting_moves": ["reassure"],
        "move_sequence": ["reassure", "answer"],
        "response_goal": "answer calmly",
        "confidence": "high",
        "reaction": None,
    })
    assert resolution.source is PlanSource.NORMALIZED
    assert resolution.plan.sequence == (
        ConversationMove.ANSWER,
        ConversationMove.REASSURE,
    )
```

**Step 2: Write failing normalization tests**

Cover all harmless drift in a table:

```python
@pytest.mark.parametrize(
    ("payload", "primary", "supporting", "confidence"),
    [
        ({}, ConversationMove.ANSWER, (), MoveConfidence.LOW),
        (
            {
                "primary_move": "unknown",
                "supporting_moves": ["reassure", "unknown", "reassure"],
                "response_goal": True,
                "confidence": "certain",
                "reaction": "unsupported",
                "extra": "ignored",
            },
            ConversationMove.ANSWER,
            (ConversationMove.REASSURE,),
            MoveConfidence.LOW,
        ),
    ],
)
def test_invalid_advisory_fields_normalize(payload, primary, supporting, confidence):
    resolution = resolve_advisory_plan(payload)
    assert resolution.source is PlanSource.NORMALIZED
    assert resolution.plan.primary is primary
    assert resolution.plan.supporting == supporting
    assert resolution.plan.confidence is confidence
    assert resolution.plan.reaction is None
```

Also assert:

- supporting moves are valid enums only, unique, different from primary, and capped at two;
- a valid compact `response_goal` is preserved;
- invalid/multiline/over-240-character goals use the same neutral direct-answer goal as `MovePlan.direct_answer()`;
- invalid reactions become `None`;
- unknown fields are never included in `repr(resolution.plan)` or returned metadata;
- the input mapping is not mutated.

**Step 3: Write failing synthesized-plan tests**

```python
from tomo_core.conversation.contract import synthesized_direct_plan, synthesized_tool_plan


def test_direct_fallback_is_low_confidence_and_has_no_reaction():
    resolution = synthesized_direct_plan()
    assert resolution.source is PlanSource.SYNTHESIZED
    assert resolution.plan.primary is ConversationMove.ANSWER
    assert resolution.plan.supporting == ()
    assert resolution.plan.confidence is MoveConfidence.LOW
    assert resolution.plan.reaction is None


def test_tool_fallback_preserves_act_then_answer_intent():
    resolution = synthesized_tool_plan()
    assert resolution.source is PlanSource.SYNTHESIZED
    assert resolution.plan.primary is ConversationMove.ACT
    assert resolution.plan.supporting == (ConversationMove.ANSWER,)
    assert resolution.plan.sequence == (
        ConversationMove.ACT,
        ConversationMove.ANSWER,
    )
```

**Step 4: Run the tests and verify RED**

Run:

```bash
cd tomo_core && uv run pytest -q tests/test_conversation_contract.py
```

Expected: import/module failures because `contract.py` does not exist.

**Step 5: Implement the minimal deep-module interface**

Create these public shapes in `contract.py`:

```python
class PlanSource(str, Enum):
    MODEL = "model"
    NORMALIZED = "normalized"
    SYNTHESIZED = "synthesized"


@dataclass(frozen=True)
class PlanResolution:
    plan: MovePlan
    source: PlanSource


def resolve_advisory_plan(payload: Mapping[str, object]) -> PlanResolution:
    ...


def synthesized_direct_plan() -> PlanResolution:
    ...


def synthesized_tool_plan() -> PlanResolution:
    ...
```

Implementation rules:

1. Parse `primary_move` as `ConversationMove`; fallback `ANSWER`.
2. Parse `supporting_moves` only when it is a list.
3. Keep at most two valid, unique moves that differ from primary.
4. Ignore model-authored `move_sequence`; pass `sequence=None` so `MovePlan` derives it.
5. Preserve only a compact, single-line, at-most-240-character `response_goal`; otherwise use the direct-answer default.
6. Parse confidence as `MoveConfidence`; fallback `LOW`.
7. Reuse the existing allowlisted, nonfatal reaction parser or move that tiny helper into this module without changing behavior.
8. Mark source `MODEL` only when the canonical fields are semantically valid and there are no deprecated/unknown fields. Any default, dropped value, or model-authored `move_sequence` marks `NORMALIZED`.
9. Never retain, return, log, or serialize unknown plan fields.

**Step 6: Run the contract tests and verify GREEN**

Run:

```bash
cd tomo_core && uv run pytest -q tests/test_conversation_contract.py
```

Expected: all contract tests pass.

**Step 7: Run model invariants**

Run:

```bash
cd tomo_core && uv run pytest -q tests/test_conversation_models.py tests/test_conversation_contract.py
```

Expected: existing `MovePlan` invariants and new resolver tests pass.

---

## Task 3: Make first-segment frames and memory controls synthesize a plan

**Objective:** Ensure the parser emits a safe plan before the first valid frame or memory control instead of raising `missing_plan`.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/framing.py:10-115`
- Modify: `tomo_core/tests/test_conversation_framing.py:12-110`

**Step 1: Replace the old missing-plan test with event-order tests**

Add:

```python
def test_first_frame_without_plan_emits_synthesized_plan_then_frame():
    parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
    records = parser.feed('{"type":"frame","text":"Direct answer."}\n')

    assert records[0] == MovePlan.direct_answer()
    assert records[1] == Frame(0, 0, "Direct answer.")
    assert parser.plan_source is PlanSource.SYNTHESIZED
    assert parser.finish() == []
```

Add a memory-control-first test using the existing valid `set_owner_setting` or memory-write fixture:

```python
def test_memory_control_without_plan_emits_plan_before_control():
    parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
    records = parser.feed(VALID_MEMORY_CONTROL_JSONL)

    assert isinstance(records[0], MovePlan)
    assert records[1].action == "set_owner_setting"
    assert parser.plan_source is PlanSource.SYNTHESIZED
```

**Step 2: Add normalized-plan parser tests**

Cover:

- canonical plan → `PlanSource.MODEL`;
- extra plan key → accepted, source `NORMALIZED`;
- invalid primary/supporting/confidence/goal + valid frame → normalized plan then frame;
- invalid reaction remains no reaction;
- model-authored `move_sequence` is ignored and runtime order is derived.

**Step 3: Preserve strict tests**

Keep or add assertions that these remain errors:

```text
invalid_json
invalid_record
unexpected_plan on later segments
duplicate_plan
late_plan
invalid_frame
frame_limit
late_memory_control
memory_control_limit
```

Change the empty first-segment expectation from `missing_plan` to no parser error:

```python
parser = SegmentFrameParser(segment_index=0, first_segment=True, budget=budget())
assert parser.finish() == []
```

The engine, not the parser, will classify an empty successful stream as `missing_frame`.

**Step 4: Run framing tests and verify RED**

Run:

```bash
cd tomo_core && uv run pytest -q tests/test_conversation_framing.py
```

Expected: frame-first, memory-first, normalization, source, and empty-finish assertions fail.

**Step 5: Implement parser-owned plan resolution**

In `SegmentFrameParser`:

- add read-only `plan_source: PlanSource | None`;
- replace `_parse_strict_move_plan_payload()` with `resolve_advisory_plan()` for `turn_plan` records;
- when the first segment receives its first `frame` or `memory_control` before a plan, prepend `synthesized_direct_plan().plan` to the returned record list;
- set `_plan_seen` and `_plan_source` before returning the synthesized plan;
- keep duplicate/late-plan checks strict;
- remove the `finish()` requirement that first segments must have a plan;
- keep later segments unchanged because their plan is fixed by the engine and their parser intentionally emits no plan.

Do not make malformed JSON skippable.

**Step 6: Run framing and parsing tests and verify GREEN**

Run:

```bash
cd tomo_core && uv run pytest -q \
  tests/test_conversation_contract.py \
  tests/test_conversation_framing.py \
  tests/test_conversation_parsing.py
```

Expected: all pass. Update old strict-plan unit tests to target `resolve_advisory_plan`; retain `_parse_strict_move_plan_payload` only if another real caller still requires it. Do not maintain two competing plan contracts solely for tests.

---

## Task 4: Preserve engine event order and synthesize tool-only plans

**Objective:** Make frame-first, memory-first, and tool-only turns produce exactly one `TurnRunStarted` before dependent events or tool execution, without a contract repair.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/engine.py:184-447`
- Modify: `tomo_core/tests/test_conversation_engine.py`

**Step 1: Write a frame-only engine regression**

```python
def test_frame_only_first_attempt_synthesizes_plan_without_repair():
    provider = ScriptedProvider([[
        ProviderTextDelta('{"type":"frame","text":"Fast answer."}\n'),
        ProviderStreamCompleted("stop"),
    ]])

    events = list(ConversationEngine(provider).respond_iter(self.request()))

    assert [type(event) for event in events] == [
        TurnRunStarted,
        FrameReady,
        TurnRunCompleted,
    ]
    assert events[0].plan == MovePlan.direct_answer()
    assert events[1].frame.text == "Fast answer."
    assert len(provider.calls) == 1
    assert events[-1].result.usage.contract_repairs == 0
```

**Step 2: Write a normalized-plan regression**

Feed a valid JSON plan containing invalid advisory values followed by a valid frame. Assert:

- one provider call;
- one `TurnRunStarted`;
- original frame text preserved;
- zero contract repairs;
- normalized low-confidence plan.

**Step 3: Write a memory-first ordering regression**

Assert event order:

```text
TurnRunStarted
MemoryControlReady
FrameReady
TurnRunCompleted
```

The runtime’s existing `_safe_memory_control()` remains the authority for whether the control is applied.

**Step 4: Write a tool-only first-segment regression**

Use a scripted first attempt containing `ProviderToolCallReady` + `ProviderStreamCompleted("tool_calls")` with no text, followed by a valid later-segment frame. Assert:

- `TurnRunStarted` is emitted before tool execution/reaction window;
- synthesized plan primary is `ACT`, supporting is `(ANSWER,)`;
- tool executes under existing registry/schema/budget checks;
- later segment receives the fixed synthesized plan;
- zero plan-related contract repairs;
- final grounded frame is delivered.

**Step 5: Write a genuinely invalid-output regression**

An empty `ProviderStreamCompleted("stop")` must still invoke the existing repair path with safe code `missing_frame`. A malformed JSON line must still invoke repair with `invalid_json`.

**Step 6: Run engine tests and verify RED**

Run:

```bash
cd tomo_core && uv run pytest -q tests/test_conversation_engine.py
```

Expected: new frame-only, normalization, memory-first, and tool-only tests fail.

**Step 7: Refactor duplicate record handling inside the engine**

The engine currently duplicates record handling for `parser.feed()` and `parser.finish()`. Before adding more branches, extract one local generator/helper inside `_respond_iter` that accepts a parsed record and performs:

- plan conflict check;
- plan assignment;
- `TurnRunStarted` emission;
- memory-control turn-limit check and event;
- frame/visible-segment/elapsed checks and event.

The helper must preserve the existing suspension timing around yielded events. This is a locality refactor, not a behavior rewrite.

**Step 8: Add tool-only synthesis at the correct seam**

After provider completion and `parser.finish()`, but before reaction-window/tool execution:

```python
if failure is None and tool_finish and index == 0 and plan is None:
    resolution = synthesized_tool_plan()
    plan = resolution.plan
    # emit source telemetry
    yield TurnRunStarted(plan)
```

Do not synthesize for:

- later segments;
- empty `stop` completions;
- provider failures;
- malformed/mismatched tool completion;
- unavailable native tools.

Validate finish reason, native-call presence, and schema availability before executing tools. Synthesis must not turn an invalid tool completion into a valid one.

**Step 9: Run engine tests and verify GREEN**

Run:

```bash
cd tomo_core && uv run pytest -q tests/test_conversation_engine.py
```

Expected: all existing and new engine tests pass.

---

## Task 5: Make the model-facing contract concise, canonical, and final

**Objective:** Reduce instruction competition while continuing to ask Tomo for one useful plan whenever it can produce one.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/contract.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py:109-162`
- Modify: `tomo_core/tests/test_conversation_contract.py`
- Modify: `tomo_core/tests/test_conversation_prompts.py:16-136`

**Step 1: Write renderer tests**

Add public renderer interfaces to the contract tests:

```python
render_first_segment_contract(...)
render_later_segment_contract(...)
```

Assert the first-segment renderer contains exactly one canonical plan example:

```json
{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"response_goal":"answer the user","confidence":"low","reaction":null}
```

Assert:

- no `move_sequence` appears;
- valid move names and reaction options are generated from enums/constants, not copied literals;
- plan language says `SHOULD emit one turn_plan before controls or frames`; omission is accepted by runtime but not advertised as the preferred path;
- ordinary completion requires one to the bounded maximum of frames;
- tool completion permits zero or one pre-tool frame and native tool calls are not JSONL records;
- memory controls remain before frames and strictly validated;
- physical-line/no-prose/no-Markdown rules are explicit;
- frame size/sentence/count limits interpolate from `TurnBudget`;
- no prompt content follows the final `OUTPUT CONTRACT` block.

**Step 2: Run prompt tests and verify RED**

Run:

```bash
cd tomo_core && uv run pytest -q \
  tests/test_conversation_contract.py \
  tests/test_conversation_prompts.py
```

Expected: renderer imports and new prompt-order assertions fail.

**Step 3: Implement canonical renderers**

Keep the interface small:

```python
def render_first_segment_contract(
    *,
    max_frames: int,
    max_sentences: int,
    max_chars: int,
    native_tools_available: bool,
) -> str:
    ...


def render_later_segment_contract(
    *,
    max_frames: int,
    max_sentences: int,
    max_chars: int,
    native_tools_available: bool,
) -> str:
    ...
```

The renderer owns serialization instructions and examples. `prompts.py` owns Tomo behavior, SOUL, capabilities, move procedures, grounding, and tool schemas.

**Step 4: Reorder prompt composition**

Build system prompts in this order:

1. Tomo identity and behavior/grounding constraints.
2. Tool guidance and schemas.
3. Capability skill index.
4. Full `<TOMO_SOUL>`.
5. Move vocabulary or fixed-plan context.
6. Final `OUTPUT CONTRACT` renderer output.

Nothing may be appended after the output contract.

Do not include model-controlled `response_goal` in a later-segment system message; retain the existing injection regression.

**Step 5: Align repair prompts**

- Fixed-plan repair: ask for frame records only, as today.
- No-plan repair: request a canonical plan plus mandatory valid frame records, but state the same no-prose JSONL grammar and keep native tools disabled.
- Empty output should repair for `missing_frame`, not `missing_plan`.
- Do not duplicate the full grammar manually in repair builders; reuse a short contract renderer/helper so prompt and repair wording cannot drift.

**Step 6: Run prompt tests and verify GREEN**

Run:

```bash
cd tomo_core && uv run pytest -q \
  tests/test_conversation_contract.py \
  tests/test_conversation_prompts.py
```

Expected: all pass, including SOUL, memory-skill, burst, tool-schema, grounding, injection, and final-contract-order assertions.

---

## Task 6: Add privacy-safe plan-source telemetry

**Objective:** Measure whether low reasoning produces model-owned, normalized, or synthesized plans without logging plan values or conversation text.

**Files:**
- Modify: `tomo_core/src/tomo_core/latency_trace.py:12-41`
- Modify: `tomo_core/src/tomo_core/sandbox_protocol.py:33-67`
- Modify: `tomo_core/src/tomo_core/conversation/engine.py`
- Modify: `tomo_core/tests/test_latency_trace.py`
- Modify: `tomo_core/tests/test_sandbox_protocol.py:319+`
- Modify: `tomo_core/tests/test_conversation_engine.py:119+`

**Step 1: Write failing allowlist tests**

Extend fixed-schema count tests for exactly these integer keys:

```text
plan_model
plan_normalized
plan_synthesized
```

Assert unknown keys and non-integer/negative values are still dropped.

**Step 2: Write failing forwarding tests**

For each source, encode one sandbox latency marker on the existing `sandbox_provider_move_plan_validated` phase and assert host parsing forwards one corresponding count set to `1`.

Never add plan text, move names, response goals, reactions, user IDs, or generated content.

**Step 3: Write failing engine emission tests**

Assert exactly one source count accompanies plan resolution:

```text
canonical plan      -> plan_model=1
normalized plan     -> plan_normalized=1
frame/tool fallback -> plan_synthesized=1
```

Do not emit the move-plan stage twice for one attempt.

**Step 4: Run tests and verify RED**

Run:

```bash
cd tomo_core && uv run pytest -q \
  tests/test_latency_trace.py \
  tests/test_sandbox_protocol.py \
  tests/test_conversation_engine.py
```

Expected: new count keys are rejected or missing.

**Step 5: Implement fixed-schema counts**

Add the three keys to both host and sandbox count allowlists. Extend the engine’s move-plan stage helper to add exactly one count based on `PlanSource`.

The existing phase name remains `sandbox_provider_move_plan_validated` for dashboard/log continuity, even when the plan was normalized or synthesized. In documentation, describe the phase as “resolved” semantically; do not rename it during temporary telemetry v1.

**Step 6: Run tests and verify GREEN**

Run:

```bash
cd tomo_core && uv run pytest -q \
  tests/test_latency_trace.py \
  tests/test_sandbox_protocol.py \
  tests/test_conversation_engine.py
```

Expected: all pass and privacy tests confirm no payload leakage.

---

## Task 7: Verify strict boundaries and conversational behavior together

**Objective:** Prove that advisory recovery does not soften authoritative contracts or alter valid Tomo output.

**Files:**
- Modify only tests if gaps remain: `tomo_core/tests/test_conversation_engine.py`
- Modify only tests if gaps remain: `tomo_core/tests/test_conversation_framing.py`
- Modify only tests if gaps remain: `tomo_core/tests/test_runtime_progressive.py`
- Modify only tests if gaps remain: `tomo_core/tests/test_personal_data.py`

**Step 1: Add a strict-boundary matrix**

Use scripted providers to verify:

- malformed JSON is never scanned for recoverable frames;
- prose before valid JSON remains invalid;
- extra frame keys remain invalid;
- oversized, multiline, Markdown, internal-label, and banned-dash frames remain invalid;
- memory control schemas remain exact and governance checks still run;
- tool call schemas, availability, finish reason, IDs, argument JSON, call budget, and confirmation paths remain strict;
- stale/cancelled generations still stop before delivery or side effects;
- a synthesized plan never creates a reaction;
- a normalized unsupported reaction never creates a reaction.

**Step 2: Add valid-output equivalence tests**

For an already-valid plan + two frames, compare before/after expectations:

- identical plan fields except runtime-derived sequence ordering where the model previously supplied a custom sequence;
- identical frame text and frame count;
- identical two `FrameReady` events;
- identical reaction behavior for allowlisted reactions;
- identical tool execution and grounded later-segment frames;
- zero repairs.

**Step 3: Run the focused behavioral suite**

Run:

```bash
cd tomo_core && uv run pytest -q \
  tests/test_conversation_contract.py \
  tests/test_conversation_parsing.py \
  tests/test_conversation_framing.py \
  tests/test_conversation_prompts.py \
  tests/test_conversation_engine.py \
  tests/test_runtime_progressive.py \
  tests/test_personal_data.py \
  tests/test_latency_trace.py \
  tests/test_sandbox_protocol.py
```

Expected: all pass.

---

## Task 8: Update architecture documentation without overwriting unrelated edits

**Objective:** Document the requested-vs-accepted contract and clarify that MovePlan is advisory, while authoritative controls remain strict.

**Files:**
- Targeted modify: `tomo_core/docs/conversation-architecture.md`
- Targeted modify if it contains the contract summary: `tomo_core/CONTEXT.md`

**Step 1: Re-read both files immediately before editing**

Both files are already modified in the current worktree. Use targeted patches only. Do not rewrite either file wholesale.

**Step 2: Document the invariant**

Add a concise table:

| Layer | Policy |
|---|---|
| Model-facing plan | SHOULD emit one canonical plan; no authored sequence |
| Runtime plan | Required internally; model-owned, normalized, or synthesized |
| Frames | Strict JSONL and content limits |
| Memory controls | Strict schema + runtime governance |
| Native tools | Strict schema, availability, budget, confirmation, and fencing |
| Malformed stream | Repair/fail; never heuristic salvage |

Document fallback behavior for ordinary, memory-first, and tool-only segments.

**Step 3: Verify documentation terminology**

Use `MovePlan`, `TurnRun`, `Segment`, `Frame`, and `Bubble` consistently. Do not claim MovePlan controls frame count or grants side-effect authority.

---

## Task 9: Run full verification and inspect the final diff

**Objective:** Ensure the implementation is complete, isolated, and ready for a controlled hosted experiment.

**Files:**
- No new files beyond the declared set.

**Step 1: Run the complete core suite**

Run:

```bash
cd tomo_core && uv run pytest -q
```

Expected: full suite passes. The previous verified baseline was 425 tests, but use the live pre-change count from Task 1 because the dirty worktree may contain additional user tests.

**Step 2: Run compile/import verification**

Run:

```bash
cd tomo_core && uv run python -m compileall -q src/tomo_core
```

Expected: exit code 0.

**Step 3: Inspect only the intended diff**

Run:

```bash
git diff -- \
  tomo_core/src/tomo_core/conversation/contract.py \
  tomo_core/src/tomo_core/conversation/framing.py \
  tomo_core/src/tomo_core/conversation/prompts.py \
  tomo_core/src/tomo_core/conversation/engine.py \
  tomo_core/src/tomo_core/latency_trace.py \
  tomo_core/src/tomo_core/sandbox_protocol.py \
  tomo_core/tests/test_conversation_contract.py \
  tomo_core/tests/test_conversation_framing.py \
  tomo_core/tests/test_conversation_prompts.py \
  tomo_core/tests/test_conversation_engine.py \
  tomo_core/tests/test_latency_trace.py \
  tomo_core/tests/test_sandbox_protocol.py \
  tomo_core/docs/conversation-architecture.md \
  tomo_core/CONTEXT.md
```

Expected: no drive-by changes, secrets, generated bytecode, lockfiles, dashboard edits, or unrelated formatting.

**Step 4: Optional commit checkpoint**

Only if the user explicitly requests a commit:

```bash
git add <only the reviewed files above>
git diff --cached --check
git commit -m "fix(core): tolerate advisory move plan drift"
```

Do not use `git add .` in this dirty worktree.

---

## Task 10: Controlled hosted A/B rollout

**Objective:** Verify that low reasoning becomes reliable without changing Tomo’s visible conversational quality.

**Prerequisite:** Only perform remote writes, commit/push, snapshot creation, Railway variable changes, or deployment after explicit user authorization.

**Step 1: Build the immutable Daytona snapshot when authorized**

These core runtime files are copied into the Daytona image, so a Railway-only redeploy is insufficient. Build a new immutable snapshot named from the deployed commit and require Daytona to report `active`.

**Step 2: Update Railway and redeploy when authorized**

Update only `TOMO_DAYTONA_SNAPSHOT`, redeploy the core service, and verify:

- deployment commit equals the intended source commit;
- configured snapshot equals the new active snapshot;
- startup logs show control API + shared Telegram listener;
- `/v1/health` returns HTTP 200;
- existing sandbox reconciles on the next message while retaining its volume.

**Step 3: Keep reasoning at medium for the first production smoke**

Send representative ordinary, reaction-eligible, memory, and tool turns. Verify no behavioral regression before combining the contract change with low reasoning.

**Step 4: Change only reasoning effort to low**

Set:

```text
TOMO_XAI_REASONING_EFFORT=low
```

Railway redeploy only; no new snapshot is required for this env-only change.

**Step 5: Run the A/B sample**

Use at least 20 representative turns, including:

- terse acknowledgements;
- factual answers;
- emotional reassurance;
- clarification;
- tool-free multi-bubble replies;
- native read-only tool calls;
- memory capture/governance language;
- reaction-eligible and reaction-ineligible messages.

Collect fixed-schema telemetry only.

**Acceptance criteria:**

- at least 95% of user turns complete without a contract repair;
- no whole-worker retry caused by `invalid_plan` or `missing_plan`;
- valid frame-only fallback completes on one provider attempt;
- `plan_synthesized` is rare insurance, not the dominant path;
- no increase in invalid frames, invalid memory controls, tool errors, partial completions, stale delivery, or duplicate bubbles;
- successful first-text latency remains in the observed low-effort range, approximately 3–5 seconds rather than 6–10 seconds;
- Tomo’s voice, answer substance, frame count, reactions on valid model plans, and tool grounding remain qualitatively intact.

**Rollback:**

1. Set `TOMO_XAI_REASONING_EFFORT=medium` immediately if quality or reliability regresses.
2. If the code contract itself regresses strict boundaries, restore the prior snapshot + Railway deployment rather than layering more retries.

---

## Risks and tradeoffs

1. **Normalized plans can hide model confusion.** Mitigation: source telemetry and 95% model/normalized first-attempt success target; investigate if synthesis dominates.
2. **Removing authored `move_sequence` changes metadata order.** Current later-segment prompts do not consume sequence, and visible frames are model-authored. Deriving primary then supporting is deterministic and removes schema drift.
3. **Tool-only fallback needs a different plan from ordinary fallback.** Use `ACT + ANSWER`, not `direct_answer()`, so later grounded segments retain tool intent.
4. **A synthesized plan has no reaction.** This intentionally skips an optional side effect rather than guessing.
5. **Memory controls arriving before a plan remain sensitive.** Plan synthesis does not authorize them; strict parsing plus `_safe_memory_control()` and governance revision checks remain mandatory.
6. **Prompt simplification can alter model behavior.** Keep full SOUL, capabilities, move procedures, grounding rules, and tool schemas; change only ordering and duplicated serialization language.
7. **Temporary telemetry spans host and sandbox allowlists.** Update and test both sides together or markers will be silently discarded.
8. **Dirty worktree collision.** Re-read modified docs/tests before every patch and stage only the reviewed slice if a commit is requested.

## Final completion checklist

- [ ] Canonical output contract is the final system-prompt block.
- [ ] Model no longer authors `move_sequence`.
- [ ] Valid canonical plans retain behavior.
- [ ] Harmless plan drift normalizes without repair.
- [ ] Frame-first and memory-first records synthesize direct plans.
- [ ] Tool-only first segments synthesize `ACT + ANSWER` plans.
- [ ] Exactly one `TurnRunStarted` precedes dependent events.
- [ ] Empty/malformed/unsafe output still repairs or fails strictly.
- [ ] Plan-source telemetry is fixed-schema and content-free.
- [ ] Focused and full suites pass.
- [ ] Final diff contains only intended files.
- [ ] Hosted rollout uses a new active Daytona snapshot when authorized.
- [ ] Low reasoning meets reliability and conversational-quality acceptance criteria.
