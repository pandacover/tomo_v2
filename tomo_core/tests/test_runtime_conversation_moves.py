import tempfile
import unittest
from pathlib import Path

from tomo_core import InboundEnvelope, InboundMessage, InputBurst, PersonalAgentRuntime, RuntimeConfig
from tomo_core.models import MessageAttachment
from tomo_core.conversation import FrameReady
from tomo_core.grok_auth import GrokAuthStore
from tomo_core.providers import GrokAuthProvider, ProviderStreamCompleted, ProviderTextDelta
from tomo_core.runtime import RuntimeCompleted, RuntimeFrameReady
from tomo_core.sessions import JsonSessionStore, StoredMessage
from tomo_core.telegram import FakeTelegramClient, TelegramDeliverySink


class ScriptedProvider:
    name = "scripted"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = False

    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []

    def stream(self, messages, *, tools=(), actor_id=None):
        self.calls.append((messages, tools, actor_id))
        return iter(self.streams.pop(0))


PLAN = '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"answer","confidence":"high"}\n'


def stream(*frames, finish_reason="stop", input_tokens=11, output_tokens=7):
    payload = PLAN + "".join(f'{{"type":"frame","text":"{frame}"}}\n' for frame in frames)
    return [ProviderTextDelta(payload), ProviderStreamCompleted(finish_reason, input_tokens, output_tokens)]


