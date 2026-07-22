import unittest
from datetime import datetime, timedelta, timezone

from tomo_core.peer_models import DirectionalGrant, PeerRequest, Relationship, RelationshipStatus, RequestAction, RequestKind
from tomo_core.peer_policy import decide


class PeerPolicyTests(unittest.TestCase):
    def test_ordinary_request_requires_bilateral_grants(self):
        now = datetime.now(timezone.utc)
        relation = Relationship("r", "a", "b", "a", RelationshipStatus.ACTIVE, now)
        request = PeerRequest("q", "r", "a", "b", "t", "g", "c", RequestKind.ORDINARY_MESSAGE, RequestAction.ASK, "hello", 1, now)
        grants = (DirectionalGrant("r", "a", "b", True, False, False, 1, now + timedelta(days=1), now), DirectionalGrant("r", "b", "a", False, True, False, 1, now + timedelta(days=1), now))
        self.assertEqual(decide(relation, grants, request, now).outcome, "allow")

    def test_sensitive_confirms_and_mutation_denies(self):
        now = datetime.now(timezone.utc)
        relation = Relationship("r", "a", "b", "a", "active", now)
        grants = (DirectionalGrant("r", "a", "b", True, False, False, 1, None, now), DirectionalGrant("r", "b", "a", False, True, False, 1, None, now))
        sensitive = PeerRequest("q", "r", "a", "b", "t", "g", "c", "sensitive", "ask", "hello", 1, now)
        mutating = PeerRequest("x", "r", "a", "b", "t", "g2", "c2", "ordinary_message", "mutating_action", "hello", 1, now)
        self.assertEqual(decide(relation, grants, sensitive, now).outcome, "confirm")
        self.assertEqual(decide(relation, grants, mutating, now).outcome, "deny")

    def test_availability_without_standing_grant_requires_one_time_confirmation(self):
        now = datetime.now(timezone.utc)
        relation = Relationship("r", "a", "b", "a", "active", now)
        grants = (
            DirectionalGrant("r", "a", "b", True, False, False, 1, None, now),
            DirectionalGrant("r", "b", "a", False, True, False, 1, None, now),
        )
        request = PeerRequest(
            "q",
            "r",
            "a",
            "b",
            "t",
            "g",
            "c",
            "availability",
            "ask",
            "are you free friday?",
            1,
            now,
        )

        self.assertEqual(decide(relation, grants, request, now).outcome, "confirm")

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
        self.assertEqual(decide(relation, grants, authorized, now).outcome, "allow")

    def test_commitment_proposal_still_requires_an_active_relationship_and_grants(self):
        now = datetime.now(timezone.utc)
        inactive = Relationship("r", "a", "b", "a", "revoked", now)
        request = PeerRequest("q", "r", "a", "b", "t", "g", "c", "sensitive", "commitment_proposal", "Could we meet?", 1, now, disclosure_scope="commitment_proposal")

        self.assertEqual(decide(inactive, (), request, now).outcome, "deny")
