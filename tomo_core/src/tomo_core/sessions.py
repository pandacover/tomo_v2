from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from .models import utc_now_iso

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

    def append(self, message: StoredMessage) -> None:
        self.messages.append(message)

    def model_history(self, limit: int = 20) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content}
            for message in self.messages[-limit:]
        ]


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
        )

    def save(self, session: ConversationSession) -> None:
        path = self._path(session.session_key)
        payload = {"session_key": session.session_key, "messages": [asdict(m) for m in session.messages]}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _path(self, session_key: str) -> Path:
        safe = session_key.replace(":", "_").replace("/", "_")
        return self.sessions_dir / f"{safe}.json"
