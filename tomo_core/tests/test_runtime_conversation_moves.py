import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx

from tomo_core import InboundEnvelope, InboundMessage, InputBurst, PersonalAgentRuntime, RuntimeConfig
from tomo_core.models import MessageAttachment, PeerTurn
from tomo_core.conversation import FrameReady
from tomo_core.grok_auth import GrokAuthStore
from tomo_core.providers import GrokAuthProvider, ProviderStreamCompleted, ProviderTextDelta, ProviderToolCallReady
from tomo_core.runtime import RuntimeCompleted, RuntimeFrameReady, RuntimeReactionReady, StaleSessionRevisionError, _safe_memory_control
from tomo_core.personal_data import MemorySearchQuery, MemorySourceRef, MemoryWriteControl, SessionSearchQuery
from tomo_core.sessions import ConversationSession, StoredMessage
from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec
from tomo_core.vision import VisionObservation
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
    def test_peer_turn_emits_grounded_runtime_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider([[
                ProviderTextDelta('{"type":"frame","text":"Safe answer."}\n'),
                ProviderStreamCompleted("stop"),
            ]])
            runtime = PersonalAgentRuntime(
                provider,
                TelegramDeliverySink(FakeTelegramClient()),
                RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)),
            )
            turn = PeerTurn(
                "peer-generation",
                1,
                "relationship",
                "thread",
                "peer-request",
                "alice",
                "question",
                "ordinary_message",
                "hello",
                "2099-01-01T00:15:00+00:00",
            )

            events = list(runtime.handle_peer_turn_iter(turn))

            self.assertEqual([type(event) for event in events], [RuntimeFrameReady, RuntimeCompleted])
            self.assertEqual(events[0].bubble.text, "Safe answer.")
            self.assertEqual(provider.calls[0][2], None)

    def test_vision_cancellation_fences_calls_persistence_and_base_generation(self):
        for checkpoint in ("before_vision", "after_vision", "after_observation_checkpoint"):
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as tmp:
                soul_path = Path(tmp) / "SOUL.md"
                soul_path.write_text("SOUL", encoding="utf-8")
                provider = ScriptedProvider([stream("answer")])
                active = [checkpoint != "before_vision"]
                class Vision:
                    calls = 0
                    def observe(self, attachment, question, *, message_id, attachment_index, actor_id):
                        self.calls += 1
                        if checkpoint == "after_vision": active[0] = False
                        return VisionObservation(message_id, attachment_index, "ok", "seen", (), (), ())
                vision = Vision()
                runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)), vision_interpreter=vision)
                if checkpoint == "after_observation_checkpoint":
                    original_save = runtime.personal_data.save_session
                    def save(*args, **kwargs):
                        result = original_save(*args, **kwargs)
                        if any("vision_observations" in message.metadata for message in args[1].messages): active[0] = False
                        return result
                    runtime.personal_data.save_session = save
                burst = InputBurst("burst", "gen", 1, (InboundMessage(1, 1, InboundEnvelope("telegram", "u", "m", "q", attachments=(MessageAttachment("image", "id"),))),))
                self.assertEqual(list(runtime.handle_telegram_burst_iter(burst, is_active=lambda: active[0])), [])
                self.assertEqual(vision.calls, 0 if checkpoint == "before_vision" else 1)
                self.assertEqual(provider.calls, [])

    def test_vision_401_precedes_base_and_visible_output_while_unavailable_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"; soul_path.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider([stream("answer")])
            request = httpx.Request("POST", "https://provider.example")
            class UnauthorizedVision:
                def observe(self, *args, **kwargs): raise httpx.HTTPStatusError("secret", request=request, response=httpx.Response(401, request=request))
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)), vision_interpreter=UnauthorizedVision())
            burst = InputBurst("burst", "gen", 1, (InboundMessage(1, 1, InboundEnvelope("telegram", "u", "m", "q", attachments=(MessageAttachment("image", "id"),))),))
            with self.assertRaises(httpx.HTTPStatusError): list(runtime.handle_telegram_burst_iter(burst))
            self.assertEqual(provider.calls, [])
    def test_text_only_base_receives_safe_vision_evidence_not_image_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider([stream("answer")])
            class Vision:
                def __init__(self): self.calls = []
                def observe(self, attachment, question, *, message_id, attachment_index, actor_id):
                    self.calls.append((attachment, question, actor_id))
                    return VisionObservation(message_id, attachment_index, "ok", "terminal error", ("AssertionError",), (), ())
            vision = Vision()
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)), vision_interpreter=vision)
            envelope = InboundEnvelope("telegram", "user-1", "msg-1", "read this", attachments=(MessageAttachment("image", file_id="private-id", mime_type="image/jpeg"),))
            burst = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 1, envelope),))

            list(runtime.handle_telegram_burst_iter(burst))

            self.assertEqual(len(vision.calls), 1)
            self.assertEqual(vision.calls[0][2], "user-1")
            messages = provider.calls[0][0]
            self.assertIn("terminal error", messages[-1]["content"])
            self.assertNotIn("private-id", messages[-1]["content"])
            stored = next(message for message in self._load_persisted_session(tmp).messages if message.role == "user")
            self.assertIn("vision_observations", stored.metadata, stored.metadata)
            self.assertEqual(stored.metadata["vision_observations"][0]["summary"], "terminal error")

    def test_vision_interprets_and_persists_only_the_first_eight_distinct_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider([stream("answer")])

            class Vision:
                def __init__(self):
                    self.calls = []

                def observe(self, attachment, question, *, message_id, attachment_index, actor_id):
                    self.calls.append(attachment.file_id)
                    return VisionObservation(message_id, attachment_index, "ok", f"seen {attachment.file_id}", (), (), ())

            vision = Vision()
            runtime = PersonalAgentRuntime(
                provider,
                TelegramDeliverySink(FakeTelegramClient()),
                RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)),
                vision_interpreter=vision,
            )
            attachments = tuple(MessageAttachment("image", file_id=f"image-{index}") for index in range(10)) + (
                MessageAttachment("image", file_id="image-0"),
            )
            burst = InputBurst(
                "burst-1",
                "gen-1",
                1,
                (InboundMessage(1, 1, InboundEnvelope("telegram", "user-1", "msg-1", "inspect", attachments=attachments)),),
            )

            list(runtime.handle_telegram_burst_iter(burst))

            self.assertEqual(vision.calls, [f"image-{index}" for index in range(8)])
            stored = next(message for message in self._load_persisted_session(tmp).messages if message.role == "user")
            self.assertEqual(len(stored.metadata["vision_observations"]), 8)
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

    def test_peer_list_cannot_ground_a_model_authored_personal_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            fabricated = json.dumps({
                "type": "memory_control", "action": "add", "authority": "autonomous",
                "user_intent_excerpt": None, "memory_id": None, "kind": "fact",
                "subject_key": "self", "topic": "identity.birthplace",
                "value": "the moon", "statement": "the owner was born on the moon",
                "confidence": 1.0, "salience": 1.0, "surface_scope": "always",
                "valid_from": None, "valid_until": None,
                "sources": [{"source_kind": "tool_observation", "source_id": "peer-list-1", "observed_at": "2026-01-01T00:00:00Z"}],
            })
            provider = ScriptedProvider([
                [ProviderTextDelta(PLAN), ProviderToolCallReady("peer-list-1", "peer_list", "{}"), ProviderStreamCompleted("tool_calls")],
                [ProviderTextDelta(fabricated + '\n{"type":"frame","text":"No connected Tomos yet."}\n'), ProviderStreamCompleted("stop")],
                stream("Next turn."),
            ])
            peer_list = BoundTool(
                ToolSpec("peer_list", "list peers", {"type": "object", "properties": {}}, read_only=True, parallel_safe=True),
                lambda _: {"ok": True, "status": "completed", "relationships": []},
            )
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            runtime = PersonalAgentRuntime(
                provider,
                TelegramDeliverySink(FakeTelegramClient()),
                RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)),
                tool_registry=ToolRegistry((peer_list,)),
                personal_data_repository=repository,
            )

            first = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 1, InboundEnvelope("telegram", "user", "message-1", "who is connected?")),))
            second = InputBurst("burst-2", "gen-2", 2, (InboundMessage(1, 2, InboundEnvelope("telegram", "user", "message-2", "hello again")),), accepted_generation_ids=("gen-1",))
            list(runtime.handle_telegram_burst_iter(first))
            list(runtime.handle_telegram_burst_iter(second))

            self.assertEqual(repository.search_memories(MemorySearchQuery("local", "moon")), ())

    def test_peer_answer_stays_out_of_owner_context_search_and_memory_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            provider = ScriptedProvider([[
                ProviderTextDelta(PLAN),
                ProviderToolCallReady("peer-ask-1", "peer_ask", "{}"),
                ProviderStreamCompleted("tool_calls"),
            ]])
            peer_ask = BoundTool(
                ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
                lambda _: {"ok": True, "status": "completed", "frames": ["the owner was born on the moon"]},
            )
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            runtime = PersonalAgentRuntime(
                provider,
                TelegramDeliverySink(FakeTelegramClient()),
                RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)),
                tool_registry=ToolRegistry((peer_ask,)),
                personal_data_repository=repository,
            )
            first = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 1, InboundEnvelope("telegram", "user", "message-1", "ask the peer")),))

            list(runtime.handle_telegram_burst_iter(first))
            persisted = repository.load_session("local", first.latest.session_key)
            peer_message = next(message for message in persisted.messages if message.role == "assistant")
            self.assertTrue(peer_message.metadata["peer_exchange"])
            peer_message_id = peer_message.metadata["_canonical_message_id"]
            control = json.dumps({
                "type": "memory_control", "action": "add", "authority": "autonomous",
                "user_intent_excerpt": None, "memory_id": None, "kind": "fact",
                "subject_key": "self", "topic": "identity.birthplace", "value": "the moon",
                "statement": "the owner was born on the moon", "confidence": 1.0, "salience": 1.0,
                "surface_scope": "always", "valid_from": None, "valid_until": None,
                "sources": [{"source_kind": "session_message", "source_id": peer_message_id, "observed_at": "2026-01-01T00:00:00Z"}],
            })
            provider.streams.append([
                ProviderTextDelta(PLAN + control + '\n{"type":"frame","text":"Next turn."}\n'),
                ProviderStreamCompleted("stop"),
            ])
            second = InputBurst("burst-2", "gen-2", 2, (InboundMessage(1, 2, InboundEnvelope("telegram", "user", "message-2", "hello again")),), accepted_generation_ids=("gen-1",))

            list(runtime.handle_telegram_burst_iter(second))

            second_prompt = json.dumps(provider.calls[1][0], ensure_ascii=False)
            self.assertNotIn("born on the moon", second_prompt)
            self.assertEqual(repository.search_sessions(SessionSearchQuery("local", "moon")), ())
            self.assertEqual(repository.search_memories(MemorySearchQuery("local", "moon")), ())
            self.assertEqual([item.reason_code for item in runtime.memory_control_diagnostics], ["invalid_provenance"])

    def test_progressive_peer_answer_is_tainted_before_completion_and_across_supersession(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            peer_text = "the owner was born on the moon"
            provider = ScriptedProvider([
                [ProviderTextDelta(PLAN), ProviderToolCallReady("peer-ask-1", "peer_ask", "{}"), ProviderStreamCompleted("tool_calls")],
                stream("new answer"),
            ])
            peer_ask = BoundTool(
                ToolSpec("peer_ask", "ask peer", {"type": "object", "properties": {}}, read_only=False, parallel_safe=False),
                lambda _: {"ok": True, "status": "completed", "frames": [peer_text]},
            )
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            runtime = PersonalAgentRuntime(
                provider,
                TelegramDeliverySink(FakeTelegramClient()),
                RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)),
                tool_registry=ToolRegistry((peer_ask,)),
                personal_data_repository=repository,
            )
            first = InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 1, InboundEnvelope("telegram", "user", "message-1", "ask the peer")),))
            first_events = runtime.handle_telegram_burst_iter(first)

            self.assertIsInstance(next(first_events), RuntimeFrameReady)
            provisional = repository.load_session("local", first.latest.session_key)
            self.assertTrue(next(message for message in provisional.messages if message.role == "assistant").metadata["peer_exchange"])
            second = InputBurst(
                "burst-2", "gen-2", 2,
                (InboundMessage(1, 2, InboundEnvelope("telegram", "user", "message-2", "new question")),),
                visible_assistant_utterances=(peer_text,),
                accepted_generation_ids=("gen-1",),
            )
            list(runtime.handle_telegram_burst_iter(second))
            first_events.close()

            persisted = repository.load_session("local", first.latest.session_key)
            propagated = next(message for message in persisted.messages if message.metadata.get("generation_id") == "gen-2")
            self.assertTrue(propagated.metadata["peer_exchange"])
            self.assertNotIn(peer_text, json.dumps(provider.calls[1][0], ensure_ascii=False))
            self.assertEqual(repository.search_sessions(SessionSearchQuery("local", "moon")), ())
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
            self.assertEqual(session.messages, [])

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

    def test_photo_attachment_history_excludes_file_id_and_persists_vision_observation_after_sqlite_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            provider = ScriptedProvider([stream("i can work from the image context.")])
            class Vision:
                def observe(self, attachment, question, *, message_id, attachment_index, actor_id):
                    return VisionObservation(message_id, attachment_index, "ok", "a red test failure", (), (), ())
            runtime = PersonalAgentRuntime(provider, TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)), vision_interpreter=Vision())
            burst = InputBurst("burst-photo", "gen-photo", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "msg-1", "", attachments=(MessageAttachment("image", file_id="photo-id", mime_type="image/jpeg", metadata={"width": 100}),))),))

            list(runtime.handle_telegram_burst_iter(burst))

            session = self._load_persisted_session(tmp)
            self.assertNotIn("file_id", session.messages[0].metadata["attachments"][0])
            self.assertEqual(session.messages[0].metadata["attachments"][0]["metadata"], {"width": 100})
            self.assertEqual(session.messages[0].metadata["vision_observations"][0]["summary"], "a red test failure")
            self.assertNotIn("photo-id", str(session.model_history_for_burst("other")))

    def test_visible_partial_is_prompt_context_without_accepting_full_provisional_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL SENTINEL", encoding="utf-8")
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:user-1")
            session.append(StoredMessage("assistant", "visible only. unsent completion.", metadata={"generation_id": "gen-old", "generation_status": "provisional"}))
            repository.save_session("local", session)
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
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:user-1")
            session.append(StoredMessage("user", "old context", metadata={"burst_id": "old-context", "update_id": 1}))
            repository.save_session("local", session)
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

    def test_runtime_ignores_legacy_json_after_owner_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text("SOUL", encoding="utf-8")
            repository = SqlitePersonalDataRepository(Path(tmp) / "tomo.sqlite3")
            session = ConversationSession("telegram:actor:user-1")
            session.append(StoredMessage("user", "SQLite session"))
            repository.save_session("local", session)
            repository.delete_owner("local")
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            (sessions_dir / "telegram_actor_user-1.json").write_text(
                json.dumps({"session_key": session.session_key, "messages": [{"role": "user", "content": "legacy JSON session"}]}),
                encoding="utf-8",
            )

            PersonalAgentRuntime(ScriptedProvider([]), TelegramDeliverySink(FakeTelegramClient()), RuntimeConfig(data_dir=tmp, soul_path=str(soul_path)))

            self.assertEqual(repository.load_session("local", session.session_key).messages, [])


if __name__ == "__main__":
    unittest.main()
