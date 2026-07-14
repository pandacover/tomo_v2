from __future__ import annotations

import threading
import time
from typing import Callable


TELEGRAM_TYPING_REFRESH_SECONDS = 4.0
TELEGRAM_TYPING_SHUTDOWN_SECONDS = 2.5


class TypingLease:
    """Best-effort typing heartbeat owned by one active generation."""

    def __init__(
        self,
        send_typing: Callable[[str], None],
        actor_id: str,
        *,
        interval_seconds: float = TELEGRAM_TYPING_REFRESH_SECONDS,
        shutdown_timeout_seconds: float = TELEGRAM_TYPING_SHUTDOWN_SECONDS,
        is_active: Callable[[], bool] = lambda: True,
        wait: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        self._send_typing = send_typing
        self._actor_id = actor_id
        self._interval_seconds = interval_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._is_active = is_active
        self._wait = wait or (lambda closed, interval: closed.wait(interval))
        self._closed = threading.Event()
        self._lock = threading.Condition()
        self._started = False
        self._paused = False
        self._in_flight = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._started or self._closed.is_set():
                return
            self._started = True
        if not self._pulse():
            return
        with self._lock:
            if self._closed.is_set():
                return
            self._thread = threading.Thread(target=self._run, name="telegram-typing-lease", daemon=True)
            self._thread.start()

    def pause_for_first_delivery(self) -> None:
        deadline = time.monotonic() + self._shutdown_timeout_seconds
        with self._lock:
            self._paused = True
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lock.wait(timeout=remaining)

    def resume_after_failed_delivery(self) -> None:
        with self._lock:
            if self._closed.is_set() or not self._is_active():
                return
            self._paused = False
        self._pulse()

    def close(self) -> None:
        deadline = time.monotonic() + self._shutdown_timeout_seconds
        self._closed.set()
        with self._lock:
            self._paused = True
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lock.wait(timeout=remaining)
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _run(self) -> None:
        while not self._wait(self._closed, self._interval_seconds):
            if not self._pulse():
                return

    def _pulse(self) -> bool:
        with self._lock:
            if self._closed.is_set() or self._paused or not self._is_active():
                return False
            self._in_flight = True
        try:
            self._send_typing(self._actor_id)
        except Exception:
            self._closed.set()
            return False
        finally:
            with self._lock:
                self._in_flight = False
                self._lock.notify_all()
        return True
