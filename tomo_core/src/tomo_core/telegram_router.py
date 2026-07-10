from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .onboarding_store import TelegramOnboardingStore


class RetryableTelegramUpdateError(RuntimeError):
    """A processing failure whose safe code may be persisted and retried."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def compact_private_update(update: dict[str, Any]) -> tuple[int, str, str] | None:
    """Return the durable representation for an update addressed to a private chat."""
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(message, dict):
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else None
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private" or chat.get("id") is None:
        return None
    return update_id, str(chat["id"]), json.dumps(update, sort_keys=True, separators=(",", ":"))


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
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _workers: list[threading.Thread] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.store.reset_interrupted_updates()

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
                update_id, chat_id, payload = compact
                # Do not acknowledge this Telegram update until SQLite durably accepts it.
                self.store.enqueue_update(update_id, chat_id, payload)
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
        return next_offset

    def process_next(self, *, now: int | None = None) -> bool:
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

    def _work_forever(self) -> None:
        while not self._stop_event.is_set():
            if not self.process_next():
                self._stop_event.wait(self.idle_sleep_seconds)
