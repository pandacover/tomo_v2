import unittest

from tomo_core.conversation.models import ConversationMove, MoveConfidence
from tomo_core.conversation.parsing import _parse_memory_control_payload, _parse_strict_move_plan_payload


class ConversationParsingTests(unittest.TestCase):
    def test_strict_move_plan_payload_accepts_only_framing_schema(self):
        plan = _parse_strict_move_plan_payload({"primary_move":"challenge","supporting_moves":["acknowledge","explore"],"move_sequence":["acknowledge","challenge","explore"],"response_goal":"challenge the assumption","confidence":"high"})
        self.assertEqual(plan.primary, ConversationMove.CHALLENGE)
        self.assertEqual(plan.supporting, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE))
        self.assertEqual(plan.sequence, (ConversationMove.ACKNOWLEDGE, ConversationMove.CHALLENGE, ConversationMove.EXPLORE))
        self.assertEqual(plan.confidence, MoveConfidence.HIGH)

    def test_strict_move_plan_payload_rejects_unknown_or_invalid_values(self):
        for payload in (
            {"primary_move": "answer", "chain_of_thought": "hidden"},
            {"primary_move": "answer", "supporting_moves": [], "response_goal": True, "confidence": "high"},
            {"primary_move": "answer", "supporting_moves": [], "response_goal": "first\nsecond", "confidence": "high"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _parse_strict_move_plan_payload(payload)

    def test_reaction_intent_is_optional_and_nonfatal(self):
        for value, expected in (("👍", "👍"), (None, None), ("🪿", None), ("not-an-emoji", None), ({"emoji": "👍"}, None)):
            with self.subTest(value=value):
                plan = _parse_strict_move_plan_payload({
                    "primary_move": "answer", "supporting_moves": [], "response_goal": "answer", "confidence": "high", "reaction": value,
                })
                self.assertEqual(plan.reaction.emoji if plan.reaction else None, expected)

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
