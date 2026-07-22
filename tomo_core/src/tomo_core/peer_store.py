from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .peer_models import (
    DirectionalGrant,
    Handle,
    NoticeStatus,
    PeerAuditEvent,
    PeerGrantSummary,
    PeerHistoryEntry,
    PeerNotice,
    PeerRelationshipSummary,
    PeerRequest,
    PeerResponse,
    Relationship,
    RelationshipStatus,
    RequestKind,
    RequestStatus,
    ResponseStatus,
)

THREAD_LIFETIME = timedelta(minutes=15)
CONFIRMATION_LIFETIME = timedelta(minutes=15)
RETENTION = timedelta(days=30)
MAX_THREAD_REQUESTS = 4
MAX_HOURLY_REQUESTS = 10
MAX_HOURLY_OUTBOUND_INVITES = 10
MAX_HOURLY_INBOUND_INVITES = 20
SCHEMA_VERSION = 1
AUDIT_CODES = frozenset(
    {
        "invite",
        "accept",
        "grant",
        "revoke",
        "block",
        "submit",
        "confirm",
        "cancel",
        "complete",
        "fail",
    }
)


def utc_now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("invalid_now")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _id(value: str, name: str) -> str:
    if not isinstance(value, str) or not (value := value.strip()) or len(value) > 256:
        raise ValueError(f"invalid_{name}")
    return value


def _preview(value: str) -> str:
    """Keep owner notices informative without preserving multiline request context."""
    return _id(" ".join(value.split()), "preview")[:160]


