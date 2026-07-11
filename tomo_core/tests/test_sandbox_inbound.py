import io
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from tomo_core.conversation import ConversationCompleted, ConversationMove, ConversationResult, MoveConfidence, MovePlan, UtteranceReady
from tomo_core.models import InboundEnvelope, InboundMessage, InputBurst, OutboundBubble, RuntimeConfig
from tomo_core.runtime import RuntimeCompleted, RuntimeUtteranceReady
from tomo_core.sandbox_inbound import CollectingTelegramSink, SandboxInboundError, build_runtime, run_once
from tomo_core.sandbox_protocol import EVENT_MARKER, SandboxErrorEvent, SandboxUtteranceEvent, encode_inbound, iter_event_markers


class SandboxInboundTests(unittest.TestCase):
    class ScriptedProvider:
        name = "scripted"
        supports_images_in = False
        supports_images_out = False
        supports_tool_calls = False

        def __init__(self):
            self.responses = [
                '{"primary_move":"answer","supporting_moves":["acknowledge"],"move_sequence":["acknowledge","answer"],"response_goal":"answer","confidence":"high"}',
                '{"utterance":"hello back."}',
                '{"utterance":"second bubble."}',
            ]

        def complete(self, messages, actor_id=None):
            return self.responses.pop(0)

    def test_run_once_runs_an_envelope_without_a_telegram_client(self):
        burst = self._burst()
        stdout = io.StringIO()
        provider = Mock()
        runtime = Mock()
        plan = MovePlan(ConversationMove.ANSWER, (), "answer", MoveConfidence.HIGH, (ConversationMove.ANSWER,))
        runtime.handle_telegram_burst_iter.return_value = iter([
            RuntimeUtteranceReady(UtteranceReady(0, ConversationMove.ANSWER, "hello back."), OutboundBubble("hello back.", "message-1")),
            RuntimeCompleted(ConversationCompleted(ConversationResult(plan, ("hello back.",)))),
        ])

        with tempfile.TemporaryDirectory() as data_dir:
            with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime) as build_runtime:
                result = run_once(
                    io.StringIO(encode_inbound("request-1", burst)),
                    stdout,
                    config=RuntimeConfig(data_dir=data_dir),
                    provider=provider,
                )

        self.assertEqual(result, 0)
        build_runtime.assert_called_once_with(provider, RuntimeConfig(data_dir=data_dir))
        runtime.handle_telegram_burst_iter.assert_called_once_with(burst)
        self.assertEqual(stdout.getvalue().count(EVENT_MARKER), 2)
        events = list(iter_event_markers(stdout.getvalue().splitlines(keepends=True), "request-1", "gen-1"))
        self.assertEqual(events[0], SandboxUtteranceEvent(0, ConversationMove.ANSWER, "hello back."))

    def test_run_once_returns_a_typed_secret_safe_failure(self):
        token = "secret-access-token"
        stdout = io.StringIO()
        provider = Mock()

        with self.assertRaises(SandboxInboundError) as raised:
            run_once(io.StringIO("not json"), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=provider, secret_values=(token,))

        self.assertEqual(raised.exception.code, "invalid_request")
        self.assertNotIn(token, str(raised.exception))
        self.assertEqual(stdout.getvalue().count(EVENT_MARKER), 1)
        self.assertEqual(list(iter_event_markers([stdout.getvalue()], "unknown", "unknown")), [SandboxErrorEvent(0, "invalid_request")])

    def test_run_once_wraps_runtime_failures_without_exposing_the_access_token(self):
        burst = self._burst()
        token = "secret-access-token"
        stdout = io.StringIO()
        runtime = Mock()
        runtime.handle_telegram_burst_iter.side_effect = RuntimeError(f"authorization failed: {token}")

        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError) as raised:
                run_once(
                    io.StringIO(encode_inbound("request-1", burst)),
                    stdout,
                    config=RuntimeConfig(data_dir="/tmp/data"),
                    provider=Mock(),
                    secret_values=(token,),
                )

        self.assertEqual(raised.exception.code, "runtime_failed")
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(token, stdout.getvalue())

    def test_run_once_uses_a_local_delivery_sink_for_a_real_runtime_turn(self):
        burst = self._burst()
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as data_dir:
            runtime = build_runtime(self.ScriptedProvider(), RuntimeConfig(data_dir=data_dir))
            self.assertIsInstance(runtime.telegram, CollectingTelegramSink)

            result = run_once(
                io.StringIO(encode_inbound("request-1", burst)),
                stdout,
                config=RuntimeConfig(data_dir=data_dir),
                provider=self.ScriptedProvider(),
            )

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().count(EVENT_MARKER), 3)
        events = list(iter_event_markers(stdout.getvalue().splitlines(keepends=True), "request-1", "gen-1"))
        self.assertEqual([event.text for event in events if isinstance(event, SandboxUtteranceEvent)], ["hello back.", "second bubble."])

    def test_run_once_maps_http_401_to_auth_expired_without_exception_text(self):
        burst = self._burst()
        token = "secret-access-token"
        stdout = io.StringIO()
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"))
        runtime = Mock()
        runtime.handle_telegram_burst_iter.side_effect = httpx.HTTPStatusError(f"unauthorized: {token}", request=response.request, response=response)

        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError) as raised:
                run_once(
                    io.StringIO(encode_inbound("request-1", burst)),
                    stdout,
                    config=RuntimeConfig(data_dir="/tmp/data"),
                    provider=Mock(),
                    secret_values=(token,),
                )

        self.assertEqual(raised.exception.code, "auth_expired")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(token, stdout.getvalue())
        self.assertEqual(stdout.getvalue().count(EVENT_MARKER), 1)

    def test_failure_after_visible_output_uses_next_sequence(self):
        burst = self._burst()
        stdout = io.StringIO()
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"))
        runtime = Mock()

        def events():
            yield RuntimeUtteranceReady(
                UtteranceReady(0, ConversationMove.ANSWER, "visible."),
                OutboundBubble("visible.", "message-1"),
            )
            raise httpx.HTTPStatusError("unauthorized", request=response.request, response=response)

        runtime.handle_telegram_burst_iter.return_value = events()
        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError):
                run_once(
                    io.StringIO(encode_inbound("request-1", burst)),
                    stdout,
                    config=RuntimeConfig(data_dir="/tmp/data"),
                    provider=Mock(),
                )

        parsed = list(iter_event_markers(stdout.getvalue().splitlines(keepends=True), "request-1", "gen-1"))
        self.assertEqual(parsed[-1], SandboxErrorEvent(1, "auth_expired"))

    def test_run_once_flushes_each_incremental_event(self):
        class RecordingStdout(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flushes = 0

            def flush(self):
                self.flushes += 1

        stdout = RecordingStdout()
        with tempfile.TemporaryDirectory() as data_dir:
            result = run_once(
                io.StringIO(encode_inbound("request-1", self._burst())),
                stdout,
                config=RuntimeConfig(data_dir=data_dir),
                provider=self.ScriptedProvider(),
            )

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().count(EVENT_MARKER), stdout.flushes)
        self.assertEqual(stdout.flushes, 3)

    def _burst(self) -> InputBurst:
        return InputBurst(
            "burst-1",
            "gen-1",
            1,
            (InboundMessage(1, 41, InboundEnvelope("telegram", "user-1", "message-1", "hello")),),
        )


if __name__ == "__main__":
    unittest.main()
