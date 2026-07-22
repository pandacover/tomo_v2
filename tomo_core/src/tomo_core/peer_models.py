from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import re


class RelationshipStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"
    BLOCKED = "blocked"


class RequestKind(StrEnum):
    ORDINARY_MESSAGE = "ordinary_message"
    AVAILABILITY = "availability"
    SENSITIVE = "sensitive"


class RequestAction(StrEnum):
    ASK = "ask"
    MUTATING_ACTION = "mutating_action"
    CREDENTIALS = "credentials"
    THIRD_PARTY_FORWARD = "third_party_forward"
    COMMITMENT_PROPOSAL = "commitment_proposal"


class RequestStatus(StrEnum):
    PENDING = "pending"
    CONFIRMATION_PENDING = "confirmation_pending"
    AUTHORIZED = "authorized"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ResponseStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class NoticeStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SENT = "sent"
    UNKNOWN = "unknown"
    SUPPRESSED = "suppressed"


def _text(value: str, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not (clean := value.strip()) or len(clean) > limit:
        raise ValueError(f"invalid_{name}")
    return clean


def _utc(value: datetime, name: str = "time") -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"invalid_{name}")
    return value.astimezone(timezone.utc)


def _count(value: int, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"invalid_{name}")
    return value


@dataclass(frozen=True)
class Handle:
    value: str

    def __post_init__(self) -> None:
        value = self.value.strip().lower() if isinstance(self.value, str) else ""
        if not re.fullmatch(r"[a-z0-9_]{3,32}", value):
            raise ValueError("invalid_handle")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class Relationship:
    relationship_id: str
    owner_a_id: str
    owner_b_id: str
    invited_by_owner_id: str
    status: RelationshipStatus | str
    created_at: datetime
    revision: int = 1

    def __post_init__(self) -> None:
        a, b = _text(self.owner_a_id, "owner_id"), _text(self.owner_b_id, "owner_id")
        inviter = _text(self.invited_by_owner_id, "owner_id")
        if a == b or inviter not in (a, b):
            raise ValueError("invalid_relationship")
        object.__setattr__(
            self, "relationship_id", _text(self.relationship_id, "relationship_id")
        )
        object.__setattr__(self, "owner_a_id", min(a, b))
        object.__setattr__(self, "owner_b_id", max(a, b))
        object.__setattr__(self, "invited_by_owner_id", inviter)
        object.__setattr__(self, "status", RelationshipStatus(self.status))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "revision", _count(self.revision, "revision", 1))

    def other_owner(self, owner_id: str) -> str:
        if owner_id == self.owner_a_id:
            return self.owner_b_id
        if owner_id == self.owner_b_id:
            return self.owner_a_id
        raise ValueError("not_found")


@dataclass(frozen=True)
class DirectionalGrant:
    relationship_id: str
    grantor_owner_id: str
    grantee_owner_id: str
    communicate: bool
    auto_reply: bool
    share_availability: bool
    revision: int
    expires_at: datetime | None
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("relationship_id", "grantor_owner_id", "grantee_owner_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.grantor_owner_id == self.grantee_owner_id:
            raise ValueError("invalid_grant")
        if any(
            not isinstance(value, bool)
            for value in (self.communicate, self.auto_reply, self.share_availability)
        ):
            raise ValueError("invalid_grant")
        object.__setattr__(self, "revision", _count(self.revision, "revision", 1))
        object.__setattr__(
            self,
            "expires_at",
            None if self.expires_at is None else _utc(self.expires_at),
        )
        object.__setattr__(self, "updated_at", _utc(self.updated_at))


@dataclass(frozen=True)
class PeerThread:
    thread_id: str
    relationship_id: str
    sender_owner_id: str
    recipient_owner_id: str
    purpose: str
    status: str
    created_at: datetime
    expires_at: datetime
    request_count: int = 0

    def __post_init__(self) -> None:
        for name in (
            "thread_id",
            "relationship_id",
            "sender_owner_id",
            "recipient_owner_id",
            "purpose",
            "status",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "expires_at", _utc(self.expires_at))
        object.__setattr__(
            self, "request_count", _count(self.request_count, "request_count")
        )
        if (
            self.status != "active"
            or self.expires_at <= self.created_at
            or self.request_count > 4
        ):
            raise ValueError("invalid_thread")


@dataclass(frozen=True)
class PeerRequest:
    request_id: str
    relationship_id: str
    sender_owner_id: str
    recipient_owner_id: str
    thread_id: str
    source_generation_id: str
    source_call_id: str
    kind: RequestKind | str
    action: RequestAction | str
    text: str
    thread_sequence: int
    created_at: datetime
    status: RequestStatus | str = RequestStatus.PENDING
    disclosure_scope: str = "none"

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "relationship_id",
            "sender_owner_id",
            "recipient_owner_id",
            "thread_id",
            "source_generation_id",
            "source_call_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.sender_owner_id == self.recipient_owner_id:
            raise ValueError("invalid_request")
        object.__setattr__(self, "kind", RequestKind(self.kind))
        object.__setattr__(self, "action", RequestAction(self.action))
        object.__setattr__(self, "text", _text(self.text, "text", 2000))
        object.__setattr__(
            self, "thread_sequence", _count(self.thread_sequence, "thread_sequence", 1)
        )
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "status", RequestStatus(self.status))
        if self.disclosure_scope not in {"none", "availability", "calendar_detail", "contact_email", "contact_phone", "precise_location", "commitment_proposal"}:
            raise ValueError("invalid_disclosure_scope")


