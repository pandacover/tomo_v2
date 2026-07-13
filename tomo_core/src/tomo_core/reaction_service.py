from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Callable


@dataclass(frozen=True)
class ReactionDeliveryKey:
    owner_id: str
    chat_id: str
    generation_id: str
    revision: int
    target_message_id: str


class ReactionService:
    """Bounded in-process reaction admission using delivery metadata only."""

    def __init__(
        self,
        *,
        cooldown_seconds: float = 0.0,
        max_recent_deliveries: int = 256,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        if max_recent_deliveries < 1:
            raise ValueError("max_recent_deliveries must be positive")
        self._cooldown_seconds = cooldown_seconds
        self._max_recent_deliveries = max_recent_deliveries
        self._clock = clock
        self._recent_deliveries: OrderedDict[ReactionDeliveryKey, float] = OrderedDict()
        self._last_chat_delivery: OrderedDict[tuple[str, str], float] = OrderedDict()

    def admit(self, key: ReactionDeliveryKey, *, reactions_enabled: bool, is_active: Callable[[], bool]) -> bool:
        """Return whether this transient delivery may proceed; never performs I/O."""
        if not reactions_enabled or not is_active() or key in self._recent_deliveries:
            return False
        now = self._clock()
        scope = (key.owner_id, key.chat_id)
        last_delivery = self._last_chat_delivery.get(scope)
        if last_delivery is not None and now - last_delivery < self._cooldown_seconds:
            return False
        self._recent_deliveries[key] = now
        self._recent_deliveries.move_to_end(key)
        self._last_chat_delivery[scope] = now
        self._last_chat_delivery.move_to_end(scope)
        while len(self._recent_deliveries) > self._max_recent_deliveries:
            self._recent_deliveries.popitem(last=False)
        # Chat cooldowns have their own bounded window so key churn cannot
        # silently disable a still-active chat's cooldown.
        while len(self._last_chat_delivery) > self._max_recent_deliveries * 4:
            self._last_chat_delivery.popitem(last=False)
        return True
