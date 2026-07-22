import io
import json
import os
import signal
import tempfile
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from tomo_core.cli import _log_shared_gateway_error, build_vision_interpreter, main
from tomo_core.cron_models import CronJob, JobIntent, ScheduleSpec
from tomo_core.cron_store import CronStore
from tomo_core.telegram_router import RetryableTelegramUpdateError
from tomo_core.providers import XaiApiProvider
from tomo_core.peer_exchange import PeerExchange


class CliTests(unittest.TestCase):
    def test_local_vision_interpreter_uses_independent_environment_configuration(self):
        base = XaiApiProvider("key", model="base-model")
        args = SimpleNamespace(static_response=None)
        with (
            patch.dict(os.environ, {"TOMO_XAI_VISION_MODEL": "vision-model", "TOMO_XAI_VISION_REASONING_EFFORT": "medium"}, clear=False),
            patch("tomo_core.cli.build_provider", return_value=base),
        ):
            interpreter = build_vision_interpreter(args, Mock(), Mock())

        self.assertEqual(interpreter._provider.model, "vision-model")
        self.assertEqual(interpreter._provider.reasoning_effort, "medium")

    def test_telegram_start_builds_a_vision_interpreter_with_the_live_client(self):
        with tempfile.TemporaryDirectory() as tmp, patch("tomo_core.cli.TelegramBotApiClient") as client_class, patch("tomo_core.cli.build_oauth_manager") as oauth_builder, patch("tomo_core.cli.build_provider") as provider_builder, patch("tomo_core.cli.build_vision_interpreter") as vision_builder, patch("tomo_core.cli.PersonalAgentRuntime") as runtime_class, patch("tomo_core.cli.TelegramPollingBot") as polling_bot, patch("sys.stdout", io.StringIO()):
            result = main(["telegram", "start", "--token", "token", "--data-dir", tmp])

        self.assertEqual(result, 0)
        vision_builder.assert_called_once()
        self.assertIs(vision_builder.call_args.args[1], oauth_builder.return_value)
        self.assertIs(vision_builder.call_args.args[2], client_class.return_value)
        self.assertIs(runtime_class.call_args.kwargs["vision_interpreter"], vision_builder.return_value)
        polling_bot.return_value.run_forever.assert_called_once()

    def test_cron_operator_inspect_and_run_once_are_owner_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = CronJob(
                "job-1",
                "owner-1",
                "telegram:1",
                JobIntent("check something current"),
                ScheduleSpec.once(datetime.now(timezone.utc) + timedelta(hours=1)),
            )
            CronStore(tmp).create(job)
            with patch("sys.stdout", io.StringIO()) as stdout:
                self.assertEqual(main(["cron", "inspect", "--data-dir", tmp, "--owner", "owner-1", "--job-id", "job-1"]), 0)
                inspected = json.loads(stdout.getvalue())
            self.assertEqual(inspected["intent"]["text"], "check something current")
            self.assertNotIn("capability", inspected)

            with patch("sys.stdout", io.StringIO()) as stdout:
                self.assertEqual(main(["cron", "run-once", "--data-dir", tmp, "--owner", "owner-1", "--job-id", "job-1", "--expected-revision", "1"]), 0)
                requested = json.loads(stdout.getvalue())
            self.assertEqual((requested["job_id"], requested["trigger"], requested["revision"]), ("job-1", "manual", 1))

            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(main(["cron", "inspect", "--data-dir", tmp, "--owner", "other", "--job-id", "job-1"]), 1)

    def test_shared_gateway_error_log_excludes_exception_messages(self):
        error = RetryableTelegramUpdateError("storage_operation_failed")
        error.__cause__ = RuntimeError("token=sensitive-value")

        with patch("sys.stderr", io.StringIO()) as stderr:
            _log_shared_gateway_error(error)

        output = stderr.getvalue()
        self.assertIn("code=storage_operation_failed", output)
        self.assertIn("exception_class=RetryableTelegramUpdateError", output)
        self.assertNotIn("sensitive-value", output)

    def test_control_start_invokes_uvicorn(self):
        with patch("uvicorn.run") as run:
            code = main(["control", "start", "--host", "127.0.0.1", "--port", "9999"])
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_telegram_shared_start_requires_token(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["telegram-shared", "start"]), 2)

    def test_telegram_shared_background_start_returns_and_writes_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.Popen") as popen, patch("tomo_core.cli._pid_is_running", return_value=False):
                popen.return_value.pid = 12345
                code = main(["telegram-shared", "start", "--token", "token", "--data-dir", tmp, "--static-response", "test", "--background"])
                self.assertEqual(code, 0)
                popen.assert_called_once()
                self.assertEqual((Path(tmp) / "telegram_shared.pid").read_text(encoding="utf-8"), "12345")

    def test_telegram_shared_stop_without_pid_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["telegram-shared", "stop", "--data-dir", tmp]), 0)

    def test_telegram_shared_restart_stops_then_starts_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("subprocess.Popen") as popen, patch("tomo_core.cli._pid_is_running", return_value=False):
                popen.return_value.pid = 67890
                code = main(["telegram-shared", "restart", "--token", "token", "--data-dir", tmp, "--static-response", "test"])
        self.assertEqual(code, 0)
        popen.assert_called_once()

    def test_telegram_shared_production_mode_fails_closed_without_hosted_dependencies(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["telegram-shared", "start", "--token", "token"]), 2)

    def test_shared_gateway_sigterm_stops_cron_scheduler(self):
        handlers = {}
        config = SimpleNamespace(
            bot_token="token",
            data_dir=None,
            runtime="local",
            control_public_url=None,
            telegram_delivery_pace_seconds=0,
            telegram_input_debounce_seconds=0,
            poll_timeout=30,
            worker_count=1,
            xai_vision_model="shared-vision-model",
            xai_vision_reasoning_effort="medium",
        )

        def register_signal(signum, handler):
            handlers[signum] = handler

        def run_forever():
            instances.call_args.kwargs["vision_interpreter_factory"]("owner")
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("tomo_core.cli.HostedRuntimeConfig.from_env", return_value=config),
            patch("tomo_core.cli.TelegramBotApiClient"),
            patch("tomo_core.cli.TelegramOnboardingStore"),
            patch("tomo_core.cli.load_or_create_key", return_value=b"k" * 32),
            patch("tomo_core.cli.load_or_create_peer_key", return_value=b"p" * 32),
            patch("tomo_core.cli.CronStore"),
            patch("tomo_core.cli.RuntimeInstanceRegistry") as instances,
            patch("tomo_core.cli.build_vision_interpreter") as vision_builder,
            patch("tomo_core.cli.SharedTelegramGateway") as gateway,
            patch("tomo_core.cli.CronSchedulerService") as scheduler,
            patch("tomo_core.cli.TelegramUpdateRouter") as router,
            patch("tomo_core.cli.signal.signal", side_effect=register_signal),
        ):
            config.data_dir = tmp
            gateway.return_value.dispatch.cancel_generation = None
            router.return_value.run_forever.side_effect = run_forever
            with self.assertRaises(KeyboardInterrupt):
                main(["telegram-shared", "start", "--token", "token", "--data-dir", tmp, "--static-response", "test"])

        scheduler.return_value.start.assert_called_once()
        scheduler.return_value.stop.assert_called_once()
        self.assertEqual(
            vision_builder.call_args.kwargs,
            {"model": "shared-vision-model", "reasoning_effort": "medium"},
        )
        self.assertEqual(gateway.call_args.kwargs["dispatch"].peer_capability_key, b"p" * 32)

    def test_telegram_shared_ignores_static_response_environment_without_explicit_flag(self):
        with patch.dict("os.environ", {"TOMO_CORE_STATIC_RESPONSE": "test"}, clear=True):
            self.assertEqual(main(["telegram-shared", "start", "--token", "token"]), 2)

    def test_sandbox_inbound_reads_environment_payload_and_uses_only_the_supergrok_access_token(self):
        token = "supergrok-access-token"
        with (
            patch.dict("os.environ", {"TOMO_SUPERGROK_ACCESS_TOKEN": token, "TOMO_INBOUND_JSON": "payload", "TOMO_CORE_DATA_DIR": "/data", "TOMO_CORE_SOUL": "/soul", "TOMO_INSTANCE_ID": "tomo-1"}, clear=True),
            patch("tomo_core.cli.supergrok_oauth_provider_from_access_token") as provider_factory,
            patch("tomo_core.cli.run_once", return_value=0) as run_once,
            patch("tomo_core.cli.RuntimeConfig") as runtime_config,
            patch("sys.stdout", io.StringIO()),
        ):
            code = main(["sandbox-inbound"])

        self.assertEqual(code, 0)
        provider_factory.assert_called_once_with(token, model="grok-4.5", reasoning_effort="high")
        runtime_config.assert_called_once_with(data_dir="/data", soul_path="/soul", owner_id="tomo-1")
        self.assertEqual(run_once.call_args.args[0].read(), "payload")
        self.assertIs(run_once.call_args.kwargs["config"], runtime_config.return_value)
        self.assertEqual(run_once.call_args.kwargs["secret_values"], (token,))

    def test_sandbox_inbound_redacts_peer_capability_as_a_secret(self):
        token, peer_capability = "supergrok-access-token", "peer-capability-derived-from-key"
        with (
            patch.dict("os.environ", {"TOMO_SUPERGROK_ACCESS_TOKEN": token, "TOMO_INBOUND_JSON": "payload", "TOMO_CORE_DATA_DIR": "/data", "TOMO_INSTANCE_ID": "tomo-1", "TOMO_PEER_CAPABILITY": peer_capability}, clear=True),
            patch("tomo_core.cli.supergrok_oauth_provider_from_access_token"),
            patch("tomo_core.cli.run_once", return_value=0) as run_once,
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(main(["sandbox-inbound"]), 0)
        self.assertEqual(run_once.call_args.kwargs["secret_values"], (token, peer_capability))

    def test_sandbox_inbound_uses_xai_model_and_reasoning_effort_environment_overrides(self):
        token = "supergrok-access-token"
        with (
            patch.dict(
                "os.environ",
                {
                    "TOMO_SUPERGROK_ACCESS_TOKEN": token,
                    "TOMO_INBOUND_JSON": "payload",
                    "TOMO_CORE_DATA_DIR": "/data",
                    "TOMO_INSTANCE_ID": "tomo-1",
                    "TOMO_XAI_MODEL": "grok-test-next",
                    "TOMO_XAI_REASONING_EFFORT": "low",
                },
                clear=True,
            ),
            patch("tomo_core.cli.supergrok_oauth_provider_from_access_token") as provider_factory,
            patch("tomo_core.cli.run_once", return_value=0),
            patch("sys.stdout", io.StringIO()),
        ):
            code = main(["sandbox-inbound"])

        self.assertEqual(code, 0)
        provider_factory.assert_called_once_with(token, model="grok-test-next", reasoning_effort="low")

    def test_sandbox_health_reads_environment_payload_without_constructing_a_provider(self):
        with (
            patch.dict("os.environ", {"TOMO_INBOUND_JSON": '{"version":1,"type":"inbound","request_id":"health-1","inbound":{"connector":"telegram","actor_id":"health","message_id":"health","text":"health","timestamp":"2026-01-01T00:00:00+00:00"}}'}, clear=True),
            patch("tomo_core.cli.supergrok_oauth_provider_from_access_token") as provider_factory,
            patch("sys.stdout", io.StringIO()),
        ):
            code = main(["sandbox-inbound", "--health"])

        self.assertEqual(code, 0)
        provider_factory.assert_not_called()

    def test_sandbox_health_succeeds_without_inbound_payload_or_model_credentials(self):
        with patch.dict("os.environ", {}, clear=True), patch("sys.stdout", io.StringIO()) as stdout:
            code = main(["sandbox-inbound", "--health"])

        self.assertEqual(code, 0)
        self.assertIn("TOMO_SANDBOX_RESULT=", stdout.getvalue())

    def test_personal_data_owner_deletion_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(["personal-data", "delete-owner", "--data-dir", tmp, "--owner", "owner"]), 2)
            self.assertEqual(main(["personal-data", "integrity-check", "--data-dir", tmp]), 0)
