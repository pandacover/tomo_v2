from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
import json
from typing import Literal

from .models import AutomationTurn, InboundMessage, MessageAttachment, PeerTurn, utc_now_iso
from .vision import VisionObservation

Role = Literal["user", "assistant", "automation"]


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
                    **({"reply_context": asdict(envelope.reply_context)} if envelope.reply_context else {}),
                },
            )
        )
        return True

    def append_automation_once(self, turn: AutomationTurn) -> bool:
        if not isinstance(turn, AutomationTurn):
            raise TypeError("turn must be an AutomationTurn")
        if any(message.role == "automation" and message.metadata.get("run_id") == turn.run_id for message in self.messages):
            return False
        self.messages.append(StoredMessage("automation", turn.event_text, timestamp=turn.scheduled_for, metadata={
            "source": "automation", "connector": turn.connector, "actor_id": turn.actor_id,
            "job_id": turn.job_id, "run_id": turn.run_id,
            "generation_id": turn.generation_id, "revision": turn.revision,
            "burst_id": turn.run_id, "chat_id": turn.chat_id, "trigger": turn.trigger,
            "will_end_after_run": turn.will_end_after_run,
        }))
        return True

    def append_peer_once(self, turn: PeerTurn) -> bool:
        if not isinstance(turn, PeerTurn):
            raise TypeError("turn must be a PeerTurn")
        if any(message.metadata.get("source") == "untrusted_peer" and message.metadata.get("burst_id") == turn.request_id for message in self.messages):
            return False
        self.messages.append(StoredMessage("user", turn.event_text, metadata={"source": "untrusted_peer", "request_id": turn.request_id, "burst_id": turn.request_id}))
        return True

    def accept_generations(self, generation_ids: tuple[str, ...]) -> None:
        accepted = set(self.accepted_generation_ids)
        for generation_id in generation_ids:
            if not isinstance(generation_id, str) or not generation_id.strip():
                raise ValueError("generation_ids must be non-empty strings")
            accepted.add(generation_id)
        self.accepted_generation_ids = tuple(sorted(accepted))

    def record_vision_observations(self, burst_id: str, observations: tuple[VisionObservation, ...]) -> None:
        """Replace safe evidence on existing inbound rows without changing burst identity."""
        if not isinstance(burst_id, str) or not burst_id.strip():
            raise ValueError("burst_id must be non-empty")
        observations = tuple(observations)
        rows = {
            message.metadata.get("message_id"): message
            for message in self.messages
            if message.role == "user" and message.metadata.get("burst_id") == burst_id
        }
        grouped: dict[str, list[tuple[int, dict[str, object]]]] = {}
        references: set[tuple[str, int]] = set()
        for observation in observations:
            if not isinstance(observation, VisionObservation):
                raise ValueError("vision observations must be VisionObservation values")
            reference = (observation.message_id, observation.attachment_index)
            row = rows.get(observation.message_id)
            attachments = row.metadata.get("attachments") if row is not None else None
            target = attachments[observation.attachment_index] if isinstance(attachments, list) and observation.attachment_index < len(attachments) else None
            if reference in references or not isinstance(target, dict) or target.get("kind") != "image":
                raise ValueError("vision observations must belong to the current input burst")
            references.add(reference)
            grouped.setdefault(observation.message_id, []).append((observation.attachment_index, observation.prompt_payload()))
        for values in grouped.values():
            values.sort(key=lambda value: value[0])
        for index, message in enumerate(self.messages):
            if message.role != "user" or message.metadata.get("burst_id") != burst_id:
                continue
            message_id = message.metadata.get("message_id")
            if message_id not in grouped:
                continue
            metadata = {**message.metadata, "vision_observations": [value for _, value in grouped[message_id]]}
            self.messages[index] = StoredMessage(message.role, message.content, message.timestamp, metadata)

    def model_history(self, limit: int = 20) -> list[dict[str, str]]:
        return [
            _model_message(message)
            for message in self.messages
            if not message.metadata.get("peer_exchange")
        ][-limit:]

    def model_history_for_burst(self, burst_id: str, limit: int = 20) -> list[dict[str, str]]:
        accepted = set(self.accepted_generation_ids)
        visible: list[StoredMessage] = []
        for message in self.messages:
            metadata = message.metadata
            if metadata.get("burst_id") == burst_id:
                continue
            if metadata.get("peer_exchange"):
                continue
            if metadata.get("generation_status") == "provisional" and metadata.get("generation_id") not in accepted:
                continue
            visible.append(message)
        return [_model_message(message) for message in visible[-limit:]]


def _attachment_metadata(attachment: MessageAttachment) -> dict:
    payload = asdict(attachment)
    return {key: value for key, value in payload.items() if key != "file_id" and value is not None}


def _model_message(message: StoredMessage) -> dict[str, str]:
    if message.role == "automation":
        return {"role": "user", "content": message.content}
    reply = message.metadata.get("reply_context")
    observations = message.metadata.get("vision_observations")
    if message.role == "user" and (isinstance(reply, dict) or observations):
        payload: dict[str, object] = {"content": message.content}
        if isinstance(reply, dict):
            payload["message_id"] = message.metadata.get("message_id")
            payload["reply_context"] = _history_reply_context(reply)
        if observations:
            attachments = message.metadata.get("attachments", [])
            payload["attachments"] = [
                {key: value for key, value in attachment.items() if key in {"kind", "mime_type", "metadata"}}
                for attachment in attachments
                if isinstance(attachment, dict)
            ]
            payload["vision_observations"] = observations
        return {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
    return {"role": message.role, "content": message.content}


def _history_reply_context(reply: dict) -> dict:
    payload = {key: reply[key] for key in ("message_id", "author_role", "text", "timestamp", "availability", "truncated") if key in reply}
    attachments = reply.get("attachments")
    if isinstance(attachments, (list, tuple)):
        payload["attachments"] = [
            _history_attachment(attachment)
            for attachment in attachments if isinstance(attachment, dict)
        ]
    return payload


def _history_attachment(attachment: dict) -> dict:
    payload = {key: attachment[key] for key in ("kind", "mime_type") if key in attachment}
    metadata = attachment.get("metadata")
    if isinstance(metadata, dict):
        safe_metadata = {
            key: value
            for key, value in metadata.items()
            if key in {"width", "height", "file_size"} and isinstance(value, (int, float, str))
        }
        if safe_metadata:
            payload["metadata"] = safe_metadata
    return payload
