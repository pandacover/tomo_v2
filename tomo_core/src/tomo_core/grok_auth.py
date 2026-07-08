from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_grok_auth_path() -> Path:
    override = os.getenv("GROK_AUTH_JSON") or os.getenv("TOMO_GROK_AUTH_JSON")
    if override:
        return Path(override).expanduser()
    home = Path(os.path.expanduser("~"))
    return home / ".grok" / "auth.json"


@dataclass(frozen=True)
class GrokAuthStore:
    auth_path: Path | None = None

    def path(self) -> Path:
        return self.auth_path or default_grok_auth_path()

    def payload(self) -> dict[str, Any] | None:
        path = self.path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def access_token(self) -> str | None:
        data = self.payload()
        if not data:
            return None
        return _find_token(data)


def _find_token(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("access_token", "accessToken", "token", "id_token", "idToken"):
            token = value.get(key)
            if isinstance(token, str) and token:
                return token
        for child in value.values():
            token = _find_token(child)
            if token:
                return token
    if isinstance(value, list):
        for child in value:
            token = _find_token(child)
            if token:
                return token
    return None
