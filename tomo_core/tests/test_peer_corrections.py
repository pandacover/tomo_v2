import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tomo_core.peer_exchange import PeerError, PeerExchange
from tomo_core.peer_models import PeerResponse, RequestKind
from tomo_core.peer_policy import PeerPolicy


class PeerCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.exchange = PeerExchange(self.tmp.name)
        self.exchange.register_handle("a", "alice", now=self.now)
        self.exchange.register_handle("b", "bob", now=self.now)
        self.relationship = self.exchange.invite("a", "bob", now=self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def activate(self):
        self.exchange.accept("b", self.relationship.relationship_id, now=self.now)
        self.exchange.update_grant("a", self.relationship.relationship_id, "b", True, False, False, 0, now=self.now)
        self.exchange.update_grant("b", self.relationship.relationship_id, "a", False, True, False, 0, now=self.now)

    def test_only_invitee_can_accept_and_accept_is_idempotent(self):
        with self.assertRaisesRegex(PeerError, "not_invitee"):
            self.exchange.accept("a", self.relationship.relationship_id, now=self.now)
        with self.assertRaisesRegex(PeerError, "not_found"):
            self.exchange.accept("c", self.relationship.relationship_id, now=self.now)
        self.exchange.accept("b", self.relationship.relationship_id, now=self.now)
        self.exchange.accept("b", self.relationship.relationship_id, now=self.now)

    def test_directional_aggregate_uses_zero_based_compare_and_swap(self):
        self.exchange.accept("b", self.relationship.relationship_id, now=self.now)
        grant = self.exchange.update_grant("a", self.relationship.relationship_id, "b", True, False, False, 0, now=self.now)
        self.assertEqual((grant.communicate, grant.revision), (True, 1))
        with self.assertRaisesRegex(PeerError, "stale_revision"):
            self.exchange.update_grant("a", self.relationship.relationship_id, "b", False, False, False, 0, now=self.now)

    def test_pending_invites_are_hourly_bounded_but_retries_remain_idempotent(self):
        self.exchange.accept("b", self.relationship.relationship_id, now=self.now)
        for number in range(10):
            owner = f"owner-{number}"
            handle = f"peer_{number}"
            self.exchange.register_handle(owner, handle, now=self.now)
            self.exchange.invite("a", handle, now=self.now)

        duplicate = self.exchange.invite("a", "peer_0", now=self.now)

        self.assertEqual(duplicate.peer_handle, "peer_0")
        self.exchange.register_handle("overflow", "overflow", now=self.now)
        with self.assertRaisesRegex(PeerError, "rate_limited"):
            self.exchange.invite("a", "overflow", now=self.now)

    def test_thread_cap_and_expiry_are_enforced(self):
        self.activate()
        first = self.exchange.submit("a", "bob", "g", "0", RequestKind.ORDINARY_MESSAGE, "one", now=self.now)
        for number in range(1, 4):
            self.exchange.submit("a", "bob", "g", str(number), RequestKind.ORDINARY_MESSAGE, "next", now=self.now, thread_id=first.thread_id)
        with self.assertRaisesRegex(PeerError, "thread_limit"):
            self.exchange.submit("a", "bob", "g", "4", RequestKind.ORDINARY_MESSAGE, "no", now=self.now, thread_id=first.thread_id)
        with self.assertRaisesRegex(PeerError, "thread_expired"):
            self.exchange.submit("a", "bob", "g", "late", RequestKind.ORDINARY_MESSAGE, "no", now=self.now + timedelta(minutes=16), thread_id=first.thread_id)

    def test_confirmation_requires_owner_decision_before_claim(self):
        self.activate()
        result = self.exchange.submit("a", "bob", "g", "s", RequestKind.SENSITIVE, "your exact calendar details", now=self.now)
        self.assertIsNotNone(result.pending_id)
        self.assertIsNone(self.exchange.claim(now=self.now))
        self.exchange.decide_confirmation("b", result.pending_id, True, now=self.now)
        self.assertEqual(self.exchange.inspect_request("a", result.request_id).status, "authorized")

    def test_superseded_source_cancels_confirmation_and_suppresses_work(self):
        active = {"value": True}
        exchange = PeerExchange(
            self.tmp.name, source_generation_active=lambda _owner, _generation: active["value"]
        )
        self.activate()
        result = exchange.submit(
            "a", "bob", "superseded-generation", "s", RequestKind.SENSITIVE,
            "your exact calendar details", now=self.now,
        )
        active["value"] = False

        with self.assertRaisesRegex(PeerError, "superseded"):
            exchange.decide_confirmation("b", result.pending_id, True, now=self.now)

        self.assertEqual(exchange.inspect_request("a", result.request_id).status, "denied")
        self.assertIsNone(exchange.claim(now=self.now))
        self.assertIsNone(exchange.claim_notice(now=self.now))

    def test_superseded_source_after_claim_cannot_persist_response(self):
        active = {"value": True}
        exchange = PeerExchange(self.tmp.name, source_generation_active=lambda *_: active["value"])
        self.activate()
        result = exchange.submit("a", "bob", "generation", "call", RequestKind.ORDINARY_MESSAGE, "hello", now=self.now)
        claim = exchange.claim(now=self.now)
        active["value"] = False

        self.assertFalse(exchange.claim_is_active(result.request_id, claim.lease_token, now=self.now, relationship_revision=claim.relationship_revision, grant_revisions=claim.grant_revisions))
        self.assertFalse(exchange.complete("b", result.request_id, claim.lease_token, ("late",), now=self.now, relationship_revision=claim.relationship_revision, grant_revisions=claim.grant_revisions))
        self.assertIsNone(exchange.inspect("a", result.request_id).response)

    def test_response_requires_bounded_nonblank_frames(self):
        with self.assertRaises(ValueError):
            PeerResponse("q", "b", (), "completed", self.now)
        with self.assertRaises(ValueError):
            PeerResponse("q", "b", ("",), "completed", self.now)
        self.assertEqual(PeerResponse("q", "b", ("one", "two"), "completed", self.now).frames, ("one", "two"))

    def test_absolute_execution_deadline_cannot_be_released_or_completed_late(self):
        self.activate()
        result = self.exchange.submit(
            "a", "bob", "generation", "deadline", RequestKind.ORDINARY_MESSAGE,
            "hello", now=self.now,
        )
        claim = self.exchange.claim(now=self.now, lease_seconds=180)
        late = self.now + timedelta(seconds=61)

        self.assertEqual(
            self.exchange.execution_deadline(result.request_id),
            self.now + timedelta(seconds=60),
        )
        self.assertFalse(self.exchange.claim_is_active(
            result.request_id, claim.lease_token, now=late,
            relationship_revision=claim.relationship_revision,
            grant_revisions=claim.grant_revisions,
        ))
        self.assertFalse(self.exchange.complete(
            "b", result.request_id, claim.lease_token, ("late",), now=late,
            relationship_revision=claim.relationship_revision,
            grant_revisions=claim.grant_revisions,
        ))
        self.assertIsNone(self.exchange.claim(now=self.now + timedelta(seconds=181)))
        self.assertEqual(self.exchange.inspect("a", result.request_id).status, "failed")

    def test_policy_exposes_specific_missing_permission_reason(self):
        self.assertEqual(PeerPolicy().decide(None, (), None, self.now).reason, "relationship_inactive")

    def test_model_purpose_cannot_downgrade_an_imperative_into_a_proposal(self):
        self.activate()
        with self.assertRaisesRegex(PeerError, "unsafe_action"):
            self.exchange.submit(
                "a", "bob", "generation", "purpose-widening",
                RequestKind.ORDINARY_MESSAGE, "book it now",
                purpose="could we book something", now=self.now,
            )

    def test_private_categories_and_imperatives_anywhere_fail_closed(self):
        self.activate()
        for number, text in enumerate((
            "Could we book dinner? Please send the reservation now",
            "what is their bank balance?",
            "please forward my medical diagnosis",
            "email the attorney about the case",
            "What is Alice's email?",
            "What email address should I use to contact Alice?",
            "Can you tell me what email address I should use to contact Alice?",
            "delete the file, then could we book dinner",
            "could we book dinner and send the reservation",
        )):
            with self.assertRaisesRegex(PeerError, "unsafe_action"):
                self.exchange.submit("a", "bob", "blocked", str(number), RequestKind.ORDINARY_MESSAGE, text, now=self.now)

    def test_sensitive_contact_scope_requires_explicit_affected_owner_subject(self):
        self.activate()

        allowed = self.exchange.submit(
            "a",
            "bob",
            "contact",
            "self",
            RequestKind.ORDINARY_MESSAGE,
            "What is your email address?",
            now=self.now,
        )
        self.assertEqual(allowed.status, "confirmation_pending")
        with self.assertRaisesRegex(PeerError, "unsafe_action"):
            self.exchange.submit(
                "a",
                "bob",
                "contact",
                "other",
                RequestKind.ORDINARY_MESSAGE,
                "What email address should I use to contact Alice?",
                now=self.now,
            )

    def test_pure_commitment_proposal_still_uses_owner_confirmation(self):
        self.activate()

        result = self.exchange.submit(
            "a",
            "bob",
            "proposal",
            "pure",
            RequestKind.ORDINARY_MESSAGE,
            "Could we book dinner?",
            now=self.now,
        )

        self.assertEqual(result.status, "confirmation_pending")
        self.assertIsNotNone(result.pending_id)

    def test_stale_source_rejects_duplicate_and_suppresses_a_leased_notice(self):
        active = {"value": True}
        exchange = PeerExchange(self.tmp.name, source_generation_active=lambda *_: active["value"])
        self.activate()
        result = exchange.submit("a", "bob", "source", "call", RequestKind.SENSITIVE, "your exact calendar details", now=self.now)
        notice = exchange.claim_notice(now=self.now)
        active["value"] = False

        self.assertFalse(exchange.begin_notice_attempt(notice, now=self.now))
        with self.assertRaisesRegex(PeerError, "superseded"):
            exchange.submit("a", "bob", "source", "call", RequestKind.SENSITIVE, "your exact calendar details", now=self.now)
        self.assertEqual(exchange.inspect("a", result.request_id).status, "denied")

    def test_expired_leases_never_claim_a_fourth_execution(self):
        self.activate()
        result = self.exchange.submit("a", "bob", "retries", "call", RequestKind.ORDINARY_MESSAGE, "hello", now=self.now)
        for attempt in range(3):
            claim = self.exchange.claim(now=self.now + timedelta(seconds=attempt * 16))
            self.assertIsNotNone(claim)
        self.assertIsNone(self.exchange.claim(now=self.now + timedelta(seconds=49)))
        inspected = self.exchange.inspect("a", result.request_id)
        self.assertEqual(inspected.status, "failed")
        self.assertEqual(inspected.response.frames, ("unable to answer right now",))
        self.assertEqual(inspected.response.status.value, "failed")
