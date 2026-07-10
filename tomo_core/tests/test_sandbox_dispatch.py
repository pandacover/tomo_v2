import tempfile
import unittest
from unittest.mock import Mock

from tomo_core.daytona_client import ExecResult, SandboxHandle
from tomo_core.models import InboundEnvelope
from tomo_core.sandbox_dispatch import SandboxDispatch, SandboxDispatchError
from tomo_core.sandbox_registry import SandboxRegistry
from tomo_core.sandbox_protocol import RESULT_MARKER, encode_result
from tomo_core.models import OutboundBubble


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
        result = encode_result("request-1", [OutboundBubble("hello", reply_to_message_id="message")])
        self.daytona.exec.return_value = ExecResult(0, f"{RESULT_MARKER}{result}\n")

        bubbles = self.dispatch.dispatch("tomo-a", "request-1", self.inbound)

        self.assertEqual(bubbles[0].text, "hello")
        self.tokens.assert_called_once_with()
        command = self.daytona.exec.call_args.args[1]
        self.assertEqual(command, "/opt/tomo/.venv/bin/tomo-core sandbox-inbound")
        env = self.daytona.exec.call_args.kwargs["env"]
        self.assertEqual(set(env), {"TOMO_INBOUND_JSON", "TOMO_CORE_DATA_DIR", "TOMO_INSTANCE_ID", "TOMO_SUPERGROK_ACCESS_TOKEN"})
        self.assertEqual(env["TOMO_CORE_DATA_DIR"], "/home/daytona/.tomo")
        self.assertEqual(env["TOMO_INSTANCE_ID"], "tomo-a")
        self.assertEqual(env["TOMO_SUPERGROK_ACCESS_TOKEN"], "fresh-token")

    def test_dispatch_rejects_a_result_with_the_wrong_request_id(self):
        result = encode_result("other", [OutboundBubble("hello")])
        self.daytona.exec.return_value = ExecResult(0, f"{RESULT_MARKER}{result}\n")

        with self.assertRaises(SandboxDispatchError) as raised:
            self.dispatch.dispatch("tomo-a", "request-1", self.inbound)

        self.assertEqual(raised.exception.code, "invalid_result")


if __name__ == "__main__":
    unittest.main()
