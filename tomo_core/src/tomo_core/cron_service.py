"""Caller-driven durable cron execution and delivery orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import Event, Lock, Thread, current_thread

from .cron_models import CronExecutionClaim, DeliveryAttempt, RunOutcome
from .cron_store import CronStore
from .telegram_bot import TelegramDeliveryError


class CronExecutionDeferred(RuntimeError):
    """A coordination contention which must not spend an execution attempt."""


class CronSchedulerService:
    """Processes at most one durable cron state-machine step per call."""

    def __init__(
        self,
        store: CronStore,
        executor: Callable[[CronExecutionClaim], str | tuple[str, ...] | tuple[RunOutcome, str | tuple[str, ...]]],
        sender: Callable[[DeliveryAttempt], str | None],
        delivery_pace_seconds: float = 0,
        execution_lease_seconds: float = 180,
        scheduler_interval_seconds: float = 0.2,
    ) -> None:
        self.store = store
        self.executor = executor
        self.sender = sender
        if delivery_pace_seconds < 0:
            raise ValueError("delivery_pace_seconds must be non-negative")
        if execution_lease_seconds <= 0:
            raise ValueError("execution_lease_seconds must be positive")
        if scheduler_interval_seconds <= 0:
            raise ValueError("scheduler_interval_seconds must be positive")
        self.delivery_pace_seconds = delivery_pace_seconds
        self.execution_lease_seconds = execution_lease_seconds
        self.scheduler_interval_seconds = scheduler_interval_seconds
        self._scheduler_stop = Event()
        self._scheduler_lock = Lock()
        self._scheduler_thread: Thread | None = None
        self._prefer_execution = False

    def start(self) -> bool:
        """Start the single traffic-independent scheduler lane."""
        with self._scheduler_lock:
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return False
            self._scheduler_stop.clear()
            self._scheduler_thread = Thread(target=self._run_scheduler, name="cron-scheduler", daemon=True)
            self._scheduler_thread.start()
            return True

    def stop(self) -> None:
        self._scheduler_stop.set()
        thread = self._scheduler_thread
        if thread is not None and thread is not current_thread():
            thread.join()

    def _run_scheduler(self) -> None:
        while not self._scheduler_stop.is_set():
            try:
                self.process_next()
            except Exception:
                self._log_scheduler_fault("cron_scheduler_tick_failed")
            self._scheduler_stop.wait(self.scheduler_interval_seconds)

    def _log_scheduler_fault(self, code: str) -> None:
        # This lane must not expose exception text, which can include task data.
        import logging

        logging.getLogger(__name__).error("%s", code)

    def process_next(self, *, now: datetime | None = None) -> str | None:
        """Recover leases, then execute one run or send one already-persisted frame."""
        self.store.recover_expired_leases(now=now, pace_seconds=self.delivery_pace_seconds)
        if self._prefer_execution:
            result = self._process_execution(now)
            if result is not None:
                self._prefer_execution = False
                return result
        delivery = self.store.claim_delivery(now=now)
        if delivery is not None:
            self._prefer_execution = True
            if not self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=now):
                self.store.suppress_delivery(delivery.delivery_id, delivery.lease_token, now=now)
                return "suppressed"
            try:
                receipt = self.sender(delivery)
            except TelegramDeliveryError as error:
                if error.uncertain:
                    self.store.mark_delivery_unknown(delivery.delivery_id, delivery.lease_token, now=now, pace_seconds=self.delivery_pace_seconds)
                    return "delivery_unknown"
                self.store.fail_delivery(delivery.delivery_id, delivery.lease_token, "delivery_failed", now=now, pace_seconds=self.delivery_pace_seconds)
                return "delivery_failed"
            except Exception:
                self.store.fail_delivery(delivery.delivery_id, delivery.lease_token, "delivery_failed", now=now, pace_seconds=self.delivery_pace_seconds)
                return "delivery_failed"
            self.store.complete_delivery(
                delivery.delivery_id,
                delivery.lease_token,
                receipt,
                now=now,
                pace_seconds=self.delivery_pace_seconds,
            )
            return "delivered"
        return self._process_execution(now)

    def _process_execution(self, now: datetime | None) -> str | None:
        turn = self.store.claim_due_run(now=now)
        if turn is not None:
            if not self.store.begin_run(turn.run.run_id, turn.lease_token, now=now, lease_seconds=self.execution_lease_seconds):
                return None
            stop_heartbeat = Event()
            lease_lost = Event()
            heartbeat = Thread(target=self._heartbeat, args=(turn, stop_heartbeat, lease_lost), daemon=True)
            heartbeat.start()
            try:
                result = self.executor(turn)
                stop_heartbeat.set()
                outcome, frames = self._outcome(result)
                if not lease_lost.is_set():
                    self.store.complete_run(turn.run.run_id, turn.lease_token, outcome, frames, now=now)
            except CronExecutionDeferred:
                self.store.defer_run(turn.run.run_id, turn.lease_token, now=now)
                return "deferred"
            except Exception:
                self.store.fail_run(turn.run.run_id, turn.lease_token, "execution_failed", now=now)
            finally:
                stop_heartbeat.set()
                heartbeat.join()
            return "executed"
        return None

    def _heartbeat(self, turn: CronExecutionClaim, stop: Event, lease_lost: Event) -> None:
        if stop.is_set():
            return
        try:
            renewed = self.store.renew_run_lease(turn.run.run_id, turn.lease_token, lease_seconds=self.execution_lease_seconds)
        except Exception:
            lease_lost.set()
            return
        if not renewed and not stop.is_set():
            lease_lost.set()
            return
        while not stop.wait(self.execution_lease_seconds / 3):
            try:
                renewed = self.store.renew_run_lease(turn.run.run_id, turn.lease_token, lease_seconds=self.execution_lease_seconds)
            except Exception:
                lease_lost.set()
                return
            if not renewed and not stop.is_set():
                lease_lost.set()
                return

    @staticmethod
    def _outcome(result: object) -> tuple[RunOutcome, str | tuple[str, ...]]:
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], RunOutcome):
            outcome, frames = result
        elif isinstance(result, str):
            outcome, frames = RunOutcome.SUCCEEDED, result
        elif isinstance(result, tuple) and all(isinstance(frame, str) for frame in result):
            outcome, frames = RunOutcome.SUCCEEDED, result
        else:
            raise ValueError("executor must return text frames or an outcome/text pair")
        values = (frames,) if isinstance(frames, str) else tuple(frames)
        if outcome is RunOutcome.APPROVAL_NEEDED and not values:
            return outcome, "This scheduled action needs your approval."
        if not 1 <= len(values) <= 3 or any(not frame.strip() or len(frame) > 4096 for frame in values):
            raise ValueError("executor output must contain one to three non-blank frames of at most 4096 characters")
        return outcome, frames
