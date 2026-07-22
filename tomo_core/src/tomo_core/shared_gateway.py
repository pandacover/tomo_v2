from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, ClassVar, Iterator, Protocol

from .daytona_supervisor import SandboxSupervisorError
from .cron_capability import CronCapability, issue_capability
from .cron_models import CronExecutionClaim, DeliveryAttempt, RunOutcome
from .cron_service import CronExecutionDeferred
from .cron_store import CronStore
from .cron_tools import CronApiClient, cron_registry
from .peer_capability import PeerCapability, issue_capability as issue_peer_capability
from .peer_tools import PeerApiClient, peer_registry
from .peer_exchange import PeerExchange, PeerError
from . import latency_trace
from .instances import RuntimeInstanceRegistry
from .models import AutomationTurn, InboundEnvelope, OutboundBubble, PeerTurn
from .onboarding_store import TelegramGenerationWork, TelegramInstallation, TelegramOnboardingStore
from .conversation.models import REACTION_EMOJI_ALLOWLIST
from .runtime import RuntimeCompleted, RuntimeFrameReady, RuntimeReactionReady
from .tool_execution import ToolExecutor
from .sandbox_dispatch import TelegramRuntimeDispatchError, burst_from_work
from .sandbox_protocol import SandboxCompletedEvent, SandboxErrorEvent, SandboxFrameEvent, SandboxReactionEvent, SandboxStaleEvent, encode_event
from .telegram import TelegramClient, TelegramDeliverySink, TelegramSendReceipt
from .telegram import reply_context_from_message
from .telegram_router import RetryableTelegramUpdateError, StaleRevisionTelegramUpdateError
from .typing_status import TypingLease


class TelegramRuntimeDispatch(Protocol):
    def ensure_worker(self, installation: TelegramInstallation) -> None:
        ...

    def deliver_telegram(
        self, installation: TelegramInstallation, update_id: int, envelope: InboundEnvelope
    ) -> list[OutboundBubble] | DirectTelegramDelivery:
        ...

    def iter_telegram_events(self, installation: TelegramInstallation, work: TelegramGenerationWork, is_active: Callable[[], bool] | None = None):
        ...

    def iter_automation_events(self, installation: TelegramInstallation, turn: AutomationTurn, generation_id: str, session_id: str, is_active: Callable[[], bool] | None = None):
        ...

    def iter_peer_events(self, installation: TelegramInstallation, turn: PeerTurn, generation_id: str, session_id: str, is_active: Callable[[], bool] | None = None):
        ...


