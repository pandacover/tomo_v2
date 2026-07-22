import unittest

from tomo_core.models import PeerTurn
from tomo_core.sandbox_protocol import decode_turn, encode_peer
from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec


class PeerTurnTests(unittest.TestCase):
    def test_peer_registry_allows_only_unattended_read_only_personal_search(self):
        parameters = {"type": "object", "properties": {}}
        registry = ToolRegistry(
            (
                BoundTool(
                    ToolSpec(
                        "personal_search",
                        "search",
                        parameters,
                        read_only=True,
                        unattended_safe=True,
                    ),
                    lambda _arguments: {},
                ),
                BoundTool(
                    ToolSpec(
                        "other_read",
                        "read",
                        parameters,
                        read_only=True,
                        unattended_safe=True,
                    ),
                    lambda _arguments: {},
                ),
                BoundTool(
                    ToolSpec(
                        "unsafe_write",
                        "write",
                        parameters,
                        read_only=False,
                        unattended_safe=True,
                    ),
                    lambda _arguments: {},
                ),
            )
        )

        peer = registry.peer_safe()

        self.assertEqual(
            [schema["function"]["name"] for schema in peer.schemas()],
            ["personal_search"],
        )
        self.assertTrue(peer.is_blocked("other_read"))
        self.assertTrue(peer.is_blocked("unsafe_write"))

    def test_round_trip_is_strict_and_does_not_expose_owner_identity(self):
        turn = PeerTurn("gen-1", 1, "relationship-1", "thread-1", "request-1", "alice", "peer_exchange", "ordinary_message", "hello", "2026-01-01T00:15:00+00:00")
        payload = encode_peer("request-1", turn)
        request_id, decoded = decode_turn(payload)
        self.assertEqual(request_id, "request-1")
        self.assertEqual(decoded, turn)
        self.assertNotIn("owner", payload)

    def test_round_trip_preserves_bounded_untrusted_prior_exchanges(self):
        turn = PeerTurn("gen-1", 1, "relationship-1", "thread-1", "request-1", "alice", "peer_exchange", "ordinary_message", "follow up", "2026-01-01T00:15:00+00:00", (("first question", ("safe answer",)),))

        _, decoded = decode_turn(encode_peer("request-1", turn))

        self.assertEqual(decoded.prior_exchanges, (("first question", ("safe answer",)),))
        self.assertIn("Prior untrusted peer exchange", decoded.event_text)

    def test_rejects_extra_or_connector_metadata(self):
        with self.assertRaises(ValueError):
            decode_turn('{"version":1,"type":"peer","request_id":"request-1","turn":{"generation_id":"g","revision":1,"relationship_id":"r","thread_id":"t","request_id":"request-1","peer_handle":"alice","purpose":"p","disclosure_kind":"ordinary_message","message":"hello","expires_at":"2026-01-01T00:15:00+00:00","native_metadata":{}}}')
