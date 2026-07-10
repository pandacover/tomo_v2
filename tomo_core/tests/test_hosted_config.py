import base64
import tempfile
import unittest
from pathlib import Path

from tomo_core.hosted_config import HostedRuntimeConfig


class HostedRuntimeConfigTests(unittest.TestCase):
    def test_explicit_local_mode_requires_only_token_and_writable_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = HostedRuntimeConfig.from_env(
                {
                    "TOMO_HOSTED_RUNTIME": "local",
                    "TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "bot-token",
                    "TOMO_CORE_DATA_DIR": tmp,
                }
            )

        self.assertEqual(config.runtime, "local")
        self.assertEqual(config.worker_count, 4)
        self.assertEqual(config.poll_timeout, 30)

    def test_static_response_explicitly_selects_local_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = HostedRuntimeConfig.from_env(
                {"TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "bot-token", "TOMO_CORE_DATA_DIR": tmp},
                static_response="hello",
            )

        self.assertEqual(config.runtime, "local")

    def test_implicit_local_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "TOMO_HOSTED_RUNTIME"):
                HostedRuntimeConfig.from_env({"TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "bot-token", "TOMO_CORE_DATA_DIR": tmp})

    def test_daytona_mode_loads_validated_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = HostedRuntimeConfig.from_env(
                {
                    "TOMO_HOSTED_RUNTIME": "daytona",
                    "TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "bot-token",
                    "TOMO_CORE_DATA_DIR": tmp,
                    "DAYTONA_API_KEY": "daytona-key",
                    "TOMO_DAYTONA_SNAPSHOT_NAME": "tomo-snapshot",
                    "TOMO_SUPERGROK_OAUTH_JSON_B64": base64.b64encode(b"{}").decode("ascii"),
                    "TOMO_TELEGRAM_POLL_TIMEOUT": "45",
                    "TOMO_ROUTER_WORKERS": "8",
                }
            )

        self.assertEqual(config.runtime, "daytona")
        self.assertEqual(config.poll_timeout, 45)
        self.assertEqual(config.worker_count, 8)

    def test_daytona_missing_configuration_reports_names_not_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as raised:
                HostedRuntimeConfig.from_env(
                    {"TOMO_HOSTED_RUNTIME": "daytona", "TOMO_CORE_DATA_DIR": tmp, "DAYTONA_API_KEY": "secret-value"}
                )

        self.assertIn("TOMO_TELEGRAM_GLOBAL_BOT_TOKEN", str(raised.exception))
        self.assertIn("TOMO_DAYTONA_SNAPSHOT_NAME", str(raised.exception))
        self.assertIn("TOMO_SUPERGROK_OAUTH_JSON_B64", str(raised.exception))
        self.assertNotIn("secret-value", str(raised.exception))

    def test_production_railway_defaults_to_daytona(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "DAYTONA_API_KEY"):
                HostedRuntimeConfig.from_env(
                    {
                        "RAILWAY_ENVIRONMENT_NAME": "production",
                        "TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "bot-token",
                        "TOMO_CORE_DATA_DIR": tmp,
                    }
                )

    def test_invalid_worker_count_and_oauth_payload_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {
                "TOMO_HOSTED_RUNTIME": "daytona",
                "TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "bot-token",
                "TOMO_CORE_DATA_DIR": tmp,
                "DAYTONA_API_KEY": "daytona-key",
                "TOMO_DAYTONA_SNAPSHOT_NAME": "tomo-snapshot",
                "TOMO_SUPERGROK_OAUTH_JSON_B64": base64.b64encode(b"{}").decode("ascii"),
                "TOMO_ROUTER_WORKERS": "0",
            }
            with self.assertRaisesRegex(ValueError, "TOMO_ROUTER_WORKERS"):
                HostedRuntimeConfig.from_env(base)
            base["TOMO_ROUTER_WORKERS"] = "4"
            base["TOMO_SUPERGROK_OAUTH_JSON_B64"] = "not-base64"
            with self.assertRaisesRegex(ValueError, "TOMO_SUPERGROK_OAUTH_JSON_B64"):
                HostedRuntimeConfig.from_env(base)

    def test_unwritable_data_path_is_rejected_without_echoing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_file = Path(tmp) / "not-a-directory"
            data_file.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "TOMO_CORE_DATA_DIR") as raised:
                HostedRuntimeConfig.from_env(
                    {
                        "TOMO_HOSTED_RUNTIME": "local",
                        "TOMO_TELEGRAM_GLOBAL_BOT_TOKEN": "bot-token",
                        "TOMO_CORE_DATA_DIR": str(data_file),
                    }
                )

        self.assertNotIn(str(data_file), str(raised.exception))
