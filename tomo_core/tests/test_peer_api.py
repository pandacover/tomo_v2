import tempfile
import unittest
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI

from tomo_core.onboarding_store import TelegramOnboardingStore
from tomo_core.peer_api import create_peer_router
from tomo_core.peer_capability import PeerCapability, issue_capability
from tomo_core.peer_exchange import PeerExchange


class PeerApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_bff_routes_fail_closed_without_a_configured_control_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = FastAPI()
            app.include_router(create_peer_router(tmp))

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/v1/peers/relationships",
                    headers={"x-api-key": "caller-chosen"},
                    params={"userId": "victim"},
                )

            self.assertEqual(response.status_code, 401)

    async def test_bff_purge_history_uses_the_authenticated_users_relationship(self):
        with tempfile.TemporaryDirectory() as tmp:
            onboarding = TelegramOnboardingStore(tmp)
            exchange = PeerExchange(tmp)
            alice = onboarding.tomo_id_for_user("alice-user")
            bob = onboarding.tomo_id_for_user("bob-user")
            exchange.register_handle(alice, "alice")
            exchange.register_handle(bob, "bob")
            relationship = exchange.invite(alice, "bob")
            exchange.accept(bob, relationship.relationship_id)
            app = FastAPI()
            app.include_router(create_peer_router(tmp, api_key="key", onboarding=onboarding, peer_exchange=exchange))

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/peers/relationships/{relationship.relationship_id}/purge-history"
                denied = await client.post(path, json={"userId": "alice-user", "confirm": True})
                accepted = await client.post(path, headers={"x-api-key": "key"}, json={"userId": "alice-user", "confirm": True})

            self.assertEqual(denied.status_code, 401)
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json(), {"ok": True})
    async def test_bff_grant_derives_peer_and_block_uses_block_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            exchange = PeerExchange(tmp)
            alice = store.tomo_id_for_user("alice-user")
            bob = store.tomo_id_for_user("bob-user")
            exchange.register_handle(alice, "alice")
            exchange.register_handle(bob, "bob")
            invitation = exchange.invite(alice, "bob")
            exchange.accept(bob, invitation.relationship_id)
            app = FastAPI()
            app.include_router(create_peer_router(tmp, api_key="key", onboarding=store, peer_exchange=exchange))

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                granted = await client.patch(
                    f"/v1/peers/relationships/{invitation.relationship_id}/grant",
                    headers={"x-api-key": "key"},
                    json={
                        "userId": "alice-user",
                        "communicate": True,
                        "autoReply": False,
                        "shareAvailability": False,
                        "expectedRevision": 0,
                    },
                )
                blocked = await client.post(
                    f"/v1/peers/relationships/{invitation.relationship_id}/block",
                    headers={"x-api-key": "key"},
                    json={"userId": "alice-user"},
                )

            self.assertEqual(granted.status_code, 200)
            self.assertEqual(granted.json()["grant"]["revision"], 1)
            self.assertNotIn(alice, granted.text)
            self.assertNotIn(bob, granted.text)
            self.assertEqual(blocked.status_code, 200)
            self.assertEqual(exchange.list_relationships(alice)[0].status.value, "blocked")

    async def test_bff_uses_user_id_to_derive_tomo_and_never_returns_owner_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            exchange = PeerExchange(tmp)
            alice = TelegramOnboardingStore(tmp).tomo_id_for_user("alice-user")
            bob = TelegramOnboardingStore(tmp).tomo_id_for_user("bob-user")
            exchange.register_handle(alice, "alice")
            exchange.register_handle(bob, "bob")
            relationship = exchange.invite(alice, "bob")
            exchange.accept(bob, relationship.relationship_id)
            exchange.update_grant(alice, relationship.relationship_id, None, True, False, False, 0)
            exchange.update_grant(bob, relationship.relationship_id, None, False, True, False, 0)
            app = FastAPI()
            app.include_router(create_peer_router(tmp, api_key="key", peer_exchange=exchange))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                denied = await client.get("/v1/peers/relationships", params={"userId": "alice-user"})
                response = await client.get("/v1/peers/relationships", headers={"x-api-key": "key"}, params={"userId": "alice-user"})

            self.assertEqual(denied.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["relationships"][0]["peerHandle"], "bob")
            peer_grant = response.json()["relationships"][0]["peerGrant"]
            self.assertEqual(peer_grant["communicate"], False)
            self.assertEqual(peer_grant["autoReply"], True)
            self.assertEqual(peer_grant["revision"], 1)
            self.assertIsNone(peer_grant["expiresAt"])
            self.assertNotIn(alice, response.text)
            self.assertNotIn(bob, response.text)

    async def test_ask_requires_capability_to_match_every_active_generation_binding(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            store = TelegramOnboardingStore(tmp)
            link = store.create_install_link("alice-user", "bot")
            installation = store.consume_start_token(link.token, "chat-1", "actor-1")
            store.enqueue_update(1, "chat-1", "message", now=0, update_kind="message", message_id="m1", tomo_id=installation.tomo_id)
            work = store.claim_next_work(now=1)
            exchange = PeerExchange(tmp)
            bob = store.tomo_id_for_user("bob-user")
            exchange.register_handle(installation.tomo_id, "alice")
            exchange.register_handle(bob, "bob")
            relationship = exchange.invite(installation.tomo_id, "bob", now=now)
            exchange.accept(bob, relationship.relationship_id, now=now)
            exchange.update_grant(installation.tomo_id, relationship.relationship_id, None, True, False, False, 0, now=now)
            exchange.update_grant(bob, relationship.relationship_id, None, False, True, False, 0, now=now)
            key = b"k" * 32
            token = issue_capability(key, PeerCapability(installation.tomo_id, "actor-1", "telegram:chat-1", work.session_id, work.generation_id, 100, 200, frozenset({"ask", "inspect_request"})))
            app = FastAPI()
            app.include_router(create_peer_router(tmp, onboarding=store, peer_exchange=exchange, peer_key=key, clock=lambda: 150))
            headers = {"authorization": f"Bearer {token}", "x-tomo-owner-id": installation.tomo_id, "x-tomo-actor-id": "actor-1", "x-tomo-destination": "telegram:chat-1", "x-tomo-session-id": work.session_id, "x-tomo-generation-id": work.generation_id}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                body = {"peerHandle": "bob", "purpose": "question", "disclosureKind": "ordinary_message", "callId": "call-1", "message": "hello"}
                accepted = await client.post("/v1/peer-agent/requests", headers=headers, json=body)
                grounding_rejected = await client.post(
                    "/v1/peer-agent/requests",
                    headers=headers,
                    json=body | {"callId": "call-2", "message": "what is bob's favorite color?"},
                )
                rejected = await client.post("/v1/peer-agent/requests", headers=headers | {"x-tomo-actor-id": "other"}, json=body)
                invalid = await client.post("/v1/peer-agent/requests", headers=headers, json=body | {"ownerId": "leak"})
                claim = exchange.claim()
                self.assertIsNotNone(claim)
                exchange.fail(
                    bob,
                    accepted.json()["requestId"],
                    claim.lease_token,
                    ("unable to answer right now",),
                    relationship_revision=claim.relationship_revision,
                    grant_revisions=claim.grant_revisions,
                    error_code="sandbox_exec_failed",
                )
                inspected = await client.get(
                    f"/v1/peer-agent/requests/{accepted.json()['requestId']}",
                    headers=headers,
                )

            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(grounding_rejected.status_code, 200)
            self.assertEqual(grounding_rejected.json(), {"ok": False, "status": "failed", "errorCode": "peer_grounding_required"})
            self.assertEqual(rejected.status_code, 401)
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(inspected.status_code, 200)
            self.assertEqual(inspected.json()["errorCode"], "sandbox_exec_failed")
            self.assertNotIn(installation.tomo_id, accepted.text)
