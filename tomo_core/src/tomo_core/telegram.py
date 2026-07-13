from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .models import OutboundBubble


@dataclass(frozen=True)
class TelegramSendReceipt:
    message_id: str


class TelegramClient(Protocol):
    def send_typing(self, actor_id: str) -> None:
        ...

    def send_message(self, actor_id: str, text: str, reply_to_message_id: str | None = None) -> TelegramSendReceipt:
        ...

    def set_message_reaction(self, actor_id: str, message_id: str, emoji: str) -> None:
        ...


@dataclass
class TelegramDeliverySink:
    client: TelegramClient

    def start_typing(self, actor_id: str) -> None:
        self.client.send_typing(actor_id)

    def send_bubbles(self, actor_id: str, bubbles: list[OutboundBubble]) -> None:
        for bubble in bubbles:
            self.client.send_message(
                actor_id=actor_id,
                text=bubble.text,
                reply_to_message_id=bubble.reply_to_message_id,
            )

    def react_to_message(self, actor_id: str, message_id: str, emoji: str) -> None:
        self.client.set_message_reaction(actor_id, message_id, emoji)


@dataclass
class FakeTelegramClient:
    typing_actor_ids: list[str] = field(default_factory=list)
    sent_messages: list[dict[str, str | None]] = field(default_factory=list)
    reactions: list[dict[str, str]] = field(default_factory=list)

    def send_typing(self, actor_id: str) -> None:
        self.typing_actor_ids.append(actor_id)

    def send_message(self, actor_id: str, text: str, reply_to_message_id: str | None = None) -> TelegramSendReceipt:
        self.sent_messages.append(
            {"actor_id": actor_id, "text": text, "reply_to_message_id": reply_to_message_id}
        )
        return TelegramSendReceipt(str(len(self.sent_messages)))

    def set_message_reaction(self, actor_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append({"actor_id": actor_id, "message_id": message_id, "emoji": emoji})
