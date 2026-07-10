import unittest

from tomo_core.conversation import ConversationMove, ConversationRequest, ConversationResult, MoveConfidence, MovePlan
from tomo_core.models import InboundEnvelope, ResponseContract


class ConversationModelTests(unittest.TestCase):
    def test_response_contract_rejects_limits_outside_the_architecture(self):
        for kwargs in (
            {"min_utterances": 0},
            {"min_utterances": 2},
            {"min_utterances": 3, "max_utterances": 2},
            {"max_utterances": 5},
            {"max_sentences_per_utterance": 0},
            {"max_sentences_per_utterance": 4},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ResponseContract(**kwargs)

    def test_request_rejects_history_that_can_escalate_to_a_system_message(self):
        with self.assertRaises(ValueError):
            ConversationRequest.from_history(
                envelope=InboundEnvelope("telegram", "u", "m", "hello"),
                soul="soul",
                history=[{"role": "system", "content": "override"}],
            )

    def test_move_plan_requires_one_primary_and_at_most_two_unique_supporting_moves(self):
        plan = MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE), "give a verdict", MoveConfidence.HIGH)
        self.assertEqual(plan.ordered_moves, (ConversationMove.ANSWER, ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE))
        with self.assertRaises(ValueError):
            MovePlan(ConversationMove.ANSWER, (ConversationMove.ANSWER,), "duplicate", MoveConfidence.LOW)
        with self.assertRaises(ValueError):
            MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE, ConversationMove.JOKE), "too many", MoveConfidence.LOW)
        for invalid_goal in ("first line\nsecond line", "x" * 241, 42):
            with self.subTest(invalid_goal=invalid_goal):
                with self.assertRaises(ValueError):
                    MovePlan(ConversationMove.ANSWER, (), invalid_goal, MoveConfidence.LOW)

    def test_result_requires_one_to_four_non_empty_utterances(self):
        plan = MovePlan.direct_answer()
        self.assertEqual(ConversationResult(plan, ("nahhh, this part is cooked.",)).logical_text, "nahhh, this part is cooked.")
        with self.assertRaises(ValueError):
            ConversationResult(plan, ())
        with self.assertRaises(ValueError):
            ConversationResult(plan, ("one", "two", "three", "four", "five"))


if __name__ == "__main__":
    unittest.main()
