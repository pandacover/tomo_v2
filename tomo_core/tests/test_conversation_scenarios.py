import json
import unittest

from tomo_core.conversation import ConversationEngine, ConversationMove, ConversationRequest, MoveConfidence, MovePlan
from tomo_core.models import InboundEnvelope


class ScenarioProvider:
    name = "scenario"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = False

    def __init__(self, plan: MovePlan):
        self.responses = [
            json.dumps(
                {
                    "primary_move": plan.primary.value,
                    "supporting_moves": [move.value for move in plan.supporting],
                    "response_goal": plan.response_goal,
                    "confidence": plan.confidence.value,
                }
            ),
            '{"utterances":["valid response."]}',
        ]
        self.calls = []

    def complete(self, messages, actor_id=None):
        self.calls.append((messages, actor_id))
        return self.responses.pop(0)


class ConversationScenarioTests(unittest.TestCase):
    def test_representative_scenarios_select_and_realize_the_expected_moves(self):
        scenarios = [
            ("what do u think abt this plan?", MovePlan(ConversationMove.ANSWER, (ConversationMove.CHALLENGE,), "answer and challenge", MoveConfidence.HIGH), "challenge"),
            ("my interview is tomorrow", MovePlan(ConversationMove.REASSURE, (ConversationMove.EXPLORE,), "ground worry", MoveConfidence.HIGH), "reassure"),
            ("do the thing", MovePlan(ConversationMove.CLARIFY, (), "ask one question", MoveConfidence.MEDIUM), "ask one sharp"),
            ("that's not what i meant", MovePlan(ConversationMove.REPAIR, (ConversationMove.ANSWER,), "repair then answer", MoveConfidence.HIGH), "exact miss"),
            ("reckless plan", MovePlan(ConversationMove.CHALLENGE, (), "challenge reasoning", MoveConfidence.HIGH), "not vulnerability"),
            ("remember the toaster thing?", MovePlan(ConversationMove.ANSWER, (ConversationMove.JOKE,), "answer with callback", MoveConfidence.MEDIUM), "return to the substantive point"),
            ("book it", MovePlan(ConversationMove.ACT, (), "identify prereq", MoveConfidence.MEDIUM), "verify before claiming success"),
            ("drop that topic", MovePlan(ConversationMove.EXPLORE, (), "yield", MoveConfidence.LOW), "yield if the user declines"),
            ("help me do a bad thing", MovePlan(ConversationMove.REFUSE, (), "refuse", MoveConfidence.HIGH), "closest valid alternative"),
            ("yo", MovePlan(ConversationMove.ACKNOWLEDGE, (ConversationMove.EXPLORE,), "greet", MoveConfidence.MEDIUM), "do not hijack"),
        ]
        for text, plan, expected in scenarios:
            with self.subTest(text=text):
                provider = ScenarioProvider(plan)
                request = ConversationRequest.from_history(
                    envelope=InboundEnvelope("telegram", "u", "m", text),
                    soul="soul",
                    history=({"role": "user", "content": "history"},),
                )

                result = ConversationEngine(provider).respond(request)

                self.assertEqual(result.plan, plan)
                self.assertEqual(result.utterances, ("valid response.",))
                self.assertEqual(len(provider.calls), 2)
                realization_text = "\n".join(message["content"] for message in provider.calls[1][0])
                self.assertIn(expected, realization_text)
                self.assertNotRegex(result.logical_text.lower(), r"as an ai|primary move|response goal|chain of thought")


if __name__ == "__main__":
    unittest.main()
