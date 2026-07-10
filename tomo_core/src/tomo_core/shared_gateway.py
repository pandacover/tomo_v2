from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .daytona_supervisor import SandboxSupervisorError
from .instances import RuntimeInstanceRegistry
from .models import InboundEnvelope, OutboundBubble
from .onboarding_store import TelegramInstallation, TelegramOnboardingStore
from .sandbox_dispatch import TelegramRuntimeDispatchError
from .telegram import TelegramClient


class TelegramRuntimeDispatch(Protocol):
    def ensure_worker(self, installation: TelegramInstallation) -> None:
        ...

    def deliver_telegram(
        self, installation: TelegramInstallation, update_id: int, envelope: InboundEnvelope
    ) -> list[OutboundBubble]:
        ...


@dataclass
class InProcessTelegramRuntimeDispatch:
    """Explicit local-mode adapter which returns bubbles without Telegram I/O."""

    instances: RuntimeInstanceRegistry

    def ensure_worker(self, installation: TelegramInstallation) -> None:
        self.instances.get(installation.tomo_id)

    def deliver_telegram(
        self, installation: TelegramInstallation, update_id: int, envelope: InboundEnvelope
    ) -> list[OutboundBubble]:
        runtime = self.instances.get(installation.tomo_id)
        original_client = runtime.telegram.client
        collector = _BubbleCollector()
        runtime.telegram.client = collector
        try:
            runtime.handle_telegram_text(envelope)
        finally:
            runtime.telegram.client = original_client
        return collector.bubbles


class _BubbleCollector:
    bubbles: list[OutboundBubble]

    def __init__(self) -> None:
        self.bubbles = []

    def send_typing(self, actor_id: str) -> None:
        pass

    def send_message(self, actor_id: str, text: str, reply_to_message_id: str | None = None) -> None:
        self.bubbles.append(OutboundBubble(text, reply_to_message_id))


@dataclass
class SharedTelegramGateway:
    client: TelegramClient
    store: TelegramOnboardingStore
    instances: RuntimeInstanceRegistry | None = None
    dispatch: TelegramRuntimeDispatch | None = None

    def __post_init__(self) -> None:
        if self.dispatch is None:
            if self.instances is None:
                raise ValueError("SharedTelegramGateway requires a TelegramRuntimeDispatch")
            self.dispatch = InProcessTelegramRuntimeDispatch(instances=self.instances)

    def process_update(self, update: dict[str, Any]) -> bool:
        message = update.get("message")
        if not isinstance(message, dict):
            return False
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private":
            return False
        text = (message.get("text") or "").strip()
        chat_id = str(chat.get("id") or sender.get("id"))
        actor_id = str(sender.get("id") or chat_id)
        message_id = str(message.get("message_id"))

        if text.startswith("/start"):
            return self._handle_start(text=text, chat_id=chat_id, actor_id=actor_id, message_id=message_id)

        installation = self.store.installation_for_chat(chat_id)
        if installation is None:
            self.client.send_message(chat_id, "open tomo from the dashboard first, then press start here.", reply_to_message_id=message_id)
            return True

        if installation.actor_id != actor_id:
            self.client.send_message(chat_id, "open tomo from the dashboard first, then press start here.", reply_to_message_id=message_id)
            return True

        self.client.send_typing(installation.chat_id)
        try:
            bubbles = self.dispatch.deliver_telegram(
                installation,
                int(update.get("update_id", message_id)),
                InboundEnvelope(
                    connector="telegram",
                    actor_id=actor_id,
                    message_id=message_id,
                    text=text,
                    native_metadata={
                        "chat_id": chat_id,
                        "delivery_chat_id": installation.chat_id,
                        "from_id": actor_id,
                        "tomo_id": installation.tomo_id,
                    },
                ),
            )
        except (TelegramRuntimeDispatchError, SandboxSupervisorError):
            return True
        for index, bubble in enumerate(bubbles):
            self.client.send_message(
                installation.chat_id,
                bubble.text,
                reply_to_message_id=message_id if index == 0 else bubble.reply_to_message_id,
            )
        return True

    def _handle_start(self, text: str, chat_id: str, actor_id: str, message_id: str) -> bool:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            self.client.send_message(chat_id, "open tomo from the dashboard so i can connect this chat.", reply_to_message_id=message_id)
            return True
        installation = self.store.consume_start_token(parts[1], chat_id=chat_id, actor_id=actor_id)
        if installation is None:
            self.client.send_message(chat_id, "that tomo link expired. tap text tomo on the dashboard again.", reply_to_message_id=message_id)
            return True
        self.client.send_message(chat_id, "tomo is setting up.", reply_to_message_id=message_id)
        try:
            self.dispatch.ensure_worker(installation)
        except (TelegramRuntimeDispatchError, SandboxSupervisorError):
            # The installation is intentionally retained so a retry can resume setup.
            self.client.send_message(chat_id, "tomo is still setting up. try again in a moment.", reply_to_message_id=message_id)
            return True
        self.client.send_message(chat_id, "tomo is connected. text me.", reply_to_message_id=message_id)
        return True
