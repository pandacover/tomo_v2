from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .onboarding_store import InterruptedGeneration, TelegramOnboardingStore


class RetryableTelegramUpdateError(RuntimeError):
    """A processing failure whose safe code may be persisted and retried."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class StaleRevisionTelegramUpdateError(RetryableTelegramUpdateError):
    def __init__(self, current_revision: int) -> None:
        super().__init__("stale_session_revision")
        self.current_revision = current_revision


@dataclass(frozen=True)
class CompactTelegramUpdate:
    update_id: int
    chat_id: str
    payload: str
    kind: str
    message_id: str | None = None
    telegram_sent_at: float | None = None

    def __iter__(self):
        yield self.update_id
        yield self.chat_id
        yield self.payload


def compact_private_update(update: dict[str, Any]) -> CompactTelegramUpdate | None:
    """Return the durable representation for an update addressed to a private chat."""
    update_id = update.get("update_id")
    message = update.get("message")
    is_callback = False
    if not isinstance(message, dict):
        callback = update.get("callback_query")
        is_callback = isinstance(callback, dict)
        message = callback.get("message") if isinstance(callback, dict) else None
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private" or chat.get("id") is None:
        return None
    text = message.get("text")
    caption = message.get("caption")
    content = text if isinstance(text, str) else caption if isinstance(caption, str) else ""
    has_photo = isinstance(message.get("photo"), list) and bool(message["photo"])
    exact_peer_confirmation = re.fullmatch(
        r"(?:/peer-(?:confirm|cancel)|(?:confirm|cancel) peer request) [0-9a-f]{8,64}",
        content.strip().lower(),
    ) is not None
    kind = "message" if not is_callback and not exact_peer_confirmation and (has_photo or (content.strip() and not content.strip().startswith("/"))) else "control"
    return CompactTelegramUpdate(
        update_id=update_id,
        chat_id=str(chat["id"]),
        payload=json.dumps(update, sort_keys=True, separators=(",", ":")),
        kind=kind,
        message_id=str(message.get("message_id")) if message.get("message_id") is not None else None,
        telegram_sent_at=float(message["date"]) if isinstance(message.get("date"), (int, float)) else None,
    )


@dataclass
class TelegramUpdateRouter:
    client: Any
    store: TelegramOnboardingStore
    process_update: Callable[[dict[str, Any]], Any]
    poll_timeout: int = 30
    idle_sleep_seconds: float = 0.2
    worker_count: int = 1
    max_attempts: int = 5
    shutdown_timeout: float = 10.0
    on_error: Callable[[Exception], None] | None = None
    cancel_generation: Callable[[InterruptedGeneration], None] | None = None
    cancellation_queue_size: int = 100
    cancellation_retry_base_seconds: float = 1.0
    cancellation_retry_max_seconds: float = 60.0
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _workers: list[threading.Thread] = field(default_factory=list, init=False, repr=False)
    _cancellations: queue.Queue[InterruptedGeneration] = field(init=False, repr=False)
    _scheduled_cancellation_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _cancellation_failures: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _cancellation_retry_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _cancellation_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._cancellations = queue.Queue(maxsize=self.cancellation_queue_size)
        self.store.reset_interrupted_updates()
        for interrupted in self.store.recover_interrupted_generations():
            self._schedule_cancellation(interrupted)

    def run_forever(self) -> None:
        self._workers = [
            threading.Thread(target=self._work_forever, name=f"telegram-update-worker-{index}", daemon=True)
            for index in range(self.worker_count)
        ]
        for worker in self._workers:
            worker.start()
        offset: int | None = None
        try:
            while not self._stop_event.is_set():
                offset = self.poll_once(offset)
                self._stop_event.wait(self.idle_sleep_seconds)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop_event.set()
        deadline = time.monotonic() + self.shutdown_timeout
        current = threading.current_thread()
        for worker in self._workers:
            if worker is current or not worker.is_alive():
                continue
            worker.join(timeout=max(0, deadline - time.monotonic()))

    def poll_once(self, offset: int | None = None) -> int | None:
        next_offset = offset
        for update in self.client.get_updates(offset=offset, timeout=self.poll_timeout):
            compact = compact_private_update(update)
            if compact is not None:
                kind = compact.kind
                tomo_id = ""
                if kind == "message":
                    installation = self.store.installation_for_chat(compact.chat_id)
                    if installation is None:
                        kind = "control"
                    else:
                        tomo_id = installation.tomo_id
                # Do not acknowledge this Telegram update until SQLite durably accepts it.
                result = self.store.enqueue_update(
                    compact.update_id,
                    compact.chat_id,
                    compact.payload,
                    update_kind=kind,
                    message_id=compact.message_id,
                    telegram_sent_at=compact.telegram_sent_at,
                    tomo_id=tomo_id,
                )
                if kind == "message" and result.enqueued:
                    try:
                        self.client.send_typing(compact.chat_id)
                    except Exception:
                        pass
                if result.superseded_generation_id and result.superseded_session_id:
                    self._schedule_cancellation(
                        InterruptedGeneration(
                            result.superseded_generation_id,
                            compact.chat_id,
                            tomo_id,
                            max(1, (result.revision or 2) - 1),
                            result.superseded_session_id,
                        )
                    )
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
        return next_offset

    def process_next(self, *, now: int | None = None) -> bool:
        work = self.store.claim_next_work(now=now)
        if work is not None:
            try:
                self.process_update(work)
            except StaleRevisionTelegramUpdateError as exc:
                self.store.fail_generation(
                    work.generation_id,
                    exc.error_code,
                    now=now,
                    max_attempts=self.max_attempts,
                    minimum_next_revision=exc.current_revision + 1,
                )
                if self.on_error:
                    self.on_error(exc)
            except RetryableTelegramUpdateError as exc:
                self.store.fail_generation(work.generation_id, exc.error_code, now=now, max_attempts=self.max_attempts)
                if self.on_error:
                    self.on_error(exc)
            except Exception as exc:
                self.store.fail_generation(work.generation_id, "processing_error", now=now, max_attempts=self.max_attempts)
                if self.on_error:
                    self.on_error(exc)
            else:
                self.store.complete_generation(work.generation_id, work.revision, now=now)
            return True
        update = self.store.claim_next_update(now=now)
        if update is None:
            return False
        try:
            self.process_update(json.loads(update.payload))
        except RetryableTelegramUpdateError as exc:
            self._retry_or_complete(update.update_id, update.attempts, exc.error_code, now=now)
            if self.on_error:
                self.on_error(exc)
        except Exception as exc:
            self._retry_or_complete(update.update_id, update.attempts, "processing_error", now=now)
            if self.on_error:
                self.on_error(exc)
        else:
            self.store.complete_update(update.update_id, now=now)
        return True

    def _retry_or_complete(self, update_id: int, attempts: int, error_code: str, *, now: int | None) -> None:
        if attempts >= self.max_attempts:
            self.store.complete_update(update_id, now=now)
        else:
            self.store.retry_update(update_id, error_code, now=now)

    def drain_cancellations(self) -> bool:
        self._refill_cancellations()
        drained = False
        all_succeeded = True
        for _ in range(self._cancellations.qsize()):
            try:
                interrupted = self._cancellations.get_nowait()
            except queue.Empty:
                break
            drained = True
            if self.cancel_generation is None:
                self.store.complete_generation_cleanup(interrupted.generation_id)
                with self._cancellation_lock:
                    self._scheduled_cancellation_ids.discard(interrupted.generation_id)
                    self._cancellation_failures.pop(interrupted.generation_id, None)
                    self._cancellation_retry_at.pop(interrupted.generation_id, None)
                continue
            try:
                self.cancel_generation(interrupted)
                self.store.complete_generation_cleanup(interrupted.generation_id)
                with self._cancellation_lock:
                    self._scheduled_cancellation_ids.discard(interrupted.generation_id)
                    self._cancellation_failures.pop(interrupted.generation_id, None)
                    self._cancellation_retry_at.pop(interrupted.generation_id, None)
            except Exception as exc:
                all_succeeded = False
                with self._cancellation_lock:
                    self._scheduled_cancellation_ids.discard(interrupted.generation_id)
                    failures = self._cancellation_failures.get(interrupted.generation_id, 0) + 1
                    self._cancellation_failures[interrupted.generation_id] = failures
                    delay = min(
                        self.cancellation_retry_max_seconds,
                        self.cancellation_retry_base_seconds * (2 ** min(failures - 1, 16)),
                    )
                    self._cancellation_retry_at[interrupted.generation_id] = time.monotonic() + delay
                if self.on_error:
                    self.on_error(exc)
        self._refill_cancellations()
        return drained and all_succeeded

    def _schedule_cancellation(self, interrupted: InterruptedGeneration) -> None:
        with self._cancellation_lock:
            if interrupted.generation_id in self._scheduled_cancellation_ids:
                return
            if time.monotonic() < self._cancellation_retry_at.get(interrupted.generation_id, 0.0):
                return
            try:
                self._cancellations.put_nowait(interrupted)
            except queue.Full:
                return
            self._scheduled_cancellation_ids.add(interrupted.generation_id)

    def _refill_cancellations(self) -> None:
        for interrupted in self.store.pending_generation_cleanups(
            limit=self.cancellation_queue_size
        ):
            self._schedule_cancellation(interrupted)

    def _work_forever(self) -> None:
        while not self._stop_event.is_set():
            if self.drain_cancellations():
                continue
            if not self.process_next():
                self._stop_event.wait(self.idle_sleep_seconds)
