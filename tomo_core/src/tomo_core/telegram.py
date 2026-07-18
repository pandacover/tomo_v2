from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .models import MAX_REPLY_CONTEXT_TEXT_CHARS, MessageAttachment, OutboundBubble, ReplyContext


def reply_context_from_message(message: dict, parent_chat_id: object) -> ReplyContext | None:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict) or reply.get("message_id") is None:
        return None
    reply_chat = reply.get("chat")
    if not isinstance(reply_chat, dict) or str(reply_chat.get("id")) != str(parent_chat_id):
        return None
    sender = reply.get("from")
    author_role = "assistant" if isinstance(sender, dict) and sender.get("is_bot") else "user" if isinstance(sender, dict) else "unknown"
    text = reply.get("text") if isinstance(reply.get("text"), str) else reply.get("caption") if isinstance(reply.get("caption"), str) else None
    truncated = bool(text and len(text) > MAX_REPLY_CONTEXT_TEXT_CHARS)
    if text is not None:
        text = text[:MAX_REPLY_CONTEXT_TEXT_CHARS]
    attachments = photo_attachments_from_message(reply)
    availability = "available" if text is not None or attachments else "unavailable"
    return ReplyContext(
        message_id=str(reply["message_id"]), author_role=author_role, text=text,
        timestamp=str(reply["date"]) if reply.get("date") is not None else None,
        attachments=attachments, availability=availability, truncated=truncated,
    )


def photo_attachments_from_message(message: dict) -> tuple[MessageAttachment, ...]:
    photos = message.get("photo")
    if not isinstance(photos, list):
        return ()
    photo = max((item for item in photos if isinstance(item, dict) and isinstance(item.get("file_id"), str)), key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0), default=None)
    if photo is None:
        return ()
    return (MessageAttachment("image", file_id=photo["file_id"], mime_type="image/jpeg", metadata={key: photo[key] for key in ("width", "height", "file_size", "file_unique_id") if key in photo}),)


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
