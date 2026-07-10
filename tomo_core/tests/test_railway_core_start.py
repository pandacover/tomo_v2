import importlib.util
import io
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "railway_core_start.py"
SPEC = importlib.util.spec_from_file_location("railway_core_start", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
railway_core_start = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(railway_core_start)


class FakeProcess:
    def __init__(self, polls):
        self.polls = iter(polls)
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        result = next(self.polls)
        if result is not None:
            self.returncode = result
        return result

    def terminate(self):
        self.terminated = True
        self.returncode = -signal.SIGTERM

    def kill(self):
        self.killed = True
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        if self.returncode is None:
            raise TimeoutError
        return self.returncode


class RailwayCoreStartTests(unittest.TestCase):
    def hosted_env(self, data_dir: str) -> dict[str, str]:
        return {
            "TOMO_HOSTED_RUNTIME": "daytona",
            "TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "secret-token",
            "TOMO_CORE_DATA_DIR": data_dir,
            "DAYTONA_API_KEY": "daytona-key",
            "TOMO_DAYTONA_SNAPSHOT": "snapshot",
            "TOMO_DAYTONA_SANDBOX_DATA_DIR": "/var/lib/tomo",
            "TOMO_SUPERGROK_OAUTH_JSON_B64": "e30=",
        }

    def test_child_failure_terminates_sibling_and_returns_failure(self):
        control = FakeProcess([None, None])
        telegram = FakeProcess([7])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(railway_core_start.subprocess, "Popen", side_effect=[control, telegram]) as popen,
            patch.object(railway_core_start.time, "sleep"),
            patch.object(railway_core_start.signal, "signal"),
        ):
            code = railway_core_start.start(self.hosted_env(tmp))

        self.assertEqual(code, 7)
        self.assertTrue(control.terminated)
        self.assertFalse(telegram.terminated)
        self.assertEqual(
            [call.args[0] for call in popen.call_args_list],
            [
                ["uv", "run", "tomo-core", "control", "start", "--host", "0.0.0.0", "--port", "8787"],
                ["uv", "run", "tomo-core", "telegram-shared", "start"],
            ],
        )
        self.assertTrue(all(call.kwargs.get("shell") is False for call in popen.call_args_list))

    def test_clean_child_exit_terminates_sibling_and_forces_railway_restart(self):
        control = FakeProcess([0])
        telegram = FakeProcess([None, None])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(railway_core_start.subprocess, "Popen", side_effect=[control, telegram]),
            patch.object(railway_core_start.time, "sleep"),
            patch.object(railway_core_start.signal, "signal"),
        ):
            code = railway_core_start.start(self.hosted_env(tmp))

        self.assertEqual(code, 1)
        self.assertTrue(telegram.terminated)

    def test_production_preflight_rejects_missing_bot_before_spawning(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(railway_core_start.subprocess, "Popen") as popen,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            code = railway_core_start.start(
                {
                    "RAILWAY_ENVIRONMENT_NAME": "production",
                    "TOMO_CORE_DATA_DIR": tmp,
                    "DAYTONA_API_KEY": "daytona-api-key",
                    "TOMO_DAYTONA_SNAPSHOT": "snapshot",
                    "TOMO_DAYTONA_SANDBOX_DATA_DIR": "/var/lib/tomo",
                    "TOMO_SUPERGROK_OAUTH_JSON_B64": "e30=",
                }
            )

        self.assertEqual(code, 2)
        popen.assert_not_called()
        self.assertIn("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN", stderr.getvalue())
        self.assertNotIn("daytona-api-key", stderr.getvalue())

    def test_control_only_requires_explicit_local_runtime(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(railway_core_start.subprocess, "Popen") as popen,
        ):
            code = railway_core_start.start({"TOMO_CORE_DATA_DIR": tmp})

        self.assertEqual(code, 2)
        popen.assert_not_called()

    def test_spawn_failure_stops_already_started_child_without_logging_error_details(self):
        control = FakeProcess([None, None])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                railway_core_start.subprocess,
                "Popen",
                side_effect=[control, OSError("listener-secret")],
            ),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            code = railway_core_start.start(self.hosted_env(tmp))

        self.assertEqual(code, 1)
        self.assertTrue(control.terminated)
        self.assertNotIn("listener-secret", stderr.getvalue())

    def test_without_bot_token_supervises_control_api_only(self):
        control = FakeProcess([0])
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(railway_core_start.subprocess, "Popen", return_value=control) as popen,
            patch.object(railway_core_start.time, "sleep"),
            patch.object(railway_core_start.signal, "signal"),
        ):
            code = railway_core_start.start({"TOMO_HOSTED_RUNTIME": "local", "TOMO_CORE_DATA_DIR": tmp, "TOMO_CONTROL_HOST": "127.0.0.1", "TOMO_CONTROL_PORT": "9999"})

        self.assertEqual(code, 1)
        self.assertEqual(popen.call_count, 1)
        self.assertNotIn("secret-token", str(popen.call_args_list))

    def test_sigterm_is_forwarded_to_all_children(self):
        control = FakeProcess([None, None])
        telegram = FakeProcess([None, None])
        handlers = {}

        def register_signal(signum, handler):
            handlers[signum] = handler

        def interrupt_supervisor(_seconds):
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(railway_core_start.subprocess, "Popen", side_effect=[control, telegram]),
            patch.object(railway_core_start.time, "sleep", side_effect=interrupt_supervisor),
            patch.object(railway_core_start.signal, "signal", side_effect=register_signal),
        ):
            code = railway_core_start.start(self.hosted_env(tmp))

        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertTrue(control.terminated)
        self.assertTrue(telegram.terminated)
        self.assertEqual(set(handlers), {signal.SIGTERM, signal.SIGINT})


if __name__ == "__main__":
    unittest.main()
