import json
import unittest

from tomo_core.peer_tools import PeerApiClient, peer_registry


class _Response:
    def __init__(self, payload): self.payload = payload
    def read(self, size=-1): return json.dumps(self.payload).encode()[:size if size >= 0 else None]
    def __enter__(self): return self
    def __exit__(self, *args): pass


def _client(opener, *, sleeper=lambda _seconds: None, clock=lambda: 0):
    return PeerApiClient("https://peer.example", "capability-secret", "owner", "actor", "telegram:chat", "session", "generation", opener=opener, sleeper=sleeper, clock=clock)


class PeerToolsTests(unittest.TestCase):
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
        responses = iter([{"requestId": "request-1", "status": "pending"}, {"requestId": "request-1", "status": "completed", "response": {"frames": ["answer"]}}])
        result = peer_registry(_client(lambda request, **_kwargs: requests.append(request) or _Response(next(responses)), clock=lambda: next(times))).resolve("peer_ask").invoke({"peer_handle": "bob", "purpose": "question", "disclosure_kind": "ordinary_message", "message": "hello", "call_id": "call"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual([request.full_url for request in requests], ["https://peer.example/v1/peer-agent/requests", "https://peer.example/v1/peer-agent/requests/request-1"])
        self.assertEqual(json.loads(requests[0].data), {"peerHandle": "bob", "purpose": "question", "disclosureKind": "ordinary_message", "message": "hello", "callId": "call"})

        clock = iter([0, 60])
        pending = _client(lambda _request, **_kwargs: _Response({"requestId": "request-2", "threadId": "thread-2", "status": "pending"}), clock=lambda: next(clock)).ask({"peer_handle": "bob", "purpose": "question", "disclosure_kind": "ordinary_message", "message": "hello", "call_id": "call"})
        self.assertEqual(pending, {"ok": True, "status": "pending", "thread_id": "thread-2"})

    def test_pending_confirmation_returns_only_safe_contract_with_expiry(self):
        responses = iter([
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
        result = _client(
            lambda _request, **_kwargs: _Response(next(responses)),
            clock=lambda: next(times),
        ).ask({
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