class PeerStore:
    """Transactional peer repository. All methods enforce owner-scoped access."""

    def __init__(self, data_dir: str | Path):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "peer.sqlite3"
        self._init()

    def register_handle(
        self, owner_id: str, handle: str | Handle, *, now: datetime | None = None
    ) -> None:
        owner_id, handle, now = (
            _id(owner_id, "owner_id"),
            Handle(handle) if not isinstance(handle, Handle) else handle,
            utc_now(now),
        )
        with self._write() as db:
            if self._owner_deleted(db, owner_id):
                raise ValueError("owner_deleted")
            row = db.execute(
                "SELECT owner_id FROM peer_handles WHERE handle=?", (handle.value,)
            ).fetchone()
            if row and row[0] != owner_id:
                raise ValueError("handle_taken")
            db.execute(
                "DELETE FROM peer_handles WHERE owner_id=? AND handle<>?",
                (owner_id, handle.value),
            )
            db.execute(
                "INSERT OR IGNORE INTO peer_handles VALUES(?,?,?)",
                (handle.value, owner_id, _iso(now)),
            )
            db.execute(
                "DELETE FROM peer_archives WHERE owner_id=? AND table_name='peer_profile'",
                (owner_id,),
            )

    def invite(
        self, owner_id: str, handle: str | Handle, *, now: datetime | None = None
    ) -> Relationship | None:
        owner_id, handle, now = (
            _id(owner_id, "owner_id"),
            Handle(handle) if not isinstance(handle, Handle) else handle,
            utc_now(now),
        )
        with self._write() as db:
            if self._owner_deleted(db, owner_id):
                raise ValueError("owner_deleted")
            row = db.execute(
                "SELECT owner_id FROM peer_handles WHERE handle=?", (handle.value,)
            ).fetchone()
            if row is None:
                return None
            a, b = sorted((owner_id, row[0]))
            if a == b:
                raise ValueError("invalid_relationship")
            existing = db.execute(
                "SELECT * FROM peer_relationships WHERE owner_a=? AND owner_b=?", (a, b)
            ).fetchone()
            if existing:
                return self._relationship(existing)
            recent = _iso(now - timedelta(hours=1))
            outbound = db.execute(
                "SELECT COUNT(*) FROM peer_relationships "
                "WHERE invited_by=? AND status='pending' AND created_at>=?",
                (owner_id, recent),
            ).fetchone()[0]
            inbound = db.execute(
                "SELECT COUNT(*) FROM peer_relationships "
                "WHERE status='pending' AND invited_by<>? "
                "AND ? IN(owner_a,owner_b) AND created_at>=?",
                (row[0], row[0], recent),
            ).fetchone()[0]
            if (
                outbound >= MAX_HOURLY_OUTBOUND_INVITES
                or inbound >= MAX_HOURLY_INBOUND_INVITES
            ):
                raise ValueError("rate_limited")
            relation = Relationship(str(uuid.uuid4()), a, b, owner_id, "pending", now)
            db.execute(
                "INSERT INTO peer_relationships VALUES(?,?,?,?,?,?,?)",
                (relation.relationship_id, a, b, owner_id, "pending", _iso(now), 1),
            )
            self._audit(db, owner_id, relation.relationship_id, "invite", now)
            return relation

    def accept(
        self, owner_id: str, relationship_id: str, *, now: datetime | None = None
    ) -> str:
        owner_id, relationship_id, now = (
            _id(owner_id, "owner_id"),
            _id(relationship_id, "relationship_id"),
            utc_now(now),
        )
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM peer_relationships WHERE relationship_id=? AND ? IN(owner_a,owner_b)",
                (relationship_id, owner_id),
            ).fetchone()
            if not row:
                return "not_found"
            if row["invited_by"] == owner_id:
                return "not_invitee"
            if row["status"] == "active":
                return "accepted"
            if row["status"] != "pending":
                return "not_pending"
            db.execute(
                "UPDATE peer_relationships SET status='active',revision=revision+1 WHERE relationship_id=?",
                (relationship_id,),
            )
            self._audit(db, owner_id, relationship_id, "accept", now)
            return "accepted"

    def update_grant(
        self,
        owner_id: str,
        relationship_id: str,
        grantee: str,
        communicate: bool,
        auto_reply: bool,
        share_availability: bool,
        expected_revision: int,
        *,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> DirectionalGrant | None:
        owner_id, relationship_id, grantee, now = (
            _id(owner_id, "owner_id"),
            _id(relationship_id, "relationship_id"),
            _id(grantee, "owner_id"),
            utc_now(now),
        )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
            or any(
                not isinstance(v, bool)
                for v in (communicate, auto_reply, share_availability)
            )
        ):
            raise ValueError("stale_revision")
        expiry = None if expires_at is None else utc_now(expires_at)
        with self._write() as db:
            relation = db.execute(
                "SELECT * FROM peer_relationships WHERE relationship_id=? AND ? IN(owner_a,owner_b) AND status='active'",
                (relationship_id, owner_id),
            ).fetchone()
            if (
                not relation
                or grantee == owner_id
                or grantee not in (relation["owner_a"], relation["owner_b"])
            ):
                return None
            current = db.execute(
                "SELECT revision FROM peer_grants WHERE relationship_id=? AND grantor=?",
                (relationship_id, owner_id),
            ).fetchone()
            revision = 1 if not current else current[0] + 1
            if (0 if not current else current[0]) != expected_revision:
                raise ValueError("stale_revision")
            db.execute(
                "INSERT INTO peer_grants VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(relationship_id,grantor) DO UPDATE SET communicate=excluded.communicate,auto_reply=excluded.auto_reply,share_availability=excluded.share_availability,revision=excluded.revision,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (
                    relationship_id,
                    owner_id,
                    int(communicate),
                    int(auto_reply),
                    int(share_availability),
                    revision,
                    None if expiry is None else _iso(expiry),
                    _iso(now),
                ),
            )
            self._audit(db, owner_id, relationship_id, "grant", now)
            return DirectionalGrant(
                relationship_id,
                owner_id,
                grantee,
                communicate,
                auto_reply,
                share_availability,
                revision,
                expiry,
                now,
            )

    def revoke(
        self,
        owner_id: str,
        relationship_id: str,
        *,
        block: bool = False,
        now: datetime | None = None,
    ) -> bool:
        owner_id, relationship_id, now = (
            _id(owner_id, "owner_id"),
            _id(relationship_id, "relationship_id"),
            utc_now(now),
        )
        with self._write() as db:
            changed = db.execute(
                "UPDATE peer_relationships SET status=?,revision=revision+1 WHERE relationship_id=? AND ? IN(owner_a,owner_b) AND status IN('pending','active')",
                ("blocked" if block else "revoked", relationship_id, owner_id),
            ).rowcount
            if changed:
                db.execute(
                    "UPDATE peer_requests SET status='denied',lease_token=NULL,lease_until=NULL WHERE relationship_id=? AND status IN('pending','authorized','leased','confirmation_pending')",
                    (relationship_id,),
                )
                db.execute(
                    "UPDATE peer_confirmation_notices SET status='suppressed',lease_token=NULL,lease_until=NULL WHERE pending_id IN(SELECT c.pending_id FROM peer_confirmations c JOIN peer_requests r ON r.request_id=c.request_id WHERE r.relationship_id=?) AND status IN('pending','leased')",
                    (relationship_id,),
                )
                self._audit(
                    db, owner_id, relationship_id, "block" if block else "revoke", now
                )
            return bool(changed)

    def delete_owner(self, owner_id: str, *, now: datetime | None = None) -> None:
        """Fence the deleted owner while retaining the other owner's disclosed history."""
        owner_id, now = _id(owner_id, "owner_id"), utc_now(now)
        with self._write() as db:
            db.execute(
                "INSERT OR IGNORE INTO peer_deleted_owners(owner_id,deleted_at) VALUES(?,?)",
                (owner_id, _iso(now)),
            )
            relations = db.execute("SELECT relationship_id FROM peer_relationships WHERE ? IN(owner_a,owner_b)", (owner_id,)).fetchall()
            for relation in relations:
                relationship_id = relation["relationship_id"]
                db.execute("UPDATE peer_relationships SET status='revoked',revision=revision+1 WHERE relationship_id=? AND status IN('pending','active')", (relationship_id,))
                db.execute("UPDATE peer_requests SET status='denied',lease_token=NULL,lease_until=NULL WHERE relationship_id=? AND status IN('pending','authorized','leased','confirmation_pending')", (relationship_id,))
                db.execute("UPDATE peer_confirmation_notices SET status='suppressed',lease_token=NULL,lease_until=NULL WHERE pending_id IN(SELECT c.pending_id FROM peer_confirmations c JOIN peer_requests r ON r.request_id=c.request_id WHERE r.relationship_id=?)", (relationship_id,))
                db.execute("DELETE FROM peer_grants WHERE relationship_id=?", (relationship_id,))
                db.execute("INSERT INTO peer_relationship_history_watermarks(owner_id,relationship_id,purged_before) VALUES(?,?,?) ON CONFLICT(owner_id,relationship_id) DO UPDATE SET purged_before=excluded.purged_before", (owner_id, relationship_id, _iso(now)))
            db.execute("DELETE FROM peer_handles WHERE owner_id=?", (owner_id,))
            db.execute("DELETE FROM peer_archives WHERE owner_id=?", (owner_id,))

    def relationship_for_handle(
        self, owner_id: str, handle: str | Handle
    ) -> Relationship | None:
        owner_id, handle = (
            _id(owner_id, "owner_id"),
            Handle(handle) if not isinstance(handle, Handle) else handle,
        )
        with self._read() as db:
            target = db.execute(
                "SELECT owner_id FROM peer_handles WHERE handle=?", (handle.value,)
            ).fetchone()
            if not target:
                return None
            a, b = sorted((owner_id, target[0]))
            row = db.execute(
                "SELECT * FROM peer_relationships WHERE owner_a=? AND owner_b=?", (a, b)
            ).fetchone()
            return None if not row else self._relationship(row)

    def grants(
        self, owner_id: str, relationship_id: str
    ) -> tuple[DirectionalGrant, ...]:
        relation = self.get_relationship(owner_id, relationship_id)
        if not relation:
            return ()
        with self._read() as db:
            return self._grants(db, relation)

    def get_relationship(
        self, owner_id: str, relationship_id: str
    ) -> Relationship | None:
        owner_id, relationship_id = (
            _id(owner_id, "owner_id"),
            _id(relationship_id, "relationship_id"),
        )
        with self._read() as db:
            if self._owner_deleted(db, owner_id):
                return None
            row = db.execute(
                "SELECT * FROM peer_relationships WHERE relationship_id=? AND ? IN(owner_a,owner_b)",
                (relationship_id, owner_id),
            ).fetchone()
            return None if not row else self._relationship(row)

    def submit(
        self, request: PeerRequest, purpose: str, *, now: datetime
    ) -> tuple[PeerRequest, str | None, bool]:
        now, purpose = utc_now(now), _id(purpose, "purpose")
        with self._write() as db:
            duplicate = db.execute(
                "SELECT * FROM peer_requests WHERE sender=? AND generation_id=? AND call_id=?",
                (
                    request.sender_owner_id,
                    request.source_generation_id,
                    request.source_call_id,
                ),
            ).fetchone()
            if duplicate:
                pending = db.execute(
                    "SELECT pending_id FROM peer_confirmations WHERE request_id=?",
                    (duplicate["request_id"],),
                ).fetchone()
                return (
                    self._request(duplicate),
                    None if not pending else pending[0],
                    True,
                )
            if (
                db.execute(
                    "SELECT COUNT(*) FROM peer_requests WHERE relationship_id=? AND created_at>=?",
                    (request.relationship_id, _iso(now - timedelta(hours=1))),
                ).fetchone()[0]
                >= MAX_HOURLY_REQUESTS
            ):
                raise ValueError("rate_limited")
            thread = db.execute(
                "SELECT * FROM peer_threads WHERE thread_id=?", (request.thread_id,)
            ).fetchone()
            if not thread:
                db.execute(
                    "INSERT INTO peer_threads VALUES(?,?,?,?,?,'active',?,?,1)",
                    (
                        request.thread_id,
                        request.relationship_id,
                        request.sender_owner_id,
                        request.recipient_owner_id,
                        purpose,
                        _iso(now),
                        _iso(now + THREAD_LIFETIME),
                    ),
                )
                sequence = 1
            else:
                if any(
                    thread[k] != v
                    for k, v in (
                        ("relationship_id", request.relationship_id),
                        ("sender", request.sender_owner_id),
                        ("recipient", request.recipient_owner_id),
                    )
                ):
                    raise ValueError("thread_not_found")
                if thread["status"] != "active" or _dt(thread["expires_at"]) <= now:
                    raise ValueError("thread_expired")
                if thread["request_count"] >= MAX_THREAD_REQUESTS:
                    raise ValueError("thread_limit")
                db.execute(
                    "UPDATE peer_threads SET request_count=request_count+1 WHERE thread_id=?",
                    (request.thread_id,),
                )
                sequence = thread["request_count"] + 1
            saved = PeerRequest(
                request.request_id,
                request.relationship_id,
                request.sender_owner_id,
                request.recipient_owner_id,
                request.thread_id,
                request.source_generation_id,
                request.source_call_id,
                request.kind,
                request.action,
                request.text,
                sequence,
                request.created_at,
                request.status,
                request.disclosure_scope,
            )
            db.execute(
                "INSERT INTO peer_requests(request_id,relationship_id,sender,recipient,thread_id,generation_id,call_id,kind,action,text,sequence,created_at,status,disclosure_scope,lease_token,lease_until,attempt_count,available_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,0,?)",
                (
                    saved.request_id,
                    saved.relationship_id,
                    saved.sender_owner_id,
                    saved.recipient_owner_id,
                    saved.thread_id,
                    saved.source_generation_id,
                    saved.source_call_id,
                    saved.kind.value,
                    saved.action.value,
                    saved.text,
                    saved.thread_sequence,
                    _iso(saved.created_at),
                    saved.status.value,
                    saved.disclosure_scope,
                    _iso(now),
                ),
            )
            pending = None
            if saved.status == RequestStatus.CONFIRMATION_PENDING:
                pending = str(uuid.uuid4())
                relation = self._relationship(
                    db.execute(
                        "SELECT * FROM peer_relationships WHERE relationship_id=?",
                        (saved.relationship_id,),
                    ).fetchone()
                )
                revisions = json.dumps(
                    {
                        g.grantor_owner_id: g.revision
                        for g in self._grants(db, relation)
                    },
                    sort_keys=True,
                )
                db.execute(
                    "INSERT INTO peer_confirmations VALUES(?,?,?,?,?,?, 'pending',?,?,NULL,?,?)",
                    (
                        pending,
                        saved.request_id,
                        saved.recipient_owner_id,
                        saved.action.value,
                        hashlib.sha256(saved.text.encode()).hexdigest(),
                        _preview(saved.text),
                        _iso(now + CONFIRMATION_LIFETIME),
                        _iso(now),
                        relation.revision,
                        revisions,
                    ),
                )
                db.execute(
                    "INSERT INTO peer_confirmation_notices VALUES(?,?,NULL,NULL,?)",
                    (pending, NoticeStatus.PENDING.value, _iso(now)),
                )
            self._audit(db, saved.sender_owner_id, saved.relationship_id, "submit", now)
            return saved, pending, False

    def duplicate_submission(self, sender: str, generation: str, call: str):
        sender, generation, call = _id(sender, "owner_id"), _id(generation, "generation_id"), _id(call, "call_id")
        with self._read() as db:
            row = db.execute("SELECT * FROM peer_requests WHERE sender=? AND generation_id=? AND call_id=?", (sender, generation, call)).fetchone()
            if row is None:
                return None
            pending = db.execute("SELECT pending_id FROM peer_confirmations WHERE request_id=?", (row["request_id"],)).fetchone()
            return self._request(row), None if pending is None else pending[0], self._handle(db, row["recipient"])

    def decide_confirmation(
        self, owner_id: str, pending_id: str, approve: bool, *, now: datetime
    ) -> str:
        owner_id, pending_id, now = (
            _id(owner_id, "owner_id"),
            _id(pending_id, "pending_id"),
            utc_now(now),
        )
        if not isinstance(approve, bool):
            raise ValueError("invalid_approval")
        with self._write() as db:
            row = db.execute(
                "SELECT c.*,r.relationship_id,r.text,r.generation_id,r.status request_status FROM peer_confirmations c JOIN peer_requests r ON r.request_id=c.request_id WHERE c.pending_id=? AND c.affected_owner=?",
                (pending_id, owner_id),
            ).fetchone()
            if not row:
                return "not_found"
            if row["status"] != "pending":
                return "replayed"
            relation = db.execute(
                "SELECT * FROM peer_relationships WHERE relationship_id=?",
                (row["relationship_id"],),
            ).fetchone()
            valid = (
                relation is not None
                and relation["status"] == "active"
                and relation["revision"] == row["relationship_revision"]
                and hashlib.sha256(row["text"].encode()).hexdigest() == row["payload_hash"]
            )
            if valid:
                current_grants = self._grants(db, self._relationship(relation))
                current = {g.grantor_owner_id: g.revision for g in current_grants}
                valid = current == json.loads(row["grant_revisions"])
                if valid:
                    request_row = db.execute(
                        "SELECT * FROM peer_requests WHERE request_id=?",
                        (row["request_id"],),
                    ).fetchone()
                    request = self._request(request_row)
                    authorized = PeerRequest(
                        request.request_id,
                        request.relationship_id,
                        request.sender_owner_id,
                        request.recipient_owner_id,
                        request.thread_id,
                        request.source_generation_id,
                        request.source_call_id,
                        request.kind,
                        request.action,
                        request.text,
                        request.thread_sequence,
                        request.created_at,
                        "authorized",
                    )
                    from .peer_policy import PeerPolicy

                    valid = (
                        PeerPolicy()
                        .decide(
                            self._relationship(relation),
                            current_grants,
                            authorized,
                            now,
                        )
                        .outcome
                        == "allow"
                    )
            if _dt(row["expires_at"]) <= now:
                db.execute(
                    "UPDATE peer_confirmations SET status='expired',decided_at=? WHERE pending_id=?",
                    (_iso(now), pending_id),
                )
                db.execute(
                    "UPDATE peer_confirmation_notices SET status='suppressed',lease_token=NULL,lease_until=NULL WHERE pending_id=? AND status IN('pending','leased')",
                    (pending_id,),
                )
                db.execute(
                    "UPDATE peer_requests SET status='denied' WHERE request_id=?",
                    (row["request_id"],),
                )
                self._audit(db, owner_id, row["relationship_id"], "cancel", now)
                return "expired"
            status = "confirmed" if approve and valid else "cancelled"
            request_status = "authorized" if status == "confirmed" else "denied"
            db.execute(
                "UPDATE peer_confirmations SET status=?,decided_at=? WHERE pending_id=?",
                (status, _iso(now), pending_id),
            )
            db.execute(
                "UPDATE peer_confirmation_notices SET status='suppressed',lease_token=NULL,lease_until=NULL WHERE pending_id=? AND status IN('pending','leased')",
                (pending_id,),
            )
            db.execute(
                "UPDATE peer_requests SET status=? WHERE request_id=?",
                (request_status, row["request_id"]),
            )
            self._audit(
                db,
                owner_id,
                row["relationship_id"],
                "confirm" if status == "confirmed" else "cancel",
                now,
            )
            return status

    def resolve_pending_prefix(
        self, owner_id: str, prefix: str, *, now: datetime
    ) -> str | None:
        owner_id, prefix, now = (
            _id(owner_id, "owner_id"),
            _id(prefix, "pending_id"),
            utc_now(now),
        )
        if len(prefix) < 8 or not re.fullmatch(r"[0-9a-f]+", prefix):
            return None
        with self._read() as db:
            rows = db.execute(
                "SELECT pending_id FROM peer_confirmations WHERE affected_owner=? AND status='pending' AND expires_at>? AND pending_id LIKE ? LIMIT 2",
                (owner_id, _iso(now), prefix + "%"),
            ).fetchall()
            return rows[0][0] if len(rows) == 1 else None

    def confirmation_source(self, pending_id: str) -> tuple[str, str] | None:
        with self._read() as db:
            row = db.execute(
                "SELECT r.sender,r.generation_id FROM peer_confirmations c JOIN peer_requests r ON r.request_id=c.request_id WHERE c.pending_id=?",
                (_id(pending_id, "pending_id"),),
            ).fetchone()
            return None if row is None else (row["sender"], row["generation_id"])

    def cancel_superseded_confirmation(
        self, pending_id: str, *, now: datetime
    ) -> bool:
        pending_id, now = _id(pending_id, "pending_id"), utc_now(now)
        with self._write() as db:
            row = db.execute(
                "SELECT request_id,affected_owner FROM peer_confirmations "
                "WHERE pending_id=? AND status='pending'",
                (pending_id,),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                "UPDATE peer_confirmations SET status='cancelled',decided_at=? "
                "WHERE pending_id=? AND status='pending'",
                (_iso(now), pending_id),
            )
            db.execute(
                "UPDATE peer_confirmation_notices SET status='suppressed',"
                "lease_token=NULL,lease_until=NULL WHERE pending_id=? "
                "AND status IN('pending','leased','attempting')",
                (pending_id,),
            )
            db.execute(
                "UPDATE peer_requests SET status='denied' WHERE request_id=? "
                "AND status='confirmation_pending'",
                (row["request_id"],),
            )
            return True

    def deny_superseded_request(self, request_id: str, *, now: datetime) -> bool:
        request_id, now = _id(request_id, "request_id"), utc_now(now)
        with self._write() as db:
            row = db.execute("SELECT pending_id FROM peer_confirmations WHERE request_id=? AND status='pending'", (request_id,)).fetchone()
            if row is not None:
                db.execute("UPDATE peer_confirmations SET status='cancelled',decided_at=? WHERE pending_id=?", (_iso(now), row[0]))
                db.execute("UPDATE peer_confirmation_notices SET status='suppressed',lease_token=NULL,lease_until=NULL WHERE pending_id=? AND status IN('pending','leased','attempting')", (row[0],))
            return bool(db.execute("UPDATE peer_requests SET status='denied',lease_token=NULL,lease_until=NULL WHERE request_id=? AND status IN('pending','authorized','confirmation_pending','leased')", (request_id,)).rowcount)

    def claim_notice(
        self, *, now: datetime, lease_seconds: int
    ) -> tuple[PeerNotice, str] | None:
        now = utc_now(now)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("invalid_lease_seconds")
        with self._write() as db:
            db.execute(
                "UPDATE peer_confirmation_notices SET status='unknown',lease_token=NULL,lease_until=NULL "
                "WHERE status='attempting' AND lease_until<=?",
                (_iso(now),),
            )
            db.execute(
                "UPDATE peer_confirmation_notices SET status='pending',lease_token=NULL,lease_until=NULL WHERE status='leased' AND lease_until<=?",
                (_iso(now),),
            )
            rows = db.execute(
                "SELECT n.pending_id,c.affected_owner,c.status confirmation_status,c.expires_at,"
                "r.relationship_id,r.sender,r.kind,t.purpose,c.preview,p.status relationship_status "
                "FROM peer_confirmation_notices n "
                "JOIN peer_confirmations c ON c.pending_id=n.pending_id "
                "JOIN peer_requests r ON r.request_id=c.request_id "
                "JOIN peer_threads t ON t.thread_id=r.thread_id "
                "JOIN peer_relationships p ON p.relationship_id=r.relationship_id "
                "WHERE n.status='pending' AND n.available_at<=? "
                "ORDER BY c.created_at,c.pending_id",
                (_iso(now),),
            ).fetchall()
            for row in rows:
                if (
                    row["confirmation_status"] != "pending"
                    or _dt(row["expires_at"]) <= now
                    or row["relationship_status"] != "active"
                ):
                    db.execute(
                        "UPDATE peer_confirmation_notices SET status='suppressed',lease_token=NULL,lease_until=NULL WHERE pending_id=?",
                        (row["pending_id"],),
                    )
                    continue
                handle = self._handle(db, row["sender"])
                if handle == "unknown":
                    db.execute(
                        "UPDATE peer_confirmation_notices SET status='suppressed' WHERE pending_id=?",
                        (row["pending_id"],),
                    )
                    continue
                token = str(uuid.uuid4())
                if db.execute(
                    "UPDATE peer_confirmation_notices SET status='leased',lease_token=?,lease_until=? WHERE pending_id=? AND status='pending'",
                    (
                        token,
                        _iso(now + timedelta(seconds=lease_seconds)),
                        row["pending_id"],
                    ),
                ).rowcount:
                    short_id = row["pending_id"].replace("-", "")[:8]
                    category = (
                        "an availability request"
                        if row["kind"] == RequestKind.AVAILABILITY.value
                        else "a sensitive request"
                    )
                    text = (
                        f"{handle}'s tomo requests one-time permission for {category}: "
                        f"{row['purpose'][:80]} - {row['preview']}. send confirm peer request {short_id} or "
                        f"cancel peer request {short_id}."
                    )
                    return PeerNotice(row["pending_id"], token, handle, text), row[
                        "affected_owner"
                    ]
            return None

    def complete_notice(
        self, pending_id: str, token: str, outcome: NoticeStatus | str, *, now: datetime
    ) -> bool:
        pending_id = _id(pending_id, "pending_id")
        token = _id(token, "lease_token")
        now = utc_now(now)
        outcome = NoticeStatus(outcome)
        if outcome not in (
            NoticeStatus.SENT,
            NoticeStatus.UNKNOWN,
            NoticeStatus.SUPPRESSED,
            NoticeStatus.PENDING,
        ):
            raise ValueError("invalid_notice_outcome")
        with self._write() as db:
            available_at = (
                now + timedelta(seconds=60) if outcome == NoticeStatus.PENDING else now
            )
            expected_status = (
                "leased" if outcome == NoticeStatus.PENDING else "attempting"
            )
            return bool(
                db.execute(
                    "UPDATE peer_confirmation_notices "
                    "SET status=?,lease_token=NULL,lease_until=NULL,available_at=? "
                    "WHERE pending_id=? AND status=? AND lease_token=? AND lease_until>?",
                    (
                        outcome.value,
                        _iso(available_at),
                        pending_id,
                        expected_status,
                        token,
                        _iso(now),
                    ),
                ).rowcount
            )

    def begin_notice_attempt(
        self, pending_id: str, token: str, *, now: datetime
    ) -> bool:
        pending_id = _id(pending_id, "pending_id")
        token = _id(token, "lease_token")
        with self._write() as db:
            return bool(
                db.execute(
                    "UPDATE peer_confirmation_notices SET status='attempting' "
                    "WHERE pending_id=? AND status='leased' AND lease_token=? AND lease_until>?",
                    (pending_id, token, _iso(utc_now(now))),
                ).rowcount
            )

    def notice_recipient(
        self, pending_id: str, token: str, *, now: datetime
    ) -> str | None:
        pending_id = _id(pending_id, "pending_id")
        token = _id(token, "lease_token")
        with self._read() as db:
            row = db.execute(
                "SELECT c.affected_owner FROM peer_confirmation_notices n JOIN peer_confirmations c ON c.pending_id=n.pending_id WHERE n.pending_id=? AND n.status='leased' AND n.lease_token=? AND n.lease_until>?",
                (pending_id, token, _iso(utc_now(now))),
            ).fetchone()
            return None if row is None else row[0]

    def claim_candidates(self, *, now: datetime, lease_seconds: int):
        now = utc_now(now)
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("invalid_lease_seconds")
        lease_seconds = min(lease_seconds, 15)
        with self._write() as db:
            exhausted = db.execute(
                "SELECT request_id,recipient,relationship_id FROM peer_requests "
                "WHERE (status IN('pending','authorized','leased') AND execution_deadline IS NOT NULL AND execution_deadline<=?) "
                "OR (status='leased' AND lease_until<=? AND attempt_count>=3)",
                (_iso(now), _iso(now)),
            ).fetchall()
            for terminal in exhausted:
                if not db.execute(
                    "UPDATE peer_requests SET status='failed',lease_token=NULL,lease_until=NULL,error_code=COALESCE(error_code,'peer_timeout') "
                    "WHERE request_id=? AND status IN('pending','authorized','leased')",
                    (terminal["request_id"],),
                ).rowcount:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO peer_responses VALUES(?,?,?,?,?)",
                    (
                        terminal["request_id"],
                        terminal["recipient"],
                        json.dumps(["unable to answer right now"]),
                        "failed",
                        _iso(now),
                    ),
                )
                self._audit(db, terminal["recipient"], terminal["relationship_id"], "fail", now)
            db.execute(
                """
                UPDATE peer_requests
                SET status = CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM peer_confirmations
                        WHERE peer_confirmations.request_id = peer_requests.request_id
                          AND peer_confirmations.status = 'confirmed'
                    ) THEN 'authorized'
                    ELSE 'pending'
                END,
                lease_token = NULL,
                lease_until = NULL
                WHERE status = 'leased' AND lease_until <= ? AND attempt_count < 3
                """,
                (_iso(now),),
            )
            rows = db.execute(
                "SELECT * FROM peer_requests r WHERE status IN('pending','authorized') AND attempt_count<3 AND available_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM peer_requests earlier WHERE earlier.thread_id=r.thread_id "
                "AND earlier.sequence<r.sequence AND earlier.status NOT IN('completed','failed','denied')) "
                "ORDER BY created_at,sequence,request_id",
                (_iso(now),),
            ).fetchall()
            for row in rows:
                relation_row = db.execute(
                    "SELECT * FROM peer_relationships WHERE relationship_id=?",
                    (row["relationship_id"],),
                ).fetchone()
                thread = db.execute(
                    "SELECT * FROM peer_threads WHERE thread_id=?", (row["thread_id"],)
                ).fetchone()
                if (
                    not relation_row
                    or relation_row["status"] != "active"
                    or not thread
                    or _dt(thread["expires_at"]) <= now
                ):
                    db.execute(
                        "UPDATE peer_requests SET status='denied' WHERE request_id=?",
                        (row["request_id"],),
                    )
                    continue
                token = str(uuid.uuid4())
                if db.execute(
                    "UPDATE peer_requests SET status='leased',lease_token=?,lease_until=?,execution_deadline=COALESCE(execution_deadline, CASE WHEN ? < ? THEN ? ELSE ? END),attempt_count=attempt_count+1 WHERE request_id=? AND status=?",
                    (
                        token,
                        _iso(now + timedelta(seconds=lease_seconds)),
                        _iso(now + timedelta(seconds=60)), _iso(_dt(thread["expires_at"])), _iso(now + timedelta(seconds=60)), _iso(_dt(thread["expires_at"])),
                        row["request_id"],
                        row["status"],
                    ),
                ).rowcount:
                    relation = self._relationship(relation_row)
                    return (
                        self._request(row),
                        token,
                        relation,
                        self._grants(db, relation),
                        int(row["attempt_count"]) + 1,
                    )
            return None

    def deny_claim(self, request_id: str, token: str, *, now: datetime) -> bool:
        request_id, token, now = (
            _id(request_id, "request_id"),
            _id(token, "lease_token"),
            utc_now(now),
        )
        with self._write() as db:
            return bool(
                db.execute(
                    "UPDATE peer_requests SET status='denied',lease_token=NULL,lease_until=NULL WHERE request_id=? AND status='leased' AND lease_token=?",
                    (request_id, token),
                ).rowcount
            )

    def defer(
        self,
        request_id: str,
        token: str,
        *,
        error_code: str,
        now: datetime,
    ) -> bool:
        request_id, token, now = (
            _id(request_id, "request_id"),
            _id(token, "lease_token"),
            utc_now(now),
        )
        error_code = _safe_error_code(error_code)
        with self._write() as db:
            row = db.execute(
                "SELECT status,attempt_count,recipient,relationship_id FROM peer_requests WHERE request_id=? AND status='leased' AND lease_token=? AND lease_until>?",
                (request_id, token, _iso(now)),
            ).fetchone()
            if row is None:
                return False
            if row["attempt_count"] >= 3:
                db.execute(
                    "UPDATE peer_requests SET status='failed',lease_token=NULL,lease_until=NULL,error_code=? WHERE request_id=? AND status='leased' AND lease_token=?",
                    (error_code, request_id, token),
                )
                db.execute(
                    "INSERT INTO peer_responses VALUES(?,?,?,?,?)",
                    (request_id, row["recipient"], json.dumps(["unable to answer right now"]), "failed", _iso(now)),
                )
                self._audit(db, row["recipient"], row["relationship_id"], "fail", now)
                return False
            delay = min(2 ** row["attempt_count"], 60)
            return bool(
                db.execute(
                    "UPDATE peer_requests SET status=CASE WHEN EXISTS(SELECT 1 FROM peer_confirmations WHERE request_id=? AND status='confirmed') THEN 'authorized' ELSE 'pending' END,lease_token=NULL,lease_until=NULL,available_at=?,error_code=? WHERE request_id=? AND status='leased' AND lease_token=?",
                    (
                        request_id,
                        _iso(now + timedelta(seconds=delay)),
                        error_code,
                        request_id,
                        token,
                    ),
                ).rowcount
            )

    def heartbeat(
        self, request_id: str, token: str, *, now: datetime, lease_seconds: int,
        relationship_revision: int | None = None, grant_revisions: dict[str, int] | None = None,
    ) -> bool:
        request_id, token, now = (
            _id(request_id, "request_id"),
            _id(token, "lease_token"),
            utc_now(now),
        )
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("invalid_lease_seconds")
        lease_seconds = min(lease_seconds, 15)
        with self._write() as db:
            if not self._claim_fence(
                db, request_id, token, now, relationship_revision, grant_revisions
            ):
                return False
            return bool(
                db.execute(
                    "UPDATE peer_requests SET lease_until=? WHERE request_id=? AND status='leased' AND lease_token=? AND lease_until>? AND execution_deadline>? AND EXISTS(SELECT 1 FROM peer_threads t JOIN peer_relationships p ON p.relationship_id=t.relationship_id WHERE t.thread_id=peer_requests.thread_id AND t.status='active' AND t.expires_at>? AND p.status='active')",
                    (
                        _iso(now + timedelta(seconds=lease_seconds)),
                        request_id,
                        token,
                        _iso(now),
                        _iso(now), _iso(now),
                    ),
                ).rowcount
            )

    def claim_is_active(
        self, request_id: str, token: str, *, now: datetime,
        relationship_revision: int | None = None, grant_revisions: dict[str, int] | None = None,
    ) -> bool:
        with self._read() as db:
            return self._claim_fence(
                db, request_id, token, utc_now(now), relationship_revision, grant_revisions
            )

    def _claim_fence(
        self, db, request_id: str, token: str, now: datetime,
        relationship_revision: int | None, grant_revisions: dict[str, int] | None,
    ) -> bool:
        row = db.execute(
            "SELECT * FROM peer_requests WHERE request_id=? AND status='leased' AND lease_token=? "
            "AND lease_until>? AND execution_deadline>? AND EXISTS(SELECT 1 FROM peer_threads t "
            "JOIN peer_relationships p ON p.relationship_id=t.relationship_id "
            "WHERE t.thread_id=peer_requests.thread_id AND t.status='active' AND t.expires_at>? AND p.status='active')",
            (_id(request_id, "request_id"), _id(token, "lease_token"), _iso(now), _iso(now), _iso(now)),
        ).fetchone()
        if row is None or relationship_revision is None or grant_revisions is None:
            return row is not None
        relation_row = db.execute(
            "SELECT * FROM peer_relationships WHERE relationship_id=?", (row["relationship_id"],)
        ).fetchone()
        relation = self._relationship(relation_row)
        grants = self._grants(db, relation)
        if relation.revision != relationship_revision:
            return False
        if {grant.grantor_owner_id: grant.revision for grant in grants} != grant_revisions:
            return False
        from .peer_policy import PeerPolicy

        return PeerPolicy().decide(relation, grants, self._request(row), now).outcome == "allow"

    def execution_deadline(self, request_id: str) -> datetime | None:
        with self._read() as db:
            row = db.execute("SELECT execution_deadline FROM peer_requests WHERE request_id=?", (_id(request_id, "request_id"),)).fetchone()
            return None if row is None or row[0] is None else _dt(row[0])

    def request_execution_context(self, request_id: str) -> tuple[str, datetime] | None:
        with self._read() as db:
            row = db.execute(
                "SELECT t.purpose,r.execution_deadline FROM peer_requests r "
                "JOIN peer_threads t ON t.thread_id=r.thread_id WHERE r.request_id=?",
                (_id(request_id, "request_id"),),
            ).fetchone()
            if row is None or row["execution_deadline"] is None:
                return None
            return row["purpose"], _dt(row["execution_deadline"])

    def request_for_output_check(self, request_id: str) -> PeerRequest | None:
        with self._read() as db:
            row = db.execute("SELECT * FROM peer_requests WHERE request_id=?", (_id(request_id, "request_id"),)).fetchone()
            return None if row is None else self._request(row)

    def prior_completed_exchanges(self, request_id: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        with self._read() as db:
            row = db.execute("SELECT thread_id,sequence FROM peer_requests WHERE request_id=?", (_id(request_id, "request_id"),)).fetchone()
            if row is None:
                return ()
            rows = db.execute(
                "SELECT r.text,p.frames FROM peer_requests r JOIN peer_responses p ON p.request_id=r.request_id "
                "WHERE r.thread_id=? AND r.sequence<? AND r.status='completed' AND p.status='completed' "
                "ORDER BY r.sequence DESC LIMIT 3", (row["thread_id"], row["sequence"])
            ).fetchall()
            exchanges = [(item["text"], tuple(json.loads(item["frames"]))) for item in reversed(rows)]
            total, bounded = 0, []
            for question, answers in exchanges:
                remaining = 4000 - total
                if remaining <= 0:
                    break
                question = question[:remaining]
                remaining -= len(question)
                bounded_answers = []
                for answer in answers:
                    if remaining <= 0:
                        break
                    answer = answer[:remaining]
                    bounded_answers.append(answer)
                    remaining -= len(answer)
                answers = tuple(bounded_answers)
                total = 4000 - remaining
                bounded.append((question, answers))
            return tuple(bounded)

    def finish(
        self,
        owner_id: str,
        request_id: str,
        token: str,
        frames: tuple[str, ...],
        relationship_revision: int,
        grant_revisions: dict[str, int],
        *,
        failed: bool,
        error_code: str | None = None,
        now: datetime,
    ) -> bool:
        owner_id, request_id, token, now = (
            _id(owner_id, "owner_id"),
            _id(request_id, "request_id"),
            _id(token, "lease_token"),
            utc_now(now),
        )
        PeerResponse(
            request_id, owner_id, frames, "failed" if failed else "completed", now
        )
        error_code = _safe_error_code(error_code) if failed else None
        if (
            isinstance(relationship_revision, bool)
            or not isinstance(relationship_revision, int)
            or not isinstance(grant_revisions, dict)
        ):
            raise ValueError("invalid_revision")
        with self._write() as db:
            row = db.execute(
                "SELECT r.*,p.owner_a,p.owner_b,p.invited_by,p.status relation_status,p.created_at relation_created_at,p.revision FROM peer_requests r JOIN peer_relationships p ON p.relationship_id=r.relationship_id JOIN peer_threads t ON t.thread_id=r.thread_id WHERE r.request_id=? AND r.recipient=? AND r.status='leased' AND r.lease_token=? AND r.lease_until>? AND r.execution_deadline>? AND t.status='active' AND t.expires_at>? AND p.status='active'",
                (request_id, owner_id, token, _iso(now), _iso(now), _iso(now)),
            ).fetchone()
            if not row or row["revision"] != relationship_revision:
                return False
            relation = Relationship(
                row["relationship_id"],
                row["owner_a"],
                row["owner_b"],
                row["invited_by"],
                row["relation_status"],
                _dt(row["relation_created_at"]),
                row["revision"],
            )
            grants = self._grants(db, relation)
            from .peer_policy import PeerPolicy

            if {
                g.grantor_owner_id: g.revision for g in grants
            } != grant_revisions or PeerPolicy().decide(
                relation, grants, self._request(row), now
            ).outcome != "allow":
                return False
            status = "failed" if failed else "completed"
            if not db.execute(
                "UPDATE peer_requests SET status=?,lease_token=NULL,lease_until=NULL,error_code=? WHERE request_id=? AND status='leased' AND lease_token=?",
                (status, error_code, request_id, token),
            ).rowcount:
                return False
            db.execute(
                "INSERT INTO peer_responses VALUES(?,?,?,?,?)",
                (
                    request_id,
                    owner_id,
                    json.dumps(list(frames), ensure_ascii=True, separators=(",", ":")),
                    status,
                    _iso(now),
                ),
            )
            self._audit(
                db,
                owner_id,
                row["relationship_id"],
                "fail" if failed else "complete",
                now,
            )
            return True

    def inspect_request(self, owner_id: str, request_id: str):
        owner_id, request_id = _id(owner_id, "owner_id"), _id(request_id, "request_id")
        with self._read() as db:
            if self._owner_deleted(db, owner_id):
                return None
            row = db.execute(
                "SELECT r.* FROM peer_requests r WHERE r.request_id=? AND ? IN(r.sender,r.recipient) "
                "AND NOT EXISTS(SELECT 1 FROM peer_relationship_history_watermarks w "
                "WHERE w.owner_id=? AND w.relationship_id=r.relationship_id "
                "AND r.created_at<=w.purged_before)",
                (request_id, owner_id, owner_id),
            ).fetchone()
            if not row:
                return None
            response = db.execute(
                "SELECT * FROM peer_responses WHERE request_id=?", (request_id,)
            ).fetchone()
            confirmation = db.execute(
                "SELECT expires_at FROM peer_confirmations "
                "WHERE request_id=? AND status='pending'",
                (request_id,),
            ).fetchone()
            return (
                self._request(row),
                None
                if not response
                else PeerResponse(
                    request_id,
                    response["responder"],
                    tuple(json.loads(response["frames"])),
                    response["status"],
                    _dt(response["created_at"]),
                ),
                self._handle(
                    db, row["recipient"] if row["sender"] == owner_id else row["sender"]
                ),
                None if confirmation is None else _dt(confirmation["expires_at"]),
                row["error_code"] if "error_code" in row.keys() else None,
            )

    def latest_thread_request(self, owner_id: str, thread_id: str):
        owner_id, thread_id = _id(owner_id, "owner_id"), _id(thread_id, "thread_id")
        with self._read() as db:
            row = db.execute(
                "SELECT request_id FROM peer_requests WHERE thread_id=? AND ? IN(sender,recipient) "
                "ORDER BY sequence DESC LIMIT 1", (thread_id, owner_id)
            ).fetchone()
            return None if row is None else self.inspect_request(owner_id, row["request_id"])

    def list_relationships(self, owner_id: str) -> tuple[PeerRelationshipSummary, ...]:
        owner_id = _id(owner_id, "owner_id")
        with self._read() as db:
            if self._owner_deleted(db, owner_id):
                return ()
            rows = db.execute(
                "SELECT * FROM peer_relationships WHERE ? IN(owner_a,owner_b)",
                (owner_id,),
            ).fetchall()
            out = []
            for row in rows:
                relation = self._relationship(row)
                peer = relation.other_owner(owner_id)
                grants = {g.grantor_owner_id: g for g in self._grants(db, relation)}
                own = grants.get(owner_id)
                other = grants.get(peer)
                safe_own = (
                    None
                    if own is None
                    else PeerGrantSummary(
                        own.communicate,
                        own.auto_reply,
                        own.share_availability,
                        own.revision,
                        own.expires_at,
                    )
                )
                out.append(
                    PeerRelationshipSummary(
                        relation.relationship_id,
                        self._handle(db, peer),
                        relation.status,
                        safe_own,
                        None
                        if other is None
                        else PeerGrantSummary(
                            other.communicate,
                            other.auto_reply,
                            other.share_availability,
                            other.revision,
                            other.expires_at,
                        ),
                        bool(other and other.communicate),
                        bool(other and other.auto_reply),
                        bool(other and other.share_availability),
                        relation.revision,
                        None if own is None else own.expires_at,
                        relation.status == RelationshipStatus.PENDING
                        and relation.invited_by_owner_id != owner_id,
                    )
                )
            live_ids = {item.relationship_id for item in out}
            archived = db.execute(
                "SELECT record_json FROM peer_archives WHERE owner_id=? AND table_name='peer_relationship' ORDER BY record_json",
                (owner_id,),
            ).fetchall()
            for row in archived:
                record = json.loads(row["record_json"])["records"]
                relationship_id = record["relationship_id"]
                if relationship_id in live_ids:
                    continue
                out.append(
                    PeerRelationshipSummary(
                        relationship_id,
                        record["peer_handle"],
                        RelationshipStatus.REVOKED,
                        None,
                        None,
                        False,
                        False,
                        False,
                        int(record.get("relationship_revision", 0)),
                        None,
                        False,
                    )
                )
            return tuple(out)

    def handle_for_owner(self, owner_id: str) -> str | None:
        owner_id = _id(owner_id, "owner_id")
        with self._read() as db:
            if self._owner_deleted(db, owner_id):
                return None
            row = db.execute(
                "SELECT handle FROM peer_handles WHERE owner_id=?", (owner_id,)
            ).fetchone()
            return None if row is None else row[0]

    def export_owner_records(self, *, owner_id: str):
        owner_id = _id(owner_id, "owner_id")
        with self._read() as db:
            if self._owner_deleted(db, owner_id):
                return
            archived = db.execute(
                "SELECT record_json FROM peer_archives WHERE owner_id=? ORDER BY table_name,record_json",
                (owner_id,),
            ).fetchall()
            archived_tables = set()
            archived_relationship_ids = set()
            for row in archived:
                record = json.loads(row["record_json"])
                archived_tables.add(record["table"])
                if record["table"] in {"peer_relationship", "peer_request"}:
                    archived_relationship_ids.add(record["records"].get("relationship_id"))
                yield record
            if "peer_profile" not in archived_tables:
                handle = self._handle(db, owner_id)
                yield {
                    "table": "peer_profile",
                    "owner_id": owner_id,
                    "records": {
                        "handle": None if handle == "unknown" else handle,
                        "history_purged_before": None,
                    },
                }
            relationships = self.list_relationships(owner_id)
            for relationship in relationships:
                if relationship.relationship_id in archived_relationship_ids:
                    continue
                yield {
                    "table": "peer_relationship",
                    "owner_id": owner_id,
                    "records": {
                        "relationship_id": relationship.relationship_id,
                        "peer_handle": relationship.peer_handle,
                        "status": relationship.status.value,
                        "can_accept": relationship.can_accept,
                        "grant": None
                        if relationship.grant is None
                        else {
                            "communicate": relationship.grant.communicate,
                            "auto_reply": relationship.grant.auto_reply,
                            "share_availability": relationship.grant.share_availability,
                            "revision": relationship.grant.revision,
                            "expires_at": None
                            if relationship.grant.expires_at is None
                            else _iso(relationship.grant.expires_at),
                        },
                        "peer_grant": None
                        if relationship.peer_grant is None
                        else {
                            "communicate": relationship.peer_grant.communicate,
                            "auto_reply": relationship.peer_grant.auto_reply,
                            "share_availability": relationship.peer_grant.share_availability,
                            "revision": relationship.peer_grant.revision,
                            "expires_at": None
                            if relationship.peer_grant.expires_at is None
                            else _iso(relationship.peer_grant.expires_at),
                        },
                        "relationship_revision": relationship.relationship_revision,
                        "expires_at": None
                        if relationship.expires_at is None
                        else _iso(relationship.expires_at),
                    },
                }
            rows = db.execute(
                "SELECT r.*, p.frames, p.status response_status, p.created_at response_created_at "
                "FROM peer_requests r "
                "JOIN peer_relationships rel ON rel.relationship_id=r.relationship_id "
                "LEFT JOIN peer_responses p ON p.request_id=r.request_id "
                "WHERE ? IN(rel.owner_a, rel.owner_b) AND NOT EXISTS(SELECT 1 FROM peer_relationship_history_watermarks w WHERE w.owner_id=? AND w.relationship_id=r.relationship_id AND r.created_at<=w.purged_before) "
                "ORDER BY r.created_at DESC, r.request_id DESC LIMIT 1000",
                (owner_id, owner_id),
            ).fetchall()
            for row in rows:
                peer_owner = (
                    row["recipient"] if row["sender"] == owner_id else row["sender"]
                )
                disclosed = row["sender"] == owner_id or row["status"] not in {
                    "confirmation_pending",
                    "denied",
                }
                records = {
                    "request_id": row["request_id"],
                    "relationship_id": row["relationship_id"],
                    "peer_handle": self._handle(db, peer_owner),
                    "direction": "outgoing"
                    if row["sender"] == owner_id
                    else "incoming",
                    "kind": row["kind"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "content": row["text"] if disclosed else None,
                }
                if row["response_status"] is not None:
                    records["response"] = {
                        "status": row["response_status"],
                        "frames": json.loads(row["frames"]),
                        "created_at": row["response_created_at"],
                    }
                yield {
                    "table": "peer_request",
                    "owner_id": owner_id,
                    "records": records,
                }

    def import_owner_records(self, owner_id: str, records) -> None:
        owner_id = _id(owner_id, "owner_id")
        values = list(records)
        for record in values:
            if (
                not isinstance(record, dict)
                or record.get("owner_id") != owner_id
                or record.get("table") not in {"peer_profile", "peer_relationship", "peer_request"}
                or not isinstance(record.get("records"), dict)
            ):
                raise ValueError("cross-owner peer records")
            payload = record["records"]
            if record["table"] == "peer_relationship":
                _id(payload.get("relationship_id"), "relationship_id")
                Handle(payload.get("peer_handle"))
            elif record["table"] == "peer_request":
                _id(payload.get("relationship_id"), "relationship_id")
        if len(values) > 1100:
            raise ValueError("too_many_peer_records")
        encoded = [json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for record in values]
        if any(len(record) > 8192 for record in encoded):
            raise ValueError("peer_record_too_large")
        with self._write() as db:
            db.execute("DELETE FROM peer_archives WHERE owner_id=?", (owner_id,))
            db.executemany(
                "INSERT OR IGNORE INTO peer_archives(owner_id,table_name,relationship_id,record_json) VALUES(?,?,?,?)",
                [
                    (
                        owner_id,
                        record["table"],
                        record["records"].get("relationship_id"),
                        value,
                    )
                    for record, value in zip(values, encoded)
                ],
            )

    def purge_history(self, owner_id: str, relationship_id: str, *, now: datetime) -> None:
        owner_id, relationship_id, now = _id(owner_id, "owner_id"), _id(relationship_id, "relationship_id"), utc_now(now)
        with self._write() as db:
            live = db.execute(
                "SELECT 1 FROM peer_relationships WHERE relationship_id=? AND ? IN(owner_a,owner_b)",
                (relationship_id, owner_id),
            ).fetchone()
            archived = db.execute(
                "SELECT 1 FROM peer_archives WHERE owner_id=? AND relationship_id=?",
                (owner_id, relationship_id),
            ).fetchone()
            if not live and not archived:
                raise ValueError("not_found")
            db.execute(
                "DELETE FROM peer_archives WHERE owner_id=? AND relationship_id=?",
                (owner_id, relationship_id),
            )
            if not live:
                return
            db.execute(
                "INSERT INTO peer_relationship_history_watermarks(owner_id,relationship_id,purged_before) VALUES(?,?,?) ON CONFLICT(owner_id,relationship_id) DO UPDATE SET purged_before=MAX(peer_relationship_history_watermarks.purged_before,excluded.purged_before)",
                (owner_id, relationship_id, _iso(now)),
            )
            cutoffs = db.execute(
                "SELECT purged_before FROM peer_relationship_history_watermarks WHERE relationship_id=?",
                (relationship_id,),
            ).fetchall()
            if len(cutoffs) == 2:
                # Shared records can disappear only after both relationship owners hid them.
                db.execute(
                    "DELETE FROM peer_requests WHERE relationship_id=? AND created_at<=?",
                    (relationship_id, min(row[0] for row in cutoffs)),
                )

    def relationship_history(
        self, owner_id: str, relationship_id: str
    ) -> tuple[PeerHistoryEntry, ...] | None:
        owner_id, relationship_id = (
            _id(owner_id, "owner_id"),
            _id(relationship_id, "relationship_id"),
        )
        with self._read() as db:
            if self._owner_deleted(db, owner_id):
                return None
            relation = db.execute(
                "SELECT 1 FROM peer_relationships WHERE relationship_id=? AND ? IN(owner_a,owner_b)",
                (relationship_id, owner_id),
            ).fetchone()
            if relation is None:
                return None
            watermark = db.execute(
                "SELECT purged_before FROM peer_relationship_history_watermarks WHERE owner_id=? AND relationship_id=?",
                (owner_id, relationship_id),
            ).fetchone()
            rows = db.execute(
                "SELECT r.request_id,r.thread_id,r.sender,r.kind,r.status,r.created_at,p.status response_status FROM peer_requests r LEFT JOIN peer_responses p ON p.request_id=r.request_id WHERE r.relationship_id=? AND r.created_at>? ORDER BY r.created_at DESC,r.request_id DESC LIMIT 20",
                (relationship_id, "" if watermark is None else watermark[0]),
            ).fetchall()
            return tuple(
                PeerHistoryEntry(
                    row["request_id"],
                    row["thread_id"],
                    "outgoing" if row["sender"] == owner_id else "incoming",
                    RequestKind(row["kind"]),
                    RequestStatus(row["status"]),
                    _dt(row["created_at"]),
                    None
                    if row["response_status"] is None
                    else ResponseStatus(row["response_status"]),
                )
                for row in rows
            )

    def audit_events(self, owner_id: str) -> tuple[PeerAuditEvent, ...]:
        owner_id = _id(owner_id, "owner_id")
        with self._read() as db:
            if self._owner_deleted(db, owner_id):
                return ()
            return tuple(
                PeerAuditEvent(
                    row["relationship_id"], row["action_code"], _dt(row["created_at"])
                )
                for row in db.execute(
                    "SELECT relationship_id,action_code,created_at FROM peer_audit_events WHERE owner_id=? ORDER BY id",
                    (owner_id,),
                )
            )

    def prune(self, now: datetime) -> None:
        now = utc_now(now)
        with self._write() as db:
            cutoff = _iso(now - RETENTION)
            db.execute("DELETE FROM peer_requests WHERE created_at<?", (cutoff,))
            db.execute("DELETE FROM peer_confirmations WHERE expires_at<?", (cutoff,))
            db.execute(
                "DELETE FROM peer_threads WHERE expires_at<? AND NOT EXISTS(SELECT 1 FROM peer_requests WHERE peer_requests.thread_id=peer_threads.thread_id)",
                (cutoff,),
            )
            db.execute("DELETE FROM peer_audit_events WHERE created_at<?", (cutoff,))

    def _grants(self, db, relation):
        return tuple(
            DirectionalGrant(
                r["relationship_id"],
                r["grantor"],
                relation.other_owner(r["grantor"]),
                bool(r["communicate"]),
                bool(r["auto_reply"]),
                bool(r["share_availability"]),
                r["revision"],
                None if r["expires_at"] is None else _dt(r["expires_at"]),
                _dt(r["updated_at"]),
            )
            for r in db.execute(
                "SELECT * FROM peer_grants WHERE relationship_id=?",
                (relation.relationship_id,),
            )
        )

    def _handle(self, db, owner):
        row = db.execute(
            "SELECT handle FROM peer_handles WHERE owner_id=?", (owner,)
        ).fetchone()
        return row[0] if row else "unknown"

    @staticmethod
    def _owner_deleted(db, owner: str) -> bool:
        return db.execute(
            "SELECT 1 FROM peer_deleted_owners WHERE owner_id=?", (owner,)
        ).fetchone() is not None

    def _audit(self, db, owner, relationship, code, now):
        if code not in AUDIT_CODES:
            raise ValueError("invalid_audit_code")
        db.execute(
            "INSERT INTO peer_audit_events(owner_id,relationship_id,action_code,created_at) VALUES(?,?,?,?)",
            (owner, relationship, code, _iso(now)),
        )

    @staticmethod
    def _relationship(row):
        return Relationship(
            row["relationship_id"],
            row["owner_a"],
            row["owner_b"],
            row["invited_by"],
            row["status"],
            _dt(row["created_at"]),
            row["revision"],
        )

    @staticmethod
    def _request(row):
        return PeerRequest(
            row["request_id"],
            row["relationship_id"],
            row["sender"],
            row["recipient"],
            row["thread_id"],
            row["generation_id"],
            row["call_id"],
            row["kind"],
            row["action"],
            row["text"],
            row["sequence"],
            _dt(row["created_at"]),
            row["status"],
            row["disclosure_scope"] if "disclosure_scope" in row.keys() else "none",
        )

    def _connect(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _read(self):
        return _Transaction(self._connect())

    def _write(self):
        db = self._connect()
        db.execute("BEGIN IMMEDIATE")
        return _Transaction(db)

    def _init(self):
        db = self._connect()
        try:
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='peer_schema'"
            ).fetchone()
            if exists:
                row = db.execute("SELECT version FROM peer_schema").fetchone()
                requests_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='peer_requests'").fetchone()
                if row is not None and row[0] != SCHEMA_VERSION:
                    raise ValueError("unsupported_peer_schema")
                columns = set() if not requests_exists else {
                    row[1]
                    for row in db.execute("PRAGMA table_info(peer_requests)").fetchall()
                }
                if requests_exists and "execution_deadline" not in columns:
                    db.execute("ALTER TABLE peer_requests ADD COLUMN execution_deadline TEXT")
                if requests_exists and "disclosure_scope" not in columns:
                    db.execute("ALTER TABLE peer_requests ADD COLUMN disclosure_scope TEXT NOT NULL DEFAULT 'none'")
                if requests_exists and "error_code" not in columns:
                    db.execute("ALTER TABLE peer_requests ADD COLUMN error_code TEXT")
            db.executescript("""
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS peer_schema(version INTEGER NOT NULL CHECK(version=1));
CREATE TABLE IF NOT EXISTS peer_deleted_owners(owner_id TEXT PRIMARY KEY,deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_handles(handle TEXT PRIMARY KEY,owner_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_relationships(relationship_id TEXT PRIMARY KEY,owner_a TEXT NOT NULL,owner_b TEXT NOT NULL,invited_by TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,revision INTEGER NOT NULL,UNIQUE(owner_a,owner_b));
CREATE TABLE IF NOT EXISTS peer_grants(relationship_id TEXT NOT NULL REFERENCES peer_relationships ON DELETE CASCADE,grantor TEXT NOT NULL,communicate INTEGER NOT NULL,auto_reply INTEGER NOT NULL,share_availability INTEGER NOT NULL,revision INTEGER NOT NULL,expires_at TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(relationship_id,grantor));
CREATE TABLE IF NOT EXISTS peer_threads(thread_id TEXT PRIMARY KEY,relationship_id TEXT NOT NULL REFERENCES peer_relationships ON DELETE CASCADE,sender TEXT NOT NULL,recipient TEXT NOT NULL,purpose TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,request_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS peer_requests(request_id TEXT PRIMARY KEY,relationship_id TEXT NOT NULL REFERENCES peer_relationships ON DELETE CASCADE,sender TEXT NOT NULL,recipient TEXT NOT NULL,thread_id TEXT NOT NULL REFERENCES peer_threads ON DELETE CASCADE,generation_id TEXT NOT NULL,call_id TEXT NOT NULL,kind TEXT NOT NULL,action TEXT NOT NULL,text TEXT NOT NULL,sequence INTEGER NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,disclosure_scope TEXT NOT NULL DEFAULT 'none',lease_token TEXT,lease_until TEXT,execution_deadline TEXT,error_code TEXT,attempt_count INTEGER NOT NULL,available_at TEXT NOT NULL,UNIQUE(sender,generation_id,call_id));
CREATE TABLE IF NOT EXISTS peer_responses(request_id TEXT PRIMARY KEY REFERENCES peer_requests ON DELETE CASCADE,responder TEXT NOT NULL,frames TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_confirmations(pending_id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE REFERENCES peer_requests ON DELETE CASCADE,affected_owner TEXT NOT NULL,action_kind TEXT NOT NULL,payload_hash TEXT NOT NULL,preview TEXT NOT NULL,status TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL,decided_at TEXT,relationship_revision INTEGER NOT NULL,grant_revisions TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_confirmation_notices(pending_id TEXT PRIMARY KEY REFERENCES peer_confirmations ON DELETE CASCADE,status TEXT NOT NULL,lease_token TEXT,lease_until TEXT,available_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_audit_events(id INTEGER PRIMARY KEY,owner_id TEXT NOT NULL,relationship_id TEXT NOT NULL,action_code TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_history_watermarks(owner_id TEXT PRIMARY KEY,purged_before TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS peer_relationship_history_watermarks(owner_id TEXT NOT NULL,relationship_id TEXT NOT NULL REFERENCES peer_relationships ON DELETE CASCADE,purged_before TEXT NOT NULL,PRIMARY KEY(owner_id,relationship_id));
CREATE TABLE IF NOT EXISTS peer_archives(owner_id TEXT NOT NULL,table_name TEXT NOT NULL,relationship_id TEXT,record_json TEXT NOT NULL,PRIMARY KEY(owner_id,table_name,record_json));
CREATE INDEX IF NOT EXISTS peer_requests_recent ON peer_requests(relationship_id,created_at);
INSERT OR IGNORE INTO peer_schema VALUES(1);
COMMIT;
""")
            db.commit()
        finally:
            db.close()


def _safe_error_code(value: object) -> str:
    if not isinstance(value, str):
        return "peer_execution_failed"
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:64]
    return normalized or "peer_execution_failed"


class _Transaction:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.db.commit()
        else:
            self.db.rollback()
        self.db.close()
