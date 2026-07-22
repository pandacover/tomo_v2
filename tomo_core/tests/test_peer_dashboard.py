import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI

from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.peer_api import create_peer_router
from tomo_core.peer_exchange import PeerExchange


class PeerDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_relationship_purge_isolated_and_mutual_cutoff_removes_shared_request(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            exchange = PeerExchange(tmp)
            for owner, handle in (("alice", "alice"), ("bob", "bob"), ("charlie", "charlie")):
                exchange.register_handle(owner, handle)
            bob_relation = exchange.invite("alice", "bob", now=now)
            charlie_relation = exchange.invite("alice", "charlie", now=now)
            exchange.accept("bob", bob_relation.relationship_id, now=now)
            exchange.accept("charlie", charlie_relation.relationship_id, now=now)
            for relation, peer in ((bob_relation, "bob"), (charlie_relation, "charlie")):
                exchange.update_grant("alice", relation.relationship_id, None, True, False, False, 0, now=now)
                exchange.update_grant(peer, relation.relationship_id, None, True, True, False, 0, now=now)
            bob_request = exchange.submit("alice", "bob", "generation", "bob-call", "ordinary_message", "private bob", now=now)
            charlie_request = exchange.submit("alice", "charlie", "generation", "charlie-call", "ordinary_message", "private charlie", now=now)

            exchange.purge_history("alice", bob_relation.relationship_id, now=now + timedelta(minutes=1))

            self.assertEqual(exchange.relationship_history("alice", bob_relation.relationship_id), ())
            self.assertEqual(len(exchange.relationship_history("alice", charlie_relation.relationship_id) or ()), 1)
            self.assertIsNone(exchange.inspect_request("alice", bob_request.request_id))
            self.assertIsNotNone(exchange.inspect_request("bob", bob_request.request_id))
            exported = list(exchange.export_owner_records(owner_id="alice"))
            self.assertNotIn(bob_request.request_id, str(exported))
            self.assertIn(charlie_request.request_id, str(exported))

            exchange.purge_history("bob", bob_relation.relationship_id, now=now + timedelta(minutes=2))

            self.assertIsNone(exchange.inspect_request("bob", bob_request.request_id))

    async def test_dashboard_handle_relationship_acceptance_and_redacted_history(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            onboarding = TelegramOnboardingStore(tmp)
            exchange = PeerExchange(tmp)
            alice = onboarding.tomo_id_for_user("alice-user")
            bob = onboarding.tomo_id_for_user("bob-user")
            charlie = onboarding.tomo_id_for_user("charlie-user")
            exchange.register_handle(alice, "alice")
            exchange.register_handle(bob, "bob")
            exchange.register_handle(charlie, "charlie")
            relationship = exchange.invite(alice, "bob", now=now)
            other = exchange.invite(bob, "charlie", now=now)
            self.assertFalse(exchange.list_relationships(alice)[0].can_accept)
            self.assertTrue(exchange.list_relationships(bob)[0].can_accept)
            exchange.accept(bob, relationship.relationship_id, now=now)
            exchange.update_grant(alice, relationship.relationship_id, None, True, False, False, 0, now=now)
            exchange.update_grant(bob, relationship.relationship_id, None, True, True, False, 0, now=now)
            for number in range(21):
                exchange.submit(alice, "bob", "generation", str(number), "ordinary_message", f"ordinary message {number}", now=now + timedelta(hours=number + 1))

            app = FastAPI()
            app.include_router(create_peer_router(tmp, api_key="key", onboarding=onboarding, peer_exchange=exchange))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                me = await client.get("/v1/peers/me", headers={"x-api-key": "key"}, params={"userId": "alice-user"})
                history = await client.get(f"/v1/peers/relationships/{relationship.relationship_id}/history", headers={"x-api-key": "key"}, params={"userId": "alice-user"})
                forbidden = await client.get(f"/v1/peers/relationships/{other.relationship_id}/history", headers={"x-api-key": "key"}, params={"userId": "alice-user"})

            self.assertEqual(me.json(), {"handle": "alice"})
            self.assertEqual(history.status_code, 200)
            rows = history.json()["history"]
            self.assertEqual(len(rows), 20)
            self.assertEqual(rows[0]["createdAt"], (now + timedelta(hours=21)).isoformat())
            self.assertEqual(rows[0]["direction"], "outgoing")
            self.assertEqual(set(rows[0]), {"requestId", "threadId", "direction", "kind", "status", "createdAt", "responseStatus"})
            self.assertTrue(rows[0]["threadId"])
            self.assertNotIn("private text", history.text)
            self.assertNotIn(alice, history.text)
            self.assertNotIn(bob, history.text)
            self.assertEqual(forbidden.status_code, 404)
