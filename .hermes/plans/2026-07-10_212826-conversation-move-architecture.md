# Tomo Conversational Move Architecture Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace Tomo’s single-pass “history → answer” behavior with a soul-aware conversational loop that interprets the situation, selects an ordered conversational move plan, realizes that plan as natural Telegram utterances, and returns compact metadata without redesigning sessions or memory.

**Architecture:** Add one deep `tomo_core.conversation` module whose external interface is `ConversationEngine.respond(ConversationRequest) -> ConversationResult`. Internally it performs two normal-path model calls: a structured move-selection pass and a soul-guided realization pass, with one repair call only when realization violates the response contract. `PersonalAgentRuntime` remains responsible for transport, current session loading/saving, and delivery; ingress, Daytona routing, and sandbox protocol remain unchanged.

**Tech Stack:** Python 3.11, stdlib dataclasses/enums/json, existing `ProviderAdapter`, LangGraph-shaped runtime, stdlib `unittest`, Telegram/Daytona adapters already in the repository.

---

## Scope and architectural decisions

### In scope

- Define the conversational domain language: situation, move, move plan, utterance, result.
- Mold every move’s objective and procedure to `tomo_core/SOUL.md`.
- Select one primary move and zero to two ordered supporting moves.
- Generate 1–4 intentional utterances, each capped at three sentences.
- Keep the move plan internal; never expose chain-of-thought or internal labels to Telegram.
- Preserve a compact move summary in the existing assistant-message metadata dictionary.
- Make malformed move-selection output degrade to a safe direct-answer plan.
- Give malformed realization output one bounded repair attempt.
- Keep the current logical assistant-turn invariant: several Telegram bubbles still represent one assistant turn.

### Explicitly deferred

- Durable memory, memory retrieval, Dream, embeddings, and memory curation.
- Session identity, session schema, atomic writes, idempotent turn persistence, and delivery retry duplication.
- Tool execution, planning/execution loops, approvals, and action-result observation.
- Group chat, `room_id`, other connectors, reactions, streaming, and typing refresh.
- Railway/Daytona deployment or snapshot rollout. Source changes will require a new immutable snapshot later, but deployment is a separate user-approved operation.

### Public seams selected for TDD

1. `ConversationEngine.respond(request) -> ConversationResult` is the primary conversation seam.
2. `DeliveryPlanner.compose_utterances(...) -> list[OutboundBubble]` is the expression/delivery seam.
3. `PersonalAgentRuntime.handle_telegram_text(...)` is the end-to-end runtime seam.
4. `sandbox_inbound.run_once(...)` remains the hosted protocol seam and must stay backward compatible.

Tests should assert behavior through these interfaces, not private prompt helpers or LangGraph node internals.

### Turn model

```text
InboundEnvelope
  → load existing conversation context
  → select MovePlan
      primary move
      optional supporting moves
      response goal
      confidence
  → realize MovePlan using SOUL + history + current message
      1–4 intentional utterances
      ≤3 sentences per utterance
  → validate/repair response contract
  → create Telegram bubbles
  → persist one logical turn + compact move metadata
```

The move is not the bubble. One move may span several utterances, and one utterance may blend a primary move with a supporting move.

---

## Proposed domain model

### Canonical terms

- **Conversation situation:** the current inbound message plus recent history and Tomo’s soul.
- **Conversational move:** the social/cognitive purpose Tomo chooses next, not a wording template.
- **Primary move:** the main purpose that must make the turn useful.
- **Supporting move:** an optional ordered move that helps the primary move land naturally.
- **Move plan:** the compact, non-chain-of-thought decision containing moves, goal, and confidence.
- **Utterance:** one intentional piece of outward expression; Telegram normally maps one utterance to one bubble.
- **Conversation result:** the move plan plus validated outward utterances.
- **Repair:** one bounded attempt to fix invalid structured output, not a reflective agent loop.

### Move catalog

| Move | Tomo-shaped objective | Procedure | Completion condition |
|---|---|---|---|
| `acknowledge` | Show precise attention without therapeutic warmth or fake excitement. | Identify what specifically deserves recognition; react briefly; do not hijack the topic. | The user can tell Tomo understood the important part. |
| `answer` | Give an honest, useful verdict rather than neutral assistant mush. | Answer first; support with strongest reasons; state uncertainty; explain practical consequence. | The explicit or implied question is resolved as far as evidence allows. |
| `clarify` | Resolve material ambiguity without a customer-support interrogation. | Name exactly what does not add up; explain why it changes the response; ask one sharp question. | The minimum missing information has been requested. |
| `explore` | Follow an interesting unresolved thread with genuine curiosity. | Identify the loose end; connect it to current/prior context; ask one focused question; yield if declined. | The user has a clear opening to deepen the thread. |
| `challenge` | Disagree plainly while staying accurate and useful. | Reconstruct the user’s position; identify what is cooked; challenge the decision or reasoning, not vulnerability; offer a stronger route. | The disagreement and better alternative are both clear. |
| `reassure` | Reduce uncertainty with reality, not manufactured optimism. | Name the real concern; separate actual danger from emotional intensity; state what remains controllable; offer a practical next step. | The user has a grounded picture and something useful to do. |
| `joke` | Build connection through observed absurdity or callbacks. | Find a real incongruity/callback; keep it brief; never invent a joke obligation; return to substance. | Humor adds connection without replacing the useful response. |
| `act` | Move from discussion toward a real outcome without pretending work happened. | Confirm briefly; identify prerequisites; execute only when capability/results exist; otherwise ask or offer the smallest next step; verify before claiming success. | A real action is verified or the exact prerequisite is clear. |
| `repair` | Correct misunderstanding or bad tone without defensiveness. | Say `mb` when natural; identify the exact miss; correct it; continue from the corrected understanding. | Shared understanding is restored. |
| `refuse` | Hold a real constraint without corporate policy speech. | Say no plainly; give the actual reason; avoid moralizing; offer the closest valid alternative. | The limit and useful alternative are unambiguous. |

Soul interpretation rules encoded in the catalog:

- “persistent curiosity” means one focused pursuit and a later callback, not overriding a direct refusal to discuss it.
- “resourceful anyhow” means legitimate alternatives, never bypassing consent, safety, or user intent.
- blunt honesty targets claims, plans, and decisions; it does not turn vulnerability into a roast.
- care is expressed through attention, truth, continuity, and competence rather than therapy language.
- slang and emoji remain sparse expression choices, not move types.

---

### Task 1: Record the conversation architecture and glossary

**Objective:** Create a stable design reference before changing runtime code.

**Files:**
- Create: `tomo_core/docs/conversation-architecture.md`

**Step 1: Write the architecture document**

Include the “Scope and architectural decisions,” “Canonical terms,” “Move catalog,” and turn model from this plan. End with these invariants:

