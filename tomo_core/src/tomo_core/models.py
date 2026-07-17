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
class AutomationFact:
    value: str
    source: str
    observed_at: str
    freshness: str
    uncertainty: str

    def __post_init__(self) -> None:
        for name, maximum in (("value", 2000), ("source", 256), ("freshness", 128), ("uncertainty", 512)):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"automation fact {name} is invalid")
            object.__setattr__(self, name, value.strip())
        try:
            observed_at = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ValueError("automation fact observed_at must be timezone-aware ISO-8601") from error
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("automation fact observed_at must be timezone-aware ISO-8601")


@dataclass(frozen=True)
class AutomationTurn:
    generation_id: str
    revision: int
    job_id: str
    run_id: str
    actor_id: str
    chat_id: str
    intent: str
    scheduled_for: str
    previous_outcome: str | None = None
    trigger: Literal["schedule", "catchup", "manual", "lifecycle"] = "schedule"
    will_end_after_run: bool = False
    connector: Connector = "telegram"
    constraints: tuple[str, ...] = ()
    successful_runs: int = 0
    facts: tuple[AutomationFact, ...] = ()

    def __post_init__(self) -> None:
        for name in ("generation_id", "job_id", "run_id", "actor_id", "chat_id", "intent"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"automation {name} is required")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("automation revision must be at least one")
        if self.connector != "telegram":
            raise ValueError("automation connector must be telegram")
        if self.trigger not in {"schedule", "catchup", "manual", "lifecycle"}:
            raise ValueError("invalid automation trigger")
        if self.previous_outcome is not None and (not isinstance(self.previous_outcome, str) or not self.previous_outcome.strip()):
            raise ValueError("automation previous_outcome must be non-empty when supplied")
        if not isinstance(self.will_end_after_run, bool):
            raise ValueError("automation will_end_after_run must be boolean")
        if isinstance(self.successful_runs, bool) or not isinstance(self.successful_runs, int) or self.successful_runs < 0:
            raise ValueError("automation successful_runs must be non-negative")
        constraints = tuple(self.constraints)
        if any(not isinstance(value, str) or not value.strip() for value in constraints):
            raise ValueError("automation constraints must be non-blank strings")
        object.__setattr__(self, "constraints", constraints)
        try:
            facts = tuple(fact if isinstance(fact, AutomationFact) else AutomationFact(**fact) for fact in self.facts)
        except (TypeError, ValueError) as error:
            raise ValueError("automation facts are invalid") from error
        if len(facts) > 8:
            raise ValueError("automation facts cannot exceed eight entries")
        object.__setattr__(self, "facts", facts)
        try:
            parsed = datetime.fromisoformat(self.scheduled_for.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as error:
            raise ValueError("automation scheduled_for must be timezone-aware ISO-8601") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("automation scheduled_for must be timezone-aware ISO-8601")

    @property
    def session_key(self) -> str:
        return f"{self.connector}:actor:{self.actor_id}"

    @property
    def event_text(self) -> str:
        prior = self.previous_outcome or "none"
        return (
            "AUTOMATION EVENT. This is system-originated scheduled work, not a user message.\n"
            f"Intent: {self.intent}\nScheduled for: {self.scheduled_for}\n"
            f"Job: {self.job_id}; run: {self.run_id}; trigger: {self.trigger}; prior outcome: {prior}; completed runs: {self.successful_runs}; constraints: {self.constraints}; "
            f"will end after run: {str(self.will_end_after_run).lower()}."
            + ("\nConnected facts: " + "; ".join(f"value={fact.value}; source={fact.source}; observed_at={fact.observed_at}; freshness={fact.freshness}; uncertainty={fact.uncertainty}" for fact in self.facts) if self.facts else "")
        )


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
    owner_id: str | None = "local"
    local_work_dir: str | None = None

    def __post_init__(self) -> None:
        self.response_contract
        if not isinstance(self.max_frames_per_segment, int) or isinstance(self.max_frames_per_segment, bool) or self.max_frames_per_segment < 1:
            raise ValueError("max_frames_per_segment must be at least 1")
        if not isinstance(self.max_chars_per_frame, int) or isinstance(self.max_chars_per_frame, bool) or not 1 <= self.max_chars_per_frame <= 4096:
            raise ValueError("max_chars_per_frame must be between 1 and 4096")
        if self.owner_id is not None and (not isinstance(self.owner_id, str) or not self.owner_id.strip()):
            raise ValueError("owner_id must be a non-empty string when supplied")

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
