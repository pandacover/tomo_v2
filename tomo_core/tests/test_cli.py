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

    def test_sandbox_inbound_uses_only_the_supergrok_access_token_and_daytona_data_dir(self):
        token = "supergrok-access-token"
        with (
            patch.dict("os.environ", {"TOMO_SUPERGROK_ACCESS_TOKEN": token}, clear=True),
            patch("tomo_core.cli.supergrok_oauth_provider_from_access_token") as provider_factory,
            patch("tomo_core.cli.run_once", return_value=0) as run_once,
            patch("sys.stdin", io.StringIO()),
            patch("sys.stdout", io.StringIO()),
        ):
            code = main(["sandbox", "inbound", "--once"])

        self.assertEqual(code, 0)
        provider_factory.assert_called_once_with(token)
        self.assertEqual(run_once.call_args.kwargs["data_dir"], "/home/daytona/.tomo")
        self.assertEqual(run_once.call_args.kwargs["secret_values"], (token,))
