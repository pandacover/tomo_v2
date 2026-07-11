from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .models import InboundMessage, utc_now_iso

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


class JsonSessionStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.sessions_dir = Path(data_dir) / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def load(self, session_key: str) -> ConversationSession:
        path = self._path(session_key)
        if not path.exists():
            return ConversationSession(session_key=session_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ConversationSession(
            session_key=session_key,
            messages=[StoredMessage(**item) for item in payload.get("messages", [])],
            accepted_generation_ids=tuple(payload.get("accepted_generation_ids", ())),
        )

    def save(self, session: ConversationSession) -> None:
        self.save_atomic(session)

    def save_atomic(self, session: ConversationSession) -> None:
        path = self._path(session.session_key)
        lock_path = path.with_suffix(f"{path.suffix}.lock")
        with _exclusive_file_lock(lock_path):
            merged = self._merge_with_disk(session, path)
            payload = {
                "session_key": merged.session_key,
                "messages": [asdict(m) for m in merged.messages],
                "accepted_generation_ids": list(merged.accepted_generation_ids),
            }
            tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
            try:
                tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp_path.replace(path)
            finally:
                tmp_path.unlink(missing_ok=True)
            session.messages = merged.messages
            session.accepted_generation_ids = merged.accepted_generation_ids

    @staticmethod
    def _merge_with_disk(session: ConversationSession, path: Path) -> ConversationSession:
        if not path.exists():
            return session
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing = [StoredMessage(**item) for item in payload.get("messages", [])]
        seen = {_message_identity(message) for message in existing}
        for message in session.messages:
            identity = _message_identity(message)
            if identity not in seen:
                existing.append(message)
                seen.add(identity)
        accepted = tuple(sorted(set(payload.get("accepted_generation_ids", ())) | set(session.accepted_generation_ids)))
        return ConversationSession(session.session_key, existing, accepted)

    def _path(self, session_key: str) -> Path:
        safe = session_key.replace(":", "_").replace("/", "_")
        return self.sessions_dir / f"{safe}.json"


def _message_identity(message: StoredMessage) -> tuple:
    metadata = message.metadata
    if message.role == "user" and metadata.get("update_id") is not None:
        return ("user", metadata.get("burst_id"), metadata.get("update_id"))
    if message.role == "assistant" and metadata.get("generation_id"):
        return ("assistant", metadata["generation_id"])
    return (message.role, message.content, message.timestamp, json.dumps(metadata, sort_keys=True, default=str))


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
