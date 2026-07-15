import unittest

from tomo_core.conversation.parsing import _parse_memory_control_payload


class ConversationParsingTests(unittest.TestCase):
    def test_memory_write_control_requires_exact_bounded_schema(self):
        payload = {
            "action": "upsert", "authority": "autonomous", "user_intent_excerpt": None, "memory_id": None,
            "kind": "project_context", "subject_key": "self", "topic": "projects.tomo", "value": {"name": "Tomo"},
            "statement": "Tomo is a personal agent", "confidence": 1.0, "salience": 0.9, "surface_scope": "always",
            "valid_from": None, "valid_until": None,
            "sources": [{"source_kind": "current_message", "source_id": "m1", "observed_at": "2026-07-13T00:00:00Z"}],
        }
        control = _parse_memory_control_payload(payload)
        self.assertEqual(control.kind, "project_context")
        self.assertEqual(control.sources[0].source_id, "m1")
        for invalid in (
            {**payload, "extra": True},
            {**payload, "confidence": 1.1},
            {**payload, "sources": []},
            {**payload, "sources": [{"source_kind": "current_message", "source_id": "m1", "observed_at": None}]},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _parse_memory_control_payload(invalid)


if __name__ == "__main__":
    unittest.main()
