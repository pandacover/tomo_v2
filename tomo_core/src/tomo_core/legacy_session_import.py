from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .sessions import ConversationSession, StoredMessage


class LegacySessionImportError(Exception):
    pass


def import_legacy_sessions(repository, owner_id: str, data_dir: str | Path) -> int:
    """Import untouched JSON files once, detecting altered files by path+hash."""
    root = Path(data_dir) / "sessions"
    if not root.exists():
        return 0
    count = 0
    primary_paths = {path for path in root.glob("*.json")}
    recovery_paths = {
        path for path in root.glob("*.json.recovery")
        if path.with_suffix("") not in primary_paths
    }
    for path in sorted(primary_paths | recovery_paths):
        raw = path.read_bytes()
        if path.suffix == ".json":
            try:
                json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                recovery = path.with_suffix(f"{path.suffix}.recovery")
                if not recovery.exists():
                    raise LegacySessionImportError("invalid_legacy_session") from None
                path = recovery
                raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        previous = repository.legacy_import_hash(str(path))
        if previous == digest:
            continue
        if previous is not None:
            raise LegacySessionImportError("legacy_session_file_changed")
        try:
            payload = json.loads(raw.decode("utf-8")); key = payload["session_key"]
            session = ConversationSession(key, [StoredMessage(**m) for m in payload.get("messages", [])], tuple(payload.get("accepted_generation_ids", ())))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise LegacySessionImportError("invalid_legacy_session") from error
        repository.import_legacy_session_file(owner_id, session, str(path), digest)
        count += 1
    return count
