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
_KEY_NAME = "attachment-capability.key"


class AttachmentCapabilityError(ValueError):
    pass


def hash_file_id(file_id: str) -> str:
    if not isinstance(file_id, str) or not file_id or len(file_id) > 4096:
        raise AttachmentCapabilityError("invalid_capability")
    return hashlib.sha256(file_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AttachmentCapability:
    owner_id: str
    generation_id: str
    file_hashes: tuple[str, ...]
    issued_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in (self.owner_id, self.generation_id)) or any(not isinstance(value, int) or isinstance(value, bool) for value in (self.issued_at, self.expires_at)) or self.expires_at <= self.issued_at:
            raise AttachmentCapabilityError("invalid_capability")
        hashes = tuple(self.file_hashes)
        if not hashes or len(hashes) > 8 or len(set(hashes)) != len(hashes) or any(not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in hashes):
            raise AttachmentCapabilityError("invalid_capability")
        object.__setattr__(self, "file_hashes", hashes)


def load_or_create_attachment_key(data_dir: str | Path) -> bytes:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / _KEY_NAME
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        temporary_path = root / f".{_KEY_NAME}.{secrets.token_hex(16)}.tmp"
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
        raise AttachmentCapabilityError("invalid_capability_key")
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            if os.stat(path).st_mode & 0o077:
                raise OSError("capability key remains group/world readable")
        except OSError as error:
            raise AttachmentCapabilityError("capability_key_permissions") from error
    return key


def issue_attachment_capability(key: bytes, capability: AttachmentCapability) -> str:
    _key(key)
    if capability.expires_at - capability.issued_at > 300:
        raise AttachmentCapabilityError("invalid_capability")
    raw = json.dumps({"v": _VERSION, "owner_id": capability.owner_id, "generation_id": capability.generation_id, "file_hashes": capability.file_hashes, "issued_at": capability.issued_at, "expires_at": capability.expires_at}, separators=(",", ":"), ensure_ascii=True).encode()
    payload = _encode(raw)
    signature = _encode(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{_VERSION}.{payload}.{signature}"


def verify_attachment_capability(key: bytes, token: str, file_id: str, *, owner_id: str, generation_id: str, now: int | None = None) -> AttachmentCapability:
    _key(key)
    try:
        version, payload, signature = token.split(".")
        expected = hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest()
        value = json.loads(_decode(payload).decode("ascii"))
        if version != _VERSION or not hmac.compare_digest(expected, _decode(signature)):
            raise ValueError
        claim = AttachmentCapability(value["owner_id"], value["generation_id"], tuple(value["file_hashes"]), value["issued_at"], value["expires_at"])
    except (AttributeError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise AttachmentCapabilityError("invalid_capability") from None
    current = int(time.time()) if now is None else now
    if claim.expires_at <= current:
        raise AttachmentCapabilityError("expired_capability")
    if claim.issued_at > current + 30 or claim.expires_at - claim.issued_at > 300:
        raise AttachmentCapabilityError("invalid_capability")
    if not hmac.compare_digest(claim.owner_id, owner_id) or not hmac.compare_digest(claim.generation_id, generation_id) or not any(hmac.compare_digest(value, hash_file_id(file_id)) for value in claim.file_hashes):
        raise AttachmentCapabilityError("forbidden_capability")
    return claim


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise ValueError
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != 32:
        raise AttachmentCapabilityError("invalid_capability_key")