class RuntimeConversationMoveTests(unittest.TestCase):
    def test_handle_telegram_burst_iter_persists_before_provider_and_emits_progressively(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            order = []

            class InspectingProvider(ScriptedProvider):
                def stream(self, messages, *, tools=(), actor_id=None):
                    saved = JsonSessionStore(tmp).load("telegram:actor:user-1")
                    order.append(("provider", [message.content for message in saved.messages if message.role == "user"]))
                    return super().stream(messages, tools=tools, actor_id=actor_id)

            provider = InspectingProvider([stream("i saw the first part.", "here is the answer.")])
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

            self.assertIsInstance(first, RuntimeFrameReady)
            self.assertEqual(first.event.sequence, 0)
            self.assertIsInstance(first.event, FrameReady)
            self.assertEqual(first.bubble.reply_to_message_id, "msg-2")
            self.assertIsInstance(second, RuntimeFrameReady)
            self.assertIsNone(second.bubble.reply_to_message_id)
            self.assertIsInstance(completed, RuntimeCompleted)
            with self.assertRaises(StopIteration):
                next(iterator)
            self.assertEqual(order[0], ("provider", ["first", "second"]))
            self.assertEqual(provider.calls[0][1], ())
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([message.role for message in session.messages], ["user", "user", "assistant"])
            assistant = session.messages[-1]
            self.assertEqual(assistant.content, "already visible. i saw the first part. here is the answer.")
            self.assertEqual(assistant.metadata["generation_id"], "gen-1")
            self.assertEqual(assistant.metadata["generation_status"], "provisional")
            self.assertEqual(assistant.metadata["delivery_bubbles"], ["i saw the first part.", "here is the answer."])
            self.assertEqual(assistant.metadata["frames"], [
                {"segment_index": 0, "frame_index": 0, "text": "i saw the first part."},
                {"segment_index": 0, "frame_index": 1, "text": "here is the answer."},
            ])
            self.assertEqual(assistant.metadata["usage"], {"model_segments": 1, "tool_rounds": 0, "tool_calls": 0, "visible_segments": 1, "contract_repairs": 0, "input_tokens": 11, "output_tokens": 7})
            self.assertNotIn("arguments", str(assistant.metadata))
            self.assertNotIn("observation", str(assistant.metadata))

    def test_completed_partial_persists_visible_frames_as_one_logical_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider([[ProviderTextDelta(PLAN + '{"type":"frame","text":"visible before interruption."}\n')]])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))

            events = list(runtime.handle_telegram_burst_iter(burst))

            self.assertIsInstance(events[0], RuntimeFrameReady)
            self.assertIsInstance(events[-1], RuntimeCompleted)
            self.assertEqual(events[-1].event.result.status.value, "completed_partial")
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([message.content for message in session.messages], ["go", "visible before interruption."])
            self.assertEqual(session.messages[-1].metadata["turn_status"], "completed_partial")

    def test_cancellation_after_visible_frame_does_not_persist_a_final_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            runtime = PersonalAgentRuntime(ScriptedProvider([stream("visible.")]), TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))
            active = {"value": True}
            iterator = runtime.handle_telegram_burst_iter(burst, is_active=lambda: active["value"])

            self.assertIsInstance(next(iterator), RuntimeFrameReady)
            active["value"] = False
            self.assertEqual(list(iterator), [])
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([message.role for message in session.messages], ["user"])

    def test_cancellation_after_completion_arrives_before_persistence_creates_no_assistant_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            runtime = PersonalAgentRuntime(ScriptedProvider([stream("visible.")]), TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))
            active = {"value": True}
            original = runtime._persist_completed_burst

            def cancel_before_persist(*args):
                active["value"] = False
                original(*args)

            runtime._persist_completed_burst = cancel_before_persist

            events = list(runtime.handle_telegram_burst_iter(burst, is_active=lambda: active["value"]))

            self.assertEqual([type(event) for event in events], [RuntimeFrameReady])
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([message.role for message in session.messages], ["user"])

    def test_handle_telegram_burst_iter_suppresses_cancelled_events_and_final_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            provider = ScriptedProvider([stream("too late.")])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))
            active = {"value": True}

            iterator = runtime.handle_telegram_burst_iter(burst, is_active=lambda: active["value"])
            active["value"] = False

            self.assertEqual(list(iterator), [])
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([message.role for message in session.messages], ["user"])

    def test_handle_telegram_burst_iter_stops_before_next_provider_call_when_inactive_after_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            provider = ScriptedProvider([stream("should not run.")])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))
            calls = {"count": 0}

            def is_active():
                calls["count"] += 1
                return calls["count"] < 4

            self.assertEqual(list(runtime.handle_telegram_burst_iter(burst, is_active=is_active)), [])
            self.assertEqual(len(provider.calls), 0)

    def test_inbound_attachment_metadata_is_persisted_for_photo_only_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            provider = ScriptedProvider([stream("i can work from the image context.")])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-photo", "gen-photo", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "", attachments=(MessageAttachment("image", file_id="photo-id", mime_type="image/jpeg", metadata={"width": 100}),))),))

            list(runtime.handle_telegram_burst_iter(burst))

            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual(session.messages[0].metadata["attachments"][0]["file_id"], "photo-id")
            self.assertEqual(session.messages[0].metadata["attachments"][0]["metadata"], {"width": 100})

    def test_visible_partial_is_prompt_context_without_accepting_full_provisional_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            store = JsonSessionStore(tmp)
            session = store.load("telegram:actor:user-1")
            session.append(StoredMessage("assistant", "visible only. unsent completion.", metadata={"generation_id": "gen-old", "generation_status": "provisional"}))
            store.save(session)
            provider = ScriptedProvider([stream("fresh answer.")])
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
            provider = ScriptedProvider([stream("one.", "two.")])
            client = FakeTelegramClient()
            config = RuntimeConfig(
                data_dir=tmp,
                soul_path=str(soul_path),
                max_bubbles=2,
                max_sentences_per_bubble=1,
                max_chars_per_frame=321,
            )
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(client), config)

            bubbles = runtime.handle_telegram_text(InboundEnvelope("telegram", "user-1", "msg-1", "yo"))

            realization_contract = "\n".join(message["content"] for message in provider.calls[0][0])
            self.assertIn("321 characters per frame", realization_contract)
            self.assertEqual(runtime.conversation.budget.max_chars_per_frame, 321)
            self.assertEqual([bubble["text"] for bubble in bubbles], ["one.", "two."])
            self.assertEqual(len(provider.calls), 1)

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
            provider = ScriptedProvider([stream("i get why that plan looks tempting.", "but the risky part is cooked, use the safer route first.")])
            client = FakeTelegramClient()
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(client), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            bubbles = runtime.handle_telegram_text(InboundEnvelope("telegram", "user-1", "msg-9", "what do u think abt this plan?"))

            self.assertEqual(client.typing_actor_ids, ["user-1"])
            self.assertEqual([bubble["text"] for bubble in bubbles], ["i get why that plan looks tempting.", "but the risky part is cooked, use the safer route first."])
            self.assertEqual(bubbles[0]["reply_to_message_id"], "msg-9")
            prompt_text = "\n".join(message["content"] for message in provider.calls[0][0])
            self.assertIn("old context", prompt_text)
            self.assertIn("SOUL SENTINEL", prompt_text)
            session = JsonSessionStore(tmp).load("telegram:actor:user-1")
            self.assertEqual([m.role for m in session.messages], ["user", "user", "assistant"])
            assistant = session.messages[-1]
            self.assertEqual(assistant.content, "i get why that plan looks tempting. but the risky part is cooked, use the safer route first.")
            self.assertEqual(assistant.metadata["conversation"], {"primary_move": "answer", "supporting_moves": [], "move_sequence": ["answer"], "response_goal": "answer", "confidence": "high"})
            self.assertEqual(assistant.metadata["delivery_bubbles"], ["i get why that plan looks tempting.", "but the risky part is cooked, use the safer route first."])
            metadata_text = str(assistant.metadata)
            self.assertNotIn("chain_of_thought", metadata_text)
            self.assertNotIn("primary_move\":", metadata_text)
            self.assertNotIn("access_token", metadata_text)


if __name__ == "__main__":
    unittest.main()
