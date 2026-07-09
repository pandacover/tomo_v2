import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from tomo_core.models import InboundEnvelope
from tomo_core.providers import StaticProvider
from tomo_core.sandbox_inbound import RESULT_MARKER, SandboxInboundError, build_runtime, run_once
from tomo_core.sandbox_protocol import encode_inbound


class SandboxInboundTests(unittest.TestCase):
    def test_run_once_runs_an_envelope_without_a_telegram_client(self):
        envelope = InboundEnvelope(connector="telegram", actor_id="user-1", message_id="message-1", text="hello")
        stdout = io.StringIO()
        provider = Mock()
        runtime = Mock()
        runtime.handle_telegram_text.return_value = [{"text": "hello back", "reply_to_message_id": "message-1"}]

        with tempfile.TemporaryDirectory() as data_dir:
            with patch("tomo_core.sandbox_inbound.build_runtime", return_value=runtime) as build_runtime:
                result = run_once(
                    io.StringIO(encode_inbound("request-1", envelope)),
                    stdout,
                    data_dir=data_dir,
                    provider=provider,
                )

        self.assertEqual(result, 0)
        build_runtime.assert_called_once_with(provider, data_dir)
        runtime.handle_telegram_text.assert_called_once_with(envelope)
        self.assertEqual(
            stdout.getvalue(),
            f'{RESULT_MARKER}{json.dumps({"ok": True, "request_id": "request-1", "bubbles": [{"text": "hello back", "reply_to_message_id": "message-1"}]}, separators=(",", ":"))}\n',
        )

    def test_run_once_returns_a_typed_secret_safe_failure(self):
        token = "secret-access-token"
        stdout = io.StringIO()
        provider = Mock()

        with self.assertRaises(SandboxInboundError) as raised:
            run_once(io.StringIO("not json"), stdout, data_dir="/tmp/data", provider=provider, secret_values=(token,))

        self.assertEqual(raised.exception.code, "invalid_inbound")
        self.assertNotIn(token, str(raised.exception))
        self.assertEqual(
            stdout.getvalue(),
            f'{RESULT_MARKER}{json.dumps({"ok": False, "error": {"code": "invalid_inbound"}}, separators=(",", ":"))}\n',
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
                    data_dir="/tmp/data",
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
            runtime = build_runtime(StaticProvider("hello back"), data_dir)
            self.assertEqual(runtime.telegram.__class__.__name__, "_NoopTelegramSink")

            result = run_once(
                io.StringIO(encode_inbound("request-1", envelope)),
                stdout,
                data_dir=data_dir,
                provider=StaticProvider("hello back"),
            )

        self.assertEqual(result, 0)
        self.assertIn('"text":"hello back"', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
