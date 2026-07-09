from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .instances import RuntimeInstanceRegistry
from .models import InboundEnvelope
from .onboarding_store import TelegramOnboardingStore
from .telegram import TelegramClient


@dataclass
class SharedTelegramGateway:
    client: TelegramClient
    store: TelegramOnboardingStore
    instances: RuntimeInstanceRegistry

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

        runtime = self.instances.get(installation.tomo_id)
        runtime.handle_telegram_text(
            InboundEnvelope(
                connector="telegram",
                actor_id=chat_id,
                message_id=message_id,
                text=text,
                native_metadata={"chat_id": chat_id, "from_id": actor_id, "tomo_id": installation.tomo_id},
            )
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
        self.instances.get(installation.tomo_id)
        self.client.send_message(chat_id, "tomo is connected. text me.", reply_to_message_id=message_id)
        return True
