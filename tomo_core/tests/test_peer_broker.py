import unittest

from tomo_core.peer_broker import canonicalize_availability


class PeerBrokerTests(unittest.TestCase):
    def test_canonicalizes_only_exact_selected_handle_availability_forms(self):
        for message, expected in (
            ("when is bob free?", "when are you free?"),
            ("when is @BoB free this friday afternoon?", "when are you free this friday afternoon?"),
            ("is bob available tomorrow?", "are you available tomorrow?"),
            ("bob's availability tomorrow", "your availability tomorrow"),
        ):
            with self.subTest(message=message):
                self.assertEqual(canonicalize_availability("bob", message), expected)

    def test_canonicalizer_leaves_direct_or_nonmatching_subjects_unchanged(self):
        for message in (
            "when are you free this friday afternoon?",
            "when is alice free?",
            "when is bob's sister free?",
            "when is carol available tomorrow?",
            "alice's availability tomorrow",
        ):
            with self.subTest(message=message):
                self.assertEqual(canonicalize_availability("bob", message), message)
