import tempfile
import unittest
from pathlib import Path

from tomo_core import InboundEnvelope, InboundMessage, InputBurst, PersonalAgentRuntime, RuntimeConfig
from tomo_core.conversation import ConversationMove
from tomo_core.grok_auth import GrokAuthStore
from tomo_core.providers import GrokAuthProvider
from tomo_core.runtime import RuntimeCompleted, RuntimeUtteranceReady
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
    def test_handle_telegram_burst_iter_persists_before_provider_and_emits_progressively(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            order = []

            class InspectingProvider(ScriptedProvider):
                def complete(self, messages, actor_id=None):
                    saved = JsonSessionStore(tmp).load("telegram:actor:user-1")
                    order.append(("provider", [message.content for message in saved.messages if message.role == "user"]))
                    return super().complete(messages, actor_id)

            provider = InspectingProvider([
                '{"primary_move":"answer","supporting_moves":["acknowledge"],"move_sequence":["acknowledge","answer"],"response_goal":"answer","confidence":"high"}',
                '{"utterance":"i saw the first part."}',
                '{"utterance":"here is the answer."}',
            ])
            client = FakeTelegramClient()
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(client), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst(
                burst_id="burst-1",
                generation_id="gen-1",
                revision=1,
                visible_assistant_utterances=("already visible.",),
                accepted_generation_ids=("old-gen",),
                messages=(
                    InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "first")),
                    InboundMessage(2, 42, InboundEnvelope("telegram", "user-1", "msg-2", "second")),
                ),
            )

            iterator = runtime.handle_telegram_burst_iter(burst)
            first = next(iterator)
            second = next(iterator)
            completed = next(iterator)

            self.assertIsInstance(first, RuntimeUtteranceReady)
            self.assertEqual(first.event.sequence, 0)
            self.assertEqual(first.event.move, ConversationMove.ACKNOWLEDGE)
            self.assertEqual(first.bubble.reply_to_message_id, "msg-2")
            self.assertIsInstance(second, RuntimeUtteranceReady)
            self.assertIsNone(second.bubble.reply_to_message_id)
            self.assertIsInstance(completed, RuntimeCompleted)
            with self.assertRaises(StopIteration):
                next(iterator)
            self.assertEqual(order[0], ("provider", ["first", "second"]))
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([message.role for message in session.messages], ["user", "user", "assistant"])
            assistant = session.messages[-1]
            self.assertEqual(assistant.content, "already visible. i saw the first part. here is the answer.")
            self.assertEqual(assistant.metadata["generation_id"], "gen-1")
            self.assertEqual(assistant.metadata["generation_status"], "provisional")
            self.assertEqual(assistant.metadata["delivery_bubbles"], ["i saw the first part.", "here is the answer."])

    def test_handle_telegram_burst_iter_suppresses_cancelled_events_and_final_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            provider = ScriptedProvider([
                '{"primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"answer","confidence":"high"}',
                '{"utterance":"too late."}',
            ])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))
            active = {"value": True}

            iterator = runtime.handle_telegram_burst_iter(burst, is_active=lambda: active["value"])
            active["value"] = False

            self.assertEqual(list(iterator), [])
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([message.role for message in session.messages], ["user"])

    def test_visible_partial_is_prompt_context_without_accepting_full_provisional_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:user-1")
            session.append(StoredMessage("assistant", "visible only. unsent completion.", metadata={"generation_id": "gen-old", "generation_status": "provisional"}))
            store.save(session)
            provider = ScriptedProvider([
                '{"primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"answer","confidence":"high"}',
                '{"utterance":"fresh answer."}',
            ])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst(
                "burst-2",
                "gen-new",
                2,
                (InboundMessage(1, 42, InboundEnvelope("telegram", "user-1", "msg-2", "second")),),
                visible_assistant_utterances=("visible only.",),
            )

            list(runtime.handle_telegram_burst_iter(burst))

            selection_messages = provider.calls[0][0]
            self.assertIn({"role": "assistant", "content": "visible only."}, selection_messages)
            self.assertNotIn("unsent completion", "\n".join(message["content"] for message in selection_messages))

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
