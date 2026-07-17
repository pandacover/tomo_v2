import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from tomo_core.cron_models import (
    CronExecutionClaim,
    ControlNotice,
    CronJob,
    DeliveryAttempt,
    JobIntent,
    JobProgress,
    JobRun,
    LifecyclePolicy,
    RunOutcome,
    ScheduleSpec,
)


UTC = timezone.utc


class CronModelTests(unittest.TestCase):
    def test_frozen_models_retain_valid_domain_values(self):
        schedule = ScheduleSpec.interval(300)
        lifecycle = LifecyclePolicy(max_successful_runs=2)
        job = CronJob("job-1", "owner-1", "telegram:chat-1", JobIntent("Check the feed", ("be concise",)), schedule, lifecycle)
        run = JobRun("run-1", "job-1", datetime(2026, 1, 1, tzinfo=UTC), "schedule", 3)

        self.assertEqual(job.status, "active")
        self.assertEqual(run.revision, 3)
        self.assertEqual(JobProgress(1, RunOutcome.SUCCEEDED).successful_runs, 1)
        self.assertEqual(CronExecutionClaim(run, "lease-token", "owner", "telegram:chat", JobIntent("Check the feed"), JobProgress()).run, run)
        self.assertEqual(ControlNotice("job-1", "failed").message, "failed")
        self.assertEqual(DeliveryAttempt("delivery-1", "run-1", 1, "lease-1").status, "pending")
        with self.assertRaises(FrozenInstanceError):
            run.revision = 4

    def test_models_reject_invalid_ids_owner_destination_intent_lifecycle_and_timezone(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        valid_intent = JobIntent("Do the thing")
        valid_schedule = ScheduleSpec.once(now)
        for factory in (
            lambda: CronJob(" ", "owner", "telegram:chat", valid_intent, valid_schedule),
            lambda: CronJob("job", " ", "telegram:chat", valid_intent, valid_schedule),
            lambda: CronJob("job", "owner", " ", valid_intent, valid_schedule),
            lambda: JobIntent(" "),
            lambda: JobIntent("x" * 4001),
            lambda: LifecyclePolicy(max_successful_runs=0),
            lambda: LifecyclePolicy(ends_at=now.replace(tzinfo=None)),
            lambda: ScheduleSpec.cron("0 9 * * *", "Not/A_Timezone"),
            lambda: ScheduleSpec.once(now.replace(tzinfo=None)),
        ):
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_run_revision_and_outcome_are_validated(self):
        due = datetime(2026, 1, 1, tzinfo=UTC)
        for factory in (
            lambda: JobRun("run", "job", due, "schedule", 0),
            lambda: JobRun("run", "job", due.replace(tzinfo=None), "schedule", 1),
            lambda: JobRun("run", "job", due, "unknown", 1),
            lambda: JobProgress(-1),
        ):
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()


if __name__ == "__main__":
    unittest.main()
