import tempfile
import unittest

from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.providers import StaticProvider
from tomo_core.telegram import FakeTelegramClient


class RuntimeInstanceRegistryTests(unittest.TestCase):
    def test_same_tomo_id_returns_same_runtime_and_isolated_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = RuntimeInstanceRegistry(data_dir=tmp, provider_factory=lambda _tomo_id: StaticProvider("ok"), telegram_client=FakeTelegramClient())
            first = registry.get("tomo-a")
            second = registry.get("tomo-a")
            other = registry.get("tomo-b")

            self.assertIs(first, second)
            self.assertIsNot(first, other)
            self.assertIn("instances", str(first.config.data_dir))
            self.assertTrue(str(first.config.data_dir).endswith("tomo-a"))