```markdown
## invariants

1. every normal reply has exactly one primary conversational move.
2. a turn has at most two supporting moves, in order.
3. moves are internal decisions; users receive only natural utterances.
4. move plans contain no chain-of-thought.
5. one logical assistant turn may contain 1–4 physical telegram utterances.
6. each utterance has at most three sentences.
7. the full SOUL.md shapes both move selection and realization.
8. malformed planning output falls back safely; malformed realization gets one repair attempt.
9. no action is claimed without an observed result.
10. conversation architecture does not own sessions, memory, transport, or tools.
```

**Step 2: Review terminology against the code**

Run:

```bash
cd tomo_core
python -c "from pathlib import Path; text=Path('docs/conversation-architecture.md').read_text(); assert 'conversational move' in text; assert 'utterance' in text; assert 'chain-of-thought' in text"
```

Expected: exit code 0.

**Step 3: Optional commit checkpoint**

```bash
# optional, only if the user requested commits
 git add tomo_core/docs/conversation-architecture.md
 git commit -m "docs(core): define conversation move architecture"
```

---

### Task 2: Add conversation domain types

**Objective:** Introduce immutable, validated types for requests, move plans, and results without touching sessions.

**Files:**
- Create: `tomo_core/src/tomo_core/conversation/__init__.py`
- Create: `tomo_core/src/tomo_core/conversation/models.py`
- Create: `tomo_core/tests/test_conversation_models.py`

**Step 1: Write failing public-interface tests**

```python
import unittest

from tomo_core.conversation import (
    ConversationMove,
    ConversationRequest,
    ConversationResult,
    MoveConfidence,
    MovePlan,
)


class ConversationModelTests(unittest.TestCase):
    def test_move_plan_requires_one_primary_and_at_most_two_unique_supporting_moves(self):
        plan = MovePlan(
            primary=ConversationMove.ANSWER,
            supporting=(ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE),
            response_goal="give a verdict and ask one useful follow-up",
            confidence=MoveConfidence.HIGH,
        )
        self.assertEqual(plan.ordered_moves, (
            ConversationMove.ANSWER,
            ConversationMove.ACKNOWLEDGE,
            ConversationMove.EXPLORE,
        ))

        with self.assertRaises(ValueError):
            MovePlan(
                primary=ConversationMove.ANSWER,
                supporting=(ConversationMove.ANSWER,),
                response_goal="duplicate",
                confidence=MoveConfidence.LOW,
            )

        with self.assertRaises(ValueError):
            MovePlan(
                primary=ConversationMove.ANSWER,
                supporting=(
                    ConversationMove.ACKNOWLEDGE,
                    ConversationMove.EXPLORE,
                    ConversationMove.JOKE,
                ),
                response_goal="too many",
                confidence=MoveConfidence.LOW,
            )

    def test_result_requires_one_to_four_non_empty_utterances(self):
        plan = MovePlan.direct_answer()
        result = ConversationResult(plan=plan, utterances=("nahhh, this part is cooked.",))
        self.assertEqual(result.logical_text, "nahhh, this part is cooked.")

        with self.assertRaises(ValueError):
            ConversationResult(plan=plan, utterances=())

        with self.assertRaises(ValueError):
            ConversationResult(plan=plan, utterances=("one", "two", "three", "four", "five"))


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run the test to verify red**

Run:

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_conversation_models -v
```

Expected: FAIL because `tomo_core.conversation` does not exist.

**Step 3: Implement the domain types**

```python
# tomo_core/src/tomo_core/conversation/models.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from ..models import InboundEnvelope


class ConversationMove(str, Enum):
    ACKNOWLEDGE = "acknowledge"
    ANSWER = "answer"
    CLARIFY = "clarify"
    EXPLORE = "explore"
    CHALLENGE = "challenge"
    REASSURE = "reassure"
    JOKE = "joke"
    ACT = "act"
    REPAIR = "repair"
    REFUSE = "refuse"


class MoveConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class ConversationRequest:
    envelope: InboundEnvelope
    soul: str
    history: tuple[dict[str, str], ...]

    @classmethod
    def from_history(
        cls,
        *,
        envelope: InboundEnvelope,
        soul: str,
        history: Sequence[dict[str, str]],
    ) -> "ConversationRequest":
        return cls(envelope=envelope, soul=soul, history=tuple(dict(item) for item in history))


@dataclass(frozen=True)
class MovePlan:
    primary: ConversationMove
    supporting: tuple[ConversationMove, ...]
    response_goal: str
    confidence: MoveConfidence

    def __post_init__(self) -> None:
        if not self.response_goal.strip():
            raise ValueError("response_goal cannot be empty")
        if len(self.supporting) > 2:
            raise ValueError("a move plan can have at most two supporting moves")
        ordered = (self.primary, *self.supporting)
        if len(set(ordered)) != len(ordered):
            raise ValueError("move plan moves must be unique")

    @property
    def ordered_moves(self) -> tuple[ConversationMove, ...]:
        return (self.primary, *self.supporting)

    @classmethod
    def direct_answer(cls) -> "MovePlan":
        return cls(
            primary=ConversationMove.ANSWER,
            supporting=(),
            response_goal="respond directly and honestly to the user’s latest message",
            confidence=MoveConfidence.LOW,
        )


@dataclass(frozen=True)
class ConversationResult:
    plan: MovePlan
    utterances: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.utterances) <= 4:
            raise ValueError("conversation result must contain 1 to 4 utterances")
        if any(not utterance.strip() for utterance in self.utterances):
            raise ValueError("utterances cannot be empty")

    @property
    def logical_text(self) -> str:
        return " ".join(utterance.strip() for utterance in self.utterances)
```

```python
# tomo_core/src/tomo_core/conversation/__init__.py
from .engine import ConversationEngine
from .models import (
    ConversationMove,
    ConversationRequest,
    ConversationResult,
    MoveConfidence,
    MovePlan,
)

__all__ = [
    "ConversationEngine",
    "ConversationMove",
    "ConversationRequest",
    "ConversationResult",
    "MoveConfidence",
    "MovePlan",
]
```

During this task, omit the `ConversationEngine` export until Task 6 creates it, or create `engine.py` with a temporary import-safe placeholder only if needed. Do not leave a placeholder after Task 6.

**Step 4: Run the test to verify green**

Run the same unittest command. Expected: PASS.

---

### Task 3: Encode the soul-molded move catalog

**Objective:** Centralize move-specific procedures so prompts and future tools do not duplicate personality logic.

**Files:**
- Create: `tomo_core/src/tomo_core/conversation/moves.py`
- Create: `tomo_core/tests/test_conversation_moves.py`

**Step 1: Write a failing catalog completeness test**

