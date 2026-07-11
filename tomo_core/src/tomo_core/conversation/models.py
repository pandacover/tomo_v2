from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence, TypeAlias

from ..models import InboundEnvelope, InboundMessage, InputBurst


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
    burst: InputBurst
    soul: str
    history: tuple[dict[str, str], ...]

    @property
    def envelope(self) -> InboundEnvelope:
        return self.burst.latest

    @classmethod
    def from_history(cls, *, envelope: InboundEnvelope, soul: str, history: Sequence[dict[str, str]]) -> "ConversationRequest":
        normalized: list[dict[str, str]] = []
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ValueError("conversation history must contain user or assistant text messages")
            normalized.append({"role": role, "content": content})
        burst = InputBurst(
            burst_id=f"legacy-{envelope.message_id}",
            generation_id=f"legacy-{envelope.message_id}",
            revision=1,
            messages=(InboundMessage(1, int(envelope.native_metadata.get("update_id", 0)), envelope),),
        )
        return cls(burst=burst, soul=soul, history=tuple(normalized))


@dataclass(frozen=True)
class MovePlan:
    primary: ConversationMove
    supporting: tuple[ConversationMove, ...]
    response_goal: str
    confidence: MoveConfidence
    sequence: tuple[ConversationMove, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.response_goal, str):
            raise ValueError("response_goal must be text")
        response_goal = self.response_goal.strip()
        if not response_goal:
            raise ValueError("response_goal cannot be empty")
        if len(response_goal) > 240 or "\n" in response_goal or "\r" in response_goal:
            raise ValueError("response_goal must be a compact single line")
        primary = ConversationMove(self.primary)
        supporting = tuple(ConversationMove(item) for item in self.supporting)
        confidence = MoveConfidence(self.confidence)
        object.__setattr__(self, "primary", primary)
        object.__setattr__(self, "supporting", supporting)
        object.__setattr__(self, "response_goal", response_goal)
        object.__setattr__(self, "confidence", confidence)
        if len(supporting) > 2:
            raise ValueError("a move plan can have at most two supporting moves")
        ordered = (primary, *supporting)
        if len(set(ordered)) != len(ordered):
            raise ValueError("move plan moves must be unique")
        sequence = tuple(ConversationMove(item) for item in (self.sequence or ordered))
        expected = {primary, *supporting}
        if len(sequence) != len(expected) or set(sequence) != expected:
            raise ValueError("move sequence must contain every selected move exactly once")
        object.__setattr__(self, "sequence", sequence)

    @property
    def ordered_moves(self) -> tuple[ConversationMove, ...]:
        return self.sequence

    @classmethod
    def direct_answer(cls) -> "MovePlan":
        return cls(
            primary=ConversationMove.ANSWER,
            supporting=(),
            response_goal="respond directly and honestly to the user's latest message",
            confidence=MoveConfidence.LOW,
            sequence=(ConversationMove.ANSWER,),
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


@dataclass(frozen=True)
class ConversationStarted:
    plan: MovePlan

    def __post_init__(self) -> None:
        if not isinstance(self.plan, MovePlan):
            raise ValueError("conversation start requires a move plan")


@dataclass(frozen=True)
class UtteranceReady:
    sequence: int
    move: ConversationMove
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("utterance sequence must be non-negative")
        object.__setattr__(self, "move", ConversationMove(self.move))
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("utterance text cannot be empty")
        object.__setattr__(self, "text", self.text.strip())


@dataclass(frozen=True)
class ConversationCompleted:
    result: ConversationResult

    def __post_init__(self) -> None:
        if not isinstance(self.result, ConversationResult):
            raise ValueError("conversation completion requires a result")


ConversationEvent: TypeAlias = ConversationStarted | UtteranceReady | ConversationCompleted
