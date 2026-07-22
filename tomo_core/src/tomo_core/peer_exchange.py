from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .peer_models import (
    PeerAuditEvent,
    PeerGrantSummary,
    PeerHistoryEntry,
    PeerNotice,
    PeerRequest,
    PeerRequestInspection,
    PeerRelationshipSummary,
    PeerResponse,
    PeerResponseSummary,
    PeerSubmitResult,
    RequestKind,
    RequestStatus,
)
from .peer_policy import PeerPolicy
from .peer_safety import contains_unauthorized_output, contains_unsafe_content
from .peer_broker import canonicalize_availability, classify, disclosure_scope
from .peer_store import PeerStore, utc_now


class PeerError(ValueError):
    """A safe peer-exchange protocol failure."""


@dataclass(frozen=True)
class PeerClaim:
    request: PeerRequest
    lease_token: str
    relationship_revision: int
    grant_revisions: dict[str, int]
    attempt_count: int


class PeerExchange:
    """Public peer boundary. Peer owners are never exposed through results."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        source_generation_active=None,
        source_generation_guard=None,
    ) -> None:
        self._store = PeerStore(data_dir)
        self._policy = PeerPolicy()
        self._source_generation_active = source_generation_active
        self._source_generation_guard = source_generation_guard

    def set_source_generation_active(self, predicate) -> None:
        """Attach the host generation fence when the exchange was wired externally."""
        if self._source_generation_active is None:
            self._source_generation_active = predicate

    def register_handle(self, owner: str, handle: str, **kwargs: object) -> None:
        self._store.register_handle(owner, handle, **kwargs)

    def invite(self, owner: str, handle: str, **kwargs: object):
        try:
            result = self._store.invite(owner, handle, **kwargs)
        except ValueError as error:
            raise PeerError(str(error)) from None
        if result is None:
            raise PeerError("handle_not_found")
        return next(
            summary
            for summary in self._store.list_relationships(owner)
            if summary.relationship_id == result.relationship_id
        )

    def accept(self, owner: str, relationship_id: str, **kwargs: object) -> None:
        result = self._store.accept(owner, relationship_id, **kwargs)
        if result != "accepted":
            raise PeerError(result)

    def update_grant(
        self,
        owner: str,
        relationship_id: str,
        grantee: str | None,
        communicate: bool,
        auto_reply: bool,
        share_availability: bool,
        expected_revision: int,
        **kwargs: object,
    ) -> PeerGrantSummary:
        if grantee is None:
            relationship = self._store.get_relationship(owner, relationship_id)
            if relationship is None:
                raise PeerError("not_found")
            try:
                grantee = relationship.other_owner(owner)
            except ValueError:
                raise PeerError("not_found") from None
        try:
            result = self._store.update_grant(
                owner,
                relationship_id,
                grantee,
                communicate,
                auto_reply,
                share_availability,
                expected_revision,
                **kwargs,
            )
        except ValueError as error:
            raise PeerError(str(error)) from None
        if result is None:
            raise PeerError("not_found")
        return PeerGrantSummary(
            result.communicate,
            result.auto_reply,
            result.share_availability,
            result.revision,
            result.expires_at,
        )

    def revoke(self, owner: str, relationship_id: str, **kwargs: object) -> None:
        if not self._store.revoke(owner, relationship_id, **kwargs):
            raise PeerError("not_found")

    def submit(
        self,
        sender: str,
        peer_handle: str,
        generation: str,
        call: str,
        kind: RequestKind | str,
        text: str,
        *,
        action: str = "ask",
        purpose: str = "peer_exchange",
        now: datetime | None = None,
        thread_id: str | None = None,
    ) -> PeerSubmitResult:
        if contains_unsafe_content(text):
            raise PeerError("unsafe_content")
        if self._source_generation_active is not None and not self._source_generation_active(sender, generation):
            raise PeerError("superseded")
        current = utc_now(now)
        duplicate = self._store.duplicate_submission(sender, generation, call)
        if duplicate is not None:
            saved, pending, handle = duplicate
            if not self._source_active(saved):
                self._store.deny_superseded_request(saved.request_id, now=current)
                raise PeerError("superseded")
            return PeerSubmitResult(saved.request_id, saved.status.value, handle, saved.thread_id, pending)
        kind, action, purpose = classify(kind, " ".join(purpose.split()), text)
        if kind == RequestKind.AVAILABILITY:
            text = canonicalize_availability(peer_handle, text)
            kind, action, purpose = classify(kind, purpose, text)
        scope = disclosure_scope(purpose, text, action)
        if kind == RequestKind.AVAILABILITY and scope != "availability":
            raise PeerError("unsafe_action")
        if kind == RequestKind.SENSITIVE and scope == "none":
            raise PeerError("unsafe_action")
        try:
            relationship = self._store.relationship_for_handle(sender, peer_handle)
        except ValueError:
            raise PeerError("handle_not_found") from None
        if relationship is None or relationship.status.value != "active":
            raise PeerError("handle_not_found")
        recipient = relationship.other_owner(sender)
        thread_id = thread_id or str(uuid.uuid4())
        request = PeerRequest(
            str(uuid.uuid4()),
            relationship.relationship_id,
            sender,
            recipient,
            thread_id,
            generation,
            call,
            kind,
            action,
            text,
            1,
            current,
            disclosure_scope=scope,
        )
        decision = self._policy.decide(
            relationship,
            self._store.grants(sender, relationship.relationship_id),
            request,
            current,
        )
        if decision.outcome == "deny":
            raise PeerError(decision.reason)
        if decision.outcome == "confirm":
            request = PeerRequest(
                request.request_id,
                request.relationship_id,
                sender,
                recipient,
                thread_id,
                generation,
                call,
                kind,
                action,
                text,
                1,
                current,
                RequestStatus.CONFIRMATION_PENDING,
                scope,
            )
        try:
            saved, pending, _ = self._store.submit(request, purpose, now=current)
        except ValueError as error:
            raise PeerError(str(error)) from None
        if not self._source_active(saved):
            self._store.deny_superseded_request(saved.request_id, now=current)
            raise PeerError("superseded")
        return PeerSubmitResult(
            saved.request_id,
            saved.status.value,
            peer_handle.strip().lower(),
            saved.thread_id,
            pending,
        )

    def decide_confirmation(
        self, owner: str, pending_id: str, approve: bool, *, now: datetime | None = None
    ) -> str:
        if self._source_generation_active is not None:
            source = self._store.confirmation_source(pending_id)
            if source is None or not self._source_generation_active(*source):
                self._store.cancel_superseded_confirmation(
                    pending_id, now=utc_now(now)
                )
                raise PeerError("superseded")
        try:
            result = self._store.decide_confirmation(
                owner, pending_id, approve, now=utc_now(now)
            )
        except ValueError as error:
            raise PeerError(str(error)) from None
        if result not in {"confirmed", "cancelled"}:
            raise PeerError(result)
        return result

    def decide_confirmation_prefix(
        self, owner: str, prefix: str, approve: bool, *, now: datetime | None = None
    ) -> str:
        current = utc_now(now)
        pending_id = self._store.resolve_pending_prefix(owner, prefix, now=current)
        if pending_id is None:
            raise PeerError("not_found")
        return self.decide_confirmation(owner, pending_id, approve, now=current)

    def claim(
        self, *, now: datetime | None = None, lease_seconds: int = 60
    ) -> PeerClaim | None:
        current = utc_now(now)
        candidate = self._store.claim_candidates(
            now=current, lease_seconds=lease_seconds
        )
        while candidate is not None:
            request, token, relationship, grants, attempt_count = candidate
            if not self._source_active(request):
                self._store.deny_claim(request.request_id, token, now=current)
                candidate = self._store.claim_candidates(now=current, lease_seconds=lease_seconds)
                continue
            decision = self._policy.decide(relationship, grants, request, current)
            if decision.outcome == "allow":
                grant_revisions = {
                    grant.grantor_owner_id: grant.revision for grant in grants
                }
                return PeerClaim(
                    request,
                    token,
                    relationship.revision,
                    grant_revisions,
                    attempt_count,
                )
            self._store.deny_claim(request.request_id, token, now=current)
            candidate = self._store.claim_candidates(
                now=current, lease_seconds=lease_seconds
            )
        return None

    def claim_notice(
        self, *, now: datetime | None = None, lease_seconds: int = 60
    ) -> PeerNotice | None:
        current = utc_now(now)
        value = self._store.claim_notice(now=current, lease_seconds=lease_seconds)
        if value is not None and self._source_generation_active is not None:
            source = self._store.confirmation_source(value[0].pending_id)
            if source is None or not self._source_generation_active(*source):
                self._store.cancel_superseded_confirmation(value[0].pending_id, now=current)
                return None
        return None if value is None else value[0]

    def notice_recipient(
        self, pending_id: str, lease_token: str, *, now: datetime | None = None
    ) -> str | None:
        # This internal worker lookup is intentionally separate from the redacted notice DTO.
        return self._store.notice_recipient(pending_id, lease_token, now=utc_now(now))

    def begin_notice_attempt(
        self, notice: PeerNotice, *, now: datetime | None = None
    ) -> bool:
        if self._source_generation_active is not None:
            source = self._store.confirmation_source(notice.pending_id)
            if source is None or not self._source_generation_active(*source):
                self._store.cancel_superseded_confirmation(notice.pending_id, now=utc_now(now))
                return False
        return self._store.begin_notice_attempt(
            notice.pending_id,
            notice.lease_token,
            now=utc_now(now),
        )

    @contextmanager
    def notice_delivery_guard(
        self, notice: PeerNotice, *, now: datetime | None = None
    ):
        current = utc_now(now)
        source = self._store.confirmation_source(notice.pending_id)
        if source is None:
            yield False
            return
        if self._source_generation_guard is not None:
            with self._source_generation_guard(*source) as active:
                if not active:
                    self._store.cancel_superseded_confirmation(
                        notice.pending_id, now=current
                    )
                yield bool(active)
            return
        active = self._source_generation_active is None or self._source_generation_active(
            *source
        )
        if not active:
            self._store.cancel_superseded_confirmation(notice.pending_id, now=current)
        yield active

    def complete_notice(
        self,
        pending_id: str,
        lease_token: str,
        outcome: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        try:
            return self._store.complete_notice(
                pending_id, lease_token, outcome, now=utc_now(now)
            )
        except ValueError:
            return False

    def complete(
        self,
        owner: str,
        request_id: str,
        lease_token: str,
        frames: tuple[str, ...],
        *,
        relationship_revision: int | None = None,
        grant_revisions: dict[str, int] | None = None,
        now: datetime | None = None,
    ) -> bool:
        if isinstance(frames, str) or any(
            contains_unsafe_content(frame) or self.output_is_unsafe(request_id, frame)
            for frame in frames
        ):
            return False
        return self._finish(
            owner,
            request_id,
            lease_token,
            frames,
            relationship_revision,
            grant_revisions,
            False,
            None,
            now,
        )

    def fail(
        self,
        owner: str,
        request_id: str,
        lease_token: str,
        frames: tuple[str, ...],
        *,
        relationship_revision: int | None = None,
        grant_revisions: dict[str, int] | None = None,
        error_code: str = "peer_execution_failed",
        now: datetime | None = None,
    ) -> bool:
        return self._finish(
            owner,
            request_id,
            lease_token,
            frames,
            relationship_revision,
            grant_revisions,
            True,
            error_code,
            now,
        )

    def _finish(
        self,
        owner: str,
        request_id: str,
        lease_token: str,
        frames: tuple[str, ...] | str,
        relationship_revision: int | None,
        grant_revisions: dict[str, int] | None,
        failed: bool,
        error_code: str | None,
        now: datetime | None,
    ) -> bool:
        if isinstance(frames, str):
            frames = (frames,)
        if relationship_revision is None or grant_revisions is None:
            return False
        request = self._store.request_for_output_check(request_id)
        if request is None or not self._source_active(request):
            return False
        try:
            return self._store.finish(
                owner,
                request_id,
                lease_token,
                tuple(frames),
                relationship_revision,
                grant_revisions,
                failed=failed,
                error_code=error_code if failed else None,
                now=utc_now(now),
            )
        except ValueError:
            return False

    def defer(
        self,
        request_id: str,
        lease_token: str,
        *,
        error_code: str = "peer_execution_failed",
        now: datetime | None = None,
    ) -> bool:
        try:
            return self._store.defer(
                request_id,
                lease_token,
                error_code=error_code,
                now=utc_now(now),
            )
        except ValueError:
            return False

    def heartbeat(
        self,
        request_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 180,
        relationship_revision: int | None = None,
        grant_revisions: dict[str, int] | None = None,
    ) -> bool:
        try:
            return self._store.heartbeat(
                request_id, lease_token, now=utc_now(now), lease_seconds=lease_seconds,
                relationship_revision=relationship_revision, grant_revisions=grant_revisions,
            )
        except ValueError:
            return False

    def claim_is_active(
        self, request_id: str, lease_token: str, *, now: datetime | None = None,
        relationship_revision: int | None = None, grant_revisions: dict[str, int] | None = None,
    ) -> bool:
        try:
            request = self._store.request_for_output_check(request_id)
            if request is None or not self._source_active(request):
                return False
            return self._store.claim_is_active(
                request_id, lease_token, now=utc_now(now), relationship_revision=relationship_revision,
                grant_revisions=grant_revisions,
            )
        except ValueError:
            return False

    def prior_completed_exchanges(self, request_id: str):
        try:
            return self._store.prior_completed_exchanges(request_id)
        except ValueError:
            return ()

    def _source_active(self, request: PeerRequest) -> bool:
        return self._source_generation_active is None or bool(
            self._source_generation_active(request.sender_owner_id, request.source_generation_id)
        )

    def execution_deadline(self, request_id: str) -> datetime | None:
        try:
            return self._store.execution_deadline(request_id)
        except ValueError:
            return None

    def request_execution_context(self, request_id: str) -> tuple[str, datetime] | None:
        try:
            return self._store.request_execution_context(request_id)
        except ValueError:
            return None

    def output_is_unsafe(self, request_id: str, text: str) -> bool:
        request = self._store.request_for_output_check(request_id)
        return request is None or contains_unauthorized_output(text, request.kind.value, request.disclosure_scope)

    def inspect_request(
        self, owner: str, request_id: str
    ) -> PeerRequestInspection | None:
        value = self._store.inspect_request(owner, request_id)
        response = None
        if value is not None and value[1] is not None:
            response = PeerResponseSummary(
                value[1].frames, value[1].status, value[1].created_at
            )
        if value is None:
            return None
        return PeerRequestInspection(
            value[0].request_id,
            value[0].status.value,
            value[2],
            response,
            value[0].thread_id,
            value[3],
            value[4],
        )

    inspect = inspect_request

    def resume_thread(self, owner: str, thread_id: str) -> PeerRequestInspection | None:
        value = self._store.latest_thread_request(owner, thread_id)
        if value is None:
            return None
        response = None if value[1] is None else PeerResponseSummary(value[1].frames, value[1].status, value[1].created_at)
        return PeerRequestInspection(
            value[0].request_id,
            value[0].status.value,
            value[2],
            response,
            value[0].thread_id,
            value[3],
            value[4],
        )

    def list_relationships(self, owner: str) -> tuple[PeerRelationshipSummary, ...]:
        return self._store.list_relationships(owner)

    def handle_for_owner(self, owner: str) -> str | None:
        return self._store.handle_for_owner(owner)

    def delete_owner(self, owner: str, *, now: datetime | None = None) -> None:
        self._store.delete_owner(owner, now=now)

    def relationship_history(
        self, owner: str, relationship_id: str
    ) -> tuple[PeerHistoryEntry, ...] | None:
        return self._store.relationship_history(owner, relationship_id)

    def audit_events(self, owner: str) -> tuple[PeerAuditEvent, ...]:
        return self._store.audit_events(owner)

    def export_owner_records(self, *, owner_id: str):
        return self._store.export_owner_records(owner_id=owner_id)

    def import_owner_records(self, owner_id: str, records: object) -> None:
        self._store.import_owner_records(owner_id, records)

    def purge_owner_history(
        self, owner: str, relationship_id: str, *, confirm: bool, now: datetime | None = None
    ) -> None:
        if confirm is not True:
            raise PeerError("invalid_confirmation")
        try:
            self._store.purge_history(owner, relationship_id, now=utc_now(now))
        except ValueError as error:
            raise PeerError(str(error)) from None

    def purge_history(self, owner: str, relationship_id: str, *, now: datetime | None = None) -> None:
        self.purge_owner_history(owner, relationship_id, confirm=True, now=now)

    def prune(self, *, now: datetime) -> None:
        self._store.prune(now)
