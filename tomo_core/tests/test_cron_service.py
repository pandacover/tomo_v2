import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from tomo_core.cron_models import CronJob, JobIntent, LifecyclePolicy, RunOutcome, ScheduleSpec
from tomo_core.cron_service import CronExecutionDeferred, CronSchedulerService
from tomo_core.cron_store import CronStore
from tomo_core.telegram_bot import TelegramDeliveryError


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def job(name="job"):
    return CronJob(name, "owner", "telegram:chat", JobIntent("Check this"), ScheduleSpec.once(NOW), LifecyclePolicy())


class CronSchedulerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CronStore(self.temp.name)
        self.sent = []

    def tearDown(self):
        self.temp.cleanup()

    def test_execution_persists_ordered_frames_before_separate_delivery(self):
        self.store.create(job())
        service = CronSchedulerService(self.store, lambda turn: ("first.", "second."), lambda attempt: self.sent.append((attempt.destination, attempt.payload)) or "receipt")

        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(self.sent, [])
        self.assertEqual(service.process_next(now=NOW), "delivered")
        self.assertEqual(service.process_next(now=NOW), "delivered")
        self.assertEqual(self.sent, [("telegram:chat", "first."), ("telegram:chat", "second.")])

    def test_execution_failure_retries_same_run_then_emits_control_notice(self):
        self.store.create(job())
        service = CronSchedulerService(self.store, lambda turn: (_ for _ in ()).throw(RuntimeError()), lambda attempt: self.sent.append(attempt.payload) or "receipt")

        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(service.process_next(now=NOW + timedelta(seconds=2)), "executed")
        self.assertEqual(service.process_next(now=NOW + timedelta(seconds=4)), "executed")
        self.assertEqual(service.process_next(now=NOW + timedelta(seconds=4)), "delivered")
        self.assertEqual(self.sent, ["Scheduled task could not be completed."])

    def test_final_normal_execution_failure_ends_after_notice_without_lifecycle_run(self):
        final_job = CronJob(
            "final-failure",
            "owner",
            "telegram:chat",
            JobIntent("Check this"),
            ScheduleSpec.interval(60, starts_at=NOW),
            LifecyclePolicy(ends_at=NOW + timedelta(seconds=90)),
        )
        self.store.create(final_job)
        service = CronSchedulerService(
            self.store,
            lambda turn: (_ for _ in ()).throw(RuntimeError()),
            lambda attempt: self.sent.append(attempt.payload) or "receipt",
        )

        self.assertEqual(service.process_next(now=NOW + timedelta(minutes=1)), "executed")
        self.assertEqual(service.process_next(now=NOW + timedelta(minutes=1, seconds=2)), "executed")
        self.assertEqual(service.process_next(now=NOW + timedelta(minutes=1, seconds=6)), "executed")
        self.assertEqual(service.process_next(now=NOW + timedelta(minutes=1, seconds=6)), "delivered")
        self.assertEqual(self.sent, ["Scheduled task could not be completed."])

        self.assertEqual(service.process_next(now=NOW + timedelta(minutes=2)), None)
        self.assertEqual(self.store.get("owner", "final-failure").status, "ended")

    def test_stale_delivery_is_suppressed_before_sender(self):
        self.store.create(job())
        service = CronSchedulerService(self.store, lambda turn: "answer", lambda attempt: self.sent.append(attempt.payload))
        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertTrue(self.store.delete("owner", "job", 1, now=NOW))

        self.assertIsNone(service.process_next(now=NOW))
        self.assertEqual(self.sent, [])

    def test_delivery_failure_retries_without_reexecuting(self):
        self.store.create(job())
        executions = []
        service = CronSchedulerService(self.store, lambda turn: executions.append(turn.run.run_id) or "answer", lambda attempt: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(service.process_next(now=NOW), "delivery_failed")
        self.assertEqual(service.process_next(now=NOW + timedelta(seconds=2)), "delivery_failed")
        self.assertEqual(len(executions), 1)

    def test_delivery_is_claimed_before_a_new_due_execution(self):
        self.store.create(job())
        executions = []
        service = CronSchedulerService(self.store, lambda turn: executions.append(turn.run.run_id) or "answer", lambda attempt: self.sent.append(attempt.payload))
        self.assertEqual(service.process_next(now=NOW), "executed")

        self.store.create(job("next"))
        self.assertEqual(service.process_next(now=NOW), "delivered")
        self.assertEqual(executions, [executions[0]])

    def test_continuous_deliveries_cannot_starve_an_eligible_execution(self):
        self.store.create(job("first"))
        self.store.create(job("second"))
        executions = []
        service = CronSchedulerService(
            self.store,
            lambda turn: executions.append(turn.run.job_id) or "answer",
            lambda attempt: self.sent.append(attempt.payload) or "receipt",
        )

        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(service.process_next(now=NOW), "delivered")
        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(executions, ["first", "second"])

    def test_uncertain_delivery_is_terminal_without_retry_or_reexecution(self):
        self.store.create(job())
        executions = []
        service = CronSchedulerService(self.store, lambda turn: executions.append(turn.run.run_id) or "secret", lambda attempt: (_ for _ in ()).throw(TelegramDeliveryError("telegram_timeout", uncertain=True)))
        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(service.process_next(now=NOW), "delivery_unknown")
        self.assertIsNone(service.process_next(now=NOW + timedelta(minutes=1)))
        self.assertEqual(len(executions), 1)

    def test_uncertain_predecessor_paces_the_next_ordered_frame(self):
        self.store.create(job())
        service = CronSchedulerService(
            self.store,
            lambda turn: ("first", "second"),
            lambda attempt: (_ for _ in ()).throw(TelegramDeliveryError("telegram_timeout", uncertain=True)) if attempt.sequence == 0 else self.sent.append(attempt.payload) or "receipt",
            delivery_pace_seconds=5,
        )

        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(service.process_next(now=NOW), "delivery_unknown")
        self.assertIsNone(service.process_next(now=NOW + timedelta(seconds=4)))
        self.assertEqual(service.process_next(now=NOW + timedelta(seconds=5)), "delivered")
        self.assertEqual(self.sent, ["second"])

    def test_definite_delivery_failure_retries_independently(self):
        self.store.create(job())
        executions = []
        service = CronSchedulerService(self.store, lambda turn: executions.append(turn.run.run_id) or "secret", lambda attempt: (_ for _ in ()).throw(TelegramDeliveryError("telegram_rejected", uncertain=False)))
        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(service.process_next(now=NOW), "delivery_failed")
        self.assertEqual(service.process_next(now=NOW + timedelta(seconds=2)), "delivery_failed")
        self.assertEqual(len(executions), 1)

    def test_empty_or_invalid_execution_output_retries_without_a_synthetic_success_message(self):
        self.store.create(job())
        service = CronSchedulerService(self.store, lambda turn: (), lambda attempt: self.sent.append(attempt.payload))

        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertIsNone(service.process_next(now=NOW))
        self.assertEqual(self.sent, [])

    def test_approval_needed_execution_delivers_only_the_safe_control_notice(self):
        self.store.create(job())
        service = CronSchedulerService(self.store, lambda turn: (RunOutcome.APPROVAL_NEEDED, ()), lambda attempt: self.sent.append(attempt.payload))

        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(service.process_next(now=NOW), "delivered")
        self.assertEqual(self.sent, ["This scheduled action needs your approval."])

    def test_five_delivery_failures_exhaust_without_rerunning_execution(self):
        self.store.create(job())
        executions = []
        service = CronSchedulerService(self.store, lambda turn: executions.append(turn.run.run_id) or "answer", lambda attempt: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(service.process_next(now=NOW), "executed")
        for attempt in range(5):
            when = NOW + timedelta(seconds=(2 ** attempt) - 1)
            self.assertEqual(service.process_next(now=when), "delivery_failed")
        self.assertIsNone(service.process_next(now=NOW + timedelta(minutes=1)))
        self.assertEqual(len(executions), 1)

    def test_coordination_defer_retries_without_spending_an_execution_attempt(self):
        self.store.create(job())
        calls = []

        def execute(claim):
            calls.append(claim.run.run_id)
            if len(calls) == 1:
                raise CronExecutionDeferred("chat_busy")
            return "answer"

        service = CronSchedulerService(self.store, execute, lambda attempt: None)

        self.assertEqual(service.process_next(now=NOW), "deferred")
        self.assertIsNone(service.process_next(now=NOW + timedelta(seconds=1)))
        self.assertEqual(service.process_next(now=NOW + timedelta(seconds=2)), "executed")
        self.assertEqual(calls[0], calls[1])

    def test_execution_heartbeat_keeps_a_long_running_sandbox_claimed(self):
        self.store.create(job())
        started = threading.Event()
        release = threading.Event()
        renewed = threading.Event()
        renewals = 0

        renew_run_lease = self.store.renew_run_lease

        def renew(*args, **kwargs):
            nonlocal renewals
            result = renew_run_lease(*args, **kwargs)
            renewals += 1
            if renewals >= 2 and result:
                renewed.set()
            return result

        self.store.renew_run_lease = renew

        def execute(claim):
            started.set()
            self.assertTrue(release.wait(1))
            return "answer"

        service = CronSchedulerService(self.store, execute, lambda attempt: None, execution_lease_seconds=0.06)
        worker = threading.Thread(target=service.process_next)
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertTrue(renewed.wait(1))
            self.assertEqual(CronStore(self.temp.name).recover_expired_leases(), 0)
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_heartbeat_renewal_failure_prevents_completion(self):
        self.store.create(job(), now=NOW)
        renewal_attempted = threading.Event()

        def renew(*args, **kwargs):
            renewal_attempted.set()
            raise RuntimeError("storage unavailable")

        self.store.renew_run_lease = renew

        def execute(claim):
            self.assertTrue(renewal_attempted.wait(1))
            return "answer"

        service = CronSchedulerService(self.store, execute, lambda attempt: None)
        self.assertEqual(service.process_next(now=NOW), "executed")
        self.assertEqual(self.store.history("owner", "job")[-1]["action"], "executing")

    def test_scheduler_lane_ticks_independently_and_stops_after_one_start(self):
        ticks = []
        service = CronSchedulerService(self.store, lambda claim: "answer", lambda attempt: None, scheduler_interval_seconds=0.01)
        service.process_next = lambda: ticks.append(1)

        self.assertTrue(service.start())
        self.assertFalse(service.start())
        time.sleep(0.03)
        service.stop()

        self.assertGreaterEqual(len(ticks), 2)
        self.assertFalse(service._scheduler_thread.is_alive())

    def test_scheduler_lane_recovers_from_an_unexpected_tick_failure(self):
        ticks = []
        logged = []
        service = CronSchedulerService(self.store, lambda claim: "answer", lambda attempt: None, scheduler_interval_seconds=0.01)

        def process():
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("secret payload token")
            service.stop()

        service.process_next = process
        service._log_scheduler_fault = lambda code: logged.append(code)
        self.assertTrue(service.start())
        service._scheduler_thread.join(1)
        self.assertGreaterEqual(len(ticks), 2)
        self.assertEqual(logged, ["cron_scheduler_tick_failed"])

    def test_multiframe_pacing_is_durable_across_scheduler_instances(self):
        self.store.create(job())
        first = CronSchedulerService(self.store, lambda turn: ("first.", "second."), lambda attempt: self.sent.append(attempt.payload), delivery_pace_seconds=5)
        self.assertEqual(first.process_next(now=NOW), "executed")
        self.assertEqual(first.process_next(now=NOW), "delivered")

        restarted = CronSchedulerService(CronStore(self.temp.name), lambda turn: "unexpected", lambda attempt: self.sent.append(attempt.payload), delivery_pace_seconds=5)
        self.assertIsNone(restarted.process_next(now=NOW + timedelta(seconds=4)))
        self.assertEqual(restarted.process_next(now=NOW + timedelta(seconds=5)), "delivered")
        self.assertEqual(self.sent, ["first.", "second."])


if __name__ == "__main__":
    unittest.main()
