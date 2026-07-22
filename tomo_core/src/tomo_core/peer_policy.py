from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .peer_models import (
    DirectionalGrant,
    PeerRequest,
    Relationship,
    RelationshipStatus,
    RequestAction,
    RequestKind,
    RequestStatus,
)


@dataclass(frozen=True)
class PeerPolicyDecision:
    outcome: str
    reason: str

    def __post_init__(self) -> None:
        if self.outcome not in {"allow", "confirm", "deny"}:
            raise ValueError("invalid_decision")


class PeerPolicy:
    """Pure authorization policy used at submit, claim, and completion fences."""

    def decide(
        self,
        relationship: Relationship | None,
        grants: tuple[DirectionalGrant, ...],
        request: PeerRequest | None,
        now: datetime,
    ) -> PeerPolicyDecision:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("invalid_now")
        if request is not None and request.action in {
            RequestAction.MUTATING_ACTION,
            RequestAction.CREDENTIALS,
            RequestAction.THIRD_PARTY_FORWARD,
        }:
            return PeerPolicyDecision("deny", "unsafe_action")
        if relationship is None or relationship.status != RelationshipStatus.ACTIVE:
            return PeerPolicyDecision("deny", "relationship_inactive")
        if request is None or request.relationship_id != relationship.relationship_id:
            return PeerPolicyDecision("deny", "relationship_inactive")
        try:
            if (
                relationship.other_owner(request.sender_owner_id)
                != request.recipient_owner_id
            ):
                return PeerPolicyDecision("deny", "relationship_inactive")
        except ValueError:
            return PeerPolicyDecision("deny", "relationship_inactive")
        current = now.astimezone(timezone.utc)
        valid = {
            grant.grantor_owner_id: grant
            for grant in grants
            if grant.relationship_id == relationship.relationship_id
        }
        sender_grant = valid.get(request.sender_owner_id)
        recipient_grant = valid.get(request.recipient_owner_id)
        if any(
            grant is not None
            and grant.expires_at is not None
            and grant.expires_at <= current
            for grant in (sender_grant, recipient_grant)
        ):
            return PeerPolicyDecision("deny", "expired_grant")
        if sender_grant is None or not sender_grant.communicate:
            return PeerPolicyDecision("deny", "missing_communicate")
        if recipient_grant is None or not recipient_grant.auto_reply:
            return PeerPolicyDecision("deny", "missing_auto_reply")
        if request.action == RequestAction.COMMITMENT_PROPOSAL:
            if request.status not in {RequestStatus.AUTHORIZED, RequestStatus.LEASED}:
                return PeerPolicyDecision("confirm", "confirmation_required")
        if (
            request.kind == RequestKind.AVAILABILITY
            and not recipient_grant.share_availability
        ):
            if request.status not in {RequestStatus.AUTHORIZED, RequestStatus.LEASED}:
                return PeerPolicyDecision("confirm", "confirmation_required")
        if request.kind == RequestKind.SENSITIVE:
            if request.status not in {RequestStatus.AUTHORIZED, RequestStatus.LEASED}:
                return PeerPolicyDecision("confirm", "confirmation_required")
        return PeerPolicyDecision("allow", "allowed")


def decide(
    relationship: Relationship | None,
    grants: tuple[DirectionalGrant, ...],
    request: PeerRequest | None,
    now: datetime,
) -> PeerPolicyDecision:
    return PeerPolicy().decide(relationship, grants, request, now)
