import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tomo_core.peer_exchange import PeerExchange, PeerError
from tomo_core.peer_models import RequestKind


class PeerExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.now = datetime(2026, 1, 1, tzinfo=timezone.utc); self.x = PeerExchange(self.tmp.name)
        self.x.register_handle("a", "alice"); self.x.register_handle("b", "bob")
        self.relationship = self.x.invite("a", "bob", now=self.now); self.x.accept("b", self.relationship.relationship_id, now=self.now)
        self.x.update_grant("a", self.relationship.relationship_id, "b", True, False, False, 0, now=self.now)
        self.x.update_grant("b", self.relationship.relationship_id, "a", False, True, False, 0, now=self.now)
    def tearDown(self): self.tmp.cleanup()
    def test_submit_claim_complete_and_owner_isolation(self):
        result = self.x.submit("a", "bob", "g", "c", RequestKind.ORDINARY_MESSAGE, "hello", now=self.now)
        self.assertEqual(result.status, "pending")
        self.assertEqual(self.x.inspect("b", result.request_id).request_id, result.request_id)
        self.assertIsNone(self.x.inspect("other", result.request_id))
        claim = self.x.claim(now=self.now); self.assertEqual(claim.request.request_id, result.request_id)
        self.assertTrue(self.x.complete("b", result.request_id, claim.lease_token, ("hi",), relationship_revision=claim.relationship_revision, grant_revisions=claim.grant_revisions, now=self.now))
        self.assertEqual(self.x.inspect("a", result.request_id).response.frames, ("hi",))
    def test_duplicate_submit_is_idempotent_and_sensitive_is_not_claimable(self):
        first = self.x.submit("a", "bob", "g", "c", "sensitive", "share your exact calendar details", now=self.now)
        duplicate = self.x.submit("a", "bob", "g", "c", "sensitive", "share your exact calendar details", now=self.now)
        self.assertEqual(first.request_id, duplicate.request_id); self.assertEqual(first.status, "confirmation_pending")
        self.assertIsNone(self.x.claim(now=self.now))

    def test_distinct_native_call_ids_create_distinct_peer_requests(self):
        first = self.x.submit("a", "bob", "generation", "provider-call-1", "ordinary_message", "hello", now=self.now)
        retry = self.x.submit("a", "bob", "generation", "provider-call-1", "ordinary_message", "hello", now=self.now)
        second = self.x.submit("a", "bob", "generation", "provider-call-2", "ordinary_message", "hello", now=self.now)

        self.assertEqual(retry.request_id, first.request_id)
        self.assertNotEqual(second.request_id, first.request_id)

    def test_resume_returns_only_the_callers_latest_thread_request(self):
        request = self.x.submit("a", "bob", "generation", "provider-call-1", "ordinary_message", "hello", thread_id="thread-1", now=self.now)

        resumed = self.x.resume_thread("a", "thread-1")

        self.assertEqual(resumed.request_id, request.request_id)
        self.assertIsNone(self.x.resume_thread("other", "thread-1"))

    def test_delete_owner_revokes_inflight_requests_and_is_idempotent(self):
        request = self.x.submit("a", "bob", "generation", "provider-call-1", "ordinary_message", "hello", now=self.now)

        self.x.delete_owner("a", now=self.now)
        self.x.delete_owner("a", now=self.now)

        self.assertEqual(self.x.inspect("b", request.request_id).status, "denied")
        self.assertIsNone(self.x.handle_for_owner("a"))
        self.assertEqual(self.x.list_relationships("a"), ())
        self.assertEqual(tuple(self.x.export_owner_records(owner_id="a")), ())
        with self.assertRaisesRegex(Exception, "owner_deleted"):
            self.x.register_handle("a", "alice_again")
    def test_stale_grant_and_revoke_during_lease_are_fenced(self):
        with self.assertRaisesRegex(PeerError, "stale_revision"):
            self.x.update_grant("a", self.relationship.relationship_id, "b", False, False, False, 0, now=self.now)
        request = self.x.submit("a", "bob", "g", "c", "ordinary_message", "hello", now=self.now)
        claim = self.x.claim(now=self.now)
        self.x.revoke("b", self.relationship.relationship_id, now=self.now)
        self.assertFalse(self.x.complete("b", request.request_id, claim.lease_token, ("no",), relationship_revision=claim.relationship_revision, grant_revisions=claim.grant_revisions, now=self.now))
    def test_ten_requests_per_relationship_per_hour_is_enforced(self):
        for number in range(10): self.x.submit("a", "bob", "g", str(number), "ordinary_message", "hello", now=self.now)
        with self.assertRaisesRegex(PeerError, "rate_limited"):
            self.x.submit("a", "bob", "g", "overflow", "ordinary_message", "hello", now=self.now)

    def test_update_grant_derives_the_other_relationship_owner_when_grantee_is_omitted(self):
        grant = self.x.update_grant("a", self.relationship.relationship_id, None, False, True, False, 1, now=self.now)
        self.assertEqual((grant.communicate, grant.auto_reply, grant.revision), (False, True, 2))

    def test_purge_requires_exact_confirmation(self):
        with self.assertRaisesRegex(PeerError, "invalid_confirmation"):
            self.x.purge_owner_history("a", self.relationship.relationship_id, confirm=False, now=self.now)

    def test_owner_export_is_keyword_only_and_purge_hides_prior_history(self):
        first = self.x.submit("a", "bob", "g", "first", "ordinary_message", "before", now=self.now)

        with self.assertRaises(TypeError):
            self.x.export_owner_records("a")
        exported = tuple(self.x.export_owner_records(owner_id="a"))
        self.assertEqual(exported[0]["owner_id"], "a")
        self.assertEqual(exported[0]["table"], "peer_profile")

        self.x.purge_history("a", self.relationship.relationship_id, now=self.now)
        second = self.x.submit("a", "bob", "g", "second", "ordinary_message", "after", now=self.now + timedelta(seconds=1))

        history = self.x.relationship_history("a", self.relationship.relationship_id)
        self.assertEqual([entry.request_id for entry in history], [second.request_id])
        self.assertIsNone(self.x.inspect("a", first.request_id))
