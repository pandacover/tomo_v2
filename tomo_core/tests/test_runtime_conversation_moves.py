import tempfile
import unittest
from pathlib import Path

from tomo_core import InboundEnvelope, PersonalAgentRuntime, RuntimeConfig
from tomo_core.grok_auth import GrokAuthStore
from tomo_core.providers import GrokAuthProvider
from tomo_core.sessions import JsonSessionStore, StoredMessage
from tomo_core.telegram import FakeTelegramClient, TelegramDeliverySink


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


class RuntimeConversationMoveTests(unittest.TestCase):
    def test_runtime_uses_one_response_contract_for_prompt_parsing_and_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            provider = ScriptedProvider([
                '{"primary_move":"answer","supporting_moves":[],"response_goal":"answer","confidence":"high"}',
                '{"utterances":["one.","two.","three."]}',
                '{"utterances":["one.","two."]}',
            ])
            client = FakeTelegramClient()
            config = RuntimeConfig(
                data_dir=tmp,
                soul_path=str(soul_path),
                max_bubbles=2,
                max_sentences_per_bubble=1,
            )
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(client), config)

            bubbles = runtime.handle_telegram_text(InboundEnvelope("telegram", "user-1", "msg-1", "yo"))

            realization_contract = "\n".join(message["content"] for message in provider.calls[1][0])
            self.assertIn("1 to 2 non-empty utterances", realization_contract)
            self.assertEqual([bubble["text"] for bubble in bubbles], ["one.", "two."])
            self.assertEqual(len(provider.calls), 3)

    def test_missing_grok_auth_reaches_the_user_without_entering_structured_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            provider = GrokAuthProvider(GrokAuthStore(Path(tmp) / "missing-auth.json"))
            client = FakeTelegramClient()
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(client), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))

            bubbles = runtime.handle_telegram_text(InboundEnvelope("telegram", "user-1", "msg-1", "yo"))

            self.assertEqual([bubble["text"] for bubble in bubbles], ["run grok login or grok login --device-auth first, then restart me."])
            self.assertEqual(client.sent_messages[0]["reply_to_message_id"], "msg-1")

    def test_handle_telegram_text_persists_compact_move_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:user-1")
            session.append(StoredMessage("user", "old context"))
            store.save(session)
            provider = ScriptedProvider([
                '{"primary_move":"challenge","supporting_moves":["acknowledge"],"response_goal":"challenge the plan and give a better route","confidence":"high"}',
                '{"utterances":["i get why that plan looks tempting.","but the risky part is cooked, use the safer route first."]}',
            ])
            client = FakeTelegramClient()
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(client), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            bubbles = runtime.handle_telegram_text(InboundEnvelope("telegram", "user-1", "msg-9", "what do u think abt this plan?"))

            self.assertEqual(client.typing_actor_ids, ["user-1"])
            self.assertEqual([bubble["text"] for bubble in bubbles], ["i get why that plan looks tempting.", "but the risky part is cooked, use the safer route first."])
            self.assertEqual(bubbles[0]["reply_to_message_id"], "msg-9")
            selection_text = "\n".join(message["content"] for message in provider.calls[0][0])
            realization_text = "\n".join(message["content"] for message in provider.calls[1][0])
            self.assertIn("old context", selection_text)
            self.assertIn("SOUL SENTINEL", selection_text)
            self.assertIn("challenge", realization_text)
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([m.role for m in session.messages], ["user", "user", "assistant"])
            assistant = session.messages[-1]
            self.assertEqual(assistant.content, "i get why that plan looks tempting. but the risky part is cooked, use the safer route first.")
            self.assertEqual(assistant.metadata["conversation"], {"primary_move": "challenge", "supporting_moves": ["acknowledge"], "response_goal": "challenge the plan and give a better route", "confidence": "high"})
            self.assertEqual(assistant.metadata["delivery_bubbles"], ["i get why that plan looks tempting.", "but the risky part is cooked, use the safer route first."])
            metadata_text = str(assistant.metadata)
            self.assertNotIn("chain_of_thought", metadata_text)
            self.assertNotIn("primary_move\":", metadata_text)
            self.assertNotIn("access_token", metadata_text)


if __name__ == "__main__":
    unittest.main()