@dataclass
class InProcessTelegramRuntimeDispatch:
    """Explicit local-mode adapter which returns bubbles without Telegram I/O."""

    instances: RuntimeInstanceRegistry
    control_url: str | None = None
    capability_key: bytes | None = None
    peer_capability_key: bytes | None = None
    clock: Callable[[], float] = time.time
    _locks: ClassVar[defaultdict[str, Lock]] = defaultdict(Lock)

    def ensure_worker(self, installation: TelegramInstallation) -> None:
        self.instances.get(installation.tomo_id)

    def deliver_telegram(
        self, installation: TelegramInstallation, update_id: int, envelope: InboundEnvelope
    ) -> DirectTelegramDelivery:
        with self._locks[installation.tomo_id]:
            runtime = self.instances.get(installation.tomo_id)
            original_client = runtime.telegram.client
            collector = _BubbleCollector()
            runtime.telegram.client = collector
            try:
                with self._interactive_tools(runtime, installation, f"telegram-update:{update_id}"):
                    runtime.handle_telegram_text(envelope)
            finally:
                runtime.telegram.client = original_client
        return DirectTelegramDelivery(collector.bubbles, collector.reactions)

    def iter_telegram_events(self, installation: TelegramInstallation, work: TelegramGenerationWork, is_active: Callable[[], bool] | None = None):
        with self._locks[installation.tomo_id]:
            runtime = self.instances.get(installation.tomo_id)
            burst = burst_from_work(installation, work)
            with self._interactive_tools(
                runtime,
                installation,
                work.generation_id,
                peer_session_id=work.session_id,
            ):
                for sequence, event in enumerate(runtime.handle_telegram_burst_iter(burst, is_active=is_active)):
                    if isinstance(event, RuntimeReactionReady):
                        yield SandboxReactionEvent(sequence, event.owner_id, event.actor_id, event.chat_id, event.target_message_id, event.generation_id, event.revision, event.emoji)
                    elif isinstance(event, RuntimeFrameReady):
                        frame = event.event.frame
                        yield SandboxFrameEvent(sequence, frame.segment_index, frame.frame_index, event.bubble.text)
                    elif isinstance(event, RuntimeCompleted):
                        payload = json.loads(encode_event("local-runtime", work.generation_id, sequence, event))
                        yield SandboxCompletedEvent(sequence, payload["result"])

    def iter_automation_events(self, installation: TelegramInstallation, turn: AutomationTurn, generation_id: str, session_id: str, is_active: Callable[[], bool] | None = None):
        with self._locks[installation.tomo_id]:
            runtime = self.instances.get(installation.tomo_id)
            for sequence, event in enumerate(runtime.handle_automation_turn_iter(turn, is_active=is_active)):
                if isinstance(event, RuntimeFrameReady):
                    frame = event.event.frame
                    yield SandboxFrameEvent(sequence, frame.segment_index, frame.frame_index, event.bubble.text)
                elif isinstance(event, RuntimeCompleted):
                    payload = json.loads(encode_event("local-runtime", generation_id, sequence, event))
                    yield SandboxCompletedEvent(sequence, payload["result"])

    def iter_peer_events(self, installation: TelegramInstallation, turn: PeerTurn, generation_id: str, session_id: str, is_active: Callable[[], bool] | None = None):
        if generation_id != turn.generation_id:
            raise ValueError("invalid peer generation")
        with self._locks[installation.tomo_id]:
            runtime = self.instances.get(installation.tomo_id)
            for sequence, event in enumerate(runtime.handle_peer_turn_iter(turn, is_active=is_active)):
                if isinstance(event, RuntimeReactionReady):
                    raise ValueError("peer turn emitted reaction")
                if isinstance(event, RuntimeFrameReady):
                    frame = event.event.frame
                    yield SandboxFrameEvent(sequence, frame.segment_index, frame.frame_index, event.bubble.text)
                elif isinstance(event, RuntimeCompleted):
                    payload = json.loads(encode_event("local-peer", generation_id, sequence, event))
                    yield SandboxCompletedEvent(sequence, payload["result"])

    @contextmanager
    def _interactive_tools(
        self,
        runtime: Any,
        installation: TelegramInstallation,
        generation_id: str,
        *,
        peer_session_id: str | None = None,
    ) -> Iterator[None]:
        if self.control_url is None or not runtime.provider.supports_tool_calls:
            yield
            return
        now = int(self.clock())
        destination = f"telegram:{installation.chat_id}"
        session_id = f"telegram:actor:{installation.actor_id}"
        original_runtime_registry = runtime.tool_registry
        original_engine_registry = runtime.conversation.tool_registry
        original_executor = runtime.conversation.tool_executor
        combined = original_runtime_registry
        if self.capability_key is not None:
            capability = issue_capability(self.capability_key, CronCapability(installation.tomo_id, installation.actor_id, destination, session_id, now, now + 300))
            combined = combined.extend(cron_registry(CronApiClient(self.control_url, capability, installation.tomo_id, installation.actor_id, destination, session_id), generation_id))
        if self.peer_capability_key is not None and peer_session_id is not None:
            capability = issue_peer_capability(
                self.peer_capability_key,
                PeerCapability(
                    installation.tomo_id,
                    installation.actor_id,
                    destination,
                    peer_session_id,
                    generation_id,
                    now,
                    now + 300,
                    frozenset({"list_relationships", "ask", "inspect_request"}),
                ),
            )
            combined = combined.extend(
                peer_registry(
                    PeerApiClient(
                        self.control_url,
                        capability,
                        installation.tomo_id,
                        installation.actor_id,
                        destination,
                        peer_session_id,
                        generation_id,
                    )
                )
            )
        runtime.tool_registry = combined
        runtime.conversation.tool_registry = combined
        runtime.conversation.tool_executor = ToolExecutor(combined)
        try:
            yield
        finally:
            runtime.tool_registry = original_runtime_registry
            runtime.conversation.tool_registry = original_engine_registry
            runtime.conversation.tool_executor = original_executor


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


