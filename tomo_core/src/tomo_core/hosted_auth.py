from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx

from .oauth import OAuthManager


_KEYS = {
    "accessToken": "access_token",
    "refreshToken": "refresh_token",
    "expiresAt": "expires_at",
    "expiry": "expires_at",
}
_REFRESH_WINDOW_SECONDS = 120
_DEFAULT_TOKEN_URL = "https://auth.x.ai/oauth2/token"


class HostedAuthError(RuntimeError):
    """A hosted-auth failure that is safe to show in operational logs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"hosted authentication failed: {code}")


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
    elif isinstance(value, list):
        for child in value:
            found = _token_payload(child)
            if found is not None:
                return found
    return None


class HostedSuperGrokTokenBroker:
    def __init__(
        self,
        data_dir: str | Path,
        bootstrap_b64: str | None,
        token_url: str = _DEFAULT_TOKEN_URL,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.bootstrap_b64 = bootstrap_b64
        self.token_url = token_url
        self.auth_path = self.data_dir / "hosted-auth" / "supergrok.json"
        self.fingerprint_path = self.auth_path.parent / ".supergrok.json.bootstrap"
        self.lock_path = self.auth_path.parent / ".supergrok.json.lock"

    def access_token(self, force_refresh: bool = False) -> str:
        """Return a current access token without revealing credential material on errors."""
        bootstrap = self._decode_bootstrap()
        fingerprint = hashlib.sha256(self.bootstrap_b64.encode("ascii")).hexdigest()
        with self._process_lock():
            payload = self._read_auth()
            if self._read_fingerprint() != fingerprint or payload is None:
                payload = bootstrap
                self._write_auth(payload)
                self._write_private(self.fingerprint_path, fingerprint)

            token = _token_payload(payload)
            if token is None or not isinstance(token.get("access_token"), str) or not token["access_token"]:
                raise HostedAuthError("access_token_unavailable")
            if self._needs_refresh(token, force_refresh):
                self._refresh(token)
                self._write_auth(payload)
            return token["access_token"]

    def _decode_bootstrap(self) -> dict[str, Any]:
        if not isinstance(self.bootstrap_b64, str) or not self.bootstrap_b64:
            raise HostedAuthError("invalid_bootstrap")
        try:
            raw = base64.b64decode(self.bootstrap_b64.encode("ascii"), validate=True)
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError):
            raise HostedAuthError("invalid_bootstrap") from None
        if not isinstance(payload, dict):
            raise HostedAuthError("invalid_bootstrap")
        return _normalize(payload)

    def _read_auth(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return _normalize(payload) if isinstance(payload, dict) else None

    def _read_fingerprint(self) -> str | None:
        try:
            return self.fingerprint_path.read_text(encoding="ascii").strip()
        except OSError:
            return None

    @staticmethod
    def _needs_refresh(token: dict[str, Any], force_refresh: bool) -> bool:
        if force_refresh:
            return True
        try:
            expires_at = int(token.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0
        return expires_at <= time.time() + _REFRESH_WINDOW_SECONDS

    def _refresh(self, token: dict[str, Any]) -> None:
        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise HostedAuthError("refresh_unavailable")
        client_id = OAuthManager.default_providers()["supergrok"].client_id
        try:
            response = httpx.post(
                self.token_url,
                data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token},
                timeout=60,
            )
            response.raise_for_status()
            refreshed = _normalize(response.json())
        except Exception:
            raise HostedAuthError("refresh_failed") from None
        if not isinstance(refreshed, dict) or not isinstance(refreshed.get("access_token"), str) or not refreshed["access_token"]:
            raise HostedAuthError("refresh_failed")
        token.update(refreshed)
        if not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
            token["refresh_token"] = refresh_token
        if "expires_in" in refreshed:
            try:
                token["expires_at"] = int(time.time()) + int(refreshed["expires_in"])
            except (TypeError, ValueError):
                raise HostedAuthError("refresh_failed") from None

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

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
