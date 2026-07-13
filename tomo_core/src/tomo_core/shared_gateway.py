from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .daytona_supervisor import SandboxSupervisorError
from .instances import RuntimeInstanceRegistry
from .models import InboundEnvelope, OutboundBubble
from .onboarding_store import TelegramGenerationWork, TelegramInstallation, TelegramOnboardingStore
from .runtime import RuntimeCompleted, RuntimeFrameReady, RuntimeReactionReady
from .sandbox_dispatch import TelegramRuntimeDispatchError, burst_from_work
from .sandbox_protocol import SandboxCompletedEvent, SandboxErrorEvent, SandboxFrameEvent, SandboxReactionEvent, encode_event
from .telegram import TelegramClient, TelegramDeliverySink, TelegramSendReceipt
from .telegram_router import RetryableTelegramUpdateError


class TelegramRuntimeDispatch(Protocol):
    def ensure_worker(self, installation: TelegramInstallation) -> None:
        ...

    def deliver_telegram(
        self, installation: TelegramInstallation, update_id: int, envelope: InboundEnvelope
    ) -> list[OutboundBubble] | DirectTelegramDelivery:
        ...

    def iter_telegram_events(self, installation: TelegramInstallation, work: TelegramGenerationWork, is_active: Callable[[], bool] | None = None):
        ...


@dataclass
class InProcessTelegramRuntimeDispatch:
    """Explicit local-mode adapter which returns bubbles without Telegram I/O."""

    instances: RuntimeInstanceRegistry

    def ensure_worker(self, installation: TelegramInstallation) -> None:
        self.instances.get(installation.tomo_id)

    def deliver_telegram(
        self, installation: TelegramInstallation, update_id: int, envelope: InboundEnvelope
    ) -> DirectTelegramDelivery:
        runtime = self.instances.get(installation.tomo_id)
        original_client = runtime.telegram.client
        collector = _BubbleCollector()
        runtime.telegram.client = collector
        try:
            runtime.handle_telegram_text(envelope)
        finally:
            runtime.telegram.client = original_client
        return DirectTelegramDelivery(collector.bubbles, collector.reactions)

    def iter_telegram_events(self, installation: TelegramInstallation, work: TelegramGenerationWork, is_active: Callable[[], bool] | None = None):
        runtime = self.instances.get(installation.tomo_id)
        burst = burst_from_work(installation, work)
        for sequence, event in enumerate(runtime.handle_telegram_burst_iter(burst, is_active=is_active)):
            if isinstance(event, RuntimeReactionReady):
                yield SandboxReactionEvent(sequence, event.owner_id, event.actor_id, event.chat_id, event.target_message_id, event.generation_id, event.revision, event.emoji)
            elif isinstance(event, RuntimeFrameReady):
                frame = event.event.frame
                yield SandboxFrameEvent(sequence, frame.segment_index, frame.frame_index, event.bubble.text)
            elif isinstance(event, RuntimeCompleted):
                payload = json.loads(encode_event("local-runtime", work.generation_id, sequence, event))
                yield SandboxCompletedEvent(sequence, payload["result"])


@dataclass(frozen=True)
class DirectTelegramDelivery:
    bubbles: list[OutboundBubble]
    reactions: list[tuple[str, str, str]]


