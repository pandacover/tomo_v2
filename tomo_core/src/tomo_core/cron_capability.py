from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path


_VERSION = "v1"
_KEY_NAME = "cron-capability.key"
_OPERATIONS = frozenset({"create", "list", "inspect", "update", "pause", "resume", "run_now", "delete", "history"})
_MAX_OWNER = 256
_MAX_ACTOR = 256
_MAX_DESTINATION = 512
_MAX_SESSION = 512


def _posix_permissions_supported() -> bool:
    return os.name != "nt"


class CronCapabilityError(ValueError):
    """A safe bearer-capability validation failure."""


@dataclass(frozen=True)
class CronCapability:
    owner_id: str
    actor_id: str
    destination: str
    session_id: str
    issued_at: int
    expires_at: int
    operations: frozenset[str] = _OPERATIONS

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id.strip() or len(self.owner_id) > _MAX_OWNER:
            raise CronCapabilityError("invalid_capability")
        if not isinstance(self.actor_id, str) or not self.actor_id.strip() or len(self.actor_id) > _MAX_ACTOR:
            raise CronCapabilityError("invalid_capability")
        if not isinstance(self.destination, str) or not self.destination.startswith("telegram:") or len(self.destination) > _MAX_DESTINATION:
            raise CronCapabilityError("invalid_capability")
        if not isinstance(self.session_id, str) or not self.session_id.strip() or len(self.session_id) > _MAX_SESSION:
            raise CronCapabilityError("invalid_capability")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (self.issued_at, self.expires_at)) or self.expires_at <= self.issued_at:
            raise CronCapabilityError("invalid_capability")
        if not self.operations or not self.operations <= _OPERATIONS:
            raise CronCapabilityError("invalid_capability")


def load_or_create_key(data_dir: str | Path) -> bytes:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / _KEY_NAME
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        temporary_path = root / f".{_KEY_NAME}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(temporary_path, flags, 0o600)
        except FileExistsError:
            raise CronCapabilityError("capability_key_creation") from None
        else:
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(key)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    os.link(temporary_path, path)
                except FileExistsError:
                    key = path.read_bytes()
            finally:
                temporary_path.unlink(missing_ok=True)
    if len(key) != 32:
        raise CronCapabilityError("invalid_capability_key")
    if _posix_permissions_supported():
        try:
            os.chmod(path, 0o600)
            if os.stat(path).st_mode & 0o077:
                raise OSError("capability key remains group/world readable")
        except OSError as error:
            raise CronCapabilityError("capability_key_permissions") from error
    return key


def issue_capability(key: bytes, capability: CronCapability) -> str:
    _validate_key(key)
    if capability.expires_at - capability.issued_at > 3600:
        raise CronCapabilityError("invalid_capability")
    payload = {
        "v": _VERSION,
        "owner_id": capability.owner_id,
        "actor_id": capability.actor_id,
        "destination": capability.destination,
        "session_id": capability.session_id,
        "issued_at": capability.issued_at,
        "expires_at": capability.expires_at,
        "operations": sorted(capability.operations),
    }
    encoded = _encode(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{_VERSION}.{encoded}.{_encode(signature)}"


def verify_capability(key: bytes, token: str, *, now: int | None = None, operation: str | None = None) -> CronCapability:
    _validate_key(key)
    if not isinstance(token, str):
        raise CronCapabilityError("invalid_capability")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _VERSION:
        raise CronCapabilityError("invalid_capability")
    try:
        payload_bytes = parts[1].encode("ascii")
        expected = hmac.new(key, payload_bytes, hashlib.sha256).digest()
        signature = _decode(parts[2])
        payload = json.loads(_decode(parts[1]).decode("ascii"))
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise CronCapabilityError("invalid_capability") from None
    if not hmac.compare_digest(expected, signature) or not isinstance(payload, dict) or payload.get("v") != _VERSION:
        raise CronCapabilityError("invalid_capability")
    operations = payload.get("operations")
    if not isinstance(operations, list) or any(not isinstance(item, str) for item in operations):
        raise CronCapabilityError("invalid_capability")
    try:
        capability = CronCapability(payload["owner_id"], payload["actor_id"], payload["destination"], payload["session_id"], payload["issued_at"], payload["expires_at"], frozenset(operations))
    except (KeyError, TypeError, CronCapabilityError):
        raise CronCapabilityError("invalid_capability") from None
    if capability.expires_at <= (int(time.time()) if now is None else now):
        raise CronCapabilityError("expired_capability")
    current = int(time.time()) if now is None else now
    if capability.issued_at > current + 30 or capability.expires_at - capability.issued_at > 3600:
        raise CronCapabilityError("invalid_capability")
    if operation is not None and operation not in capability.operations:
        raise CronCapabilityError("forbidden_capability")
    return capability


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("invalid encoding")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise CronCapabilityError("invalid_capability_key")
