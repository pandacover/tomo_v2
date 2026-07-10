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
    def from_history(cls, *, envelope: InboundEnvelope, soul: str, history: Sequence[dict[str, str]]) -> "ConversationRequest":
        normalized: list[dict[str, str]] = []
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ValueError("conversation history must contain user or assistant text messages")
            normalized.append({"role": role, "content": content})
        return cls(envelope=envelope, soul=soul, history=tuple(normalized))


@dataclass(frozen=True)
class MovePlan:
    primary: ConversationMove
    supporting: tuple[ConversationMove, ...]
    response_goal: str
    confidence: MoveConfidence

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

    @property
    def ordered_moves(self) -> tuple[ConversationMove, ...]:
        return (self.primary, *self.supporting)

    @classmethod
    def direct_answer(cls) -> "MovePlan":
        return cls(
            primary=ConversationMove.ANSWER,
            supporting=(),
            response_goal="respond directly and honestly to the user's latest message",
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
