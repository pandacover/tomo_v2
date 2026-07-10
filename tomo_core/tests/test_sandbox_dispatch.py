import tempfile
import unittest
from unittest.mock import Mock, patch

from tomo_core.daytona_client import ExecResult, SandboxHandle
from tomo_core.models import InboundEnvelope
from tomo_core.sandbox_dispatch import SandboxDispatch, SandboxDispatchError
from tomo_core.sandbox_registry import SandboxRegistry
from tomo_core.sandbox_protocol import RESULT_MARKER, encode_error, encode_result
from tomo_core.models import OutboundBubble
from tomo_core.onboarding_store import TelegramInstallation


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
