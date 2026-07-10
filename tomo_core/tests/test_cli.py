import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tomo_core.cli import main


class CliTests(unittest.TestCase):
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

    def test_telegram_shared_ignores_static_response_environment_without_explicit_flag(self):
        with patch.dict("os.environ", {"TOMO_CORE_STATIC_RESPONSE": "test"}, clear=True):
            self.assertEqual(main(["telegram-shared", "start", "--token", "token"]), 2)

    def test_sandbox_inbound_reads_environment_payload_and_uses_only_the_supergrok_access_token(self):
        token = "supergrok-access-token"
        with (
            patch.dict("os.environ", {"TOMO_SUPERGROK_ACCESS_TOKEN": token, "TOMO_INBOUND_JSON": "payload", "TOMO_CORE_DATA_DIR": "/data"}, clear=True),
            patch("tomo_core.cli.supergrok_oauth_provider_from_access_token") as provider_factory,
            patch("tomo_core.cli.run_once", return_value=0) as run_once,
            patch("sys.stdout", io.StringIO()),
        ):
            code = main(["sandbox-inbound"])

        self.assertEqual(code, 0)
        provider_factory.assert_called_once_with(token)
        self.assertEqual(run_once.call_args.args[0].read(), "payload")
        self.assertEqual(run_once.call_args.kwargs["data_dir"], "/data")
        self.assertEqual(run_once.call_args.kwargs["secret_values"], (token,))

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
