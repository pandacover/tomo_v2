from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .models import RuntimeConfig
from .providers import ProviderAdapter
from .runtime import PersonalAgentRuntime
from .telegram import TelegramDeliverySink, TelegramClient
from .tools import ToolRegistry
from .vision import VisionInterpreter


class RuntimeInstanceRegistry:
    def __init__(self, data_dir: str | Path, provider_factory: Callable[[str], ProviderAdapter], telegram_client: TelegramClient, soul_path: str = "SOUL.md", tool_registry_factory: Callable[[str], ToolRegistry] | None = None, vision_interpreter_factory: Callable[[str], VisionInterpreter | None] | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.provider_factory = provider_factory
        self.telegram_client = telegram_client
        self.soul_path = soul_path
        self.tool_registry_factory = tool_registry_factory
        self.vision_interpreter_factory = vision_interpreter_factory
        self._instances: dict[str, PersonalAgentRuntime] = {}

    def get(self, tomo_id: str) -> PersonalAgentRuntime:
        if tomo_id not in self._instances:
            instance_dir = self.data_dir / "instances" / tomo_id
            instance_dir.mkdir(parents=True, exist_ok=True)
            tool_registry = self.tool_registry_factory(tomo_id) if self.tool_registry_factory is not None else None
            if tool_registry is not None and not isinstance(tool_registry, ToolRegistry):
                raise ValueError("tool_registry_factory must return a ToolRegistry")
            self._instances[tomo_id] = PersonalAgentRuntime(
                provider=self.provider_factory(tomo_id),
                telegram=TelegramDeliverySink(self.telegram_client),
                config=RuntimeConfig(data_dir=str(instance_dir), soul_path=self.soul_path, owner_id=tomo_id),
                tool_registry=tool_registry,
                vision_interpreter=self.vision_interpreter_factory(tomo_id) if self.vision_interpreter_factory is not None else None,
            )
        return self._instances[tomo_id]
