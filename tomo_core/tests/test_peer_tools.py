import json
import unittest
from datetime import UTC, datetime

from tomo_core.peer_tools import PeerApiClient, peer_registry


class _Response:
    def __init__(self, payload): self.payload = payload
    def read(self, size=-1): return json.dumps(self.payload).encode()[:size if size >= 0 else None]
    def __enter__(self): return self
    def __exit__(self, *args): pass


def _client(opener, *, sleeper=lambda _seconds: None, clock=lambda: 0, utc_now=lambda: datetime(2026, 1, 1, tzinfo=UTC)):
    return PeerApiClient("https://peer.example", "capability-secret", "owner", "actor", "telegram:chat", "session", "generation", opener=opener, sleeper=sleeper, clock=clock, utc_now=utc_now)


class PeerToolsTests(unittest.TestCase):
    def test_relationship_observation_exposes_only_safe_permission_readiness(self):
        payload = {
            "relationships": [{
                "relationshipId": "private-id",
                "peerHandle": "bob",
                "status": "active",
                "grant": {"communicate": False, "autoReply": True, "shareAvailability": True},
                "peerGrant": {"communicate": True, "autoReply": True, "shareAvailability": False},
            }],
        }

        result = _client(lambda _request, **_kwargs: _Response(payload)).list_relationships()

        self.assertEqual(result, {
            "ok": True,
            "status": "completed",
            "relationships": [{
                "peer_handle": "bob",
                "status": "active",
                "can_ask": False,
                "peer_auto_reply": True,
                "peer_share_availability": False,
            }],
        })
        self.assertNotIn("private-id", json.dumps(result))

    def test_relationship_observation_fails_closed_for_malformed_grants(self):
        result = _client(lambda _request, **_kwargs: _Response({"relationships": [{"peerHandle": "bob", "status": "active", "grant": [], "peerGrant": None}]})).list_relationships()

        self.assertEqual(result["relationships"][0], {"peer_handle": "bob", "status": "active", "can_ask": False, "peer_auto_reply": False, "peer_share_availability": False})

    def test_relationship_readiness_requires_an_active_relationship(self):
        result = _client(lambda _request, **_kwargs: _Response({"relationships": [{
            "peerHandle": "bob", "status": "pending",
            "grant": {"communicate": True},
            "peerGrant": {"autoReply": True, "shareAvailability": True},
        }]})).list_relationships()

        self.assertEqual(result["relationships"][0], {"peer_handle": "bob", "status": "pending", "can_ask": False, "peer_auto_reply": False, "peer_share_availability": False})

    def test_relationship_readiness_fails_closed_for_missing_malformed_and_expired_grants(self):
        result = _client(lambda _request, **_kwargs: _Response({"relationships": [
            {"peerHandle": "missing", "status": "active"},
            {"peerHandle": "malformed", "status": "active", "grant": {"communicate": True, "expiresAt": "not-a-date"}, "peerGrant": {"autoReply": True, "shareAvailability": True, "expiresAt": "not-a-date"}},
            {"peerHandle": "expired", "status": "active", "grant": {"communicate": True, "expiresAt": "2026-01-01T00:00:00Z"}, "peerGrant": {"autoReply": True, "shareAvailability": True, "expiresAt": "2025-12-31T23:59:59Z"}},
        ]})).list_relationships()

        self.assertEqual(
            [row for row in result["relationships"]],
            [
                {"peer_handle": "missing", "status": "active", "can_ask": False, "peer_auto_reply": False, "peer_share_availability": False},
                {"peer_handle": "malformed", "status": "active", "can_ask": False, "peer_auto_reply": False, "peer_share_availability": False},
                {"peer_handle": "expired", "status": "active", "can_ask": False, "peer_auto_reply": False, "peer_share_availability": False},
            ],
        )

    def test_relationship_readiness_preserves_active_unexpired_and_nonexpiring_grants(self):
        result = _client(lambda _request, **_kwargs: _Response({"relationships": [
            {"peerHandle": "unexpired", "status": "active", "grant": {"communicate": True, "expiresAt": "2026-01-01T00:00:01Z"}, "peerGrant": {"autoReply": True, "shareAvailability": True, "expiresAt": "2026-01-01T00:00:01Z"}},
            {"peerHandle": "permanent", "status": "active", "grant": {"communicate": True}, "peerGrant": {"autoReply": True, "shareAvailability": True}},
        ]})).list_relationships()

        self.assertEqual(
            [(row["can_ask"], row["peer_auto_reply"], row["peer_share_availability"]) for row in result["relationships"]],
            [(True, True, True), (True, True, True)],
        )

    def test_specs_are_explicit_and_handlers_are_bound(self):
        calls = []
        registry = peer_registry(_client(lambda request, **_kwargs: calls.append(request) or _Response({"relationships": []})))
        schemas = {item["function"]["name"]: item["function"]["parameters"] for item in registry.schemas()}
        self.assertEqual(set(schemas), {"peer_list", "peer_ask", "peer_resume"})
        self.assertNotIn("call_id", schemas["peer_ask"]["properties"])
        self.assertEqual(schemas["peer_ask"]["properties"]["peer_handle"], {"type": "string", "pattern": "^[a-z0-9_]{3,32}$"})
        self.assertTrue(registry.resolve("peer_list").spec.read_only)
        self.assertTrue(registry.resolve("peer_list").spec.parallel_safe)
        self.assertFalse(registry.resolve("peer_ask").spec.read_only)
        self.assertFalse(registry.resolve("peer_ask").spec.parallel_safe)
        observation = registry.resolve("peer_list").invoke({})
        self.assertEqual(observation, {"ok": True, "status": "completed", "relationships": []})
        self.assertEqual(calls[0].full_url, "https://peer.example/v1/peer-agent/relationships")
        self.assertEqual(calls[0].get_method(), "GET")
        self.assertIsNone(calls[0].data)

    def test_client_sends_bound_headers_quotes_request_and_returns_only_observations(self):
        captured = {}
        def opener(request, **_kwargs):
            captured["request"] = request
            return _Response({"token": "leak", "ownerId": "owner-leak", "relationshipId": "r", "nested": {"secret": "leak", "value": "ok"}})
        result = _client(opener).inspect_request("request/with space")
        request = captured["request"]
        self.assertEqual(request.full_url, "https://peer.example/v1/peer-agent/requests/request%2Fwith%20space")
        self.assertIsNone(request.data)
        self.assertEqual(dict(request.header_items()), {"Authorization": "Bearer capability-secret", "Accept": "application/json", "X-tomo-owner-id": "owner", "X-tomo-actor-id": "actor", "X-tomo-destination": "telegram:chat", "X-tomo-session-id": "session", "X-tomo-generation-id": "generation"})
        self.assertEqual(result, {"ok": True, "status": "unknown"})

    def test_ask_posts_once_then_polls_to_terminal_or_safe_timeout(self):
        requests, times = [], iter([0, 0, 0.25, 0.25])
        responses = iter([
            {"relationships": [{"peerHandle": "bob", "status": "active", "grant": {"communicate": True}, "peerGrant": {"autoReply": True}}]},
            {"requestId": "request-1", "status": "pending"},
            {"requestId": "request-1", "status": "completed", "response": {"frames": ["answer"]}},
        ])
        registry = peer_registry(_client(lambda request, **_kwargs: requests.append(request) or _Response(next(responses)), clock=lambda: next(times)))
        registry.resolve("peer_list").invoke({})
        result = registry.resolve("peer_ask").invoke({"peer_handle": "bob", "purpose": "question", "disclosure_kind": "ordinary_message", "message": "hello", "call_id": "call"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual([request.full_url for request in requests], ["https://peer.example/v1/peer-agent/relationships", "https://peer.example/v1/peer-agent/requests", "https://peer.example/v1/peer-agent/requests/request-1"])
        self.assertEqual(json.loads(requests[1].data), {"peerHandle": "bob", "purpose": "question", "disclosureKind": "ordinary_message", "message": "hello", "callId": "call"})

        clock = iter([0, 60])
        pending_client = _client(lambda request, **_kwargs: _Response(
            {"relationships": [{"peerHandle": "bob", "status": "active", "grant": {"communicate": True}, "peerGrant": {"autoReply": True}}]}
            if request.get_method() == "GET" else {"requestId": "request-2", "threadId": "thread-2", "status": "pending"}
        ), clock=lambda: next(clock))
        pending_client.list_relationships()
        pending = pending_client.ask({"peer_handle": "bob", "purpose": "question", "disclosure_kind": "ordinary_message", "message": "hello", "call_id": "call"})
        self.assertEqual(pending, {"ok": True, "status": "pending", "thread_id": "thread-2"})

    def test_ask_requires_a_current_turn_ready_relationship_observation(self):
        requests = []
        client = _client(lambda request, **_kwargs: requests.append(request) or _Response({"relationships": [{"peerHandle": "bob", "status": "active", "grant": {"communicate": True}, "peerGrant": {"autoReply": True}}]}))
        ask = {"peer_handle": "bob", "purpose": "question", "disclosure_kind": "ordinary_message", "message": "hello", "call_id": "call"}

        self.assertEqual(client.ask(ask), {"ok": False, "status": "failed", "error_code": "peer_connection_unavailable"})
        self.assertEqual(requests, [])
        client.list_relationships()
        self.assertEqual(client.ask(ask | {"peer_handle": "guessed"}), {"ok": False, "status": "failed", "error_code": "peer_connection_unavailable"})
        self.assertEqual([request.get_method() for request in requests], ["GET"])

    def test_ask_rejects_ungrounded_owner_fact_before_network_submission(self):
        requests = []
        client = _client(lambda request, **_kwargs: requests.append(request) or _Response({
            "relationships": [{"peerHandle": "bob", "status": "active", "grant": {"communicate": True}, "peerGrant": {"autoReply": True}}]
        }))
        client.list_relationships()

        result = client.ask({"peer_handle": "bob", "purpose": "question", "disclosure_kind": "ordinary_message", "message": "what is bob's favorite color?", "call_id": "call"})

        self.assertEqual(result, {"ok": False, "status": "failed", "error_code": "peer_grounding_required"})
        self.assertEqual([request.get_method() for request in requests], ["GET"])

    def test_safe_worker_error_is_reduced_to_a_public_failure_category(self):
        result = _client(lambda _request, **_kwargs: _Response({
            "requestId": "private-request",
            "status": "failed",
            "errorCode": "sandbox_exec_failed",
            "token": "private-token",
        })).inspect_request("request")

        self.assertEqual(result, {
            "ok": True,
            "status": "failed",
            "error_code": "peer_unavailable",
        })

    def test_provider_stream_failure_is_reduced_to_peer_unavailable(self):
        result = _client(lambda _request, **_kwargs: _Response({
            "requestId": "private-request",
            "status": "failed",
            "errorCode": "provider_stream_failure",
        })).inspect_request("request")

        self.assertEqual(result, {
            "ok": True,
            "status": "failed",
            "error_code": "peer_unavailable",
        })

    def test_authority_grounding_failure_remains_a_public_evidence_boundary(self):
        result = _client(lambda _request, **_kwargs: _Response({
            "ok": False,
            "status": "failed",
            "errorCode": "peer_grounding_required",
        })).inspect_request("request")

        self.assertEqual(result, {
            "ok": False,
            "status": "failed",
            "error_code": "peer_grounding_required",
        })

    def test_ask_uses_only_an_exact_listed_ready_handle_and_replaces_the_cache(self):
        requests = []
        responses = iter([
            {"relationships": [
                {"peerHandle": "bob", "status": "active", "grant": {"communicate": True}, "peerGrant": {"autoReply": True}},
                {"peerHandle": "inactive", "status": "pending", "grant": {"communicate": True}, "peerGrant": {"autoReply": True}},
                {"peerHandle": "no_communicate", "status": "active", "grant": {}, "peerGrant": {"autoReply": True}},
                {"peerHandle": "no_auto_reply", "status": "active", "grant": {"communicate": True}, "peerGrant": {}},
            ]},
            {"requestId": "request-1", "status": "pending"},
            {"relationships": [{"peerHandle": "cora", "status": "active", "grant": {"communicate": True}, "peerGrant": {"autoReply": True}}]},
        ])
        client = _client(lambda request, **_kwargs: requests.append(request) or _Response(next(responses)), clock=iter([0, 60]).__next__)
        ask = lambda handle: client.ask({"peer_handle": handle, "purpose": "question", "disclosure_kind": "ordinary_message", "message": "hello", "call_id": handle})

        client.list_relationships()
        for handle in ("guessed", "inactive", "no_communicate", "no_auto_reply"):
            self.assertEqual(ask(handle), {"ok": False, "status": "failed", "error_code": "peer_connection_unavailable"})
        self.assertEqual(ask("bob"), {"ok": True, "status": "pending"})
        client.list_relationships()
        self.assertEqual(ask("bob"), {"ok": False, "status": "failed", "error_code": "peer_connection_unavailable"})
        self.assertEqual([request.get_method() for request in requests], ["GET", "POST", "GET"])

    def test_pending_confirmation_returns_only_safe_contract_with_expiry(self):
        responses = iter([
            {"relationships": [{"peerHandle": "bob", "status": "active", "grant": {"communicate": True}, "peerGrant": {"autoReply": True}}]},
            {"requestId": "request-1", "status": "confirmation_pending"},
            {
                "requestId": "request-1",
                "status": "confirmation_pending",
                "peerHandle": "bob",
                "threadId": "thread-1",
                "expiresAt": "2026-01-01T00:05:00+00:00",
                "pendingId": "secret-control-id",
            },
        ])
        times = iter([0, 0])
        client = _client(
            lambda _request, **_kwargs: _Response(next(responses)),
            clock=lambda: next(times),
        )
        client.list_relationships()
        result = client.ask({
            "peer_handle": "bob",
            "purpose": "question",
            "disclosure_kind": "sensitive",
            "message": "what exact calendar event is next?",
            "call_id": "call",
        })

        self.assertEqual(result, {
            "ok": True,
            "status": "confirmation_pending",
            "peer_handle": "bob",
            "thread_id": "thread-1",
            "expires_at": "2026-01-01T00:05:00+00:00",
        })

    def test_resume_returns_a_safe_thread_observation_without_request_identifiers(self):
        result = peer_registry(_client(lambda _request, **_kwargs: _Response({
            "requestId": "internal-request-id",
            "pendingId": "internal-pending-id",
            "status": "completed",
            "peerHandle": "bob",
            "threadId": "thread-1",
            "response": {"frames": ["answer"]},
        }))).resolve("peer_resume").invoke({"thread_id": "thread-1"})

        self.assertEqual(result, {"ok": True, "status": "completed", "peer_handle": "bob", "thread_id": "thread-1", "frames": ["answer"]})