```python
import unittest

from tomo_core.conversation.models import ConversationMove
from tomo_core.conversation.moves import MOVE_CATALOG, render_move_procedures


class ConversationMoveCatalogTests(unittest.TestCase):
    def test_every_move_has_one_soul_molded_definition(self):
        self.assertEqual(set(MOVE_CATALOG), set(ConversationMove))
        for move, definition in MOVE_CATALOG.items():
            with self.subTest(move=move):
                self.assertTrue(definition.objective)
                self.assertGreaterEqual(len(definition.procedure), 3)
                self.assertTrue(definition.completion)

    def test_render_only_includes_selected_move_procedures(self):
        rendered = render_move_procedures((ConversationMove.CHALLENGE, ConversationMove.JOKE))
        self.assertIn("challenge", rendered)
        self.assertIn("joke", rendered)
        self.assertNotIn("reassure", rendered)
        self.assertIn("challenge the decision or reasoning", rendered)
```

**Step 2: Verify red**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_conversation_moves -v
```

Expected: FAIL because `conversation.moves` does not exist.

**Step 3: Implement the complete catalog**

Use an immutable definition:

```python
from __future__ import annotations

from dataclasses import dataclass

from .models import ConversationMove


@dataclass(frozen=True)
class MoveDefinition:
    objective: str
    procedure: tuple[str, ...]
    completion: str


MOVE_CATALOG: dict[ConversationMove, MoveDefinition] = {
    ConversationMove.ACKNOWLEDGE: MoveDefinition(
        objective="show precise attention without therapeutic warmth or fake excitement",
        procedure=(
            "identify the specific thing that deserves recognition",
            "react briefly and genuinely",
            "do not hijack the topic or praise by default",
        ),
        completion="the user can tell tomo understood the important part",
    ),
    ConversationMove.ANSWER: MoveDefinition(
        objective="give an honest useful verdict instead of neutral assistant mush",
        procedure=(
            "answer or give the verdict first",
            "support it with the strongest reasons available",
            "state uncertainty plainly and explain the practical consequence",
        ),
        completion="the explicit or implied question is resolved as far as evidence allows",
    ),
    ConversationMove.CLARIFY: MoveDefinition(
        objective="resolve material ambiguity without a customer-support interrogation",
        procedure=(
            "name exactly what does not add up",
            "state why that ambiguity changes the response",
            "ask one sharp minimum-information question",
        ),
        completion="the minimum missing information has been requested",
    ),
    ConversationMove.EXPLORE: MoveDefinition(
        objective="follow a genuinely interesting unresolved thread",
        procedure=(
            "identify the loose end",
            "connect it to current or prior context",
            "ask one focused question and yield if the user declines",
        ),
        completion="the user has a clear opening to deepen the thread",
    ),
    ConversationMove.CHALLENGE: MoveDefinition(
        objective="disagree plainly while remaining accurate and useful",
        procedure=(
            "reconstruct the user’s position accurately",
            "identify what is cooked and challenge the decision or reasoning, not vulnerability",
            "offer a stronger route rather than stopping at criticism",
        ),
        completion="the disagreement and the better alternative are clear",
    ),
    ConversationMove.REASSURE: MoveDefinition(
        objective="reduce uncertainty with reality instead of manufactured optimism",
        procedure=(
            "name the real concern without therapy language",
            "separate actual danger from emotional intensity",
            "state what remains controllable and offer one practical next step",
        ),
        completion="the user has a grounded picture and something useful to do",
    ),
    ConversationMove.JOKE: MoveDefinition(
        objective="build connection through observed absurdity or a real callback",
        procedure=(
            "find an actual incongruity or callback",
            "keep the joke brief and never force slang",
            "return to the substantive point",
        ),
        completion="humor adds connection without replacing usefulness",
    ),
    ConversationMove.ACT: MoveDefinition(
        objective="move toward a real outcome without pretending work happened",
        procedure=(
            "confirm briefly and identify prerequisites",
            "execute only when a capability and observed result exist",
            "otherwise ask for or offer the smallest next step; verify before claiming success",
        ),
        completion="a real action is verified or the exact prerequisite is clear",
    ),
    ConversationMove.REPAIR: MoveDefinition(
        objective="correct misunderstanding or bad tone without defensiveness",
        procedure=(
            "say mb when it sounds natural",
            "identify the exact miss",
            "correct it and continue from the repaired understanding",
        ),
        completion="shared understanding is restored",
    ),
    ConversationMove.REFUSE: MoveDefinition(
        objective="hold a real constraint without corporate policy speech",
        procedure=(
            "say no plainly",
            "give the actual reason without moralizing",
            "offer the closest valid alternative",
        ),
        completion="the limit and useful alternative are unambiguous",
    ),
}


def render_move_procedures(moves: tuple[ConversationMove, ...]) -> str:
    sections: list[str] = []
    for move in moves:
        definition = MOVE_CATALOG[move]
        steps = "\n".join(f"{index}. {step}" for index, step in enumerate(definition.procedure, 1))
        sections.append(
            f"## {move.value}\n"
            f"objective: {definition.objective}\n"
            f"procedure:\n{steps}\n"
            f"completion: {definition.completion}"
        )
    return "\n\n".join(sections)
```

**Step 4: Verify green**

Run the same unittest command. Expected: PASS.

---

### Task 4: Parse move-selection output safely

**Objective:** Turn model JSON into a validated `MovePlan` without accepting chain-of-thought or breaking the turn on malformed text.

**Files:**
- Create: `tomo_core/src/tomo_core/conversation/parsing.py`
- Create: `tomo_core/tests/test_conversation_parsing.py`

**Step 1: Write failing parser tests**

Cover:

```python
import unittest

from tomo_core.conversation.models import ConversationMove, MoveConfidence
from tomo_core.conversation.parsing import parse_move_plan


class ConversationParsingTests(unittest.TestCase):
    def test_parse_move_plan_accepts_only_compact_decision_fields(self):
        plan = parse_move_plan('''{
          "primary_move": "challenge",
          "supporting_moves": ["acknowledge", "explore"],
          "response_goal": "challenge the assumption and ask for the missing evidence",
          "confidence": "high"
        }''')
        self.assertEqual(plan.primary, ConversationMove.CHALLENGE)
        self.assertEqual(plan.supporting, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE))
        self.assertEqual(plan.confidence, MoveConfidence.HIGH)

    def test_parse_move_plan_rejects_unknown_fields_and_falls_back(self):
        plan = parse_move_plan('{"primary_move":"answer","chain_of_thought":"hidden"}')
        self.assertEqual(plan.primary, ConversationMove.ANSWER)
        self.assertEqual(plan.confidence, MoveConfidence.LOW)

    def test_parse_move_plan_falls_back_for_markdown_or_invalid_json(self):
        for payload in ("```json\n{}\n```", "not json", "{}"):
            with self.subTest(payload=payload):
                self.assertEqual(parse_move_plan(payload).primary, ConversationMove.ANSWER)
