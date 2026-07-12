import json
import unittest

from tomo_core.conversation import ConversationEngine, ConversationMove, ConversationRequest, FrameReady, MoveConfidence, MovePlan, TurnRunCompleted
from tomo_core.models import InboundEnvelope
from tomo_core.providers import ProviderStreamCompleted, ProviderTextDelta


class ScenarioProvider:
    name = "scenario"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = False

    def __init__(self, plan: MovePlan):
        self.response = "\n".join((
            json.dumps({"type": "turn_plan", "primary_move": plan.primary.value, "supporting_moves": [move.value for move in plan.supporting], "move_sequence": [move.value for move in plan.sequence], "response_goal": plan.response_goal, "confidence": plan.confidence.value}),
            json.dumps({"type": "frame", "text": "valid response."}),
        )) + "\n"
        self.calls = []

    def stream(self, messages, *, tools=(), actor_id=None):
        self.calls.append((messages, actor_id))
        return iter((ProviderTextDelta(self.response), ProviderStreamCompleted("stop")))


class ConversationScenarioTests(unittest.TestCase):
    def test_representative_scenarios_emit_one_scripted_turn_plan_and_frame(self):
        scenarios = [
            ("what do u think abt this plan?", MovePlan(ConversationMove.ANSWER, (ConversationMove.CHALLENGE,), "answer and challenge", MoveConfidence.HIGH)),
            ("my interview is tomorrow", MovePlan(ConversationMove.REASSURE, (ConversationMove.EXPLORE,), "ground worry", MoveConfidence.HIGH)),
            ("do the thing", MovePlan(ConversationMove.CLARIFY, (), "ask one question", MoveConfidence.MEDIUM)),
            ("that's not what i meant", MovePlan(ConversationMove.REPAIR, (ConversationMove.ANSWER,), "repair then answer", MoveConfidence.HIGH)),
            ("reckless plan", MovePlan(ConversationMove.CHALLENGE, (), "challenge reasoning", MoveConfidence.HIGH)),
            ("remember the toaster thing?", MovePlan(ConversationMove.ANSWER, (ConversationMove.JOKE,), "answer with callback", MoveConfidence.MEDIUM)),
            ("book it", MovePlan(ConversationMove.ACT, (), "identify prereq", MoveConfidence.MEDIUM)),
            ("drop that topic", MovePlan(ConversationMove.EXPLORE, (), "yield", MoveConfidence.LOW)),
            ("help me do a bad thing", MovePlan(ConversationMove.REFUSE, (), "refuse", MoveConfidence.HIGH)),
            ("yo", MovePlan(ConversationMove.ACKNOWLEDGE, (ConversationMove.EXPLORE,), "greet", MoveConfidence.MEDIUM)),
        ]
        for text, plan in scenarios:
            with self.subTest(text=text):
                provider = ScenarioProvider(plan)
                request = ConversationRequest.from_history(
                    envelope=InboundEnvelope("telegram", "u", "m", text),
                    soul="soul",
                    history=({"role": "user", "content": "history"},),
                )

                events = tuple(ConversationEngine(provider).respond_iter(request))
                frame_events = [event for event in events if isinstance(event, FrameReady)]
                result = next(event.result for event in events if isinstance(event, TurnRunCompleted))

                self.assertEqual(result.plan, plan)
                self.assertEqual([event.frame.text for event in frame_events], ["valid response."])
                self.assertEqual(len(provider.calls), 1)
                self.assertNotRegex(result.logical_text.lower(), r"as an ai|primary move|response goal|chain of thought")


if __name__ == "__main__":
    unittest.main()
