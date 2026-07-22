import unittest
from datetime import datetime, timezone

from tomo_core.peer_models import Handle, PeerRequest, RequestKind


class PeerModelTests(unittest.TestCase):
    def test_handle_is_normalized_without_owner_disclosure(self):
        self.assertEqual(Handle("  Alice_7 ").value, "alice_7")
        with self.assertRaises(ValueError):
            Handle("no")

    def test_ask_rejects_boolean_counts_and_naive_times(self):
        with self.assertRaises(ValueError):
            PeerRequest("id", "r", "a", "b", "thread", "generation", "call", RequestKind.ORDINARY_MESSAGE, "ask", "hello", True, datetime.now())
        ask = PeerRequest("id", "r", "a", "b", "thread", "generation", "call", RequestKind.ORDINARY_MESSAGE, "ask", "hello", 1, datetime.now(timezone.utc))
        self.assertEqual(ask.thread_sequence, 1)
