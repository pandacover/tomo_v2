import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from tomo_core.peer_exchange import PeerError, PeerExchange
from tomo_core.peer_models import ConfirmationStatus, PendingConfirmation, RequestKind
from tomo_core.peer_store import PeerStore


class PeerPhaseOneAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.x = PeerExchange(self.tmp.name)
        self.x.register_handle("a", "alice", now=self.now)
        self.x.register_handle("b", "bob", now=self.now)
        self.relationship = self.x.invite("a", "bob", now=self.now)
        self.x.accept("b", self.relationship.relationship_id, now=self.now)
        self.x.update_grant("a", self.relationship.relationship_id, "b", True, False, False, 0, now=self.now)
        self.x.update_grant("b", self.relationship.relationship_id, "a", False, True, False, 0, now=self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirmed_sensitive_is_claimable_once(self):
        result = self.x.submit("a", "bob", "g", "s", "sensitive", "share your exact calendar details", now=self.now)
        self.x.decide_confirmation("b", result.pending_id, True, now=self.now)
        claim = self.x.claim(now=self.now)
        self.assertEqual(claim.request.request_id, result.request_id)
        self.assertIsNone(self.x.claim(now=self.now))

    def test_confirmed_sensitive_can_complete_after_claim(self):
        result = self.x.submit("a", "bob", "g", "complete-sensitive", "sensitive", "share your exact calendar details", now=self.now)
        self.x.decide_confirmation("b", result.pending_id, True, now=self.now)

        claim = self.x.claim(now=self.now, lease_seconds=30)

        self.assertTrue(
            self.x.complete(
                "b",
                result.request_id,
                claim.lease_token,
                ("calendar meeting at 3",),
                relationship_revision=claim.relationship_revision,
                grant_revisions=claim.grant_revisions,
                now=self.now + timedelta(seconds=1),
            )
        )
        self.assertEqual(self.x.inspect("a", result.request_id).response.frames, ("calendar meeting at 3",))

    def test_expired_sensitive_lease_recovers_as_authorized(self):
        result = self.x.submit("a", "bob", "g", "recover-sensitive", "sensitive", "share your exact calendar details", now=self.now)
        self.x.decide_confirmation("b", result.pending_id, True, now=self.now)
        first_claim = self.x.claim(now=self.now, lease_seconds=1)

        recovered_claim = self.x.claim(now=self.now + timedelta(seconds=2), lease_seconds=30)

        self.assertEqual(recovered_claim.request.request_id, first_claim.request.request_id)
        self.assertNotEqual(recovered_claim.lease_token, first_claim.lease_token)

    def test_confirmation_fences_changed_grants_and_rejects_non_bool_or_replay(self):
        result = self.x.submit("a", "bob", "g", "s", "sensitive", "share your exact calendar details", now=self.now)
        self.x.update_grant("a", self.relationship.relationship_id, "b", True, False, False, 1, now=self.now)
        self.assertEqual(
            self.x.decide_confirmation("b", result.pending_id, True, now=self.now),
            "cancelled",
        )
        with self.assertRaisesRegex(PeerError, "replayed"):
            self.x.decide_confirmation("b", result.pending_id, True, now=self.now)
        result = self.x.submit("a", "bob", "g", "t", "sensitive", "share your exact calendar details", now=self.now)
        with self.assertRaisesRegex(PeerError, "invalid_approval"):
            self.x.decide_confirmation("b", result.pending_id, "false", now=self.now)
        with self.assertRaisesRegex(PeerError, "not_found"):
            self.x.decide_confirmation("other", result.pending_id, False, now=self.now)
        with self.assertRaisesRegex(PeerError, "expired"):
            self.x.decide_confirmation("b", result.pending_id, False, now=self.now + timedelta(minutes=16))

    def test_confirmation_cancels_when_current_grant_has_expired_or_relationship_revoked(self):
        result = self.x.submit("a", "bob", "g", "expiry", "sensitive", "share your exact calendar details", now=self.now)
        self.x.update_grant("a", self.relationship.relationship_id, "b", True, False, False, 1, expires_at=self.now + timedelta(seconds=1), now=self.now)
        self.assertEqual(
            self.x.decide_confirmation("b", result.pending_id, True, now=self.now + timedelta(seconds=2)),
            "cancelled",
        )
        result = self.x.submit("a", "bob", "g", "revoke", "sensitive", "share your exact calendar details", now=self.now)
        self.x.revoke("b", self.relationship.relationship_id, now=self.now)
        self.assertEqual(
            self.x.decide_confirmation("b", result.pending_id, True, now=self.now),
            "cancelled",
        )

    def test_completion_rechecks_expired_grants_without_response(self):
        expiry = self.now + timedelta(minutes=1)
        self.x.update_grant("a", self.relationship.relationship_id, "b", True, False, False, 1, expires_at=expiry, now=self.now)
        result = self.x.submit("a", "bob", "g", "e", "ordinary_message", "hello", now=self.now)
        claim = self.x.claim(now=self.now)
        self.assertFalse(self.x.complete("b", result.request_id, claim.lease_token, ("no",), relationship_revision=claim.relationship_revision, grant_revisions=claim.grant_revisions, now=expiry + timedelta(seconds=1)))
        self.assertIsNone(self.x.inspect("a", result.request_id).response)

    def test_submit_and_inspection_never_expose_peer_owner_id(self):
        with self.assertRaisesRegex(PeerError, "handle_not_found"):
            self.x.submit("a", "b", "g", "id", "ordinary_message", "hello", now=self.now)
        result = self.x.submit("a", "bob", "g", "ok", "ordinary_message", "hello", now=self.now)
        self.assertFalse(hasattr(result, "recipient_owner_id"))
        self.assertEqual(self.x.inspect("a", result.request_id).peer_handle, "bob")

    def test_relationship_and_audit_are_owner_scoped_and_safe(self):
        summary = self.x.list_relationships("a")[0]
        self.assertEqual(summary.peer_handle, "bob")
        self.assertFalse(any("owner" in field for field in summary.__dataclass_fields__))
        self.assertEqual(self.x.list_relationships("third"), ())
        events = self.x.audit_events("a")
        self.assertTrue(events)
        self.assertFalse(any("private" in repr(event) or "text" in repr(event) for event in events))
        self.assertEqual(self.x.audit_events("third"), ())

    def test_public_invite_and_grant_results_do_not_expose_owner_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            exchange = PeerExchange(tmp)
            exchange.register_handle("owner-a", "alice", now=self.now)
            exchange.register_handle("owner-b", "bob", now=self.now)

            invitation = exchange.invite("owner-a", "bob", now=self.now)

            self.assertEqual(invitation.peer_handle, "bob")
            self.assertFalse(any("owner" in field for field in invitation.__dataclass_fields__))
            exchange.accept("owner-b", invitation.relationship_id, now=self.now)
            grant = exchange.update_grant(
                "owner-a",
                invitation.relationship_id,
                "owner-b",
                True,
                False,
                False,
                0,
                now=self.now,
            )
            self.assertFalse(any("owner" in field for field in grant.__dataclass_fields__))

    def test_pending_confirmation_validation(self):
        valid = dict(pending_id="p", request_id="r", affected_owner_id="b", action_kind="ask", payload_hash="a" * 64, preview="preview", status="pending", expires_at=self.now + timedelta(minutes=1), created_at=self.now, decided_at=None, relationship_revision=1)
        self.assertEqual(PendingConfirmation(**valid).status, ConfirmationStatus.PENDING)
        for key, value in (("payload_hash", "A" * 64), ("preview", ""), ("preview", "x" * 257), ("decided_at", self.now), ("relationship_revision", 0)):
            invalid = valid | {key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                PendingConfirmation(**invalid)

    def test_store_rejects_invalid_finish_frames_and_lease(self):
        with self.assertRaisesRegex(ValueError, "invalid_lease_seconds"):
            self.x.claim(now=self.now, lease_seconds=True)
        with self.assertRaisesRegex(ValueError, "invalid_frames"):
            PeerStore(self.tmp.name).finish("b", "r", "t", ["bad"], 1, {}, failed=False, now=self.now)

    def test_schema_version_is_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(f"{tmp}/peer.sqlite3")
            db.execute("CREATE TABLE peer_schema(version INTEGER NOT NULL)")
            db.execute("INSERT INTO peer_schema VALUES(99)")
            db.commit()
            db.close()
            with self.assertRaisesRegex(ValueError, "unsupported_peer_schema"):
                PeerStore(tmp)

    def test_concurrent_duplicate_and_quota_are_serialized(self):
        barrier = threading.Barrier(2)
        results, errors = [], []
        def submit(call):
            try:
                barrier.wait()
                results.append(self.x.submit("a", "bob", "concurrent", call, "ordinary_message", "hello", now=self.now))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=submit, args=("same",)) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({item.request_id for item in results}), 1)
        for number in range(8): self.x.submit("a", "bob", "quota", str(number), "ordinary_message", "hello", now=self.now)
        barrier = threading.Barrier(2); errors = []
        threads = [threading.Thread(target=submit, args=(f"last-{number}",)) for number in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(isinstance(error, PeerError) and str(error) == "rate_limited" for error in errors), 1)
