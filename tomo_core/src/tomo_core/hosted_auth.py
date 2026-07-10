from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .grok_auth import default_grok_auth_path
from .oauth import OAuthManager


_KEYS = {
    "accessToken": "access_token",
    "refreshToken": "refresh_token",
    "expiresAt": "expires_at",
    "expiry": "expires_at",
}
_REFRESH_WINDOW_SECONDS = 300


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {_KEYS.get(key, key): _normalize(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_normalize(child) for child in value]
    return value


def _token_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("access_token"), str):
            return value
        for child in value.values():
            found = _token_payload(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _token_payload(child)
            if found is not None:
                return found
    return None


@dataclass(frozen=True)
class HostedGrokAuth:
    encoded_auth: str | None
    auth_path: Path

    @classmethod
    def from_environment(cls, *, auth_path: Path | None = None) -> HostedGrokAuth:
        return cls(
            encoded_auth=(
                os.getenv("TOMO_SUPERGROK_OAUTH_JSON_B64")
                or os.getenv("TOMO_GROK_AUTH_B64")
                or os.getenv("GROK_AUTH_B64")
            ),
            auth_path=auth_path or default_grok_auth_path(),
        )

    def bootstrap(self) -> bool:
        if not self.encoded_auth:
            return False
        bootstrap = self._decode()
        fingerprint = hashlib.sha256(self.encoded_auth.encode("ascii")).hexdigest()
        fingerprint_path = self.auth_path.parent / f".{self.auth_path.name}.bootstrap"
        current = self._read_auth()
        if self._read_fingerprint(fingerprint_path) != fingerprint or current is None:
            current = bootstrap
            self._write_auth(current)
            self._write_private(fingerprint_path, fingerprint)
        if self._refresh_if_needed(current):
            self._write_auth(current)
        return True

    def access_token(self) -> str:
        """Refresh and return the host-held token immediately before sandbox use."""
        if not self.bootstrap():
            raise RuntimeError("hosted access token unavailable")
        token = _token_payload(self._read_auth())
        if token is None or not isinstance(token.get("access_token"), str) or not token["access_token"]:
            raise RuntimeError("hosted access token unavailable")
        return token["access_token"]

    def refresh(self) -> None:
        """Force one Railway-managed OAuth refresh after a sandbox rejection."""
        if not self.bootstrap():
            raise RuntimeError("hosted access token unavailable")
        payload = self._read_auth()
        if payload is None:
            raise RuntimeError("hosted access token unavailable")
        self._refresh_if_needed(payload, force=True)
        self._write_auth(payload)

    def _decode(self) -> dict[str, Any]:
        try:
            raw = base64.b64decode(self.encoded_auth.encode("ascii"), validate=True)
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid hosted Grok authentication configuration") from error
        if not isinstance(payload, dict):
            raise ValueError("invalid hosted Grok authentication configuration")
        return _normalize(payload)

    def _read_auth(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _read_fingerprint(path: Path) -> str | None:
        try:
            return path.read_text(encoding="ascii").strip()
        except OSError:
            return None

    def _refresh_if_needed(self, payload: dict[str, Any], *, force: bool = False) -> bool:
        token = _token_payload(payload)
        if token is None or not isinstance(token.get("refresh_token"), str):
            return False
        try:
            expires_at = int(token.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0
        if not force and expires_at > time.time() + _REFRESH_WINDOW_SECONDS:
            return False
        config = OAuthManager.default_providers()["supergrok"]
        response = httpx.post(
            config.token_url,
            data={"grant_type": "refresh_token", "client_id": config.client_id, "refresh_token": token["refresh_token"]},
            timeout=60,
        )
        response.raise_for_status()
        refreshed = _normalize(response.json())
        if not isinstance(refreshed, dict) or not isinstance(refreshed.get("access_token"), str):
            raise ValueError("hosted Grok authentication refresh returned an invalid response")
        old_refresh_token = token["refresh_token"]
        token.update(refreshed)
        token.setdefault("refresh_token", old_refresh_token)
        if "expires_in" in refreshed:
            token["expires_at"] = int(time.time()) + int(refreshed["expires_in"])
        return True

    def _write_auth(self, payload: dict[str, Any]) -> None:
        self._write_private(self.auth_path, json.dumps(payload, separators=(",", ":")))

    @staticmethod
    def _write_private(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(value)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
