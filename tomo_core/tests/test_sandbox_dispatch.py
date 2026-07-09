import json
import tempfile
import unittest
from unittest.mock import Mock

from tomo_core.daytona_client import ExecResult, SandboxHandle
from tomo_core.models import InboundEnvelope
from tomo_core.sandbox_dispatch import SandboxDispatch, SandboxDispatchError
from tomo_core.sandbox_registry import SandboxRegistry


class SandboxDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SandboxRegistry(self.tmp.name)
        self.registry.upsert("tomo-a", "sbx-1", "base", "ready")
        self.daytona = Mock()
        self.daytona.get.return_value = SandboxHandle("sbx-1", self.registry.get("tomo-a").sandbox_name)
        self.tokens = Mock(return_value="fresh-token")
        self.dispatch = SandboxDispatch(self.registry, self.daytona, self.tokens)
        self.inbound = InboundEnvelope(connector="telegram", actor_id="user", message_id="message", text="hello")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dispatch_uses_a_fresh_token_and_returns_marked_bubbles(self):
        result = {"ok": True, "request_id": "request-1", "bubbles": [{"text": "hello", "reply_to_message_id": "message"}]}
        self.daytona.exec.return_value = ExecResult(0, f"TOMO_SANDBOX_RESULT:{json.dumps(result, separators=(',', ':'))}\n")

        bubbles = self.dispatch.dispatch("tomo-a", "request-1", self.inbound)

        self.assertEqual(bubbles[0].text, "hello")
        self.tokens.assert_called_once_with()
        command = self.daytona.exec.call_args.args[1]
        self.assertIn("tomo-core sandbox inbound --once", command)
        env = self.daytona.exec.call_args.kwargs["env"]
        self.assertEqual(env["TOMO_DATA_DIR"], "/home/daytona/.tomo")
        self.assertEqual(env["TOMO_SUPERGROK_ACCESS_TOKEN"], "fresh-token")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)

    def test_dispatch_rejects_a_result_with_the_wrong_request_id(self):
        result = {"ok": True, "request_id": "other", "bubbles": [{"text": "hello"}]}
        self.daytona.exec.return_value = ExecResult(0, f"TOMO_SANDBOX_RESULT:{json.dumps(result)}\n")

        with self.assertRaises(SandboxDispatchError) as raised:
            self.dispatch.dispatch("tomo-a", "request-1", self.inbound)

        self.assertEqual(raised.exception.code, "invalid_result")


if __name__ == "__main__":
    unittest.main()
