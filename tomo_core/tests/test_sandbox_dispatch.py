import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from tomo_core.daytona_client import DaytonaClientError, ExecResult, SandboxHandle, SessionCommandHandle
from tomo_core.models import InboundEnvelope
from tomo_core.models import AutomationTurn
from tomo_core.onboarding_store import InterruptedGeneration, TelegramGenerationInput, TelegramGenerationWork
from tomo_core.sandbox_dispatch import SandboxDispatch, SandboxDispatchError
from tomo_core.sandbox_registry import SandboxRegistry
from tomo_core.sandbox_protocol import EVENT_MARKER, RESULT_MARKER, SandboxCompletedEvent, SandboxErrorEvent, SandboxFrameEvent, SandboxTracebackFrame, encode_error, encode_event, encode_result
from tomo_core.conversation.models import ConversationMove, Frame, FrameReady, MoveConfidence, MovePlan, SegmentFinish, SegmentResult, ToolCall, TurnBudget, TurnRunCompleted, TurnRunResult, TurnRunStatus, TurnUsage
from tomo_core.runtime import RuntimeCompleted, RuntimeFrameReady
from tomo_core.models import OutboundBubble
from tomo_core.onboarding_store import TelegramInstallation
from tomo_core.cron_capability import verify_capability


def legacy_event(request_id, generation_id, sequence, event_type, **fields):
    import json
    return EVENT_MARKER + json.dumps({
        "version": 2,
        "request_id": request_id,
        "generation_id": generation_id,
        "sequence": sequence,
        "type": event_type,
        **fields,
    }) + "\n"


def completed_marker(request_id, generation_id, sequence, text, move=ConversationMove.ANSWER):
    return legacy_event(
        request_id,
        generation_id,
        sequence,
        "completed",
        result={"logical_text": text, "utterances": [text], "plan": {"primary_move": move.value, "supporting_moves": [], "move_sequence": [move.value], "response_goal": move.value, "confidence": "high"}},
    )


def v3_markers(request_id, generation_id, text):
    frame = Frame(0, 0, text)
    plan = MovePlan(ConversationMove.ANSWER, (), "answer", MoveConfidence.HIGH, (ConversationMove.ANSWER,))
    result = TurnRunResult(plan, (SegmentResult(0, (frame,), (), SegmentFinish.COMPLETE),), (frame,), TurnUsage(1, 0, 0, 1), TurnRunStatus.COMPLETED)
    return (
        EVENT_MARKER + encode_event(request_id, generation_id, 0, RuntimeFrameReady(FrameReady(0, frame), OutboundBubble(text))) + "\n",
        EVENT_MARKER + encode_event(request_id, generation_id, 1, RuntimeCompleted(TurnRunCompleted(result))) + "\n",
    )


def v3_tool_turn_markers(request_id, generation_id):
    frame = Frame(4, 0, "complete.")
    plan = MovePlan(ConversationMove.ANSWER, (), "answer", MoveConfidence.HIGH, (ConversationMove.ANSWER,))
    segments = tuple(
        SegmentResult(index, (), (ToolCall(f"call-{index}", "lookup", {}),), SegmentFinish.TOOL_BATCH)
        for index in range(4)
    ) + (SegmentResult(4, (frame,), (), SegmentFinish.COMPLETE),)
    result = TurnRunResult(plan, segments, (frame,), TurnUsage(5, 4, 4, 1), TurnRunStatus.COMPLETED)
    return (
        EVENT_MARKER + encode_event(request_id, generation_id, 0, RuntimeFrameReady(FrameReady(0, frame), OutboundBubble(frame.text))) + "\n",
        EVENT_MARKER + encode_event(request_id, generation_id, 1, RuntimeCompleted(TurnRunCompleted(result))) + "\n",
    )


class SandboxDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SandboxRegistry(self.tmp.name)
        self.registry.upsert("tomo-a", "sbx-1", "base", "ready")
        self.daytona = Mock()
        self.daytona.get.return_value = SandboxHandle("sbx-1", self.registry.get("tomo-a").sandbox_name)
        self.supervisor = Mock()
        self.supervisor.reconcile.return_value = self.registry.get("tomo-a")
        self.auth = Mock()
        self.auth.access_token.return_value = "fresh-token"
        self.dispatch = SandboxDispatch(
            self.supervisor,
            self.daytona,
            self.auth,
            data_dir="/var/lib/tomo",
            xai_model="grok-4.5",
            xai_reasoning_effort="high",
        )
        self.installation = TelegramInstallation("user", "tomo-a", "chat", "user", 0)
        self.inbound = InboundEnvelope(connector="telegram", actor_id="user", message_id="message", text="hello")

    def _work(self):
        return TelegramGenerationWork(
            generation_id="burst:one/r1",
            burst_id="burst:one",
            chat_id="chat",
            tomo_id="tomo-a",
            revision=1,
            session_id="persisted-session",
            inputs=(
                TelegramGenerationInput(
                    update_id=42,
                    ordinal=1,
                    payload='{"update_id":42,"message":{"message_id":7,"date":10,"from":{"id":111},"chat":{"id":222,"type":"private"},"text":"hello"}}',
                    message_id="7",
                    telegram_sent_at=10.0,
                ),
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_deliver_ensures_the_sandbox_and_uses_the_update_request_id(self):
        result = encode_result("telegram:update:42", [OutboundBubble("hello", reply_to_message_id="message")])
        self.daytona.exec.return_value = ExecResult(0, f"{RESULT_MARKER}{result}\n")

        bubbles = self.dispatch.deliver_telegram(self.installation, 42, self.inbound)

        self.assertEqual(bubbles[0].text, "hello")
        self.supervisor.reconcile.assert_called_once_with("tomo-a")
        self.auth.access_token.assert_called_once_with(force_refresh=False)
        self.daytona.get.assert_called_once_with("sbx-1")
        command = self.daytona.exec.call_args.args[1]
        self.assertEqual(command, "/opt/tomo/.venv/bin/tomo-core sandbox-inbound")
        env = self.daytona.exec.call_args.kwargs["env"]
        self.assertEqual(
            set(env),
            {
                "TOMO_INBOUND_JSON",
                "TOMO_CORE_DATA_DIR",
                "TOMO_INSTANCE_ID",
                "TOMO_SUPERGROK_ACCESS_TOKEN",
                "TOMO_CORE_SOUL",
                "TOMO_XAI_MODEL",
                "TOMO_XAI_REASONING_EFFORT",
            },
        )
        self.assertEqual(env["TOMO_CORE_DATA_DIR"], "/var/lib/tomo")
        self.assertEqual(env["TOMO_INSTANCE_ID"], "tomo-a")
        self.assertEqual(env["TOMO_SUPERGROK_ACCESS_TOKEN"], "fresh-token")
        self.assertEqual(env["TOMO_CORE_SOUL"], "/opt/tomo/SOUL.md")
        self.assertEqual(env["TOMO_XAI_MODEL"], "grok-4.5")
        self.assertEqual(env["TOMO_XAI_REASONING_EFFORT"], "high")
        self.assertIn('"text":"hello"', env["TOMO_INBOUND_JSON"])
        self.assertNotIn("hello", command)
        self.assertEqual(self.daytona.exec.call_args.kwargs["timeout"], 120)

    def test_iter_telegram_events_streams_v2_events_from_named_session(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(
            [
                "log line\n",
                legacy_event("telegram-generation-burst-one-r1", "burst:one/r1", 0, "utterance", move="acknowledge", text="hello."),
                completed_marker("telegram-generation-burst-one-r1", "burst:one/r1", 1, "hello.", ConversationMove.ACKNOWLEDGE),
            ]
        )
        self.daytona.session_command_exit_code.return_value = 0

        events = list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual(events[0].text, "hello.")
        self.assertEqual((events[0].segment_index, events[0].frame_index, events[0].legacy_move), (0, 0, ConversationMove.ACKNOWLEDGE))
        self.daytona.start_session_command.assert_called_once()
        sandbox, session_id, command = self.daytona.start_session_command.call_args.args
        self.assertEqual(sandbox.id, "sbx-1")
        self.assertEqual(session_id, "persisted-session")
        self.assertEqual(command, "/opt/tomo/.venv/bin/tomo-core sandbox-inbound")
        env = self.daytona.start_session_command.call_args.kwargs["env"]
        self.assertIn('"generation_id":"burst:one/r1"', env["TOMO_INBOUND_JSON"])
        self.assertIn('"text":"hello"', env["TOMO_INBOUND_JSON"])
        self.assertNotIn("hello", command)
        self.daytona.iter_session_logs.assert_called_once_with(sandbox, SessionCommandHandle("telegram-burst-one-r1", "cmd-1"))
        self.daytona.delete_session.assert_called_once_with(sandbox, "persisted-session")

    def test_interactive_turn_gets_short_owner_bound_cron_capability_but_automation_does_not(self):
        capability_key = b"k" * 32
        self.dispatch.control_url = "https://control.example.test"
        self.dispatch.capability_key = capability_key
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(v3_markers("telegram-generation-burst-one-r1", work.generation_id, "hello."))
        self.daytona.session_command_exit_code.return_value = 0

        list(self.dispatch.iter_telegram_events(self.installation, work))

        interactive_env = self.daytona.start_session_command.call_args.kwargs["env"]
        claim = verify_capability(capability_key, interactive_env["TOMO_CRON_CAPABILITY"], now=int(time.time()), operation="create")
        self.assertEqual((interactive_env["TOMO_CRON_CONTROL_URL"], claim.owner_id, claim.actor_id, claim.destination, claim.session_id), ("https://control.example.test", "tomo-a", "user", "telegram:chat", "telegram:actor:user"))
        self.assertEqual((interactive_env["TOMO_CRON_OWNER_ID"], interactive_env["TOMO_CRON_ACTOR_ID"], interactive_env["TOMO_CRON_DESTINATION"], interactive_env["TOMO_CRON_SESSION_ID"]), (claim.owner_id, claim.actor_id, claim.destination, claim.session_id))

        turn = AutomationTurn("cron/r1", 1, "job", "run", "user", "chat", "check", "2026-01-01T00:00:00+00:00")
        self.daytona.start_session_command.reset_mock()
        self.daytona.iter_session_logs.return_value = iter(v3_markers("telegram-generation-cron-r1", turn.generation_id, "hello."))

        list(self.dispatch.iter_automation_events(self.installation, turn, turn.generation_id, "cron-session"))

        automation_env = self.daytona.start_session_command.call_args.kwargs["env"]
        self.assertNotIn("TOMO_CRON_CONTROL_URL", automation_env)
        self.assertNotIn("TOMO_CRON_CAPABILITY", automation_env)

    def test_iter_telegram_events_streams_v3_frame_coordinates(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(v3_markers("telegram-generation-burst-one-r1", "burst:one/r1", "hello."))
        self.daytona.session_command_exit_code.return_value = 0

        events = list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual((events[0].segment_index, events[0].frame_index, events[0].legacy_move), (0, 0, None))

    def test_iter_telegram_events_emits_safe_latency_phases_and_usage_counts(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(v3_tool_turn_markers("telegram-generation-burst-one-r1", "burst:one/r1"))
        self.daytona.session_command_exit_code.return_value = 0

        with patch("tomo_core.sandbox_dispatch.latency_trace.emit") as emit:
            list(self.dispatch.iter_telegram_events(self.installation, work))

        phases = [call.args[1] for call in emit.call_args_list]
        self.assertEqual(phases, ["dispatch_start", "sandbox_reconcile", "sandbox_lookup", "oauth_access", "pty_ready", "sandbox_first_frame", "sandbox_completed"])
        self.assertTrue(all(call.args[0] == work.burst_id for call in emit.call_args_list))
        completed = emit.call_args_list[-1]
        self.assertEqual(completed.kwargs["model_segments"], 5)
        self.assertEqual(completed.kwargs["tool_rounds"], 4)
        self.assertEqual(completed.kwargs["tool_calls"], 4)
        self.assertEqual(completed.kwargs["visible_segments"], 1)
        self.assertNotIn("complete.", repr(emit.call_args_list))

    def test_iter_telegram_events_uses_the_configured_tool_budget(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        markers = v3_tool_turn_markers("telegram-generation-burst-one-r1", "burst:one/r1")
        self.daytona.iter_session_logs.return_value = iter(markers)
        self.daytona.session_command_exit_code.return_value = 0

        events = list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual(events[0].segment_index, 4)

        limited = SandboxDispatch(self.supervisor, self.daytona, self.auth, data_dir="/var/lib/tomo", xai_model="grok-4.5", xai_reasoning_effort="high", budget=TurnBudget(4, 3, 3, 3, 3, 3, 800))
        self.daytona.iter_session_logs.return_value = iter(markers)
        with self.assertRaises(SandboxDispatchError) as raised:
            list(limited.iter_telegram_events(self.installation, work))
        self.assertEqual(raised.exception.code, "invalid_result")

    def test_iter_telegram_events_does_not_start_when_inactive(self):
        with patch("tomo_core.sandbox_dispatch.latency_trace.emit") as emit:
            self.assertEqual(list(self.dispatch.iter_telegram_events(self.installation, self._work(), is_active=lambda: False)), [])
        emit.assert_not_called()
        self.daytona.start_session_command.assert_not_called()

    def test_iter_telegram_events_forwards_only_validated_latency_without_sensitive_values(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter([
            "TOMO_SANDBOX_LATENCY_V1=phase=sandbox_provider_attempt outcome=ok elapsed_ms=7 attempt=2 segment=1 repair=1\n",
            *v3_markers("telegram-generation-burst-one-r1", "burst:one/r1", "hello."),
        ])
        self.daytona.session_command_exit_code.return_value = 0
        with patch.dict("os.environ", {"TOMO_LATENCY_TRACE": "1", "TOMO_LATENCY_TRACE_KEY": "test-latency-trace-key-at-least-32-bytes"}, clear=False), patch("tomo_core.sandbox_dispatch.latency_trace.emit") as emit:
            list(self.dispatch.iter_telegram_events(self.installation, work))
        forwarded = [call for call in emit.call_args_list if call.args[1] == "sandbox_provider_attempt"]
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(forwarded[0].kwargs, {"outcome": "ok", "elapsed_ms": 7, "attempt": 2, "segment": 1, "repair": 1})
        env = self.daytona.start_session_command.call_args.kwargs["env"]
        self.assertEqual(env["TOMO_LATENCY_TRACE"], "1")
        self.assertNotIn("TOMO_LATENCY_TRACE_KEY", env)
        self.assertNotIn("hello", repr(forwarded))

    def test_iter_telegram_events_stops_after_a_frame_becomes_stale(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(v3_markers("telegram-generation-burst-one-r1", "burst:one/r1", "hello."))
        self.daytona.session_command_exit_code.return_value = 0
        active = [True]
        events = self.dispatch.iter_telegram_events(self.installation, work, is_active=lambda: active[0])

        self.assertEqual(next(events).text, "hello.")
        active[0] = False
        self.assertEqual(list(events), [])
        self.daytona.delete_session.assert_called_once()

    def test_iter_telegram_events_ignores_session_cleanup_failure(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(v3_markers("telegram-generation-burst-one-r1", "burst:one/r1", "hello."))
        self.daytona.session_command_exit_code.return_value = 0
        self.daytona.delete_session.side_effect = DaytonaClientError("cleanup failed")

        self.assertEqual([event.text for event in self.dispatch.iter_telegram_events(self.installation, work) if hasattr(event, "text")], ["hello."])

    def test_iter_telegram_events_preserves_photo_attachment_boundary(self):
        work = TelegramGenerationWork(
            generation_id="gen-photo",
            burst_id="burst-photo",
            chat_id="chat",
            tomo_id="tomo-a",
            revision=1,
            session_id="photo-session",
            inputs=(
                TelegramGenerationInput(
                    update_id=43,
                    ordinal=1,
                    payload='{"update_id":43,"message":{"message_id":8,"date":11,"from":{"id":111},"chat":{"id":222,"type":"private"},"caption":"look","photo":[{"file_id":"small","width":90,"height":90},{"file_id":"large","width":900,"height":900}]}}',
                    message_id="8",
                    telegram_sent_at=11.0,
                ),
            ),
        )
        self.daytona.start_session_command.return_value = SessionCommandHandle("photo-session", "photo-session")
        self.daytona.iter_session_logs.return_value = iter(
            [
                legacy_event("telegram-generation-gen-photo", "gen-photo", 0, "utterance", move="answer", text="done."),
                completed_marker("telegram-generation-gen-photo", "gen-photo", 1, "done."),
            ]
        )
        self.daytona.session_command_exit_code.return_value = 0

        list(self.dispatch.iter_telegram_events(self.installation, work))

        env = self.daytona.start_session_command.call_args.kwargs["env"]
        self.assertIn('"attachments":[{"kind":"image","file_id":"large"', env["TOMO_INBOUND_JSON"])
        self.assertIn('"text":"look"', env["TOMO_INBOUND_JSON"])

    def test_burst_from_work_captures_reply_context_from_the_queued_raw_update(self):
        work = self._work()
        payload = __import__("json").loads(work.inputs[0].payload)
        payload["message"]["reply_to_message"] = {"message_id": 6, "from": {"id": 111}, "chat": {"id": 222, "type": "private"}, "text": "referent"}
        work = __import__("dataclasses").replace(work, inputs=(__import__("dataclasses").replace(work.inputs[0], payload=__import__("json").dumps(payload)),))

        burst = __import__("tomo_core.sandbox_dispatch", fromlist=["burst_from_work"]).burst_from_work(self.installation, work)

        self.assertEqual((burst.latest.reply_context.message_id, burst.latest.reply_context.text), ("6", "referent"))

    def test_iter_telegram_events_rejects_wrong_generation_without_delivery(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(
            [
                legacy_event("telegram-generation-burst-one-r1", "other-generation", 0, "utterance", move="answer", text="wrong.")
            ]
        )

        with self.assertRaises(SandboxDispatchError) as raised:
            list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual(raised.exception.code, "invalid_result")

    def test_automation_rejects_foreign_generation_or_installation_before_sandbox_io(self):
        turn = AutomationTurn("generation", 1, "job", "run", "other-user", "other-chat", "Check status", "2026-01-01T00:00:00+00:00")
        with self.assertRaises(SandboxDispatchError) as raised:
            list(self.dispatch.iter_automation_events(self.installation, turn, "different-generation", "session"))
        self.assertEqual(raised.exception.code, "invalid_result")
        self.supervisor.reconcile.assert_not_called()
        self.daytona.get.assert_not_called()
        self.daytona.start_session_command.assert_not_called()

    def test_cancel_generation_deletes_only_the_process_session(self):
        self.dispatch.cancel_generation(InterruptedGeneration("gen:1", "chat", "tomo-a", 1, "session-from-store"))

        sandbox = self.daytona.get.return_value
        self.supervisor.reconcile.assert_called_once_with("tomo-a")
        self.daytona.get.assert_called_once_with("sbx-1")
        self.daytona.delete_session.assert_called_once_with(sandbox, "session-from-store")
        self.daytona.delete.assert_not_called()

    def test_iter_telegram_events_refreshes_auth_once_before_visible_output_only(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.side_effect = [
            iter([EVENT_MARKER + encode_event("telegram-generation-burst-one-r1", "burst:one/r1", 0, SandboxErrorEvent(0, "auth_expired")) + "\n"]),
            iter(
                [
                    legacy_event("telegram-generation-burst-one-r1", "burst:one/r1", 0, "utterance", move="answer", text="ok."),
                    completed_marker("telegram-generation-burst-one-r1", "burst:one/r1", 1, "ok."),
                ]
            ),
        ]
        self.daytona.session_command_exit_code.return_value = 0

        events = list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual(events[0].text, "ok.")
        self.auth.access_token.assert_has_calls([unittest.mock.call(force_refresh=False), unittest.mock.call(force_refresh=True)])
        self.assertEqual(self.daytona.start_session_command.call_count, 2)
        self.assertEqual(self.daytona.delete_session.call_count, 2)

    def test_iter_telegram_events_logs_only_safe_error_diagnostics(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(
            [
                EVENT_MARKER
                + encode_event(
                    "telegram-generation-burst-one-r1",
                    "burst:one/r1",
                    0,
                    SandboxErrorEvent(
                        0,
                        "runtime_failed",
                        "RuntimeError",
                        (SandboxTracebackFrame("runtime.py", "handle_turn", 42),),
                    ),
                )
                + "\n"
            ]
        )

        with self.assertLogs("tomo_core.sandbox_dispatch", level="WARNING") as logs:
            with self.assertRaises(SandboxDispatchError) as raised:
                list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual(raised.exception.code, "runtime_failed")
        self.assertIn("RuntimeError", logs.output[0])
        self.assertIn("runtime.py", logs.output[0])
        self.assertNotIn("burst:one/r1", logs.output[0])
        self.assertNotIn("tomo-a", logs.output[0])
        self.assertNotIn("hello", logs.output[0])

    def test_iter_telegram_events_logs_parser_location_without_exception_text(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")

        def reject_stream(*args, **kwargs):
            raise ValueError("token=sensitive-value")

        with patch("tomo_core.sandbox_dispatch.iter_event_markers", side_effect=reject_stream):
            with self.assertLogs("tomo_core.sandbox_dispatch", level="WARNING") as logs:
                with self.assertRaises(SandboxDispatchError) as raised:
                    list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual(raised.exception.code, "invalid_result")
        self.assertIn("sandbox parser failure code=invalid_result", logs.output[0])
        self.assertIn("reject_stream", logs.output[0])
        self.assertNotIn("sensitive-value", logs.output[0])
        self.assertNotIn("burst:one/r1", logs.output[0])
        self.assertNotIn("tomo-a", logs.output[0])

    def test_burst_from_work_excludes_destination_chat_ids_from_sandbox_metadata(self):
        burst = __import__("tomo_core.sandbox_dispatch", fromlist=["burst_from_work"]).burst_from_work(self.installation, self._work())

        metadata = burst.messages[0].envelope.native_metadata
        self.assertNotIn("chat_id", metadata)
        self.assertNotIn("delivery_chat_id", metadata)
        self.assertEqual(metadata["from_id"], "111")
        self.assertEqual(metadata["update_id"], 42)

    def test_deliver_rejects_a_result_with_the_wrong_request_id(self):
        result = encode_result("other", [OutboundBubble("hello")])
        self.daytona.exec.return_value = ExecResult(0, f"{RESULT_MARKER}{result}\n")

        with self.assertRaises(SandboxDispatchError) as raised:
            self.dispatch.deliver_telegram(self.installation, 42, self.inbound)

        self.assertEqual(raised.exception.code, "invalid_result")

    def test_deliver_refreshes_once_and_retries_once_when_the_sandbox_reports_expired_auth(self):
        expired = encode_error("telegram:update:42", "auth_expired")
        success = encode_result("telegram:update:42", [OutboundBubble("hello")])
        self.daytona.exec.side_effect = [
            ExecResult(0, f"{RESULT_MARKER}{expired}\n"),
            ExecResult(0, f"{RESULT_MARKER}{success}\n"),
        ]

        self.assertEqual(self.dispatch.deliver_telegram(self.installation, 42, self.inbound), [OutboundBubble("hello")])

        self.auth.refresh.assert_not_called()
        self.auth.access_token.assert_has_calls([unittest.mock.call(force_refresh=False), unittest.mock.call(force_refresh=True)])
        self.assertEqual(self.daytona.exec.call_count, 2)

    def test_deliver_reads_xai_environment_overrides_on_each_turn(self):
        first = encode_result("telegram:update:1", [OutboundBubble("first")])
        second = encode_result("telegram:update:2", [OutboundBubble("second")])
        self.daytona.exec.side_effect = [
            ExecResult(0, f"{RESULT_MARKER}{first}\n"),
            ExecResult(0, f"{RESULT_MARKER}{second}\n"),
        ]

        self.dispatch.deliver_telegram(self.installation, 1, self.inbound)
        with patch.dict("os.environ", {"TOMO_XAI_MODEL": "grok-next", "TOMO_XAI_REASONING_EFFORT": "low"}):
            self.dispatch.deliver_telegram(self.installation, 2, self.inbound)

        first_env = self.daytona.exec.call_args_list[0].kwargs["env"]
        second_env = self.daytona.exec.call_args_list[1].kwargs["env"]
        self.assertEqual(first_env["TOMO_XAI_MODEL"], "grok-4.5")
        self.assertEqual(first_env["TOMO_XAI_REASONING_EFFORT"], "high")
        self.assertEqual(second_env["TOMO_XAI_MODEL"], "grok-next")
        self.assertEqual(second_env["TOMO_XAI_REASONING_EFFORT"], "low")

    def test_deliver_refreshes_once_when_auth_expired_result_exits_nonzero(self):
        expired = encode_error("telegram:update:42", "auth_expired")
        success = encode_result("telegram:update:42", [OutboundBubble("hello")])
        self.daytona.exec.side_effect = [
            ExecResult(1, f"{RESULT_MARKER}{expired}\n"),
            ExecResult(0, f"{RESULT_MARKER}{success}\n"),
        ]

        self.assertEqual(self.dispatch.deliver_telegram(self.installation, 42, self.inbound), [OutboundBubble("hello")])

        self.auth.access_token.assert_has_calls([unittest.mock.call(force_refresh=False), unittest.mock.call(force_refresh=True)])
        self.assertEqual(self.daytona.exec.call_count, 2)

    def test_deliver_rejects_a_nonzero_exit_with_a_safe_code(self):
        self.daytona.exec.return_value = ExecResult(1, "provider traceback")

        with self.assertRaises(SandboxDispatchError) as raised:
            self.dispatch.deliver_telegram(self.installation, 42, self.inbound)

        self.assertEqual(raised.exception.code, "sandbox_exec_failed")

    def test_deliver_rejects_nonzero_success_or_invalid_result_markers_with_a_safe_code(self):
        success = encode_result("telegram:update:42", [OutboundBubble("hello")])
        for output in (f"{RESULT_MARKER}{success}\n", f"{RESULT_MARKER}not-json\n"):
            with self.subTest(output=output):
                self.daytona.exec.return_value = ExecResult(1, output)

                with self.assertRaises(SandboxDispatchError) as raised:
                    self.dispatch.deliver_telegram(self.installation, 42, self.inbound)

                self.assertEqual(raised.exception.code, "sandbox_exec_failed")

    def test_deliver_rejects_an_execution_timeout_with_a_safe_code(self):
        self.daytona.exec.side_effect = TimeoutError()

        with self.assertRaises(SandboxDispatchError) as raised:
            self.dispatch.deliver_telegram(self.installation, 42, self.inbound)

        self.assertEqual(raised.exception.code, "sandbox_timeout")


if __name__ == "__main__":
    unittest.main()