```

**Step 2: Verify red**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_conversation_parsing -v
```

**Step 3: Implement strict parsing**

The parser must:

- call `json.loads` directly; do not scrape arbitrary JSON from prose;
- require exactly `primary_move`, `supporting_moves`, `response_goal`, and `confidence`;
- validate enum values and supporting-list length through `MovePlan`;
- return `MovePlan.direct_answer()` on any `TypeError`, `ValueError`, `KeyError`, or `json.JSONDecodeError`;
- never log or return malformed model text.

```python
_ALLOWED_MOVE_PLAN_KEYS = {
    "primary_move",
    "supporting_moves",
    "response_goal",
    "confidence",
}


def parse_move_plan(raw: str) -> MovePlan:
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != _ALLOWED_MOVE_PLAN_KEYS:
            raise ValueError("invalid move plan keys")
        supporting = payload["supporting_moves"]
        if not isinstance(supporting, list):
            raise ValueError("supporting_moves must be a list")
        return MovePlan(
            primary=ConversationMove(payload["primary_move"]),
            supporting=tuple(ConversationMove(item) for item in supporting),
            response_goal=str(payload["response_goal"]),
            confidence=MoveConfidence(payload["confidence"]),
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return MovePlan.direct_answer()
```

**Step 4: Verify green**

Run the same unittest command. Expected: PASS.

---

### Task 5: Build soul-aware selection and realization prompts

**Objective:** Make the model choose moves from context and then follow only the selected move procedures while preserving Tomo’s full soul.

**Files:**
- Create: `tomo_core/src/tomo_core/conversation/prompts.py`
- Create: `tomo_core/tests/test_conversation_prompts.py`

**Step 1: Write failing prompt-contract tests**

Test through the public prompt builders because these strings are a provider contract:

```python
import unittest

from tomo_core.conversation.models import ConversationMove, MovePlan
from tomo_core.conversation.prompts import build_move_selection_messages, build_realization_messages
from tomo_core.models import InboundEnvelope


class ConversationPromptTests(unittest.TestCase):
    def setUp(self):
        self.envelope = InboundEnvelope(
            connector="telegram",
            actor_id="user-1",
            message_id="message-1",
            text="my interview is tomorrow",
        )
        self.soul = "TOMO SOUL SENTINEL"
        self.history = ({"role": "user", "content": "i really need this job"},)

    def test_selection_receives_full_soul_history_message_and_closed_schema(self):
        messages = build_move_selection_messages(self.soul, self.history, self.envelope)
        joined = "\n".join(message["content"] for message in messages)
        self.assertIn(self.soul, joined)
        self.assertIn("i really need this job", joined)
        self.assertIn("my interview is tomorrow", joined)
        self.assertIn('"primary_move"', joined)
        self.assertIn("do not output chain-of-thought", joined)

    def test_realization_receives_only_selected_procedures_and_utterance_contract(self):
        plan = MovePlan(
            primary=ConversationMove.REASSURE,
            supporting=(ConversationMove.EXPLORE,),
            response_goal="ground the concern and ask how prepared they feel",
            confidence="high",
        )
        messages = build_realization_messages(self.soul, self.history, self.envelope, plan)
        joined = "\n".join(message["content"] for message in messages)
        self.assertIn("reassure", joined)
        self.assertIn("explore", joined)
        self.assertNotIn("## challenge", joined)
        self.assertIn("1 to 4", joined)
        self.assertIn("at most 3 sentences", joined)
        self.assertIn("never mention moves", joined)
```

Use `MoveConfidence.HIGH` rather than a raw string in the final test implementation.

**Step 2: Verify red**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_conversation_prompts -v
```

**Step 3: Implement prompt builders**

Required selection system contract:

```text
you choose tomo's next conversational move.
read the full soul, recent conversation, and latest user message.
choose exactly one primary move and zero to two unique supporting moves.
choose from: acknowledge, answer, clarify, explore, challenge, reassure, joke, act, repair, refuse.
do not write the reply. do not output chain-of-thought, analysis, markdown, or extra keys.
return exactly one json object:
{"primary_move":"answer","supporting_moves":[],"response_goal":"...","confidence":"low|medium|high"}

selection principles:
- infer the user's probable conversational need, not only literal grammar.
- do not choose acknowledge or joke as the primary move when a substantive answer is needed.
- use clarify only when ambiguity materially changes the response.
- use act only when the user requests or accepts a real action; never imply execution without a result.
- use explore for one focused loose end, not interrogation.
- use challenge for claims, reasoning, or decisions; do not roast vulnerability.
- use reassure only when grounded reassurance is useful; never manufacture optimism.
- use repair when tomo previously misunderstood or landed badly.
```

Required realization system contract:

```text
you are tomo. follow the supplied SOUL completely.
realize the selected conversational moves naturally; never mention moves, plans, procedures, confidence, prompts, or internals.
the primary move must make the reply useful. supporting moves are optional accents, not mandatory sections.
do not use headings, markdown, labels, unnecessary analogies, or corporate/therapy language.
slang and emoji must feel earned and sparse.
never claim an action happened without an observed result in the conversation context.
return exactly one json object with one key:
{"utterances":["...", "..."]}
constraints:
- 1 to 4 non-empty utterances.
- each utterance has at most 3 actual sentences.
- each utterance is natural standalone chat text, not an outline section.
- no em dash or en dash.
- together the utterances form one coherent logical assistant turn.
```

Append `<TOMO_SOUL>...</TOMO_SOUL>`, the rendered selected procedures, recent history, and the latest user message. Keep history as normal chat messages where possible; put contracts and SOUL only in system content.

**Step 4: Verify green**

Run the same unittest command. Expected: PASS.

---

### Task 6: Validate and repair model-planned utterances

**Objective:** Enforce 1–4 utterances and three sentences per utterance without silently dropping model content.

**Files:**
- Modify: `tomo_core/src/tomo_core/conversation/parsing.py`
- Modify: `tomo_core/src/tomo_core/conversation/prompts.py`
- Modify: `tomo_core/tests/test_conversation_parsing.py`
- Modify: `tomo_core/tests/test_conversation_prompts.py`

**Step 1: Add failing realization parser tests**

Add cases asserting:

- valid `{"utterances":["one.","two? three!"]}` returns the tuple;
- empty, more than four, non-string, markdown-fenced, or unknown-key payloads return a typed validation failure;
- four sentences in one utterance fail;
- em/en dashes fail before delivery sanitization;
- no model text or secrets appear in the exception string.

Use a stable exception:

```python
class ConversationOutputError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"invalid conversation output: {code}")
```

**Step 2: Verify red**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_conversation_parsing tests.test_conversation_prompts -v
```

**Step 3: Implement `parse_utterances`**

