import tempfile
import unittest
from unittest.mock import Mock, patch

from tomo_core.daytona_client import ExecResult, SandboxHandle, SessionCommandHandle
from tomo_core.models import InboundEnvelope
from tomo_core.onboarding_store import InterruptedGeneration, TelegramGenerationInput, TelegramGenerationWork
from tomo_core.sandbox_dispatch import SandboxDispatch, SandboxDispatchError
from tomo_core.sandbox_registry import SandboxRegistry
from tomo_core.sandbox_protocol import EVENT_MARKER, RESULT_MARKER, SandboxErrorEvent, SandboxTracebackFrame, encode_error, encode_event, encode_result
from tomo_core.conversation.models import ConversationCompleted, ConversationMove, ConversationResult, MoveConfidence, MovePlan, UtteranceReady
from tomo_core.models import OutboundBubble
from tomo_core.onboarding_store import TelegramInstallation


def completed_marker(request_id, generation_id, sequence, text, move=ConversationMove.ANSWER):
    plan = MovePlan(move, (), move.value, MoveConfidence.HIGH, (move,))
    completed = ConversationCompleted(ConversationResult(plan, (text,)))
    return EVENT_MARKER + encode_event(request_id, generation_id, sequence, completed) + "\n"


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
                EVENT_MARKER
                + encode_event(
                    "telegram-generation-burst-one-r1",
                    "burst:one/r1",
                    0,
                    UtteranceReady(0, ConversationMove.ACKNOWLEDGE, "hello."),
                )
                + "\n",
                completed_marker("telegram-generation-burst-one-r1", "burst:one/r1", 1, "hello.", ConversationMove.ACKNOWLEDGE),
            ]
        )
        self.daytona.session_command_exit_code.return_value = 0

        events = list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual(events[0].text, "hello.")
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
                EVENT_MARKER
                + encode_event(
                    "telegram-generation-gen-photo",
                    "gen-photo",
                    0,
                    UtteranceReady(0, ConversationMove.ANSWER, "done."),
                )
                + "\n",
                completed_marker("telegram-generation-gen-photo", "gen-photo", 1, "done."),
            ]
        )
        self.daytona.session_command_exit_code.return_value = 0

        list(self.dispatch.iter_telegram_events(self.installation, work))

        env = self.daytona.start_session_command.call_args.kwargs["env"]
        self.assertIn('"attachments":[{"kind":"image","file_id":"large"', env["TOMO_INBOUND_JSON"])
        self.assertIn('"text":"look"', env["TOMO_INBOUND_JSON"])

    def test_iter_telegram_events_rejects_wrong_generation_without_delivery(self):
        work = self._work()
        self.daytona.start_session_command.return_value = SessionCommandHandle("telegram-burst-one-r1", "cmd-1")
        self.daytona.iter_session_logs.return_value = iter(
            [
                EVENT_MARKER
                + encode_event(
                    "telegram-generation-burst-one-r1",
                    "other-generation",
                    0,
                    UtteranceReady(0, ConversationMove.ANSWER, "wrong."),
                )
                + "\n"
            ]
        )

        with self.assertRaises(SandboxDispatchError) as raised:
            list(self.dispatch.iter_telegram_events(self.installation, work))

        self.assertEqual(raised.exception.code, "invalid_result")

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
                    EVENT_MARKER + encode_event("telegram-generation-burst-one-r1", "burst:one/r1", 0, UtteranceReady(0, ConversationMove.ANSWER, "ok.")) + "\n",
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
