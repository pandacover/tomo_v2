from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .instances import RuntimeInstanceRegistry
from .models import InboundEnvelope
from .onboarding_store import TelegramOnboardingStore
from .telegram import TelegramClient


@dataclass
class TelegramRuntimeDispatch:
    """Delivers a trusted Telegram chat's work to its personal runtime."""

    client: TelegramClient
    instances: RuntimeInstanceRegistry

    def send_setup(self, chat_id: str, tomo_id: str, reply_to_message_id: str) -> None:
        self.client.send_message(chat_id, "tomo is setting up.", reply_to_message_id=reply_to_message_id)

    def ensure_worker(self, tomo_id: str) -> None:
        self.instances.get(tomo_id)

    def send_connected(self, chat_id: str, reply_to_message_id: str) -> None:
        self.client.send_message(chat_id, "tomo is connected. text me.", reply_to_message_id=reply_to_message_id)

    def send_retry(self, chat_id: str, reply_to_message_id: str) -> None:
        self.client.send_message(chat_id, "tomo is still setting up. try again in a moment.", reply_to_message_id=reply_to_message_id)

    def dispatch(self, installation, envelope: InboundEnvelope) -> None:
        runtime = self.instances.get(installation.tomo_id)
        # A runtime session belongs to the Telegram sender, but Railway delivery
        # must remain pinned to the installation's trusted chat.
        original_client = runtime.telegram.client
        runtime.telegram.client = _BoundChatTelegramClient(self.client, installation.chat_id)
        try:
            runtime.handle_telegram_text(envelope)
        finally:
            runtime.telegram.client = original_client


@dataclass
class HostedTelegramRuntimeDispatch:
    """Keeps Telegram delivery on Railway while executing turns in Daytona."""

    client: TelegramClient
    supervisor: Any
    sandbox_dispatch: Any

    def send_setup(self, chat_id: str, tomo_id: str, reply_to_message_id: str) -> None:
        self.client.send_message(chat_id, "tomo is setting up.", reply_to_message_id=reply_to_message_id)

    def ensure_worker(self, tomo_id: str) -> None:
        self.supervisor.reconcile(tomo_id)

    def send_connected(self, chat_id: str, reply_to_message_id: str) -> None:
        self.client.send_message(chat_id, "tomo is connected. text me.", reply_to_message_id=reply_to_message_id)

    def send_retry(self, chat_id: str, reply_to_message_id: str) -> None:
        self.client.send_message(chat_id, "tomo is still setting up. try again in a moment.", reply_to_message_id=reply_to_message_id)

    def dispatch(self, installation, envelope: InboundEnvelope) -> None:
        request_id = f"telegram:{installation.chat_id}:{envelope.message_id}"
        for bubble in self.sandbox_dispatch(installation.tomo_id, request_id, envelope):
            self.client.send_message(
                installation.chat_id,
                bubble.text,
                reply_to_message_id=bubble.reply_to_message_id,
            )


@dataclass
class _BoundChatTelegramClient:
    client: TelegramClient
    chat_id: str

    def send_typing(self, actor_id: str) -> None:
        self.client.send_typing(self.chat_id)

    def send_message(self, actor_id: str, text: str, reply_to_message_id: str | None = None) -> None:
        self.client.send_message(self.chat_id, text, reply_to_message_id=reply_to_message_id)


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
            self.dispatch = TelegramRuntimeDispatch(client=self.client, instances=self.instances)

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

        self.dispatch.dispatch(
            installation,
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
        self.dispatch.send_setup(chat_id, installation.tomo_id, message_id)
        try:
            self.dispatch.ensure_worker(installation.tomo_id)
        except Exception:
            # The installation is intentionally retained so a retry can resume setup.
            self.dispatch.send_retry(chat_id, message_id)
            return True
        self.dispatch.send_connected(chat_id, message_id)
        return True
