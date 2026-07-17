import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tomo_core.cron_models import CronJob, JobIntent, LifecyclePolicy, RunOutcome, ScheduleSpec
from tomo_core.cron_store import CronStore


UTC = timezone.utc
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def job(job_id="job-1", *, schedule=None, lifecycle=None):
    return CronJob(job_id, "owner-1", "telegram:chat-1", JobIntent("Check status"), schedule or ScheduleSpec.interval(60, starts_at=NOW), lifecycle or LifecyclePolicy())


class CronStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CronStore(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_persists_idempotent_owner_scoped_jobs_and_orders_history(self):
        created = self.store.create(job(), now=NOW)
        self.assertEqual(self.store.create(job(), now=NOW), created)
        restarted = CronStore(self.temp.name)
        self.assertEqual(restarted.get("owner-1", "job-1"), created)
        self.assertEqual(restarted.get("owner-2", "job-1"), None)
        self.assertEqual(restarted.list("owner-2"), ())
        self.assertEqual(restarted.list("owner-1"), (created,))
        self.assertTrue(restarted.pause("owner-1", "job-1", 1, now=NOW + timedelta(minutes=1)))
        self.assertTrue(restarted.resume("owner-1", "job-1", 2, now=NOW + timedelta(minutes=2)))
        self.assertEqual([entry["action"] for entry in restarted.history("owner-1", "job-1")], ["create", "pause", "resume"])

    def test_terminal_redaction_erases_receipt_secrets_and_replay_returns_tombstone(self):
        sensitive = CronJob("secret", "owner-1", "telegram:private-destination", JobIntent("secret intent", ("secret constraint",)), ScheduleSpec.once(NOW))
        key = "raw-secret-key"
        request = '{"intent":"secret intent","destination":"telegram:private-destination","constraint":"secret constraint"}'
        created, replayed = self.store.mutate_idempotently("owner-1", "create", key, request, job=sensitive, now=NOW)
        self.assertFalse(replayed)
        self.assertEqual(created.intent.text, "secret intent")
        self.assertTrue(self.store.cancel("owner-1", "secret", 1, now=NOW))

        with self.store._read() as db:
            columns = {row["name"] for row in db.execute("pragma table_info(cron_mutation_receipts)")}
            receipt = db.execute("select * from cron_mutation_receipts").fetchone()
        self.assertEqual(columns, {"owner_id", "operation", "job_id", "idempotency_key_hash", "request_hash", "response", "created_at"})
        self.assertNotIn(key, str(tuple(receipt)))
        self.assertNotIn(request, str(tuple(receipt)))
        self.assertNotIn("secret intent", receipt["response"])
        self.assertNotIn("secret constraint", receipt["response"])
        self.assertNotIn("telegram:private-destination", receipt["response"])
        replay, replayed = self.store.mutate_idempotently("owner-1", "create", key, request, job=sensitive, now=NOW)
        self.assertTrue(replayed)
        self.assertEqual((replay.status, replay.intent.text, replay.destination), ("cancelled", "[redacted]", "telegram:redacted"))

    def test_idempotent_pause_cannot_resurrect_ended_job(self):
        self.store.create(job("ended", schedule=ScheduleSpec.once(NOW)))
        run = self.store.claim_due_run(now=NOW)
        self.assertTrue(self.store.complete_run(run.run.run_id, run.lease_token, RunOutcome.SUCCEEDED, "done", now=NOW))
        changed, replayed = self.store.mutate_idempotently("owner-1", "pause", "fresh-pause-key", '{"jobId":"ended","revision":1}', job_id="ended", revision=1, now=NOW)
        self.assertIsNone(changed)
        self.assertFalse(replayed)
        self.assertEqual(self.store.get("owner-1", "ended").status, "ended")

    def test_update_uses_revision_cas_and_scheduled_occurrences_are_deduplicated(self):
        self.store.create(job())
        self.assertIsNone(self.store.update("owner-1", job(schedule=ScheduleSpec.interval(120, starts_at=NOW)), 2))
        updated = self.store.update("owner-1", job(schedule=ScheduleSpec.interval(120, starts_at=NOW)), 1, now=NOW)
        self.assertEqual(updated.revision, 2)
        first = self.store.claim_due_run(now=NOW + timedelta(minutes=2), lease_seconds=30)
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.claim_due_run(now=NOW + timedelta(minutes=2), lease_seconds=30))

    def test_new_interval_and_cron_jobs_anchor_their_first_due_time_to_creation(self):
        created = NOW + timedelta(seconds=17)
        with patch("tomo_core.cron_store._time", return_value=created):
            self.store.create(job("interval-created", schedule=ScheduleSpec.interval(60)))
            self.store.create(job("cron-created", schedule=ScheduleSpec.cron("*/5 * * * *")))

        self.assertIsNone(self.store.claim_due_run(now=created + timedelta(seconds=59)))
        interval = self.store.claim_due_run(now=created + timedelta(seconds=60), lease_seconds=10)
        self.assertEqual(interval.run.job_id, "interval-created")
        self.store.complete_run(interval.run.run_id, interval.lease_token, RunOutcome.SUCCEEDED, "ok", now=created + timedelta(seconds=60))
        cron = self.store.claim_due_run(now=datetime(2026, 1, 1, 12, 5, tzinfo=UTC), lease_seconds=10)
        self.assertEqual(cron.run.job_id, "cron-created")
        self.assertEqual(cron.run.scheduled_for, datetime(2026, 1, 1, 12, 5, tzinfo=UTC))

    def test_leases_recover_across_instances_and_manual_runs_do_not_advance_schedule(self):
        self.store.create(job())
        manual = self.store.request_manual_run("owner-1", "job-1", now=NOW)
        claimed = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertEqual(claimed.run.run_id, manual.run_id)
        second = CronStore(self.temp.name)
        self.assertIsNone(second.claim_due_run(now=NOW + timedelta(seconds=5), lease_seconds=10))
        self.assertEqual(second.recover_expired_leases(now=NOW + timedelta(seconds=11)), 1)
        recovered = second.claim_due_run(now=NOW + timedelta(seconds=11), lease_seconds=10)
        self.assertEqual(recovered.run.run_id, manual.run_id)
        self.assertTrue(second.complete_run(recovered.run.run_id, recovered.lease_token, RunOutcome.SUCCEEDED, "ok", now=NOW + timedelta(seconds=12)))
        delivery = second.claim_delivery(now=NOW + timedelta(seconds=12))
        self.assertTrue(second.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(seconds=12)))
        self.assertTrue(second.complete_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(seconds=12)))
        scheduled = second.claim_due_run(now=NOW + timedelta(minutes=1), lease_seconds=10)
        self.assertEqual(scheduled.run.trigger, "schedule")

    def test_begin_run_extends_execution_lease_past_sandbox_timeout(self):
        self.store.create(job())
        claimed = self.store.claim_due_run(now=NOW + timedelta(minutes=1), lease_seconds=1)
        self.assertTrue(self.store.begin_run(claimed.run.run_id, claimed.lease_token, now=NOW + timedelta(minutes=1)))
        self.assertEqual(self.store.recover_expired_leases(now=NOW + timedelta(minutes=3, seconds=1)), 0)
        self.assertEqual(self.store.recover_expired_leases(now=NOW + timedelta(minutes=4, seconds=1)), 1)

    def test_expired_claim_cannot_mutate_a_run_reclaimed_by_another_store(self):
        self.store.create(job(schedule=ScheduleSpec.once(NOW)))
        first = self.store.claim_due_run(now=NOW, lease_seconds=1)
        self.assertTrue(self.store.begin_run(first.run.run_id, first.lease_token, now=NOW))

        second_store = CronStore(self.temp.name)
        self.assertEqual(second_store.recover_expired_leases(now=NOW + timedelta(minutes=4)), 1)
        second = second_store.claim_due_run(now=NOW + timedelta(minutes=4), lease_seconds=60)
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertTrue(second_store.begin_run(second.run.run_id, second.lease_token, now=NOW + timedelta(minutes=4)))
        self.assertFalse(self.store.run_is_current(first.run.run_id, first.run.revision, first.lease_token))
        self.assertTrue(second_store.run_is_current(second.run.run_id, second.run.revision, second.lease_token))
        self.assertTrue(second_store.complete_run(second.run.run_id, second.lease_token, RunOutcome.SUCCEEDED, "fresh", now=NOW + timedelta(minutes=4)))

        self.assertFalse(self.store.renew_run_lease(first.run.run_id, first.lease_token, now=NOW + timedelta(minutes=4)))
        self.assertFalse(self.store.begin_run(first.run.run_id, first.lease_token, now=NOW + timedelta(minutes=4)))
        self.assertFalse(self.store.complete_run(first.run.run_id, first.lease_token, RunOutcome.SUCCEEDED, "stale", now=NOW + timedelta(minutes=4)))
        self.assertFalse(self.store.fail_run(first.run.run_id, first.lease_token, "stale", now=NOW + timedelta(minutes=4)))
        self.assertFalse(self.store.defer_run(first.run.run_id, first.lease_token, now=NOW + timedelta(minutes=4)))

    def test_pause_resume_creates_one_catchup_and_lifecycle_ends_after_success_or_expiry(self):
        self.store.create(job())
        self.assertTrue(self.store.pause("owner-1", "job-1", 1, now=NOW))
        self.assertTrue(self.store.resume("owner-1", "job-1", 2, now=NOW + timedelta(minutes=3)))
        catchup = self.store.claim_due_run(now=NOW + timedelta(minutes=3), lease_seconds=10)
        self.assertEqual(catchup.run.trigger, "catchup")
        self.store.complete_run(catchup.run.run_id, catchup.lease_token, RunOutcome.SUCCEEDED, "ok", now=NOW + timedelta(minutes=3))
        self.assertTrue(self.store.cancel("owner-1", "job-1", 3, now=NOW + timedelta(minutes=3)))

        bounded = job("bounded", lifecycle=LifecyclePolicy(max_successful_runs=1))
        self.store.create(bounded)
        final = self.store.claim_due_run(now=NOW + timedelta(minutes=4), lease_seconds=10)
        self.assertFalse(final.will_end_after_run)
        self.store.complete_run(final.run.run_id, final.lease_token, RunOutcome.SUCCEEDED, "done", now=NOW + timedelta(minutes=4))
        self.assertEqual(self.store.get("owner-1", "bounded").status, "ended")

        expired = job("expired", lifecycle=LifecyclePolicy(ends_at=NOW))
        self.store.create(expired)
        lifecycle = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertEqual(lifecycle.run.trigger, "lifecycle")
        self.assertEqual(self.store.get("owner-1", "expired").status, "ended")

    def test_automation_turn_marks_every_final_execution(self):
        self.store.create(job("one-shot", schedule=ScheduleSpec.once(NOW)))
        one_shot = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertTrue(one_shot.will_end_after_run)
        self.store.complete_run(one_shot.run.run_id, one_shot.lease_token, RunOutcome.SUCCEEDED, "ok", now=NOW)

        self.store.create(job("ends-before-next", lifecycle=LifecyclePolicy(ends_at=NOW + timedelta(seconds=90))))
        ends_before_next = self.store.claim_due_run(now=NOW + timedelta(minutes=1), lease_seconds=10)
        self.assertTrue(ends_before_next.will_end_after_run)

        self.store.create(job("lifecycle", lifecycle=LifecyclePolicy(ends_at=NOW)))
        lifecycle = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertEqual(lifecycle.run.trigger, "lifecycle")
        self.assertTrue(lifecycle.will_end_after_run)

    def test_final_normal_occurrence_ends_without_a_duplicate_lifecycle_run(self):
        self.store.create(job("final-normal", lifecycle=LifecyclePolicy(ends_at=NOW + timedelta(seconds=90))))
        final = self.store.claim_due_run(now=NOW + timedelta(minutes=1))
        self.assertTrue(final.will_end_after_run)
        self.assertTrue(self.store.complete_run(final.run.run_id, final.lease_token, RunOutcome.SUCCEEDED, "done", now=NOW + timedelta(minutes=1)))

        self.assertEqual(self.store.get("owner-1", "final-normal").status, "ended")
        self.assertIsNone(self.store.claim_due_run(now=NOW + timedelta(minutes=2)))

    def test_downtime_at_ends_at_claims_the_last_normal_occurrence(self):
        self.store.create(job("downtime-final", lifecycle=LifecyclePolicy(ends_at=NOW + timedelta(minutes=3, seconds=30))))
        claimed = self.store.claim_due_run(now=NOW + timedelta(minutes=5))
        self.assertEqual((claimed.run.trigger, claimed.run.scheduled_for, claimed.will_end_after_run), ("schedule", NOW + timedelta(minutes=3), True))

    def test_claim_invalidates_non_active_or_stale_runs_and_claims_next_eligible_run(self):
        self.store.create(job("cancelled", schedule=ScheduleSpec.once(NOW)), now=NOW)
        pending = self.store.request_manual_run("owner-1", "cancelled", now=NOW)
        self.assertTrue(self.store.cancel("owner-1", "cancelled", 1, now=NOW))
        self.store.create(job("eligible", schedule=ScheduleSpec.once(NOW)))

        claimed = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertEqual(claimed.run.job_id, "eligible")
        self.assertEqual(self.store.history("owner-1", "cancelled")[-1]["action"], "invalidated")
        self.assertEqual(pending.status, "pending")

    def test_claim_invalidates_paused_and_revision_stale_retry_runs(self):
        self.store.create(job("paused"), now=NOW)
        self.store.request_manual_run("owner-1", "paused", now=NOW)
        self.assertTrue(self.store.pause("owner-1", "paused", 1, now=NOW))

        self.store.create(job("stale"))
        stale = self.store.request_manual_run("owner-1", "stale", now=NOW)
        claimed = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertEqual(claimed.run.run_id, stale.run_id)
        self.assertTrue(self.store.fail_run(stale.run_id, claimed.lease_token, "temporary", now=NOW))
        self.assertIsNotNone(self.store.update("owner-1", job("stale", schedule=ScheduleSpec.interval(120, starts_at=NOW)), 1, now=NOW))

        self.store.create(job("eligible", schedule=ScheduleSpec.once(NOW)))
        eligible = self.store.claim_due_run(now=NOW + timedelta(seconds=2), lease_seconds=10)
        self.assertNotEqual(eligible.run.run_id, stale.run_id)
        self.assertEqual(self.store.history("owner-1", "paused")[-1]["action"], "invalidated")
        self.assertIn("invalidated", [entry["action"] for entry in self.store.history("owner-1", "stale")])

    def test_lifecycle_retry_remains_claimable_after_job_ends(self):
        self.store.create(job("expiring", lifecycle=LifecyclePolicy(ends_at=NOW)))
        first = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertEqual(first.run.trigger, "lifecycle")
        self.assertTrue(self.store.fail_run(first.run.run_id, first.lease_token, "temporary", now=NOW))

        retry = self.store.claim_due_run(now=NOW + timedelta(seconds=2), lease_seconds=10)
        self.assertEqual(retry.run.run_id, first.run.run_id)

    def test_failed_manual_run_does_not_end_job_before_remaining_schedule_occurrence(self):
        ends_at = NOW + timedelta(minutes=3, seconds=30)
        self.store.create(job("manual-near-end", lifecycle=LifecyclePolicy(ends_at=ends_at)))
        manual = self.store.request_manual_run("owner-1", "manual-near-end", now=NOW + timedelta(minutes=2, seconds=50))
        claimed = self.store.claim_due_run(now=NOW + timedelta(minutes=2, seconds=50), lease_seconds=10)
        self.assertEqual((claimed.run.run_id, claimed.run.trigger), (manual.run_id, "manual"))

        failure_times = (NOW + timedelta(minutes=2, seconds=50), NOW + timedelta(minutes=2, seconds=52), NOW + timedelta(minutes=2, seconds=56))
        for attempt, failure_time in enumerate(failure_times):
            self.assertTrue(self.store.fail_run(claimed.run.run_id, claimed.lease_token, "temporary", now=failure_time))
            if attempt < 2:
                claimed = self.store.claim_due_run(now=failure_time + timedelta(seconds=2 ** attempt), lease_seconds=10)

        self.assertEqual(self.store.get("owner-1", "manual-near-end").status, "active")
        delivery = self.store.claim_delivery(now=NOW + timedelta(minutes=2, seconds=56))
        self.assertIsNotNone(delivery)
        self.assertTrue(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=2, seconds=56)))
        self.assertTrue(self.store.complete_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=2, seconds=56)))
        scheduled = self.store.claim_due_run(now=NOW + timedelta(minutes=3))
        self.assertEqual(scheduled.run.trigger, "schedule")

    def test_schedule_update_anchors_progression_at_update_time(self):
        self.store.create(job("updated", schedule=ScheduleSpec.interval(60, starts_at=NOW)))
        updated_at = NOW + timedelta(hours=1)
        updated = self.store.update(
            "owner-1",
            job("updated", schedule=ScheduleSpec.interval(120)),
            1,
            now=updated_at,
        )
        self.assertEqual(updated.revision, 2)
        self.assertIsNone(self.store.claim_due_run(now=updated_at + timedelta(seconds=119)))
        claimed = self.store.claim_due_run(now=updated_at + timedelta(seconds=120), lease_seconds=10)
        self.assertEqual(claimed.run.scheduled_for, updated_at + timedelta(seconds=120))

    def test_delivery_claim_suppresses_revision_stale_output(self):
        self.store.create(job("delivery"))
        run = self.store.request_manual_run("owner-1", "delivery", now=NOW)
        self.assertIsNotNone(run)
        claimed = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertIsNotNone(claimed)
        self.assertTrue(self.store.complete_run(run.run_id, claimed.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW))
        self.assertIsNotNone(self.store.update("owner-1", job("delivery", schedule=ScheduleSpec.once(NOW)), 1, now=NOW))

        self.assertIsNone(self.store.claim_delivery(now=NOW, lease_seconds=10))
        with self.store._read() as db:
            self.assertEqual(db.execute("select status from cron_deliveries where run_id=?", (run.run_id,)).fetchone()[0], "suppressed")

    def test_delivery_admission_rejects_a_claim_invalidated_before_send(self):
        self.store.create(job("admission"))
        run = self.store.claim_due_run(now=NOW + timedelta(minutes=1))
        self.assertTrue(self.store.complete_run(run.run.run_id, run.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW + timedelta(minutes=1)))
        delivery = self.store.claim_delivery(now=NOW + timedelta(minutes=1))
        self.assertTrue(self.store.update("owner-1", job("admission", schedule=ScheduleSpec.interval(120, starts_at=NOW)), 1, now=NOW + timedelta(minutes=1)))

        self.assertFalse(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=1)))

    def test_delivery_lease_fences_stale_workers_after_recovery_and_reclaim(self):
        self.store.create(job("delivery-lease", schedule=ScheduleSpec.once(NOW)))
        run = self.store.claim_due_run(now=NOW)
        self.assertTrue(self.store.complete_run(run.run.run_id, run.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW))
        first = self.store.claim_delivery(now=NOW, lease_seconds=1)

        second_store = CronStore(self.temp.name)
        self.assertEqual(second_store.recover_expired_leases(now=NOW + timedelta(seconds=2)), 1)
        current = second_store.claim_delivery(now=NOW + timedelta(seconds=2), lease_seconds=60)
        self.assertNotEqual(first.lease_token, current.lease_token)
        self.assertTrue(second_store.admit_delivery(current.delivery_id, current.lease_token, now=NOW + timedelta(seconds=2)))

        self.assertFalse(self.store.complete_delivery(first.delivery_id, first.lease_token, now=NOW + timedelta(seconds=2)))
        self.assertFalse(self.store.fail_delivery(first.delivery_id, first.lease_token, "stale", now=NOW + timedelta(seconds=2)))
        self.assertFalse(self.store.mark_delivery_unknown(first.delivery_id, first.lease_token, now=NOW + timedelta(seconds=2)))
        self.assertTrue(second_store.complete_delivery(current.delivery_id, current.lease_token, now=NOW + timedelta(seconds=2)))

    def test_interval_starts_at_is_the_first_due_time_and_catchup_origin(self):
        start = NOW + timedelta(minutes=10)
        self.store.create(job("future-start", schedule=ScheduleSpec.interval(60, starts_at=start)))
        self.assertIsNone(self.store.claim_due_run(now=start - timedelta(seconds=1)))
        first = self.store.claim_due_run(now=start)
        self.assertEqual(first.run.scheduled_for, start)

        self.store.create(job("delayed-start", schedule=ScheduleSpec.interval(60, starts_at=NOW)))
        delayed = self.store.claim_due_run(now=NOW + timedelta(minutes=3, seconds=30))
        self.assertEqual(delayed.run.scheduled_for, NOW + timedelta(minutes=3))

    def test_partial_and_approval_outcomes_end_final_jobs_and_redact_after_delivery(self):
        for name, outcome in (("partial-once", RunOutcome.PARTIAL), ("approval-final", RunOutcome.APPROVAL_NEEDED)):
            with self.subTest(outcome=outcome):
                schedule = ScheduleSpec.once(NOW) if name == "partial-once" else ScheduleSpec.interval(60, starts_at=NOW)
                lifecycle = LifecyclePolicy() if name == "partial-once" else LifecyclePolicy(ends_at=NOW + timedelta(seconds=90))
                self.store.create(job(name, schedule=schedule, lifecycle=lifecycle))
                claimed = self.store.claim_due_run(now=NOW + timedelta(minutes=1))
                self.assertTrue(self.store.complete_run(claimed.run.run_id, claimed.lease_token, outcome, "result", now=NOW + timedelta(minutes=1)))
                self.assertEqual(self.store.get("owner-1", name).status, "ended")
                delivery = self.store.claim_delivery(now=NOW + timedelta(minutes=1))
                self.assertTrue(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=1)))
                self.assertTrue(self.store.complete_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=1)))
                with self.store._read() as db:
                    self.assertEqual(db.execute("select output from cron_deliveries where delivery_id=?", (delivery.delivery_id,)).fetchone()[0], None)

        self.store.create(job("partial-recurring"))
        claimed = self.store.claim_due_run(now=NOW + timedelta(minutes=1))
        self.assertTrue(self.store.complete_run(claimed.run.run_id, claimed.lease_token, RunOutcome.PARTIAL, "result", now=NOW + timedelta(minutes=1)))
        self.assertEqual(self.store.get("owner-1", "partial-recurring").status, "active")

    def test_revision_mutation_fails_after_delivery_is_admitted(self):
        self.store.create(job("sending"))
        run = self.store.claim_due_run(now=NOW + timedelta(minutes=1))
        self.store.complete_run(run.run.run_id, run.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW + timedelta(minutes=1))
        delivery = self.store.claim_delivery(now=NOW + timedelta(minutes=1))
        self.assertTrue(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=1)))

        self.assertFalse(self.store.pause("owner-1", "sending", 1, now=NOW))
        self.assertEqual(self.store.get("owner-1", "sending").revision, 1)

    def test_delivery_admission_fails_after_revision_mutation_commits(self):
        self.store.create(job("sending"))
        run = self.store.claim_due_run(now=NOW + timedelta(minutes=1))
        self.store.complete_run(run.run.run_id, run.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW + timedelta(minutes=1))
        delivery = self.store.claim_delivery(now=NOW + timedelta(minutes=1))
        self.assertTrue(self.store.update("owner-1", job("sending", schedule=ScheduleSpec.interval(120)), 1, now=NOW + timedelta(minutes=1)))

        self.assertFalse(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=1)))

    def test_unknown_delivery_is_terminal_and_redacts_after_send_attempt(self):
        self.store.create(job("unknown", schedule=ScheduleSpec.once(NOW)))
        run = self.store.claim_due_run(now=NOW)
        self.store.complete_run(run.run.run_id, run.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW)
        delivery = self.store.claim_delivery(now=NOW)
        self.assertTrue(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW))
        self.assertTrue(self.store.mark_delivery_unknown(delivery.delivery_id, delivery.lease_token, now=NOW))
        self.assertIsNone(self.store.claim_delivery(now=NOW))
        with self.store._read() as db:
            self.assertEqual(db.execute("select status,output,error from cron_deliveries where delivery_id=?", (delivery.delivery_id,)).fetchone()[:], ("unknown", None, None))

    def test_pending_delivery_coalesces_the_next_due_occurrence_for_its_job(self):
        self.store.create(job("coalesced"))
        first = self.store.claim_due_run(now=NOW + timedelta(minutes=1))
        self.assertTrue(self.store.complete_run(first.run.run_id, first.lease_token, RunOutcome.SUCCEEDED, "result", now=NOW + timedelta(minutes=1)))

        self.assertIsNone(self.store.claim_due_run(now=NOW + timedelta(minutes=2)))

    def test_delivery_frames_wait_for_lower_sequence_retry(self):
        self.store.create(job("ordered", schedule=ScheduleSpec.once(NOW)))
        claimed = self.store.claim_due_run(now=NOW)
        self.assertTrue(self.store.complete_run(claimed.run.run_id, claimed.lease_token, RunOutcome.SUCCEEDED, ("first", "second"), now=NOW))
        first = self.store.claim_delivery(now=NOW)
        self.assertEqual(first.sequence, 0)
        self.assertTrue(self.store.admit_delivery(first.delivery_id, first.lease_token, now=NOW))
        self.assertTrue(self.store.fail_delivery(first.delivery_id, first.lease_token, "temporary", now=NOW))
        self.assertIsNone(self.store.claim_delivery(now=NOW))
        retry = self.store.claim_delivery(now=NOW + timedelta(seconds=1))
        self.assertEqual(retry.sequence, 0)

    def test_delivery_frames_wait_for_lower_sequence_sending_across_stores(self):
        self.store.create(job("ordered-sending", schedule=ScheduleSpec.once(NOW)))
        claimed = self.store.claim_due_run(now=NOW)
        self.assertTrue(self.store.complete_run(claimed.run.run_id, claimed.lease_token, RunOutcome.SUCCEEDED, ("first", "second"), now=NOW))
        first_store = CronStore(self.temp.name)
        second_store = CronStore(self.temp.name)

        first = first_store.claim_delivery(now=NOW)
        self.assertEqual(first.sequence, 0)
        self.assertTrue(first_store.admit_delivery(first.delivery_id, first.lease_token, now=NOW))
        self.assertIsNone(second_store.claim_delivery(now=NOW))

    def test_unknown_predecessor_paces_the_next_ordered_frame(self):
        self.store.create(job("ordered-unknown", schedule=ScheduleSpec.once(NOW)))
        claimed = self.store.claim_due_run(now=NOW)
        self.assertTrue(self.store.complete_run(claimed.run.run_id, claimed.lease_token, RunOutcome.SUCCEEDED, ("first", "second"), now=NOW))
        first = self.store.claim_delivery(now=NOW)
        self.assertTrue(self.store.admit_delivery(first.delivery_id, first.lease_token, now=NOW))
        self.assertTrue(self.store.mark_delivery_unknown(first.delivery_id, first.lease_token, now=NOW, pace_seconds=5))

        self.assertIsNone(self.store.claim_delivery(now=NOW + timedelta(seconds=4)))
        second = self.store.claim_delivery(now=NOW + timedelta(seconds=5))
        self.assertEqual(second.sequence, 1)

    def test_exhausted_and_recovered_sending_predecessors_pace_the_next_frame(self):
        for name, terminal in (("exhausted", "exhausted"), ("recovered", "recovered")):
            with self.subTest(terminal=terminal):
                self.store.create(job(name, schedule=ScheduleSpec.once(NOW)))
                claimed = self.store.claim_due_run(now=NOW)
                self.assertTrue(self.store.complete_run(claimed.run.run_id, claimed.lease_token, RunOutcome.SUCCEEDED, ("first", "second"), now=NOW))
                first = self.store.claim_delivery(now=NOW, lease_seconds=1)
                self.assertTrue(self.store.admit_delivery(first.delivery_id, first.lease_token, now=NOW))
                if terminal == "exhausted":
                    self.assertTrue(self.store.fail_delivery(first.delivery_id, first.lease_token, "offline", now=NOW, max_attempts=1, pace_seconds=5))
                    ready_at = NOW + timedelta(seconds=5)
                else:
                    self.assertEqual(self.store.recover_expired_leases(now=NOW + timedelta(seconds=2), pace_seconds=5), 1)
                    ready_at = NOW + timedelta(seconds=7)

                self.assertIsNone(self.store.claim_delivery(now=ready_at - timedelta(seconds=1)))
                second = self.store.claim_delivery(now=ready_at)
                self.assertEqual(second.sequence, 1)

    def test_claimed_context_preserves_intent_constraints_and_progress(self):
        self.store.create(CronJob("context", "owner-1", "telegram:chat-1", JobIntent("Check status", ("be concise",)), ScheduleSpec.interval(60, starts_at=NOW)))
        first = self.store.claim_due_run(now=NOW + timedelta(minutes=1))
        self.assertEqual(first.intent, JobIntent("Check status", ("be concise",)))
        self.assertEqual(first.progress.successful_runs, 0)
        self.assertIsNone(first.progress.previous_outcome)
        self.store.complete_run(first.run.run_id, first.lease_token, RunOutcome.SUCCEEDED, "done", now=NOW + timedelta(minutes=1))
        delivery = self.store.claim_delivery(now=NOW + timedelta(minutes=1))
        self.assertTrue(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=1)))
        self.assertTrue(self.store.complete_delivery(delivery.delivery_id, delivery.lease_token, now=NOW + timedelta(minutes=1)))
        second = self.store.claim_due_run(now=NOW + timedelta(minutes=2))
        self.assertEqual(second.progress.successful_runs, 1)
        self.assertEqual(second.progress.previous_outcome, RunOutcome.SUCCEEDED)

    def test_one_shot_ends_after_final_execution_failure_and_keeps_one_notice(self):
        self.store.create(job("failing-once", schedule=ScheduleSpec.once(NOW)))
        run = self.store.claim_due_run(now=NOW)
        for attempt, when in enumerate((NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=3))):
            self.assertTrue(self.store.fail_run(run.run.run_id, run.lease_token, "failed", now=when))
            if attempt < 2:
                run = self.store.claim_due_run(now=when + timedelta(seconds=2 ** attempt))
        self.assertEqual(self.store.get("owner-1", "failing-once").status, "ended")
        with self.store._read() as db:
            self.assertEqual(db.execute("select count(*) from cron_deliveries where run_id=?", (run.run.run_id,)).fetchone()[0], 1)

    def test_sqlite_uses_full_synchronous_durability(self):
        with self.store._read() as db:
            self.assertEqual(db.execute("pragma synchronous").fetchone()[0], 2)

    def test_stale_output_is_suppressed_and_terminal_jobs_are_redacted_only_after_delivery(self):
        self.store.create(job(schedule=ScheduleSpec.once(NOW)))
        claimed = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertTrue(self.store.cancel("owner-1", "job-1", 1, now=NOW))
        self.assertFalse(self.store.complete_run(claimed.run.run_id, claimed.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW))
        self.assertIsNone(self.store.claim_delivery(now=NOW, lease_seconds=10))
        self.assertTrue(self.store.redact_terminal_job("owner-1", "job-1"))
        self.store.create(job("delivered", schedule=ScheduleSpec.once(NOW)))
        normal = self.store.claim_due_run(now=NOW, lease_seconds=10)
        self.assertTrue(self.store.complete_run(normal.run.run_id, normal.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW))
        self.assertFalse(self.store.redact_terminal_job("owner-1", "delivered"))
        delivery = self.store.claim_delivery(now=NOW, lease_seconds=10)
        self.assertTrue(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW))
        self.assertTrue(self.store.complete_delivery(delivery.delivery_id, delivery.lease_token, now=NOW))
        self.assertTrue(self.store.redact_terminal_job("owner-1", "delivered"))
        self.assertEqual(self.store.history("owner-1", "delivered")[-1]["output"], None)
        with self.store._read() as db:
            redacted = db.execute("select intent_text,constraints,destination from cron_jobs where job_id='delivered'").fetchone()
        self.assertEqual(tuple(redacted), ("", "[]", ""))

    def test_update_and_cancel_immediately_invalidate_old_work_and_cancel_redacts(self):
        self.store.create(job(schedule=ScheduleSpec.once(NOW)))
        claimed = self.store.claim_due_run(now=NOW)
        updated = self.store.update("owner-1", job(schedule=ScheduleSpec.once(NOW + timedelta(hours=1))), 1, now=NOW)
        self.assertEqual(updated.revision, 2)
        self.assertFalse(self.store.run_is_current(claimed.run.run_id, claimed.run.revision))
        self.assertTrue(self.store.cancel("owner-1", "job-1", 2, now=NOW))
        with self.store._read() as db:
            run = db.execute("select status from cron_runs where run_id=?", (claimed.run.run_id,)).fetchone()
            tombstone = db.execute("select intent_text,constraints,destination from cron_jobs where job_id='job-1'").fetchone()
        self.assertEqual(run["status"], "invalidated")
        self.assertEqual(tuple(tombstone), ("", "[]", ""))

    def test_exhausted_terminal_delivery_redacts_without_manual_cleanup(self):
        self.store.create(job(schedule=ScheduleSpec.once(NOW)))
        claimed = self.store.claim_due_run(now=NOW)
        self.assertTrue(self.store.complete_run(claimed.run.run_id, claimed.lease_token, RunOutcome.SUCCEEDED, "secret", now=NOW))
        delivery = self.store.claim_delivery(now=NOW)
        self.assertTrue(self.store.admit_delivery(delivery.delivery_id, delivery.lease_token, now=NOW))
        self.assertTrue(self.store.fail_delivery(delivery.delivery_id, delivery.lease_token, "offline", now=NOW, max_attempts=1))
        with self.store._read() as db:
            job_row = db.execute("select intent_text,constraints,destination from cron_jobs where job_id='job-1'").fetchone()
            delivery_row = db.execute("select status,output,error from cron_deliveries where delivery_id=?", (delivery.delivery_id,)).fetchone()
        self.assertEqual(tuple(job_row), ("", "[]", ""))
        self.assertEqual(tuple(delivery_row), ("exhausted", None, None))

    def test_delivery_sequence_is_unique_per_run(self):
        self.store.create(job("unique", schedule=ScheduleSpec.once(NOW)))
        claimed = self.store.claim_due_run(now=NOW)
        self.store.complete_run(claimed.run.run_id, claimed.lease_token, RunOutcome.SUCCEEDED, "one", now=NOW)
        with self.store._write() as db:
            with self.assertRaises(Exception):
                db.execute("insert into cron_deliveries(delivery_id,run_id,sequence,attempts,status,created_at,updated_at) values('duplicate',?,0,0,'pending',?,?)", (claimed.run.run_id, NOW.isoformat(), NOW.isoformat()))

    def test_existing_delivery_table_migrates_to_a_unique_run_sequence_index(self):
        with tempfile.TemporaryDirectory() as temp:
            path = f"{temp}/cron.sqlite3"
            legacy = sqlite3.connect(path)
            try:
                with legacy as db:
                    db.execute("create table cron_deliveries(delivery_id text primary key,run_id text not null,sequence integer not null,attempts integer not null,status text not null,available_at text,lease_until text,created_at text not null,updated_at text not null,output text,error text,destination text,job_revision integer,telegram_receipt_id text)")
                    db.executemany("insert into cron_deliveries values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (("old-1", "run", 0, 0, "pending", None, None, NOW.isoformat(), NOW.isoformat(), None, None, None, None, None), ("old-2", "run", 0, 0, "pending", None, None, NOW.isoformat(), NOW.isoformat(), None, None, None, None, None)))
            finally:
                legacy.close()
            migrated = CronStore(temp)
            with migrated._read() as db:
                self.assertEqual([row[0] for row in db.execute("select sequence from cron_deliveries order by delivery_id")], [0, 1])
                self.assertTrue(any(row[1] == "cron_deliveries_run_sequence" for row in db.execute("pragma index_list(cron_deliveries)")))

    def test_cron_schema_uses_versioned_atomic_migrations(self):
        with tempfile.TemporaryDirectory() as temp:
            fresh = CronStore(temp)
            with fresh._read() as db:
                self.assertEqual(db.execute("select max(version) from schema_migrations").fetchone()[0], 4)
                self.assertIn("lease_token", {row["name"] for row in db.execute("pragma table_info(cron_runs)")})
                self.assertIn("lease_token", {row["name"] for row in db.execute("pragma table_info(cron_deliveries)")})
                self.assertEqual({row["name"] for row in db.execute("pragma table_info(cron_mutation_receipts)")}, {"owner_id", "operation", "job_id", "idempotency_key_hash", "request_hash", "response", "created_at"})

    def test_exact_legacy_cron_schema_migrates_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = f"{temp}/cron.sqlite3"
            legacy = sqlite3.connect(path)
            try:
                with legacy as db:
                    db.execute("create table cron_jobs(job_id text primary key,owner_id text not null,destination text not null,intent_text text not null,constraints text not null,schedule text not null,ends_at text,max_successes integer,status text not null,revision integer not null,successful_runs integer not null,last_due text,paused_at text,created_at text not null,updated_at text not null,schedule_anchor text not null)")
                    db.execute("create table cron_runs(run_id text primary key,job_id text not null references cron_jobs(job_id),scheduled_for text not null,trigger text not null,revision integer not null,status text not null,attempts integer not null,available_at text,lease_until text,outcome text,output text,error text,created_at text not null,updated_at text not null,unique(job_id,scheduled_for,trigger))")
                    db.execute("create table cron_deliveries(delivery_id text primary key,run_id text not null references cron_runs(run_id),sequence integer not null default 0,attempts integer not null,status text not null,available_at text,lease_until text,created_at text not null,updated_at text not null,output text,error text,destination text,job_revision integer,telegram_receipt_id text)")
                    db.execute("create table cron_events(id integer primary key,job_id text not null references cron_jobs(job_id),action text not null,created_at text not null)")
                    db.execute("insert into cron_jobs values('job','owner','telegram:chat','intent','[]','{}',null,null,'active',1,0,null,null,?,?,?)", (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
                    db.execute("insert into cron_runs values('run','job',?,'manual',1,'pending',0,null,null,null,null,null,?,?)", (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()))
            finally:
                legacy.close()

            migrated = CronStore(temp)
            with migrated._read() as db:
                self.assertEqual(db.execute("select job_id from cron_runs where run_id='run'").fetchone()[0], "job")
                self.assertEqual(db.execute("select max(version) from schema_migrations").fetchone()[0], 4)

    def test_cron_schema_rejects_future_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = f"{temp}/cron.sqlite3"
            legacy = sqlite3.connect(path)
            try:
                with legacy as db:
                    db.execute("create table schema_migrations(version integer primary key,applied_at text not null)")
                    db.execute("insert into schema_migrations values(5,?)", (NOW.isoformat(),))
            finally:
                legacy.close()
            with self.assertRaisesRegex(RuntimeError, "unsupported_schema_version"):
                CronStore(temp)

    def test_cron_migration_rolls_back_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = f"{temp}/cron.sqlite3"
            legacy = sqlite3.connect(path)
            try:
                with legacy as db:
                    db.execute("create table cron_jobs(job_id text primary key,owner_id text not null,destination text not null,intent_text text not null,constraints text not null,schedule text not null,ends_at text,max_successes integer,status text not null,revision integer not null,successful_runs integer not null,last_due text,paused_at text,created_at text not null,updated_at text not null,schedule_anchor text not null)")
                    db.execute("create table cron_runs(run_id text primary key,job_id text not null,scheduled_for text not null,trigger text not null,revision integer not null,status text not null,attempts integer not null,available_at text,lease_until text,outcome text,output text,error text,created_at text not null,updated_at text not null)")
            finally:
                legacy.close()
            with patch.object(CronStore, "_migrate_v1_to_v2", side_effect=sqlite3.OperationalError("injected")):
                with self.assertRaises(sqlite3.OperationalError):
                    CronStore(temp)
            check = sqlite3.connect(path)
            try:
                self.assertNotIn("schema_migrations", {row[0] for row in check.execute("select name from sqlite_master where type='table'")})
                self.assertNotIn("lease_token", {row[1] for row in check.execute("pragma table_info(cron_runs)")})
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main()
