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
                code = main(["telegram-shared", "start", "--token", "token", "--data-dir", tmp, "--background"])
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
                code = main(["telegram-shared", "restart", "--token", "token", "--data-dir", tmp])
        self.assertEqual(code, 0)
        popen.assert_called_once()