@dataclass(frozen=True)
class PeerResponse:
    request_id: str
    responder_owner_id: str
    frames: tuple[str, ...]
    status: ResponseStatus | str
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _text(self.request_id, "request_id"))
        object.__setattr__(
            self, "responder_owner_id", _text(self.responder_owner_id, "owner_id")
        )
        if not isinstance(self.frames, tuple) or not 1 <= len(self.frames) <= 3 or sum(len(frame) for frame in self.frames if isinstance(frame, str)) > 2000:
            raise ValueError("invalid_frames")
        object.__setattr__(
            self, "frames", tuple(_text(frame, "frame", 2000) for frame in self.frames)
        )
        object.__setattr__(self, "status", ResponseStatus(self.status))
        object.__setattr__(self, "created_at", _utc(self.created_at))


@dataclass(frozen=True)
class PendingConfirmation:
    pending_id: str
    request_id: str
    affected_owner_id: str
    action_kind: str
    payload_hash: str
    preview: str
    status: ConfirmationStatus | str
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None
    relationship_revision: int

    def __post_init__(self) -> None:
        for name in ("pending_id", "request_id", "affected_owner_id", "action_kind"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not isinstance(self.payload_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.payload_hash
        ):
            raise ValueError("invalid_payload_hash")
        object.__setattr__(self, "preview", _text(self.preview, "preview", 256))
        object.__setattr__(self, "status", ConfirmationStatus(self.status))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(
            self,
            "relationship_revision",
            _count(self.relationship_revision, "relationship_revision", 1),
        )
        decided_at = (
            None if self.decided_at is None else _utc(self.decided_at, "decided_at")
        )
        if (self.status == ConfirmationStatus.PENDING) != (decided_at is None):
            raise ValueError("invalid_decided_at")
        object.__setattr__(self, "decided_at", decided_at)


@dataclass(frozen=True)
class PeerNotice:
    pending_id: str
    lease_token: str
    peer_handle: str
    text: str


@dataclass(frozen=True)
class PeerSubmitResult:
    request_id: str
    status: str
    peer_handle: str
    thread_id: str | None = None
    pending_id: str | None = None


@dataclass(frozen=True)
class PeerRequestInspection:
    request_id: str
    status: str
    peer_handle: str
    response: PeerResponseSummary | None = None
    thread_id: str | None = None
    pending_expires_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class PeerRelationshipSummary:
    relationship_id: str
    peer_handle: str
    status: RelationshipStatus
    grant: PeerGrantSummary | None
    peer_grant: PeerGrantSummary | None
    peer_communicate: bool
    peer_auto_reply: bool
    peer_share_availability: bool
    relationship_revision: int
    expires_at: datetime | None
    can_accept: bool


@dataclass(frozen=True)
class PeerAuditEvent:
    relationship_id: str
    action_code: str
    created_at: datetime


@dataclass(frozen=True)
class PeerHistoryEntry:
    request_id: str
    thread_id: str
    direction: str
    kind: RequestKind
    status: RequestStatus
    created_at: datetime
    response_status: ResponseStatus | None


@dataclass(frozen=True)
class PeerGrantSummary:
    communicate: bool
    auto_reply: bool
    share_availability: bool
    revision: int
    expires_at: datetime | None


@dataclass(frozen=True)
class PeerResponseSummary:
    frames: tuple[str, ...]
    status: ResponseStatus
    created_at: datetime
