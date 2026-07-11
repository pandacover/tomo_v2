import unittest

from tomo_core.conversation import ConversationCompleted, ConversationMove, ConversationRequest, ConversationResult, ConversationStarted, MoveConfidence, MovePlan, UtteranceReady
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
        plan = MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE), "give a verdict", MoveConfidence.HIGH, (ConversationMove.ACKNOWLEDGE, ConversationMove.ANSWER, ConversationMove.EXPLORE))
        self.assertEqual(plan.ordered_moves, (ConversationMove.ACKNOWLEDGE, ConversationMove.ANSWER, ConversationMove.EXPLORE))
        with self.assertRaises(ValueError):
            MovePlan(ConversationMove.ANSWER, (ConversationMove.ANSWER,), "duplicate", MoveConfidence.LOW)
        with self.assertRaises(ValueError):
            MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE, ConversationMove.EXPLORE, ConversationMove.JOKE), "too many", MoveConfidence.LOW)
        with self.assertRaises(ValueError):
            MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE,), "missing primary", MoveConfidence.LOW, (ConversationMove.ACKNOWLEDGE,))
        for invalid_goal in ("first line\nsecond line", "x" * 241, 42):
            with self.subTest(invalid_goal=invalid_goal):
                with self.assertRaises(ValueError):
                    MovePlan(ConversationMove.ANSWER, (), invalid_goal, MoveConfidence.LOW)

    def test_progressive_events_validate_user_visible_steps(self):
        plan = MovePlan(ConversationMove.ANSWER, (ConversationMove.ACKNOWLEDGE,), "answer", MoveConfidence.HIGH, (ConversationMove.ACKNOWLEDGE, ConversationMove.ANSWER))
        result = ConversationResult(plan, ("got you.", "the answer."))

        self.assertEqual(ConversationStarted(plan).plan, plan)
        self.assertEqual(UtteranceReady(0, ConversationMove.ACKNOWLEDGE, "got you.").text, "got you.")
        self.assertEqual(ConversationCompleted(result).result, result)
        with self.assertRaises(ValueError):
            UtteranceReady(-1, ConversationMove.ANSWER, "bad")
        with self.assertRaises(ValueError):
            UtteranceReady(0, ConversationMove.ANSWER, " ")
        with self.assertRaises(ValueError):
            ConversationCompleted(None)

    def test_result_requires_one_to_four_non_empty_utterances(self):
        plan = MovePlan.direct_answer()
        self.assertEqual(ConversationResult(plan, ("nahhh, this part is cooked.",)).logical_text, "nahhh, this part is cooked.")
        with self.assertRaises(ValueError):
            ConversationResult(plan, ())
        with self.assertRaises(ValueError):
            ConversationResult(plan, ("one", "two", "three", "four", "five"))


if __name__ == "__main__":
    unittest.main()
