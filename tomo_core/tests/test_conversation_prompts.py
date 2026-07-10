import unittest

from tomo_core.conversation.models import ConversationMove, MoveConfidence, MovePlan
from tomo_core.conversation.prompts import build_move_selection_messages, build_realization_messages, build_repair_messages
from tomo_core.models import InboundEnvelope, ResponseContract


class ConversationPromptTests(unittest.TestCase):
    def setUp(self):
        self.envelope = InboundEnvelope("telegram", "user-1", "message-1", "my interview is tomorrow")
        self.soul = "TOMO SOUL SENTINEL"
        self.history = ({"role": "user", "content": "i really need this job"},)

    def test_selection_receives_full_soul_history_message_and_closed_schema(self):
        messages = build_move_selection_messages(self.soul, self.history, self.envelope)
        joined = "\n".join(message["content"] for message in messages)
        self.assertIn(self.soul, joined)
        self.assertIn("i really need this job", joined)
        self.assertIn("my interview is tomorrow", joined)
        self.assertIn('"primary_move"', joined)
        self.assertIn("do not output chain-of-thought", joined)
        self.assertEqual([message["role"] for message in messages], ["system", "user", "user"])

    def test_realization_receives_only_selected_procedures_and_utterance_contract(self):
        plan = MovePlan(ConversationMove.REASSURE, (ConversationMove.EXPLORE,), "ground concern", MoveConfidence.HIGH)
        messages = build_realization_messages(self.soul, self.history, self.envelope, plan)
        joined = "\n".join(message["content"] for message in messages)
        self.assertIn("reassure", joined)
        self.assertIn("explore", joined)
        self.assertNotIn("## challenge", joined)
        self.assertIn("1 to 4", joined)
        self.assertIn("at most 3 sentences", joined)
        self.assertIn("never mention moves", joined)
        self.assertNotIn("ground concern", joined)
        self.assertEqual([message["role"] for message in messages], ["system", "user", "user"])

    def test_realization_contract_uses_the_runtime_limits(self):
        plan = MovePlan.direct_answer()
        contract = ResponseContract(max_utterances=2, max_sentences_per_utterance=1)
        messages = build_realization_messages(self.soul, self.history, self.envelope, plan, contract)
        joined = "\n".join(message["content"] for message in messages)
        self.assertIn("1 to 2 non-empty utterances", joined)
        self.assertIn("at most 1 sentence", joined)

    def test_repair_prompt_keeps_raw_output_in_the_assistant_role(self):
        messages = build_repair_messages([{"role": "system", "content": "contract"}], "secret raw", "invalid_json")
        self.assertEqual(messages[-2], {"role": "assistant", "content": "secret raw"})
        self.assertIn("invalid_json", messages[-1]["content"])
        self.assertNotIn("secret raw", messages[-1]["content"])

    def test_repair_prompt_bounds_the_invalid_assistant_output(self):
        messages = build_repair_messages([{"role": "system", "content": "contract"}], "x" * 9000, "invalid_shape")
        self.assertEqual(messages[-2]["role"], "assistant")
        self.assertEqual(len(messages[-2]["content"]), 8000)


if __name__ == "__main__":
    unittest.main()