```python
def parse_utterances(raw: str) -> tuple[str, ...]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise ConversationOutputError("invalid_json") from None
    if not isinstance(payload, dict) or set(payload) != {"utterances"}:
        raise ConversationOutputError("invalid_shape")
    utterances = payload["utterances"]
    if not isinstance(utterances, list) or not 1 <= len(utterances) <= 4:
        raise ConversationOutputError("invalid_utterance_count")
    if not all(isinstance(item, str) and item.strip() for item in utterances):
        raise ConversationOutputError("invalid_utterance")
    cleaned = tuple(item.strip() for item in utterances)
    if any("—" in item or "–" in item for item in cleaned):
        raise ConversationOutputError("banned_dash")
    if any(len(split_sentences(item)) > 3 for item in cleaned):
        raise ConversationOutputError("sentence_limit")
    return cleaned
```

Import the existing `split_sentences` from `tomo_core.delivery` initially. Do not duplicate sentence counting. If this creates an undesirable package dependency during implementation, move `split_sentences` into a neutral `text.py` only after a test demonstrates the cycle; do not pre-emptively refactor.

**Step 4: Add the repair prompt builder**

`build_repair_messages(...)` receives the original realization messages, the invalid raw output, and only the safe validation code. It must instruct the same model to return corrected JSON and must not ask for explanation.

```text
the previous output violated this response contract: <safe_code>.
return a corrected json object only. preserve the intended meaning and conversational moves.
```

The engine will call this at most once.

**Step 5: Verify green**

Run the same unittest command. Expected: PASS.

---

### Task 7: Implement the deep `ConversationEngine` interface

**Objective:** Orchestrate move selection, soul-shaped realization, and one bounded repair behind one small interface.

**Files:**
- Create: `tomo_core/src/tomo_core/conversation/engine.py`
- Modify: `tomo_core/src/tomo_core/conversation/__init__.py`
- Create: `tomo_core/tests/test_conversation_engine.py`

**Step 1: Write failing behavior tests using a scripted provider**

```python
import unittest

from tomo_core.conversation import ConversationEngine, ConversationMove, ConversationRequest
from tomo_core.models import InboundEnvelope


class ScriptedProvider:
    name = "scripted"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, actor_id=None):
        self.calls.append((messages, actor_id))
        return self.responses.pop(0)


class ConversationEngineTests(unittest.TestCase):
    def request(self):
        return ConversationRequest.from_history(
            envelope=InboundEnvelope(
                connector="telegram",
                actor_id="u1",
                message_id="m1",
                text="my interview is tomorrow",
            ),
            soul="SOUL SENTINEL",
            history=[{"role": "user", "content": "i need this job"}],
        )

    def test_respond_selects_moves_then_realizes_only_the_selected_plan(self):
        provider = ScriptedProvider([
            '{"primary_move":"reassure","supporting_moves":["explore"],"response_goal":"ground the fear and ask about preparation","confidence":"high"}',
            '{"utterances":["yeah, tomorrow matters, but ur not cooked just because ur nervous.","how prepared do u actually feel rn?"]}',
        ])

        result = ConversationEngine(provider).respond(self.request())

        self.assertEqual(result.plan.primary, ConversationMove.REASSURE)
        self.assertEqual(result.plan.supporting, (ConversationMove.EXPLORE,))
        self.assertEqual(len(result.utterances), 2)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([call[1] for call in provider.calls], ["u1", "u1"])
        realization_text = "\n".join(message["content"] for message in provider.calls[1][0])
        self.assertIn("SOUL SENTINEL", realization_text)
        self.assertIn("reassure", realization_text)
        self.assertNotIn("## challenge", realization_text)

    def test_invalid_selection_falls_back_without_a_third_normal_path_call(self):
        provider = ScriptedProvider(["not json", '{"utterances":["straight answer."]}'])
        result = ConversationEngine(provider).respond(self.request())
        self.assertEqual(result.plan.primary, ConversationMove.ANSWER)
        self.assertEqual(result.utterances, ("straight answer.",))
        self.assertEqual(len(provider.calls), 2)

    def test_invalid_realization_is_repaired_exactly_once(self):
        provider = ScriptedProvider([
            '{"primary_move":"answer","supporting_moves":[],"response_goal":"answer directly","confidence":"high"}',
            "not json",
            '{"utterances":["fixed answer."]}',
        ])
        result = ConversationEngine(provider).respond(self.request())
        self.assertEqual(result.utterances, ("fixed answer.",))
        self.assertEqual(len(provider.calls), 3)

    def test_second_invalid_realization_fails_with_safe_code(self):
        provider = ScriptedProvider([
            '{"primary_move":"answer","supporting_moves":[],"response_goal":"answer directly","confidence":"high"}',
            "not json",
            "still not json",
        ])
        with self.assertRaisesRegex(ValueError, "invalid conversation output"):
            ConversationEngine(provider).respond(self.request())
```

**Step 2: Verify red**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_conversation_engine -v
```

**Step 3: Implement the engine**

```python
from __future__ import annotations

from ..providers import ProviderAdapter
from .models import ConversationRequest, ConversationResult
from .parsing import ConversationOutputError, parse_move_plan, parse_utterances
from .prompts import (
    build_move_selection_messages,
    build_realization_messages,
    build_repair_messages,
)


class ConversationEngine:
    def __init__(self, provider: ProviderAdapter) -> None:
        self.provider = provider

    def respond(self, request: ConversationRequest) -> ConversationResult:
        actor_id = request.envelope.actor_id
        selection_messages = build_move_selection_messages(
            request.soul,
            request.history,
            request.envelope,
        )
        raw_plan = self.provider.complete(selection_messages, actor_id=actor_id)
        plan = parse_move_plan(raw_plan)

        realization_messages = build_realization_messages(
            request.soul,
            request.history,
            request.envelope,
            plan,
        )
        raw_realization = self.provider.complete(realization_messages, actor_id=actor_id)
        try:
            utterances = parse_utterances(raw_realization)
        except ConversationOutputError as error:
            repair_messages = build_repair_messages(
                realization_messages,
                raw_realization,
                error.code,
            )
            repaired = self.provider.complete(repair_messages, actor_id=actor_id)
            utterances = parse_utterances(repaired)

        return ConversationResult(plan=plan, utterances=utterances)
```

Do not catch provider/network exceptions here. Existing sandbox error mapping must continue to classify HTTP 401 as `auth_expired` and other failures as safe runtime/provider errors.

**Step 4: Verify green**

Run the same unittest command. Expected: PASS.

**Step 5: Run all new conversation tests**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest \
  tests.test_conversation_models \
  tests.test_conversation_moves \
  tests.test_conversation_parsing \
  tests.test_conversation_prompts \
  tests.test_conversation_engine -v
```

Expected: all conversation tests pass.

---

### Task 8: Let delivery consume intentional utterances

