import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from tomo_core.models import InboundEnvelope, OutboundBubble, RuntimeConfig
from tomo_core.providers import StaticProvider
from tomo_core.sandbox_inbound import CollectingTelegramSink, SandboxInboundError, build_runtime, run_once
from tomo_core.sandbox_protocol import RESULT_MARKER, encode_inbound


class SandboxInboundTests(unittest.TestCase):
    class ScriptedProvider:
        name = "scripted"
        supports_images_in = False
        supports_images_out = False
        supports_tool_calls = False

        def __init__(self):
            self.responses = [
                '{"primary_move":"answer","supporting_moves":[],"response_goal":"answer","confidence":"high"}',
                '{"utterances":["hello back.","second bubble."]}',
            ]

        def complete(self, messages, actor_id=None):
            return self.responses.pop(0)

    def test_run_once_runs_an_envelope_without_a_telegram_client(self):
        envelope = InboundEnvelope(connector="telegram", actor_id="user-1", message_id="message-1", text="hello")
        stdout = io.StringIO()
        provider = Mock()
        runtime = Mock()
        runtime.telegram.bubbles = [OutboundBubble(text="hello back", reply_to_message_id="message-1")]

        with tempfile.TemporaryDirectory() as data_dir:
            with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime) as build_runtime:
                result = run_once(
                    io.StringIO(encode_inbound("request-1", envelope)),
                    stdout,
                    config=RuntimeConfig(data_dir=data_dir),
                    provider=provider,
                )

        self.assertEqual(result, 0)
        build_runtime.assert_called_once_with(provider, RuntimeConfig(data_dir=data_dir))
        runtime.handle_telegram_text.assert_called_once_with(envelope)
        self.assertEqual(
            stdout.getvalue(),
            f'{RESULT_MARKER}{json.dumps({"version": 1, "request_id": "request-1", "ok": True, "bubbles": [{"text": "hello back", "reply_to_message_id": "message-1"}]}, separators=(",", ":"))}\n',
        )

    def test_run_once_returns_a_typed_secret_safe_failure(self):
        token = "secret-access-token"
        stdout = io.StringIO()
        provider = Mock()

        with self.assertRaises(SandboxInboundError) as raised:
            run_once(io.StringIO("not json"), stdout, config=RuntimeConfig(data_dir="/tmp/data"), provider=provider, secret_values=(token,))

        self.assertEqual(raised.exception.code, "invalid_request")
        self.assertNotIn(token, str(raised.exception))
        self.assertEqual(
            stdout.getvalue(),
            f'{RESULT_MARKER}{json.dumps({"version": 1, "request_id": "unknown", "ok": False, "error": {"code": "invalid_request"}}, separators=(",", ":"))}\n',
        )

    def test_run_once_wraps_runtime_failures_without_exposing_the_access_token(self):
        envelope = InboundEnvelope(connector="telegram", actor_id="user-1", message_id="message-1", text="hello")
        token = "secret-access-token"
        stdout = io.StringIO()
        runtime = Mock()
        runtime.handle_telegram_text.side_effect = RuntimeError(f"authorization failed: {token}")

        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError) as raised:
                run_once(
                    io.StringIO(encode_inbound("request-1", envelope)),
                    stdout,
                    config=RuntimeConfig(data_dir="/tmp/data"),
                    provider=Mock(),
                    secret_values=(token,),
                )

        self.assertEqual(raised.exception.code, "runtime_failed")
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(token, stdout.getvalue())

    def test_run_once_uses_a_local_delivery_sink_for_a_real_runtime_turn(self):
        envelope = InboundEnvelope(connector="telegram", actor_id="user-1", message_id="message-1", text="hello")
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as data_dir:
            runtime = build_runtime(self.ScriptedProvider(), RuntimeConfig(data_dir=data_dir))
            self.assertIsInstance(runtime.telegram, CollectingTelegramSink)

            result = run_once(
                io.StringIO(encode_inbound("request-1", envelope)),
                stdout,
                config=RuntimeConfig(data_dir=data_dir),
                provider=self.ScriptedProvider(),
            )

        self.assertEqual(result, 0)
        self.assertIn('"text":"hello back."', stdout.getvalue())
        self.assertIn('"text":"second bubble."', stdout.getvalue())
        self.assertIn('"reply_to_message_id":"message-1"', stdout.getvalue())

    def test_run_once_maps_http_401_to_auth_expired_without_exception_text(self):
        envelope = InboundEnvelope(connector="telegram", actor_id="user-1", message_id="message-1", text="hello")
        token = "secret-access-token"
        stdout = io.StringIO()
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"))
        runtime = Mock()
        runtime.handle_telegram_text.side_effect = httpx.HTTPStatusError(f"unauthorized: {token}", request=response.request, response=response)

        with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime):
            with self.assertRaises(SandboxInboundError) as raised:
                run_once(
                    io.StringIO(encode_inbound("request-1", envelope)),
                    stdout,
                    config=RuntimeConfig(data_dir="/tmp/data"),
                    provider=Mock(),
                    secret_values=(token,),
                )

        self.assertEqual(raised.exception.code, "auth_expired")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn(token, stdout.getvalue())
        self.assertEqual(stdout.getvalue().count(RESULT_MARKER), 1)


if __name__ == "__main__":
    unittest.main()
