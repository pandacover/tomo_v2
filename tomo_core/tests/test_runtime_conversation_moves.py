import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core import InboundEnvelope, InboundMessage, InputBurst, PersonalAgentRuntime, RuntimeConfig
from tomo_core.models import MessageAttachment
from tomo_core.conversation import FrameReady
from tomo_core.grok_auth import GrokAuthStore
from tomo_core.providers import GrokAuthProvider, ProviderStreamCompleted, ProviderTextDelta
from tomo_core.runtime import RuntimeCompleted, RuntimeFrameReady, RuntimeReactionReady, StaleSessionRevisionError, _safe_memory_control
from tomo_core.personal_data import MemorySourceRef, MemoryWriteControl
from tomo_core.sessions import ConversationSession, JsonSessionStore, StoredMessage
from tomo_core.sqlite_personal_data import SqlitePersonalDataRepository
from tomo_core.telegram import FakeTelegramClient, TelegramDeliverySink


class ScriptedProvider:
    name = "scripted"
    supports_images_in = False
    supports_images_out = False
    supports_tool_calls = True

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
    def test_latency_marker_is_suppressed_when_inactive_after_session_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider([stream("answer")])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))
            active = [True]
            original_load = runtime.personal_data.load_session

            def load_and_cancel(*args, **kwargs):
                session = original_load(*args, **kwargs)
                active[0] = False
                return session

            with patch("tomo_core.runtime.latency_trace.emit_sandbox") as emit, patch.object(runtime.personal_data, "load_session", side_effect=load_and_cancel):
                self.assertEqual(list(runtime.handle_telegram_burst_iter(burst, is_active=lambda: active[0])), [])
            self.assertNotIn("sandbox_session_load", [call.args[0] for call in emit.call_args_list])

    def test_reaction_follows_leading_setting_control_and_precedes_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            plan = PLAN.replace('"confidence":"high"', '"confidence":"high","reaction":"👍"')
            provider = ScriptedProvider([[ProviderTextDelta(plan + '{"type":"frame","text":"Visible."}\n'), ProviderStreamCompleted("stop")]])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))

            events = list(runtime.handle_telegram_burst_iter(burst))

            self.assertEqual([type(event) for event in events], [RuntimeReactionReady, RuntimeFrameReady, RuntimeCompleted])
            self.assertEqual(
                events[0],
                RuntimeReactionReady("local", "user-1", "user-1", "msg-1", "gen-1", 1, "👍"),
            )

    def test_same_turn_reaction_opt_out_vetoes_reaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            plan = PLAN.replace('"confidence":"high"', '"confidence":"high","reaction":"👍"')
            control = '{"type":"memory_control","action":"set_owner_setting","setting":"reactions_enabled","enabled":false,"user_intent_excerpt":"stop reactions"}\n'
            provider = ScriptedProvider([[ProviderTextDelta(plan + control + '{"type":"frame","text":"Understood."}\n'), ProviderStreamCompleted("stop")]])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "stop reactions")),))

            events = list(runtime.handle_telegram_burst_iter(burst))

            self.assertNotIn(RuntimeReactionReady, [type(event) for event in events])
            self.assertFalse(runtime.personal_data.memory_settings("local").reactions_enabled)
    def test_memory_control_accepts_only_current_turn_tool_observations(self):
        burst = InputBurst("burst", "generation", 1, (InboundMessage(1, 1, InboundEnvelope("telegram", "user", "message-1", "hello")),))
        control = MemoryWriteControl("add", "autonomous", None, None, "fact", "self", "tool", "value", "observed", 1, 1, "contextual", None, None, (MemorySourceRef("tool_observation", "call-1", "2026-01-01T00:00:00Z"),))
        self.assertTrue(_safe_memory_control(control, burst, ConversationSession("telegram:actor:user"), {"call-1"}))
        self.assertFalse(_safe_memory_control(control, burst, ConversationSession("telegram:actor:user"), {"old-call"}))
    @staticmethod
    def _load_persisted_session(data_dir, session_key="telegram:actor:user-1"):
        return SqlitePersonalDataRepository(Path(data_dir) / "tomo.sqlite3").load_session("local", session_key)

    def test_handle_telegram_burst_iter_persists_before_provider_and_emits_progressively(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            order = []

            class InspectingProvider(ScriptedProvider):
                def stream(self, messages, *, tools=(), actor_id=None):
                    saved = RuntimeConversationMoveTests._load_persisted_session(tmp)
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
            self.assertEqual({tool["function"]["name"] for tool in provider.calls[0][1]}, {"search_memories", "search_sessions"})
            session = self._load_persisted_session(tmp)
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

    def test_storage_failure_before_first_frame_propagates_without_releasing_a_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            runtime = PersonalAgentRuntime(ScriptedProvider([stream("visible.")]), TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)), personal_data_repository=repository)
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))
            original = repository.save_session

            def fail_assistant(owner_id, session, **kwargs):
                if any(message.role == "assistant" for message in session.messages):
                    from tomo_core.personal_data import StorageBusyError
                    raise StorageBusyError("storage_busy")
                return original(owner_id, session, **kwargs)

            repository.save_session = fail_assistant
            iterator = runtime.handle_telegram_burst_iter(burst)
            with self.assertRaisesRegex(Exception, "storage_busy"):
                next(iterator)

    def test_initial_stale_revision_raises_before_provider_or_later_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:user-1")
            self.assertTrue(repository.save_session("local", session, generation_id="new", revision=2))
            provider = ScriptedProvider([stream("should not run.")])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)), personal_data_repository=repository)
            burst = InputBurst("burst-1", "old", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))

            with self.assertRaises(StaleSessionRevisionError) as raised:
                list(runtime.handle_telegram_burst_iter(burst))

            self.assertEqual(raised.exception.current_revision, 2)
            self.assertEqual(provider.calls, [])
            self.assertEqual(repository.current_session_revision("local", "telegram:actor:user-1"), 2)

    def test_rejected_memory_control_records_only_a_content_free_reason_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            secret = "sk-test-00000000000000000000000000000000"
            control = ('{"type":"memory_control","action":"add","authority":"autonomous",'
                       '"user_intent_excerpt":null,"memory_id":null,"kind":"fact","subject_key":"self",'
                       '"topic":"credential","value":"' + secret + '","statement":"' + secret + '",'
                       '"confidence":0.8,"salience":0.8,"surface_scope":"archive","valid_from":null,'
                       '"valid_until":null,"sources":[{"source_kind":"current_message","source_id":"msg-1",'
                       '"observed_at":"2026-01-01T00:00:00Z"}]}\n')
            provider = ScriptedProvider([[ProviderTextDelta(PLAN + control + '{"type":"frame","text":"Safe."}\n'), ProviderStreamCompleted("stop")]])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))

            list(runtime.handle_telegram_burst_iter(burst))

            self.assertEqual([diagnostic.reason_code for diagnostic in runtime.memory_control_diagnostics], ["secret_detected"])
            self.assertNotIn(secret, repr(runtime.memory_control_diagnostics))

    def test_memory_storage_failure_records_only_a_content_free_reason_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            secret = "owner-private-memory-content"
            control = ('{"type":"memory_control","action":"add","authority":"autonomous",'
                       '"user_intent_excerpt":null,"memory_id":null,"kind":"fact","subject_key":"self",'
                       '"topic":"preference","value":"' + secret + '","statement":"' + secret + '",'
                       '"confidence":0.8,"salience":0.8,"surface_scope":"archive","valid_from":null,'
                       '"valid_until":null,"sources":[{"source_kind":"current_message","source_id":"msg-1",'
                       '"observed_at":"2026-01-01T00:00:00Z"}]}\n')
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")

            def fail_stage(*args, **kwargs):
                from tomo_core.personal_data import StorageBusyError
                raise StorageBusyError(secret)

            repository.stage_memory_controls = fail_stage
            provider = ScriptedProvider([[ProviderTextDelta(PLAN + control + '{"type":"frame","text":"Safe."}\n'), ProviderStreamCompleted("stop")]])
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)), personal_data_repository=repository)
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "go")),))

            list(runtime.handle_telegram_burst_iter(burst))

            self.assertEqual([diagnostic.reason_code for diagnostic in runtime.memory_control_diagnostics], ["storage_failure"])
            self.assertNotIn(secret, repr(runtime.memory_control_diagnostics))

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
            session = self._load_persisted_session(tmp)
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
            session = self._load_persisted_session(tmp)
            self.assertEqual([message.role for message in session.messages], ["user", "assistant"])
            self.assertEqual(session.messages[-1].metadata["generation_status"], "provisional")

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
            session = self._load_persisted_session(tmp)
            self.assertEqual([message.role for message in session.messages], ["user", "assistant"])
            self.assertEqual(session.messages[-1].metadata["generation_status"], "provisional")

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
            session = self._load_persisted_session(tmp)
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

            session = self._load_persisted_session(tmp)
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
            session = self._load_persisted_session(tmp)
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