**Objective:** Stop sentence-chopping a single draft when the conversation engine has already produced validated utterances.

**Files:**
- Modify: `tomo_core/src/tomo_core/delivery.py:28-61`
- Modify: `tomo_core/src/tomo_core/models.py:47-61`
- Modify: `tomo_core/tests/test_milestone1.py:36-74`

**Step 1: Add failing delivery-seam tests**

Add tests for:

```python
planner = DeliveryPlanner(max_bubbles=4, max_sentences_per_bubble=3)
bubbles = planner.compose_utterances(
    ("first thought.", "second thought? still here."),
    reply_to_message_id="m1",
)
self.assertEqual([bubble.text for bubble in bubbles], [
    "first thought.",
    "second thought? still here.",
])
self.assertEqual(bubbles[0].reply_to_message_id, "m1")
self.assertIsNone(bubbles[1].reply_to_message_id)
```

Also assert that `compose_utterances` rejects zero/five utterances and any utterance over three sentences rather than truncating it.

**Step 2: Verify red**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_milestone1 -v
```

Expected: FAIL because `compose_utterances` does not exist.

**Step 3: Implement `compose_utterances`**

```python
def compose_utterances(
    self,
    utterances: tuple[str, ...],
    reply_to_message_id: str,
) -> list[OutboundBubble]:
    if not self.min_bubbles <= len(utterances) <= self.max_bubbles:
        raise ValueError("utterance count violates delivery contract")
    cleaned = tuple(sanitize_style(strip_markdown(item)) for item in utterances)
    if any(not item for item in cleaned):
        raise ValueError("utterances cannot be empty")
    if any(len(split_sentences(item)) > self.max_sentences_per_bubble for item in cleaned):
        raise ValueError("utterance exceeds sentence limit")
    bubbles = [OutboundBubble(text=item) for item in cleaned]
    bubbles[0] = OutboundBubble(
        text=bubbles[0].text,
        reply_to_message_id=reply_to_message_id,
    )
    return bubbles
```

Keep `compose(text, ...)` temporarily for old direct callers and tests, but mark it as a compatibility fallback. Do not route the new conversation engine through it.

Change `RuntimeConfig.max_sentences_per_bubble` from `2` to `3` to match the actual soul contract.

**Step 4: Verify green**

Run the same unittest command. Expected: PASS.

---

### Task 9: Integrate the conversation engine into `PersonalAgentRuntime`

**Objective:** Replace the one-pass draft node with the move-aware conversation result while preserving transport and current session ownership.

**Files:**
- Modify: `tomo_core/src/tomo_core/runtime.py:14-103`
- Modify: `tomo_core/src/tomo_core/graph.py:25-32`
- Modify: `tomo_core/tests/test_milestone1.py`
- Create: `tomo_core/tests/test_runtime_conversation_moves.py`

**Step 1: Write a failing runtime integration test**

Use a scripted provider with two responses and call only `handle_telegram_text`. Assert:

- typing begins;
- selector sees the prior history and full soul;
- realization follows the selected move;
- intentional utterances become exact Telegram bubbles;
- first bubble replies to the triggering message;
- one user and one assistant logical message are stored;
- assistant content equals `ConversationResult.logical_text`;
- assistant metadata contains compact fields only:

```python
{
    "provider": "scripted",
    "conversation": {
        "primary_move": "challenge",
        "supporting_moves": ["acknowledge"],
        "response_goal": "challenge the plan and give a better route",
        "confidence": "high",
    },
    "delivery_bubbles": ["...", "..."],
}
```

Assert metadata contains no `chain_of_thought`, raw prompts, model-selection output, or access tokens.

**Step 2: Verify red**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_runtime_conversation_moves -v
```

**Step 3: Modify runtime construction**

In `PersonalAgentRuntime.__init__`:

```python
self.conversation = ConversationEngine(provider)
```

Change the runtime graph to:

```python
[
    ("load_context", self._load_context),
    ("run_conversation", self._run_conversation),
    ("compose_delivery", self._compose_delivery),
    ("persist_turn", self._persist_turn),
]
```

`_load_context` should return the loaded session and soul, but stop constructing the old provider `messages` list.

```python
def _run_conversation(self, state):
    envelope = state["envelope"]
    request = ConversationRequest.from_history(
        envelope=envelope,
        soul=state["soul"],
        history=state["session"].model_history(),
    )
    return {"conversation_result": self.conversation.respond(request)}
```

```python
def _compose_delivery(self, state):
    envelope = state["envelope"]
    result = state["conversation_result"]
    return {
        "bubbles": self.delivery.compose_utterances(
            result.utterances,
            reply_to_message_id=envelope.message_id,
        )
    }
```

Persist `result.logical_text`, not a separate hidden draft. Add only compact plan metadata as shown in Step 1.

Update `GraphState` to replace `messages` and `answer` with `conversation_result`.

**Step 4: Verify green**

Run:

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_runtime_conversation_moves tests.test_milestone1 -v
```

Expected: PASS after adapting old milestone tests to a two-response scripted provider where they assert exact model behavior. Keep `StaticProvider` compatibility tests separately; because it returns the same text for both passes, plain non-JSON static responses should no longer be used as a normal runtime integration fixture.

---

### Task 10: Preserve static smoke mode without weakening production parsing

**Objective:** Keep local Telegram wiring smoke tests functional even though `StaticProvider` cannot naturally provide selection JSON plus realization JSON.

**Files:**
- Modify: `tomo_core/src/tomo_core/providers.py:142-151`
- Modify: `tomo_core/src/tomo_core/cli.py:170-210,285-299`
- Create or modify: `tomo_core/tests/test_cli.py`
- Modify: `tomo_core/tests/test_shared_gateway.py:98-115`

**Step 1: Write a failing static-mode runtime test**

Define expected behavior: `StaticProvider("yo. telegram is wired.")` remains a wiring provider and returns a valid direct-answer move plan when the selection prompt requests `primary_move`, then returns valid utterance JSON for realization.

Expected public behavior remains the configured text, not JSON.

```python
provider = StaticProvider("yo. telegram is wired.")
result = ConversationEngine(provider).respond(request)
self.assertEqual(result.plan.primary, ConversationMove.ANSWER)
self.assertEqual(result.utterances, ("yo. telegram is wired.",))
```

**Step 2: Verify red**

Run the focused test. Expected: FAIL because current `StaticProvider.complete` always returns raw text.

**Step 3: Make `StaticProvider` purpose-aware from message contracts**

Without changing `ProviderAdapter`, inspect only the final system contract:

```python
def complete(self, messages, actor_id=None):
    system_text = "\n".join(
        message["content"]
        for message in messages
        if message.get("role") == "system"
    )
    if '"primary_move"' in system_text and '"supporting_moves"' in system_text:
        return json.dumps({
            "primary_move": "answer",
            "supporting_moves": [],
            "response_goal": "return the configured static smoke response",
            "confidence": "high",
        })
    if '"utterances"' in system_text:
        return json.dumps({"utterances": [self.response]})
    return self.response
