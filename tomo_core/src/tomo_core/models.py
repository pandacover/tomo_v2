from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Connector = Literal["telegram"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MessageAttachment:
    kind: Literal["image"]
    file_id: str | None = None
    url: str | None = None
    path: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundEnvelope:
    connector: Connector
    actor_id: str
    message_id: str
    text: str
    timestamp: str = field(default_factory=utc_now_iso)
    attachments: tuple[MessageAttachment, ...] = ()
    location: dict[str, float] | None = None
    native_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        # dm-only v1: actor_id is the conversation identity. no room_id yet.
        return f"{self.connector}:actor:{self.actor_id}"


@dataclass(frozen=True)
class InboundMessage:
    ordinal: int
    update_id: int
    envelope: InboundEnvelope

    def __post_init__(self) -> None:
        if not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise ValueError("inbound message ordinal must be a positive integer")
        if not isinstance(self.update_id, int):
            raise ValueError("inbound message update_id must be an integer")
        if not isinstance(self.envelope, InboundEnvelope):
            raise ValueError("inbound message envelope is required")


@dataclass(frozen=True)
class InputBurst:
    burst_id: str
    generation_id: str
    revision: int
    messages: tuple[InboundMessage, ...]
    visible_assistant_utterances: tuple[str, ...] = ()
    accepted_generation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.burst_id, str) or not self.burst_id.strip():
            raise ValueError("burst_id is required")
        if not isinstance(self.generation_id, str) or not self.generation_id.strip():
            raise ValueError("generation_id is required")
        if not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("burst revision must be at least one")
        messages = tuple(self.messages)
        if not messages:
            raise ValueError("input burst must contain at least one message")
        ordinals = [message.ordinal for message in messages]
        if ordinals != list(range(1, len(messages) + 1)):
            raise ValueError("input burst ordinals must be contiguous from one")
        update_ids = [message.update_id for message in messages]
        message_ids = [message.envelope.message_id for message in messages]
        if len(set(update_ids)) != len(update_ids):
            raise ValueError("input burst update IDs must be unique")
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("input burst message IDs must be unique")
        connectors = {message.envelope.connector for message in messages}
        actor_ids = {message.envelope.actor_id for message in messages}
        if len(connectors) != 1 or len(actor_ids) != 1:
            raise ValueError("input burst messages must share connector and actor")
        visible = tuple(self.visible_assistant_utterances)
        if any(not isinstance(item, str) or not item.strip() for item in visible):
            raise ValueError("visible assistant utterances cannot be blank")
        accepted = tuple(self.accepted_generation_ids)
        if any(not isinstance(item, str) or not item.strip() for item in accepted):
            raise ValueError("accepted generation IDs cannot be blank")
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "visible_assistant_utterances", visible)
        object.__setattr__(self, "accepted_generation_ids", accepted)

    @property
    def latest(self) -> InboundEnvelope:
        return self.messages[-1].envelope


@dataclass(frozen=True)
class OutboundBubble:
    text: str
    reply_to_message_id: str | None = None


@dataclass(frozen=True)
class ResponseContract:
    min_utterances: int = 1
    max_utterances: int = 4
    max_sentences_per_utterance: int = 3

    def __post_init__(self) -> None:
        if self.min_utterances != 1:
            raise ValueError("min_utterances must be 1")
        if self.max_utterances < self.min_utterances:
            raise ValueError("max_utterances must be >= min_utterances")
        if self.max_utterances > 4:
            raise ValueError("max_utterances cannot exceed 4")
        if self.max_sentences_per_utterance < 1:
            raise ValueError("max_sentences_per_utterance must be at least 1")
        if self.max_sentences_per_utterance > 3:
            raise ValueError("max_sentences_per_utterance cannot exceed 3")


@dataclass(frozen=True)
class RuntimeConfig:
    max_bubbles: int = 4
    min_bubbles: int = 1
    max_sentences_per_bubble: int = 3
    max_frames_per_segment: int = 3
    max_chars_per_frame: int = 800
    data_dir: str = ".tomo_core"
    soul_path: str = "SOUL.md"

    def __post_init__(self) -> None:
        self.response_contract
        if not isinstance(self.max_frames_per_segment, int) or isinstance(self.max_frames_per_segment, bool) or self.max_frames_per_segment < 1:
            raise ValueError("max_frames_per_segment must be at least 1")
        if not isinstance(self.max_chars_per_frame, int) or isinstance(self.max_chars_per_frame, bool) or not 1 <= self.max_chars_per_frame <= 4096:
            raise ValueError("max_chars_per_frame must be between 1 and 4096")

    @property
    def response_contract(self) -> ResponseContract:
        return ResponseContract(
            min_utterances=self.min_bubbles,
            max_utterances=self.max_bubbles,
            max_sentences_per_utterance=self.max_sentences_per_bubble,
        )

    @property
    def ordinary_turn_budget(self):
        from .conversation.models import TurnBudget

        return TurnBudget(1, 0, 0, 1, self.max_frames_per_segment, self.max_sentences_per_bubble, self.max_chars_per_frame)

    @property
    def tool_turn_budget(self):
        from .conversation.models import TurnBudget

        return TurnBudget(6, 5, 5, 3, self.max_frames_per_segment, self.max_sentences_per_bubble, self.max_chars_per_frame)
