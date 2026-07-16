from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from .models import InboundMessage, MessageAttachment, utc_now_iso

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class StoredMessage:
    role: Role
    content: str
    timestamp: str = field(default_factory=utc_now_iso)
    metadata: dict = field(default_factory=dict)


@dataclass
class ConversationSession:
    session_key: str
    messages: list[StoredMessage] = field(default_factory=list)
    accepted_generation_ids: tuple[str, ...] = ()

    def append(self, message: StoredMessage) -> None:
        self.messages.append(message)

    def append_inbound_once(self, message: InboundMessage, burst_id: str) -> bool:
        if not isinstance(message, InboundMessage):
            raise TypeError("message must be an InboundMessage")
        if not isinstance(burst_id, str) or not burst_id.strip():
            raise ValueError("burst_id must be non-empty")
        for stored in self.messages:
            metadata = stored.metadata
            if stored.role == "user" and metadata.get("update_id") == message.update_id and metadata.get("burst_id") == burst_id:
                return False
        envelope = message.envelope
        self.messages.append(
            StoredMessage(
                role="user",
                content=envelope.text,
                timestamp=envelope.timestamp,
                metadata={
                    "connector": envelope.connector,
                    "actor_id": envelope.actor_id,
                    "message_id": envelope.message_id,
                    "update_id": message.update_id,
                    "ordinal": message.ordinal,
                    "burst_id": burst_id,
                    "attachments": [_attachment_metadata(attachment) for attachment in envelope.attachments],
                },
            )
        )
        return True

    def accept_generations(self, generation_ids: tuple[str, ...]) -> None:
        accepted = set(self.accepted_generation_ids)
        for generation_id in generation_ids:
            if not isinstance(generation_id, str) or not generation_id.strip():
                raise ValueError("generation_ids must be non-empty strings")
            accepted.add(generation_id)
        self.accepted_generation_ids = tuple(sorted(accepted))

    def model_history(self, limit: int = 20) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content}
            for message in self.messages[-limit:]
        ]

    def model_history_for_burst(self, burst_id: str, limit: int = 20) -> list[dict[str, str]]:
        accepted = set(self.accepted_generation_ids)
        visible: list[StoredMessage] = []
        for message in self.messages:
            metadata = message.metadata
            if message.role == "user" and metadata.get("burst_id") == burst_id:
                continue
            if metadata.get("generation_status") == "provisional" and metadata.get("generation_id") not in accepted:
                continue
            visible.append(message)
        return [{"role": message.role, "content": message.content} for message in visible[-limit:]]


def _attachment_metadata(attachment: MessageAttachment) -> dict:
    payload = asdict(attachment)
    return {key: value for key, value in payload.items() if value is not None}