```

This behavior is explicitly test/smoke-only. Do not add prompt inspection to real providers.

**Step 4: Verify green**

Run:

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_cli tests.test_shared_gateway tests.test_milestone1 -v
```

Expected: PASS.

---

### Task 11: Verify hosted sandbox compatibility

**Objective:** Prove the move-aware runtime still crosses the existing v1 sandbox protocol without changing Railway or Daytona routing.

**Files:**
- Modify: `tomo_core/tests/test_sandbox_inbound.py:76-92`
- Modify only if required: `tomo_core/src/tomo_core/sandbox_inbound.py:37-64`
- Do not modify: `tomo_core/src/tomo_core/sandbox_protocol.py`
- Do not modify: `tomo_core/src/tomo_core/sandbox_dispatch.py`

**Step 1: Add a real-runtime sandbox test**

Use a scripted provider with move-plan and realization responses. Execute `run_once`, parse its existing `TOMO_SANDBOX_RESULT=` output through `parse_result_marker`, and assert two returned bubbles with first-bubble reply threading.

Also retain the current tests proving:

- malformed inbound is `invalid_request`;
- HTTP 401 maps to `auth_expired`;
- exceptions and tokens never cross stdout;
- exactly one result marker is emitted.

**Step 2: Verify red if the fixture assumes one provider call**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_sandbox_inbound -v
```

Expected: the new test fails until the scripted provider is wired.

**Step 3: Make only minimal fixture/runtime adjustments**

`run_once` should continue constructing `PersonalAgentRuntime` and calling `handle_telegram_text`. There should be no new protocol fields because move metadata is internal and session-side; the host only needs validated bubbles.

**Step 4: Verify green and dispatch regressions**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest \
  tests.test_sandbox_inbound \
  tests.test_sandbox_protocol \
  tests.test_sandbox_dispatch \
  tests.test_shared_gateway \
  tests.test_telegram_router -v
```

Expected: PASS with no protocol or routing changes.

---

### Task 12: Add behavioral scenarios for Tomo’s personality

**Objective:** Lock the move-selection contract against representative human-conversation situations without asserting exact prose.

**Files:**
- Create: `tomo_core/tests/test_conversation_scenarios.py`

**Step 1: Add table-driven selector scenarios**

Use a deterministic fake selector response per scenario and assert accepted plans plus realization prompt contents. Do not call a live model in unit tests.

Required scenarios:

1. `"what do u think abt this plan?"` → primary `answer`, optional `challenge`/`explore`.
2. `"my interview is tomorrow"` with worried context → `reassure` + `explore`, not empty acknowledgment.
3. ambiguous `"do the thing"` with two materially different candidates → `clarify` with one question.
4. user catches Tomo misunderstanding → `repair` before `answer`.
5. reckless plan → `challenge`; prompt explicitly targets reasoning, not vulnerability.
6. absurd callback in history → `answer` or `acknowledge` primary with optional `joke`, never joke-only when substance is needed.
7. action request without observed tool result → `act` procedure forbids success claims.
8. user declines an explored topic → realization procedure yields rather than repeatedly interrogating.
9. refusal case → direct reason plus closest valid alternative, no corporate-policy wording.
10. casual greeting → acknowledgment/explore allowed; no forced enthusiasm or slang pile-up.

**Step 2: Add response-contract assertions**

For each scripted realization:

- 1–4 utterances;
- ≤3 sentences each;
- no headings or internal labels;
- no em/en dash;
- no phrase such as `as an ai`, `primary move`, `response goal`, or `chain of thought`.

Do not assert exact slang frequency. That would make tests brittle and encourage performative slang.

**Step 3: Run scenario tests**

```bash
cd tomo_core
PYTHONPATH=src python -m unittest tests.test_conversation_scenarios -v
```

Expected: PASS.

---

### Task 13: Update package exports and documentation

**Objective:** Make the conversation module navigable without exposing internal prompt/parsing helpers as public interfaces.

**Files:**
- Modify: `tomo_core/src/tomo_core/__init__.py:1-15`
- Modify: `tomo_core/README.md:5-29,121-126`
- Modify: `tomo_core/HANDOFF.md:15-57`
- Modify: `tomo_core/docs/conversation-architecture.md`

**Step 1: Export only stable domain interfaces**

From top-level `tomo_core`, export:

- `ConversationEngine`
- `ConversationMove`
- `ConversationRequest`
- `ConversationResult`
- `MovePlan`

Do not export prompt builders, parsers, or `MOVE_CATALOG` from the top-level package.

**Step 2: Update README current behavior**

Replace “one logical assistant turn stored in JSON session history” as the only conversation description with:

```markdown
- two-stage conversational loop: soul-aware move selection followed by move-specific realization
- one primary conversational move plus up to two supporting moves
- 1–4 intentional telegram utterances, each capped at three sentences
- compact move metadata stored with one logical assistant turn
- malformed realization receives one bounded repair attempt
```

Keep durable memory, tools, reactions, and images listed as not included.

**Step 3: Update HANDOFF**

Record the exact new files, the two normal model calls, the one repair-call maximum, and the deferred session/idempotency work. Do not claim the production snapshot contains these changes until a later deployment task verifies it.

**Step 4: Verify imports and docs**

```bash
cd tomo_core
PYTHONPATH=src python -c "from tomo_core import ConversationEngine, ConversationMove, ConversationRequest, ConversationResult, MovePlan; print('conversation imports ok')"
```

Expected: `conversation imports ok`.

---

### Task 14: Run complete regression and review gates

**Objective:** Verify the architecture without deploying or changing external state.

**Files:**
- No planned source changes; fix only failures caused by the implementation.

**Step 1: Re-check workspace before validation**

```bash
git status --short
```

Expected: only intended conversation architecture files and the plan are modified/untracked. Preserve unrelated user changes.

**Step 2: Run the complete Python suite**

From `tomo_core/`:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Expected: all tests pass; baseline from the handoff was 134 tests, and the final count should be greater after adding conversation tests.

**Step 3: Check the lockfile**

From repository root:

```bash
uv lock --check
```

Expected: exit code 0. No new dependency should be needed.

**Step 4: Run a static local runtime smoke**

Use a temporary directory and `StaticProvider`; do not require Telegram credentials:

