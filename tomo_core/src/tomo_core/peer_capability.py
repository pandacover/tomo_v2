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
_PREFIX = b"tomo-peer-capability\x00"
_KEY_NAME = "peer-capability.key"
_OPERATIONS = frozenset({"list_relationships", "ask", "inspect_request"})
_FIELDS = frozenset(
    {
        "v",
        "owner_id",
        "actor_id",
        "destination",
        "session_id",
        "generation_id",
        "issued_at",
        "expires_at",
        "operations",
    }
)


class PeerCapabilityError(ValueError):
    pass


@dataclass(frozen=True)
class PeerCapability:
    owner_id: str
    actor_id: str
    destination: str
    session_id: str
    generation_id: str
    issued_at: int
    expires_at: int
    operations: frozenset[str]

    def __post_init__(self):
        if any(
            not isinstance(v, str) or not v.strip() or len(v) > 512
            for v in (
                self.owner_id,
                self.actor_id,
                self.destination,
                self.session_id,
                self.generation_id,
            )
        ):
            raise PeerCapabilityError("invalid_capability")
        if ":" not in self.destination or self.destination.startswith("peer:"):
            raise PeerCapabilityError("invalid_capability")
        if (
            any(
                not isinstance(v, int) or isinstance(v, bool)
                for v in (self.issued_at, self.expires_at)
            )
            or self.expires_at <= self.issued_at
        ):
            raise PeerCapabilityError("invalid_capability")
        if not self.operations or self.operations - _OPERATIONS:
            raise PeerCapabilityError("invalid_capability")


def load_or_create_key(data_dir: str | Path) -> bytes:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / _KEY_NAME
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        temporary = root / f".{_KEY_NAME}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(key)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                key = path.read_bytes()
        finally:
            temporary.unlink(missing_ok=True)
    if len(key) != 32:
        raise PeerCapabilityError("invalid_capability_key")
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            if os.stat(path).st_mode & 0o077:
                raise OSError()
        except OSError as error:
            raise PeerCapabilityError("capability_key_permissions") from error
    return key


def issue_capability(key: bytes, capability: PeerCapability) -> str:
    _key(key)
    if capability.expires_at - capability.issued_at > 300:
        raise PeerCapabilityError("invalid_capability")
    payload = {
        "v": _VERSION,
        "owner_id": capability.owner_id,
        "actor_id": capability.actor_id,
        "destination": capability.destination,
        "session_id": capability.session_id,
        "generation_id": capability.generation_id,
        "issued_at": capability.issued_at,
        "expires_at": capability.expires_at,
        "operations": sorted(capability.operations),
    }
    encoded = _encode(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    )
    signature = hmac.new(
        key, _PREFIX + encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{_VERSION}.{encoded}.{_encode(signature)}"


def verify_capability(
    key: bytes,
    token: str,
    *,
    now: int | None = None,
    operation: str | None = None,
    owner_id: str | None = None,
    actor_id: str | None = None,
    destination: str | None = None,
    session_id: str | None = None,
    generation_id: str | None = None,
) -> PeerCapability:
    _key(key)
    current = int(time.time()) if now is None else now
    try:
        version, encoded, signed = token.split(".")
        payload = json.loads(_decode(encoded).decode("ascii"))
        if (
            version != _VERSION
            or not isinstance(payload, dict)
            or frozenset(payload) != _FIELDS
            or not isinstance(payload["operations"], list)
            or len(payload["operations"]) != len(set(payload["operations"]))
        ):
            raise ValueError()
        signature = _decode(signed)
        claim = PeerCapability(
            payload["owner_id"],
            payload["actor_id"],
            payload["destination"],
            payload["session_id"],
            payload["generation_id"],
            payload["issued_at"],
            payload["expires_at"],
            frozenset(payload["operations"]),
        )
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        PeerCapabilityError,
    ):
        raise PeerCapabilityError("invalid_capability") from None
    if (
        not hmac.compare_digest(
            signature,
            hmac.new(key, _PREFIX + encoded.encode("ascii"), hashlib.sha256).digest(),
        )
        or claim.issued_at > current + 30
        or claim.expires_at - claim.issued_at > 300
    ):
        raise PeerCapabilityError("invalid_capability")
    if claim.expires_at <= current:
        raise PeerCapabilityError("expired_capability")
    if operation is not None and operation not in claim.operations:
        raise PeerCapabilityError("forbidden_capability")
    for field, expected in {
        "owner_id": owner_id,
        "actor_id": actor_id,
        "destination": destination,
        "session_id": session_id,
        "generation_id": generation_id,
    }.items():
        if expected is not None and getattr(claim, field) != expected:
            raise PeerCapabilityError("forbidden_capability")
    return claim


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not value
        or any(
            c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for c in value
        )
    ):
        raise ValueError()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise PeerCapabilityError("invalid_capability_key")
