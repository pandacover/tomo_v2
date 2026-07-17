import io
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch

import httpx

from tomo_core.conversation import Frame, FrameReady, MoveConfidence, MovePlan, SegmentFinish, SegmentResult, TurnRunCompleted, TurnRunResult, TurnRunStatus, TurnUsage
from tomo_core.conversation.parsing import ConversationOutputError
from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst, OutboundBubble, RuntimeConfig
from tomo_core.runtime import RuntimeCompleted, RuntimeFrameReady, StaleSessionRevisionError
from tomo_core.sandbox_inbound import CollectingTelegramSink, SandboxInboundError, build_runtime, run_once
from tomo_core.sandbox_protocol import EVENT_MARKER, SandboxErrorEvent, SandboxFrameEvent, SandboxStaleEvent, encode_inbound, iter_event_markers
from tomo_core.providers import ProviderStreamCompleted, ProviderTextDelta


class SandboxInboundTests(unittest.TestCase):
    class ScriptedProvider:
        name = "scripted"
        supports_images_in = False
        supports_images_out = False
        supports_tool_calls = False

        def stream(self, messages, *, tools=(), actor_id=None):
            payload = '{"type":"turn_plan","primary_move":"answer","supporting_moves":[],"move_sequence":["answer"],"response_goal":"answer","confidence":"high"}\n{"type":"frame","text":"hello back."}\n'
            return iter((ProviderTextDelta(payload), ProviderStreamCompleted("stop")))

    def test_run_once_emits_v4_frames_then_one_completion(self):
        frame, completed = self._runtime_events()
        runtime = Mock()
        runtime.handle_telegram_burst_iter.return_value = iter((frame, completed))
        stdout = io.StringIO()
        provider = Mock()
        config = RuntimeConfig(data_dir="/tmp/data")
        burst = self._burst()
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime) as build_runtime:
            self.assertEqual(run_once(io.StringIO(encode_inbound("request-1", burst)), stdout, config=config, provider=provider), 0)
        build_runtime.assert_called_once_with(provider, config, generation_id="gen-1")
        runtime.handle_telegram_burst_iter.assert_called_once_with(burst)
        events = list(iter_event_markers(stdout.getvalue().splitlines(keepends=True), "request-1", "gen-1"))
        self.assertEqual(events[0], SandboxFrameEvent(0, 0, 0, "hello back."))
        self.assertEqual(events[-1].result["status"], "completed")
        self.assertEqual(stdout.getvalue().count(EVENT_MARKER), 2)

    def test_iterator_exhaustion_after_frames_emits_one_terminal_error(self):
        frame, _ = self._runtime_events()
        runtime = Mock()
        runtime.handle_telegram_burst_iter.return_value = iter((frame,))
        stdout = io.StringIO()
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            self.assertEqual(run_once(io.StringIO(encode_inbound("request-1", self._burst())), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=Mock()), 1)
        events = list(iter_event_markers(stdout.getvalue().splitlines(keepends=True), "request-1", "gen-1"))
        self.assertEqual([type(event) for event in events], [SandboxFrameEvent, SandboxErrorEvent])
        self.assertEqual(events[-1].code, "runtime_missing_terminal")

    def test_stale_runtime_revision_emits_one_terminal_stale_event(self):
        runtime = Mock()
        runtime.handle_telegram_burst_iter.side_effect = StaleSessionRevisionError(12)
        stdout = io.StringIO()
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            self.assertEqual(run_once(io.StringIO(encode_inbound("request-1", self._burst())), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=Mock()), 0)
        self.assertEqual(
            list(iter_event_markers(stdout.getvalue().splitlines(keepends=True), "request-1", "gen-1")),
            [SandboxStaleEvent(0, 12)],
        )

    def test_invalid_input_emits_a_safe_v4_error(self):
        stdout = io.StringIO()
        with self.assertRaises(SandboxInboundError):
            run_once(io.StringIO("not json"), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=Mock(), secret_values=("secret",))
        event = list(iter_event_markers([stdout.getvalue()], "unknown", "unknown"))[0]
        self.assertEqual(event.code, "invalid_request")
        self.assertNotIn("secret", stdout.getvalue())

    def test_run_once_wraps_runtime_failures_without_exposing_the_access_token(self):
        token, stdout, runtime = "secret-access-token", io.StringIO(), Mock()
        runtime.handle_telegram_burst_iter.side_effect = RuntimeError(f"authorization failed: {token}")
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError) as raised:
                run_once(io.StringIO(encode_inbound("request-1", self._burst())), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=Mock(), secret_values=(token,))
        self.assertEqual(raised.exception.code, "runtime_failed")
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(token, stdout.getvalue())
        event = list(iter_event_markers([stdout.getvalue()], "request-1", "gen-1"))[0]
        self.assertEqual(event.exception_class, "RuntimeError")
        self.assertTrue(event.traceback)
        self.assertTrue(all("/" not in frame.basename and "\\" not in frame.basename for frame in event.traceback))
        self.assertNotIn("authorization failed", stdout.getvalue())

    def test_run_once_preserves_safe_conversation_output_code_without_exception_text(self):
        token, stdout, runtime = "secret-access-token", io.StringIO(), Mock()
        runtime.handle_telegram_burst_iter.side_effect = ConversationOutputError("invalid_json")
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError) as raised:
                run_once(
                    io.StringIO(encode_inbound("request-1", self._burst())),
                    stdout,
                    config=RuntimeConfig(data_dir="/tmp/data"),
                    provider=Mock(),
                    secret_values=(token,),
                )

        self.assertEqual(raised.exception.code, "invalid_json")
        event = list(iter_event_markers([stdout.getvalue()], "request-1", "gen-1"))[0]
        self.assertEqual(event.code, "invalid_json")
        self.assertEqual(event.exception_class, "ConversationOutputError")
        self.assertNotIn("invalid conversation output", stdout.getvalue())
        self.assertNotIn(token, stdout.getvalue())

    def test_run_once_bounds_and_sanitizes_long_unsafe_traceback_diagnostics(self):
        stdout, runtime = io.StringIO(), Mock()
        runtime.handle_telegram_burst_iter.side_effect = RuntimeError("secret-access-token")
        summaries = [traceback.FrameSummary(f"/private/path/secret-access-token-{'x' * 400} {index}.py", index + 1, "<string>" if index == 11 else f"<function name {'y' * 400} {index}>") for index in range(12)]
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime), patch("tomo_core.sandbox_inbound.traceback.extract_tb", return_value=summaries):
            with self.assertRaises(SandboxInboundError) as raised:
                run_once(io.StringIO(encode_inbound("request-1", self._burst())), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=Mock(), secret_values=("secret-access-token",))
        self.assertEqual(raised.exception.code, "runtime_failed")
        self.assertLessEqual(len(stdout.getvalue().rstrip("\n")), 900)
        event = list(iter_event_markers([stdout.getvalue()], "request-1", "gen-1"))[0]
        self.assertEqual(event.code, "runtime_failed")
        self.assertEqual(event.exception_class, "RuntimeError")
        self.assertLess(len(event.traceback), 12)
        self.assertTrue(all("<" not in frame.function and " " not in frame.function for frame in event.traceback))
        self.assertEqual(event.traceback[-1].function, "_string_")
        self.assertNotIn("secret-access-token", stdout.getvalue())

    def test_run_once_uses_a_local_delivery_sink_for_a_real_runtime_turn(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as data_dir:
            runtime = build_runtime(self.ScriptedProvider(), RuntimeConfig(data_dir=data_dir))
            self.assertIsInstance(runtime.telegram, CollectingTelegramSink)
            result = run_once(io.StringIO(encode_inbound("request-1", self._burst())), stdout, config=RuntimeConfig(data_dir=data_dir), provider=self.ScriptedProvider())
        self.assertEqual(result, 0)
        events = list(iter_event_markers(stdout.getvalue().splitlines(keepends=True), "request-1", "gen-1"))
        self.assertEqual([event.text for event in events if isinstance(event, SandboxFrameEvent)], ["hello back."])

    def test_run_once_maps_http_401_to_auth_expired_without_exception_text(self):
        token, stdout, runtime = "secret-access-token", io.StringIO(), Mock()
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"))
        runtime.handle_telegram_burst_iter.side_effect = httpx.HTTPStatusError(f"unauthorized: {token}", request=response.request, response=response)
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError) as raised:
                run_once(io.StringIO(encode_inbound("request-1", self._burst())), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=Mock(), secret_values=(token,))
        self.assertEqual(raised.exception.code, "auth_expired")
        self.assertNotIn(token, stdout.getvalue())
        self.assertEqual(stdout.getvalue().count(EVENT_MARKER), 1)

    def test_failure_after_visible_frame_uses_next_sequence(self):
        stdout, runtime = io.StringIO(), Mock()
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"))
        frame, _ = self._runtime_events()
        def events():
            yield frame
            raise httpx.HTTPStatusError("unauthorized", request=response.request, response=response)
        runtime.handle_telegram_burst_iter.return_value = events()
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError):
                run_once(io.StringIO(encode_inbound("request-1", self._burst())), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=Mock())
        parsed = list(iter_event_markers(stdout.getvalue().splitlines(keepends=True), "request-1", "gen-1"))
        self.assertEqual((parsed[-1].sequence, parsed[-1].code), (1, "auth_expired"))

    def test_run_once_flushes_each_incremental_event(self):
        class RecordingStdout(io.StringIO):
            def __init__(self): super().__init__(); self.flushes = 0
            def flush(self): self.flushes += 1
        stdout = RecordingStdout()
        with tempfile.TemporaryDirectory() as data_dir:
            result = run_once(io.StringIO(encode_inbound("request-1", self._burst())), stdout, config=RuntimeConfig(data_dir=data_dir), provider=self.ScriptedProvider())
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().count(EVENT_MARKER), stdout.flushes)
        self.assertEqual(stdout.flushes, 2)

    def _burst(self):
        return InputBurst("burst-1", "gen-1", 1, (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "message-1", "hello")),))

    def _runtime_events(self):
        frame = Frame(0, 0, "hello back.")
        plan = MovePlan("answer", (), "answer", MoveConfidence.HIGH, ("answer",))
        result = TurnRunResult(plan, (SegmentResult(0, (frame,), (), SegmentFinish.COMPLETE),), (frame,), TurnUsage(1, 0, 0, 1), TurnRunStatus.COMPLETED)
        return RuntimeFrameReady(FrameReady(0, frame), OutboundBubble("hello back.", "message-1")), RuntimeCompleted(TurnRunCompleted(result))


if __name__ == "__main__":
    unittest.main()
