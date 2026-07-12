import unittest

from tomo_core.conversation.models import ConversationMove, MoveConfidence
from tomo_core.conversation.parsing import _parse_strict_move_plan_payload


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


if __name__ == "__main__":
    unittest.main()
