import unittest
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