def _peer_confirmation_control(text: str) -> tuple[bool, str] | None:
    """Parse only exact, direct owner language; ambiguous replies stay ordinary turns."""
    match = re.fullmatch(
        r"(?:/peer-(confirm|cancel)|(confirm|cancel) peer request) ([0-9a-f]{8,64})",
        text.strip().lower(),
    )
    if match is None:
        return None
    action = match.group(1) or match.group(2)
    return action == "confirm", match.group(3)


@dataclass
class SharedTelegramGateway:
    client: TelegramClient
    store: TelegramOnboardingStore
    instances: RuntimeInstanceRegistry | None = None
    dispatch: TelegramRuntimeDispatch | None = None
    pace_seconds: float = 1.5
    sleeper: Callable[[float], None] = time.sleep
    typing_lease_factory: Callable[..., TypingLease] = TypingLease
    cron_store: CronStore | None = None
    peer_exchange: PeerExchange | None = None

    def __post_init__(self) -> None:
        if self.dispatch is None:
            if self.instances is None:
                raise ValueError("SharedTelegramGateway requires a TelegramRuntimeDispatch")
            self.dispatch = InProcessTelegramRuntimeDispatch(instances=self.instances)
        if self.cron_store is None:
            self.cron_store = CronStore(self.store.data_dir)
        if self.peer_exchange is None:
            self.peer_exchange = PeerExchange(
                self.store.data_dir,
                source_generation_active=self.store.generation_not_superseded,
            )
        else:
            self.peer_exchange.set_source_generation_active(
                self.store.generation_not_superseded
            )

    def send_peer_notice(self, installation, text: str):
        return self.client.send_message(installation.chat_id, text)

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

        confirmation_control = _peer_confirmation_control(text)
        if confirmation_control is not None:
            approve, short_id = confirmation_control
            try:
                assert self.peer_exchange is not None
                self.peer_exchange.decide_confirmation_prefix(
                    installation.tomo_id,
                    short_id,
                    approve,
                )
            except PeerError:
                self.client.send_message(chat_id, "that peer request is no longer available.", reply_to_message_id=message_id)
            else:
                self.client.send_message(
                    chat_id,
                    "peer request approved." if approve else "peer request cancelled.",
                    reply_to_message_id=message_id,
                )
            return True
        if text.startswith("/peer-confirm") or text.startswith("/peer-cancel"):
            self.client.send_message(
                chat_id,
                "that peer request is no longer available.",
                reply_to_message_id=message_id,
            )
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
                        "from_id": actor_id,
                        "tomo_id": installation.tomo_id,
                    },
                    reply_context=reply_context_from_message(message, chat.get("id")),
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
                if emoji not in REACTION_EMOJI_ALLOWLIST:
                    continue
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
        telegram_sent_at = work.inputs[-1].telegram_sent_at
        if isinstance(telegram_sent_at, (int, float)):
            latency_trace.emit(work.burst_id, "telegram_origin_to_worker_start", elapsed_ms=max(0, int((time.time() - telegram_sent_at) * 1000)))
        if isinstance(work.eligible_at, (int, float)) and isinstance(work.claimed_at, (int, float)):
            latency_trace.emit(work.burst_id, "telegram_queue_wait", elapsed_ms=max(0, int((work.claimed_at - work.eligible_at) * 1000)))
        installation = self.store.installation_for_chat(work.chat_id)
        if installation is None:
            raise RetryableTelegramUpdateError("installation_missing")
        if installation.tomo_id != work.tomo_id:
            raise RetryableTelegramUpdateError("installation_rebound")
        last_send_at: float | None = None
        first_frame_delivered = False
        first_send_attempted = False
        first_send_accepted = False
        delivery_uncertain = False
        is_active = lambda: self.store.is_generation_active(work.generation_id, work.revision)
        lease = self.typing_lease_factory(self.client.send_typing, installation.chat_id, is_active=is_active)
        lease.start()
        try:
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
                    if event.emoji not in REACTION_EMOJI_ALLOWLIST:
                        self.store.mark_reaction_failed(work.generation_id, work.revision, target)
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
                    is_first_delivery_attempt = not first_send_attempted
                    first_send_attempted = True
                    if not first_send_accepted:
                        lease.pause_for_first_delivery()
                    if not is_active():
                        self.store.mark_delivery_suppressed(work.generation_id, event.sequence)
                        continue
                    send_started_at = time.monotonic()
                    try:
                        receipt = self.client.send_message(
                            installation.chat_id,
                            event.text,
                            reply_to_message_id=work.inputs[-1].message_id if not first_frame_delivered else None,
                        )
                    except Exception:
                        self.store.mark_delivery_unknown(work.generation_id, event.sequence)
                        delivery_uncertain = True
                        if is_first_delivery_attempt:
                            latency_trace.emit(work.burst_id, "telegram_first_delivery", outcome="error", elapsed_ms=max(0, int((time.monotonic() - send_started_at) * 1000)))
                        if not first_send_accepted and is_active():
                            lease.resume_after_failed_delivery()
                        continue
                    send_completed_at = time.monotonic()
                    if not first_send_accepted:
                        latency_trace.emit(
                            work.burst_id,
                            "telegram_first_delivery",
                            outcome="send_complete",
                            elapsed_ms=max(0, int((send_completed_at - send_started_at) * 1000)),
                        )
                        if isinstance(telegram_sent_at, (int, float)):
                            latency_trace.emit(work.burst_id, "telegram_first_delivery", outcome="origin_to_delivery", elapsed_ms=max(0, int((time.time() - telegram_sent_at) * 1000)))
                        first_send_accepted = True
                        lease.close()
                    if not self.store.mark_delivery_sent(work.generation_id, event.sequence, receipt.message_id):
                        self.store.mark_delivery_unknown(work.generation_id, event.sequence)
                        delivery_uncertain = True
                        continue
                    last_send_at = send_completed_at
                    first_frame_delivered = True
                elif isinstance(event, SandboxCompletedEvent):
                    if delivery_uncertain:
                        # A timeout can follow Telegram accepting the bubble. Replaying
                        # this generation risks a duplicate, so retain the unknown record.
                        self.store.complete_generation(work.generation_id, work.revision)
                        return True
                    self.store.complete_generation(work.generation_id, work.revision)
                    return True
                elif isinstance(event, SandboxErrorEvent):
                    raise RetryableTelegramUpdateError(event.code)
                elif isinstance(event, SandboxStaleEvent):
                    raise StaleRevisionTelegramUpdateError(event.current_revision)
        except (TelegramRuntimeDispatchError, SandboxSupervisorError) as error:
            raise RetryableTelegramUpdateError(error.code) from error
        finally:
            lease.close()
        return True

    def execute_cron_claim(self, claim: CronExecutionClaim) -> tuple[RunOutcome, tuple[str, ...]]:
        """Execute an owner-bound claim through the automation lane, never Telegram directly."""
        prefix, separator, chat_id = claim.destination.partition(":")
        if prefix != "telegram" or not separator or not chat_id or ":" in chat_id:
            raise RuntimeError("invalid_destination")
        installation = self.store.installation_for_chat(chat_id)
        if installation is None or installation.tomo_id != claim.owner_id or installation.chat_id != chat_id:
            raise RuntimeError("installation_unavailable")
        reservation = self.store.reserve_automation_generation(chat_id, claim.owner_id, claim.run.run_id)
        if reservation is None:
            raise CronExecutionDeferred("chat_busy")
        is_active = lambda: (
            self.store.is_generation_active(reservation.generation_id, reservation.revision)
            and self.cron_store is not None
            and self.cron_store.run_is_current(claim.run.run_id, claim.run.revision, claim.lease_token)
        )
        turn = AutomationTurn(
            reservation.generation_id, reservation.revision, claim.run.job_id, claim.run.run_id,
            installation.actor_id, installation.chat_id, claim.intent.text, claim.run.scheduled_for.isoformat(),
            None if claim.progress.previous_outcome is None else claim.progress.previous_outcome.value,
            claim.run.trigger, claim.will_end_after_run, constraints=claim.intent.constraints,
            successful_runs=claim.progress.successful_runs,
        )
        frames: list[str] = []
        try:
            for event in self.dispatch.iter_automation_events(installation, turn, reservation.generation_id, reservation.session_id, is_active=is_active):
                if not is_active():
                    if self.cron_store is not None and not self.cron_store.run_is_current(claim.run.run_id, claim.run.revision, claim.lease_token):
                        raise RuntimeError("cron_run_invalidated")
                    raise CronExecutionDeferred("superseded")
                if isinstance(event, SandboxFrameEvent):
                    frames.append(event.text)
                    if len(frames) > 3:
                        raise ValueError("automation emitted too many frames")
                elif isinstance(event, SandboxStaleEvent):
                    self.store.fail_generation(
                        reservation.generation_id,
                        "stale_revision",
                        minimum_next_revision=event.current_revision + 1,
                    )
                    raise CronExecutionDeferred("stale_revision")
                elif isinstance(event, SandboxErrorEvent):
                    raise RuntimeError(event.code)
                elif isinstance(event, SandboxCompletedEvent):
                    status = event.result.get("status") if isinstance(event.result, dict) else None
                    if status == "completed":
                        if not frames:
                            raise ValueError("automation completed without frames")
                        outcome = RunOutcome.SUCCEEDED
                    elif status == "completed_partial":
                        if not frames:
                            raise ValueError("automation completed without frames")
                        outcome = RunOutcome.PARTIAL
                    elif status == "approval_needed":
                        outcome = RunOutcome.APPROVAL_NEEDED
                    else:
                        raise RuntimeError("invalid_completion_status")
                    if not self.store.complete_generation(reservation.generation_id, reservation.revision):
                        raise CronExecutionDeferred("superseded")
                    return outcome, tuple(frames)
            if not is_active():
                if self.cron_store is not None and not self.cron_store.run_is_current(claim.run.run_id, claim.run.revision, claim.lease_token):
                    raise RuntimeError("cron_run_invalidated")
                raise CronExecutionDeferred("superseded")
            raise RuntimeError("automation_missing_completion")
        except CronExecutionDeferred:
            # Stale session generations are released for the interactive lane;
            # cron coordination retries without consuming its run attempt.
            if self.store.is_generation_active(reservation.generation_id, reservation.revision):
                self.store.fail_generation(reservation.generation_id, "superseded")
            raise
        except Exception:
            self.store.fail_generation(reservation.generation_id, "automation_failed", max_attempts=1)
            raise

    def send_cron_delivery(self, attempt: DeliveryAttempt) -> str:
        """Send a durably fenced cron frame only to its still-bound installation."""
        prefix, separator, chat_id = (attempt.destination or "").partition(":")
        if prefix != "telegram" or not separator or not chat_id or ":" in chat_id or not attempt.owner_id:
            raise RuntimeError("invalid_delivery_destination")
        installation = self.store.installation_for_chat(chat_id)
        if installation is None or installation.tomo_id != attempt.owner_id or installation.chat_id != chat_id:
            raise RuntimeError("installation_unavailable")
        receipt = self.client.send_message(chat_id, attempt.payload or "", reply_to_message_id=None)
        return receipt.message_id

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