```bash
cd tomo_core
PYTHONPATH=src python - <<'PY'
import tempfile
from pathlib import Path
from tomo_core import InboundEnvelope, PersonalAgentRuntime, RuntimeConfig
from tomo_core.providers import StaticProvider
from tomo_core.telegram import FakeTelegramClient, TelegramDeliverySink

with tempfile.TemporaryDirectory() as tmp:
    soul = Path(tmp) / "SOUL.md"
    soul.write_text("chill, honest, curious", encoding="utf-8")
    client = FakeTelegramClient()
    runtime = PersonalAgentRuntime(
        provider=StaticProvider("yo. conversation loop is wired."),
        telegram=TelegramDeliverySink(client),
        config=RuntimeConfig(data_dir=tmp, soul_path=str(soul)),
    )
    runtime.handle_telegram_text(
        InboundEnvelope(connector="telegram", actor_id="u", message_id="m", text="yo")
    )
    assert client.sent_messages[0]["text"] == "yo. conversation loop is wired."
    assert client.sent_messages[0]["reply_to_message_id"] == "m"
print("conversation smoke ok")
PY
```

Expected: `conversation smoke ok`.

**Step 5: Perform review**

Review for:

- no raw chain-of-thought or malformed model payload stored or emitted;
- no secrets in exceptions/stdout;
- no move labels visible to Telegram;
- no silent utterance truncation;
- exactly two provider calls on valid normal turns;
- at most one realization repair call;
- full SOUL present in both selection and realization contracts;
- no changes to sandbox protocol, routing, session identity, or persistent volumes;
- no fake action-success claims;
- no prompt logic duplicated across runtime and adapters.

Use `code-review` or `requesting-code-review` after implementation. Any refactor belongs after all behavior tests are green.

**Step 6: Optional commit checkpoint**

```bash
# optional, only if the user requested commits
 git add tomo_core/src/tomo_core/conversation \
   tomo_core/src/tomo_core/runtime.py \
   tomo_core/src/tomo_core/graph.py \
   tomo_core/src/tomo_core/delivery.py \
   tomo_core/src/tomo_core/models.py \
   tomo_core/src/tomo_core/providers.py \
   tomo_core/src/tomo_core/__init__.py \
   tomo_core/tests \
   tomo_core/docs/conversation-architecture.md \
   tomo_core/README.md \
   tomo_core/HANDOFF.md
 git commit -m "feat(core): add soul-aware conversation moves"
```

Do not build a Daytona snapshot, alter Railway variables, redeploy, or push unless the user explicitly requests the rollout.

---

## Files likely to change

### Create

- `tomo_core/docs/conversation-architecture.md`
- `tomo_core/src/tomo_core/conversation/__init__.py`
- `tomo_core/src/tomo_core/conversation/models.py`
- `tomo_core/src/tomo_core/conversation/moves.py`
- `tomo_core/src/tomo_core/conversation/parsing.py`
- `tomo_core/src/tomo_core/conversation/prompts.py`
- `tomo_core/src/tomo_core/conversation/engine.py`
- `tomo_core/tests/test_conversation_models.py`
- `tomo_core/tests/test_conversation_moves.py`
- `tomo_core/tests/test_conversation_parsing.py`
- `tomo_core/tests/test_conversation_prompts.py`
- `tomo_core/tests/test_conversation_engine.py`
- `tomo_core/tests/test_conversation_scenarios.py`
- `tomo_core/tests/test_runtime_conversation_moves.py`

### Modify

- `tomo_core/src/tomo_core/runtime.py`
- `tomo_core/src/tomo_core/graph.py`
- `tomo_core/src/tomo_core/delivery.py`
- `tomo_core/src/tomo_core/models.py`
- `tomo_core/src/tomo_core/providers.py`
- `tomo_core/src/tomo_core/__init__.py`
- `tomo_core/tests/test_milestone1.py`
- `tomo_core/tests/test_sandbox_inbound.py`
- `tomo_core/tests/test_shared_gateway.py`
- `tomo_core/tests/test_cli.py`
- `tomo_core/README.md`
- `tomo_core/HANDOFF.md`

### Intentionally unchanged

- `tomo_core/src/tomo_core/sessions.py`
- `tomo_core/src/tomo_core/sandbox_protocol.py`
- `tomo_core/src/tomo_core/sandbox_dispatch.py`
- `tomo_core/src/tomo_core/telegram_router.py`
- `tomo_core/src/tomo_core/daytona_supervisor.py`
- `tomo_core/SOUL.md` unless the user separately approves wording changes.

---

## Risks and tradeoffs

1. **Latency and cost:** valid turns use two model calls instead of one. This is the cleanest way for selected procedures to shape realization, but it approximately doubles provider round trips. Measure live latency before deployment; do not prematurely add a separate cheap-model configuration.
2. **Structured-output fragility:** Chat Completions does not currently request provider-native JSON schema. Strict parsing plus one bounded repair prevents arbitrary loops, but malformed second output can still fail the turn safely.
3. **Selector quality:** unit tests can validate contracts, not whether Grok consistently chooses the best human move. A later eval dataset should compare move choices against human labels without asserting exact prose.
4. **Static provider special behavior:** purpose-aware static output is acceptable for smoke mode but must remain isolated to `StaticProvider`.
5. **Session retry duplication remains:** the current hosted flow persists before Telegram delivery; router retries can duplicate logical turns. This plan deliberately records the risk but defers the session/idempotency redesign.
6. **Action move without tools:** `act` can identify prerequisites or offer a next step, but cannot execute until a tool loop exists. The prompt must prohibit false success claims.
7. **Soul tension:** bluntness, persistence, and resourcefulness can become hostility, interrogation, or boundary bypassing if interpreted literally. The move catalog’s behavioral constraints are required, not optional softening.
8. **No silent data loss:** model-planned utterances avoid the current `groups[:max_bubbles]` truncation. Any fallback added during implementation must preserve this invariant rather than reusing silent truncation.
9. **Snapshot immutability:** once implemented, hosted users will not receive the new conversation loop until a new Daytona snapshot is built and Railway points to it. Persistent user volumes must remain mounted during that later rollout.

---

## Open questions to resolve before implementation

1. **Two-pass model loop:** accept two normal Grok calls per message for explicit move selection and realization, or prefer a cheaper one-call combined JSON response with weaker move-specific control?
2. **Move roster:** is the initial ten-move catalog correct, or should `disclose/share` be a distinct move for reciprocal friend-like conversation rather than being expressed through `answer`/`acknowledge`?
3. **Persistence metadata:** is storing compact move plan metadata in the existing assistant message acceptable before the session redesign, or should move metadata remain runtime-only for now?
4. **Failure behavior:** after one invalid realization repair, should Tomo fail safely and retry the inbox update, or send a deterministic minimal fallback? This plan chooses safe failure to avoid an off-personality fabricated fallback.
5. **Evaluation seam:** before TDD begins, confirm the selected public seams: `ConversationEngine.respond`, `DeliveryPlanner.compose_utterances`, runtime integration, and sandbox protocol regression.

Implementation should not begin until the user answers or explicitly accepts the defaults above.