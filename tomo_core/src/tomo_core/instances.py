from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .models import RuntimeConfig
from .providers import ProviderAdapter
from .runtime import PersonalAgentRuntime
from .telegram import TelegramDeliverySink, TelegramClient


class RuntimeInstanceRegistry:
    def __init__(self, data_dir: str | Path, provider_factory: Callable[[str], ProviderAdapter], telegram_client: TelegramClient, soul_path: str = "SOUL.md") -> None:
        self.data_dir = Path(data_dir)
        self.provider_factory = provider_factory
        self.telegram_client = telegram_client
        self.soul_path = soul_path
        self._instances: dict[str, PersonalAgentRuntime] = {}

    def get(self, tomo_id: str) -> PersonalAgentRuntime:
        if tomo_id not in self._instances:
            instance_dir = self.data_dir / "instances" / tomo_id
            instance_dir.mkdir(parents=True, exist_ok=True)
            self._instances[tomo_id] = PersonalAgentRuntime(
                provider=self.provider_factory(tomo_id),
                telegram=TelegramDeliverySink(self.telegram_client),
                config=RuntimeConfig(data_dir=str(instance_dir), soul_path=self.soul_path),
            )
        return self._instances[tomo_id]
