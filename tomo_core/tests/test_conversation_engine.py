import unittest

from tomo_core.conversation import ConversationCompleted, ConversationEngine, ConversationMove, ConversationRequest, ConversationStarted, UtteranceReady
from tomo_core.models import InboundEnvelope
from tomo_core.providers import ProviderSetupRequired


class ScriptedProvider:
    name = "scripted"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, actor_id=None):
        self.calls.append((messages, actor_id))
        return self.responses.pop(0)


class ConversationEngineTests(unittest.TestCase):
    def request(self):
        return ConversationRequest.from_history(
            envelope=InboundEnvelope("telegram", "u1", "m1", "my interview is tomorrow"),
            soul="SOUL SENTINEL",
            history=[{"role": "user", "content": "i need this job"}],
        )

    def test_respond_selects_moves_then_realizes_only_the_selected_plan(self):
        provider = ScriptedProvider(['{"primary_move":"reassure","supporting_moves":["explore"],"response_goal":"ground fear","confidence":"high"}', '{"utterances":["yeah, tomorrow matters.","how prepared do u actually feel rn?"]}'])
        result = ConversationEngine(provider).respond(self.request())
        self.assertEqual(result.plan.primary, ConversationMove.REASSURE)
        self.assertEqual(result.plan.supporting, (ConversationMove.EXPLORE,))
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([call[1] for call in provider.calls], ["u1", "u1"])
        realization_text = "\n".join(message["content"] for message in provider.calls[1][0])
        self.assertIn("SOUL SENTINEL", realization_text)
        self.assertIn("reassure", realization_text)
        self.assertNotIn("## challenge", realization_text)

    def test_invalid_selection_falls_back_without_a_third_normal_path_call(self):
        provider = ScriptedProvider(["not json", '{"utterances":["straight answer."]}'])
        result = ConversationEngine(provider).respond(self.request())
        self.assertEqual(result.plan.primary, ConversationMove.ANSWER)
        self.assertEqual(result.utterances, ("straight answer.",))
        self.assertEqual(len(provider.calls), 2)

    def test_invalid_realization_is_repaired_exactly_once(self):
        provider = ScriptedProvider(['{"primary_move":"answer","supporting_moves":[],"response_goal":"answer directly","confidence":"high"}', "not json", '{"utterances":["fixed answer."]}'])
        self.assertEqual(ConversationEngine(provider).respond(self.request()).utterances, ("fixed answer.",))
        self.assertEqual(len(provider.calls), 3)

    def test_second_invalid_realization_fails_with_safe_code(self):
        provider = ScriptedProvider(['{"primary_move":"answer","supporting_moves":[],"response_goal":"answer directly","confidence":"high"}', "not json", "still not json"])
        with self.assertRaisesRegex(ValueError, "invalid conversation output"):
            ConversationEngine(provider).respond(self.request())

    def test_provider_setup_guidance_bypasses_the_model_contract_safely(self):
        class SetupRequiredProvider(ScriptedProvider):
            def complete(self, messages, actor_id=None):
                self.calls.append((messages, actor_id))
                raise ProviderSetupRequired("use /connect to connect supergrok oauth first.")

        provider = SetupRequiredProvider([])
        result = ConversationEngine(provider).respond(self.request())

        self.assertEqual(result.plan.primary, ConversationMove.ANSWER)
        self.assertEqual(result.utterances, ("use /connect to connect supergrok oauth first.",))
        self.assertEqual(len(provider.calls), 1)

    def test_respond_iter_yields_each_move_before_realizing_the_next(self):
        provider = ScriptedProvider([
            '{"primary_move":"answer","supporting_moves":["acknowledge"],"move_sequence":["acknowledge","answer"],"response_goal":"answer","confidence":"high"}',
            '{"utterance":"got you."}',
            '{"utterance":"the answer."}',
        ])
        iterator = ConversationEngine(provider).respond_iter(self.request())

        self.assertIsInstance(next(iterator), ConversationStarted)
        first = next(iterator)
        self.assertEqual(first, UtteranceReady(0, ConversationMove.ACKNOWLEDGE, "got you."))
        self.assertEqual(len(provider.calls), 2)
        second = next(iterator)
        self.assertEqual(second, UtteranceReady(1, ConversationMove.ANSWER, "the answer."))
        self.assertIn("got you.", "\n".join(message["content"] for message in provider.calls[2][0]))
        self.assertIsInstance(next(iterator), ConversationCompleted)

    def test_respond_iter_sequences_current_user_before_same_turn_utterances(self):
        provider = ScriptedProvider([
            '{"primary_move":"answer","supporting_moves":["acknowledge","explore"],"move_sequence":["acknowledge","explore","answer"],"response_goal":"answer","confidence":"high"}',
            '{"utterance":"t1"}',
            '{"utterance":"t2"}',
            '{"utterance":"t3"}',
        ])
        current_user = {
            "role": "user",
            "content": "current u1",
        }
        request = ConversationRequest.from_history(
            envelope=InboundEnvelope("telegram", "actor", "m1", "current u1", "2026-01-01T00:00:00+00:00"),
            soul="SOUL SENTINEL",
            history=[],
        )

        list(ConversationEngine(provider).respond_iter(request))

        self.assertEqual(provider.calls[1][0][-1:], [current_user])
        self.assertEqual(
            provider.calls[2][0][-2:],
            [
                current_user,
                {"role": "assistant", "content": "t1"},
            ],
        )
        self.assertEqual(
            provider.calls[3][0][-3:],
            [
                current_user,
                {"role": "assistant", "content": "t1"},
                {"role": "assistant", "content": "t2"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