class _BubbleCollector:
    bubbles: list[OutboundBubble]

    def __init__(self) -> None:
        self.bubbles = []
        self.reactions: list[tuple[str, str, str]] = []

    def send_typing(self, actor_id: str) -> None:
        pass

    def send_message(self, actor_id: str, text: str, reply_to_message_id: str | None = None) -> TelegramSendReceipt:
        self.bubbles.append(OutboundBubble(text, reply_to_message_id))
        return TelegramSendReceipt(str(len(self.bubbles)))

    def set_message_reaction(self, actor_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((actor_id, message_id, emoji))


@dataclass
class SharedTelegramGateway:
    client: TelegramClient
    store: TelegramOnboardingStore
    instances: RuntimeInstanceRegistry | None = None
    dispatch: TelegramRuntimeDispatch | None = None
    pace_seconds: float = 1.5
    sleeper: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.dispatch is None:
            if self.instances is None:
                raise ValueError("SharedTelegramGateway requires a TelegramRuntimeDispatch")
            self.dispatch = InProcessTelegramRuntimeDispatch(instances=self.instances)

    def process_update(self, update: dict[str, Any] | TelegramGenerationWork) -> bool:
        if isinstance(update, TelegramGenerationWork):
            return self._process_generation_work(update)
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
            delivery = self.dispatch.deliver_telegram(
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
        except (TelegramRuntimeDispatchError, SandboxSupervisorError) as error:
            self.client.send_message(
                installation.chat_id,
                "tomo had trouble replying. try again in a moment.",
                reply_to_message_id=message_id,
            )
            raise RetryableTelegramUpdateError(error.code) from error
        if isinstance(delivery, DirectTelegramDelivery):
            for _, target_message_id, emoji in delivery.reactions:
                try:
                    TelegramDeliverySink(self.client).react_to_message(installation.chat_id, target_message_id, emoji)
                except Exception:
                    pass
            bubbles = delivery.bubbles
        else:
            bubbles = delivery
        for index, bubble in enumerate(bubbles):
            self.client.send_message(
                installation.chat_id,
                bubble.text,
                reply_to_message_id=message_id if index == 0 else bubble.reply_to_message_id,
            )
        return True

    def _process_generation_work(self, work: TelegramGenerationWork) -> bool:
        installation = self.store.installation_for_chat(work.chat_id)
        if installation is None:
            raise RetryableTelegramUpdateError("installation_missing")
        if installation.tomo_id != work.tomo_id:
            raise RetryableTelegramUpdateError("installation_rebound")
        self.client.send_typing(installation.chat_id)
        last_send_at: float | None = None
        first_frame_delivered = False
        delivery_uncertain = False
        try:
            is_active = lambda: self.store.is_generation_active(work.generation_id, work.revision)
            for event in self.dispatch.iter_telegram_events(installation, work, is_active=is_active):
                if isinstance(event, SandboxReactionEvent):
                    target = work.inputs[-1].message_id
                    if (event.owner_id != installation.tomo_id or event.actor_id != installation.actor_id or event.chat_id != installation.chat_id
                            or event.target_message_id != str(target) or event.generation_id != work.generation_id
                            or event.revision != work.revision):
                        continue
                    if target is None or not self.store.is_generation_active(work.generation_id, work.revision):
                        continue
                    if not self.store.reserve_reaction(work.generation_id, work.revision, target, event.emoji):
                        continue
                    if not self.store.is_generation_active(work.generation_id, work.revision):
                        self.store.mark_reaction_suppressed(work.generation_id, work.revision, target)
                        continue
                    try:
                        self.client.set_message_reaction(installation.chat_id, target, event.emoji)
                    except Exception:
                        self.store.mark_reaction_failed(work.generation_id, work.revision, target)
                    else:
                        self.store.mark_reaction_sent(work.generation_id, work.revision, target)
                elif isinstance(event, SandboxFrameEvent):
                    if not self.store.is_generation_active(work.generation_id, work.revision):
                        continue
                    if last_send_at is not None and self.pace_seconds > 0:
                        elapsed = time.monotonic() - last_send_at
                        remaining = self.pace_seconds - elapsed
                        if remaining > 0:
                            self.sleeper(remaining)
                    if not self.store.reserve_delivery(
                        work.generation_id,
                        work.revision,
                        event.sequence,
                        event.segment_index,
                        event.frame_index,
                        event.text,
                        work.inputs[-1].message_id if not first_frame_delivered else None,
                        legacy_move=event.legacy_move.value if event.legacy_move is not None else None,
                    ):
                        continue
                    if not self.store.is_generation_active(work.generation_id, work.revision):
                        self.store.mark_delivery_suppressed(work.generation_id, event.sequence)
                        continue
                    try:
                        receipt = self.client.send_message(
                            installation.chat_id,
                            event.text,
                            reply_to_message_id=work.inputs[-1].message_id if not first_frame_delivered else None,
                        )
                    except Exception:
                        self.store.mark_delivery_unknown(work.generation_id, event.sequence)
                        delivery_uncertain = True
                        continue
                    if not self.store.mark_delivery_sent(work.generation_id, event.sequence, receipt.message_id):
                        self.store.mark_delivery_unknown(work.generation_id, event.sequence)
                        delivery_uncertain = True
                        continue
                    last_send_at = time.monotonic()
                    first_frame_delivered = True
                elif isinstance(event, SandboxCompletedEvent):
                    if not delivery_uncertain:
                        self.store.complete_generation(work.generation_id, work.revision)
                    return True
                elif isinstance(event, SandboxErrorEvent):
                    raise RetryableTelegramUpdateError(event.code)
        except (TelegramRuntimeDispatchError, SandboxSupervisorError) as error:
            raise RetryableTelegramUpdateError(error.code) from error
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
