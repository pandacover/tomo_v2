import tempfile
import unittest

from tomo_core.instances import RuntimeInstanceRegistry
from tomo_core.models import RuntimeConfig
from tomo_core.providers import StaticProvider
from tomo_core.runtime import PersonalAgentRuntime
from tomo_core.telegram import FakeTelegramClient, TelegramDeliverySink
from tomo_core.tools import BoundTool, ToolRegistry, ToolSpec


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

    def test_runtime_uses_exact_bound_registry_schemas_and_tool_budget(self):
        tool_registry = self._registry()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = PersonalAgentRuntime(
                self._tool_provider(),
                TelegramDeliverySink(FakeTelegramClient()),
                RuntimeConfig(data_dir=tmp),
                tool_registry=tool_registry,
            )

        self.assertEqual(
            [schema["function"]["name"] for schema in runtime.conversation.tool_registry.schemas()],
            ["search_memories", "search_sessions", "lookup"],
        )
        self.assertEqual(runtime.conversation.budget, runtime.config.tool_turn_budget)

    def test_runtime_rejects_bound_tools_when_provider_does_not_support_tool_calls(self):
        provider = StaticProvider("ok")
        provider.supports_tool_calls = False
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "does not support tool calls"):
                PersonalAgentRuntime(
                    provider,
                    TelegramDeliverySink(FakeTelegramClient()),
                    RuntimeConfig(data_dir=tmp),
                    tool_registry=self._registry(),
                )

    def test_runtime_allows_empty_registry_when_provider_does_not_support_tool_calls(self):
        provider = StaticProvider("ok")
        provider.supports_tool_calls = False
        with tempfile.TemporaryDirectory() as tmp:
            runtime = PersonalAgentRuntime(
                provider,
                TelegramDeliverySink(FakeTelegramClient()),
                RuntimeConfig(data_dir=tmp),
                tool_registry=ToolRegistry(),
            )

        self.assertEqual(runtime.conversation.tool_registry.schemas(), ())

    def test_instance_registry_passes_exact_factory_registry_to_runtime(self):
        tool_registry = self._registry()
        factory_calls = []
        with tempfile.TemporaryDirectory() as tmp:
            registry = RuntimeInstanceRegistry(
                data_dir=tmp,
                provider_factory=lambda _tomo_id: self._tool_provider(),
                telegram_client=FakeTelegramClient(),
                tool_registry_factory=lambda tomo_id: factory_calls.append(tomo_id) or tool_registry,
            )
            runtime = registry.get("tomo-a")

        self.assertEqual(factory_calls, ["tomo-a"])
        self.assertEqual(
            [schema["function"]["name"] for schema in runtime.conversation.tool_registry.schemas()],
            ["search_memories", "search_sessions", "lookup"],
        )

    def test_default_runtime_registry_includes_owner_bound_personal_search_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = RuntimeInstanceRegistry(
                data_dir=tmp,
                provider_factory=lambda _tomo_id: self._tool_provider(),
                telegram_client=FakeTelegramClient(),
            ).get("tomo-a")

        self.assertEqual([schema["function"]["name"] for schema in runtime.conversation.tool_registry.schemas()], ["search_memories", "search_sessions"])
        self.assertEqual(runtime.conversation.budget, runtime.config.tool_turn_budget)

    def test_instance_registry_rejects_non_registry_factory_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = RuntimeInstanceRegistry(
                data_dir=tmp,
                provider_factory=lambda _tomo_id: StaticProvider("ok"),
                telegram_client=FakeTelegramClient(),
                tool_registry_factory=lambda _tomo_id: object(),
            )

            with self.assertRaisesRegex(ValueError, "tool_registry_factory must return a ToolRegistry"):
                registry.get("tomo-a")

    @staticmethod
    def _registry():
        return ToolRegistry((BoundTool(ToolSpec("lookup", "Looks up a record.", {"type": "object", "properties": {}}), lambda _: "found"),))

    @staticmethod
    def _tool_provider():
        provider = StaticProvider("ok")
        provider.supports_tool_calls = True
        return provider
