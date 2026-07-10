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
    data_dir: str = ".tomo_core"
    soul_path: str = "SOUL.md"

    def __post_init__(self) -> None:
        self.response_contract

    @property
    def response_contract(self) -> ResponseContract:
        return ResponseContract(
            min_utterances=self.min_bubbles,
            max_utterances=self.max_bubbles,
            max_sentences_per_utterance=self.max_sentences_per_bubble,
        )
