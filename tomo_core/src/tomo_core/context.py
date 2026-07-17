from __future__ import annotations

from dataclasses import dataclass

from .conversation.models import ConversationRequest


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
        if not isinstance(request, ConversationRequest):
            raise ValueError("request must be a ConversationRequest")
        history: list[dict[str, str]] = []
        for message in request.history:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise ValueError("conversation history must contain normalized messages")
            role = message["role"]
            content = message["content"]
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise ValueError("conversation history must contain user or assistant text messages")
            history.append({"role": role, "content": content})
        return ContextSnapshot(
            history=tuple(history),
            visible_frames=tuple(getattr(request.burst, "visible_assistant_utterances", ())),
        )
