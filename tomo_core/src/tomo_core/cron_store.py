from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cron_models import CronExecutionClaim, CronJob, DeliveryAttempt, JobIntent, JobProgress, JobRun, JobStatus, LifecyclePolicy, RunOutcome, RunStatus, ScheduleSpec
from .cron_schedule import catch_up, next_occurrence


def _time(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _dump_schedule(value: ScheduleSpec) -> str:
    return json.dumps({"kind": value.kind, "at": _iso(value.at), "every": value.every.total_seconds() if value.every else None,
                       "expression": value.expression, "timezone_name": value.timezone_name, "starts_at": _iso(value.starts_at)})


def _load_schedule(value: str) -> ScheduleSpec:
    data = json.loads(value)
    return ScheduleSpec(data["kind"], at=_dt(data["at"]), every=timedelta(seconds=data["every"]) if data["every"] else None,
                        expression=data["expression"], timezone_name=data["timezone_name"], starts_at=_dt(data["starts_at"]))


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(timezone.utc)


class CronStore:
    """Owner-scoped durable cron ledger. All mutations take a SQLite write lease."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "cron.sqlite3"
        self._init_db()

    def create(self, job: CronJob, *, now: datetime | None = None) -> CronJob:
        now = _time(now)
        with self._write() as db:
            row = db.execute("select * from cron_jobs where job_id=?", (job.job_id,)).fetchone()
            if row:
                if row["owner_id"] != job.owner_id:
                    raise ValueError("job_id belongs to another owner")
                if self._job(row) != job:
                    raise ValueError("idempotency conflict")
                return self._job(row)
            db.execute("insert into cron_jobs values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job.job_id, job.owner_id, job.destination, job.intent.text,
                json.dumps(job.intent.constraints), _dump_schedule(job.schedule), _iso(job.lifecycle.ends_at), job.lifecycle.max_successful_runs,
                job.status.value, job.revision, 0, None, None, _iso(now), _iso(now), _iso(job.schedule.starts_at or now)))
            self._event(db, job.job_id, "create", now)
            return job

    def list(self, owner_id: str) -> tuple[CronJob, ...]:
        with self._read() as db:
            return tuple(self._job(row) for row in db.execute("select * from cron_jobs where owner_id=? order by created_at, job_id", (owner_id,)))

    def get(self, owner_id: str, job_id: str) -> CronJob | None:
        with self._read() as db:
            row = db.execute("select * from cron_jobs where owner_id=? and job_id=?", (owner_id, job_id)).fetchone()
            return None if row is None else self._job(row)

    def update(self, owner_id: str, job: CronJob, expected_revision: int, *, now: datetime | None = None) -> CronJob | None:
        now = _time(now)
        with self._write() as db:
            result = db.execute("""update cron_jobs set destination=?,intent_text=?,constraints=?,schedule=?,ends_at=?,max_successes=?,revision=revision+1,last_due=null,paused_at=null,schedule_anchor=?,updated_at=?
                where owner_id=? and job_id=? and revision=? and status in ('active','paused') and not exists (select 1 from cron_deliveries d join cron_runs r on r.run_id=d.run_id where r.job_id=cron_jobs.job_id and d.status='sending')""", (job.destination, job.intent.text, json.dumps(job.intent.constraints),
                _dump_schedule(job.schedule), _iso(job.lifecycle.ends_at), job.lifecycle.max_successful_runs, _iso(now), _iso(now), owner_id, job.job_id, expected_revision))
            if not result.rowcount: return None
            current_revision = int(db.execute("select revision from cron_jobs where job_id=?", (job.job_id,)).fetchone()[0])
            self._invalidate_stale_work(db, job.job_id, current_revision, now)
            self._event(db, job.job_id, "update", now)
            return self._job(db.execute("select * from cron_jobs where job_id=?", (job.job_id,)).fetchone())

    def pause(self, owner_id: str, job_id: str, expected_revision: int, *, now: datetime | None = None) -> bool:
        return self._change(owner_id, job_id, expected_revision, "paused", "pause", now)

    def resume(self, owner_id: str, job_id: str, expected_revision: int, *, now: datetime | None = None) -> bool:
        return self._change(owner_id, job_id, expected_revision, "active", "resume", now)

    def cancel(self, owner_id: str, job_id: str, expected_revision: int, *, now: datetime | None = None) -> bool:
        return self._change(owner_id, job_id, expected_revision, "cancelled", "cancel", now)

    def delete(self, owner_id: str, job_id: str, expected_revision: int, *, now: datetime | None = None) -> bool:
        now = _time(now)
        with self._write() as db:
            result = db.execute("update cron_jobs set status='cancelled',revision=revision+1,updated_at=? where owner_id=? and job_id=? and revision=? and status in ('active','paused','ended','cancelled') and not exists (select 1 from cron_deliveries d join cron_runs r on r.run_id=d.run_id where r.job_id=cron_jobs.job_id and d.status='sending')", (_iso(now), owner_id, job_id, expected_revision))
            if not result.rowcount:
                return False
            current_revision = int(db.execute("select revision from cron_jobs where job_id=?", (job_id,)).fetchone()[0])
            self._invalidate_stale_work(db, job_id, current_revision, now)
            self._event(db, job_id, "delete", now)
            self._redact_terminal_if_ready(db, job_id)
            return True

    def request_manual_run(self, owner_id: str, job_id: str, expected_revision: int | None = None, *, now: datetime | None = None) -> JobRun | None:
        now = _time(now)
        with self._write() as db:
            row = db.execute("select * from cron_jobs where owner_id=? and job_id=? and status='active'", (owner_id, job_id)).fetchone()
            if row is None or (expected_revision is not None and row["revision"] != expected_revision) or self._active_run(db, job_id): return None
            return self._run(db, row, now, "manual", now)

    def mutate_idempotently(self, owner_id: str, operation: str, key: str, request: str, *, job: CronJob | None = None, job_id: str | None = None, revision: int | None = None, now: datetime | None = None) -> tuple[CronJob | None, bool]:
        """Apply one API mutation and persist its response snapshot in one lease."""
        now = _time(now)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        request_hash = hashlib.sha256(request.encode()).hexdigest()
        with self._write() as db:
            receipt = db.execute("select request_hash,response from cron_mutation_receipts where owner_id=? and operation=? and idempotency_key_hash=?", (owner_id, operation, key_hash)).fetchone()
            if receipt is not None:
                if receipt["request_hash"] != request_hash:
                    raise ValueError("idempotency conflict")
                return self._job_snapshot(json.loads(receipt["response"])), True
            if operation == "create":
                if job is None:
                    raise ValueError("invalid mutation")
                row = db.execute("select * from cron_jobs where job_id=?", (job.job_id,)).fetchone()
                if row is not None:
                    raise ValueError("idempotency conflict")
                db.execute("insert into cron_jobs values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job.job_id, job.owner_id, job.destination, job.intent.text, json.dumps(job.intent.constraints), _dump_schedule(job.schedule), _iso(job.lifecycle.ends_at), job.lifecycle.max_successful_runs, job.status.value, job.revision, 0, None, None, _iso(now), _iso(now), _iso(job.schedule.starts_at or now)))
                self._event(db, job.job_id, "create", now)
            elif operation == "update":
                if job is None or revision is None:
                    raise ValueError("invalid mutation")
                changed = db.execute("update cron_jobs set destination=?,intent_text=?,constraints=?,schedule=?,ends_at=?,max_successes=?,revision=revision+1,last_due=null,paused_at=null,schedule_anchor=?,updated_at=? where owner_id=? and job_id=? and revision=? and status in ('active','paused') and not exists (select 1 from cron_deliveries d join cron_runs r on r.run_id=d.run_id where r.job_id=cron_jobs.job_id and d.status='sending')", (job.destination, job.intent.text, json.dumps(job.intent.constraints), _dump_schedule(job.schedule), _iso(job.lifecycle.ends_at), job.lifecycle.max_successful_runs, _iso(now), _iso(now), owner_id, job.job_id, revision))
                if not changed.rowcount: return None, False
                current = int(db.execute("select revision from cron_jobs where job_id=?", (job.job_id,)).fetchone()[0])
                self._invalidate_stale_work(db, job.job_id, current, now); self._event(db, job.job_id, "update", now)
            else:
                if job_id is None or revision is None:
                    raise ValueError("invalid mutation")
                status = {"pause": "paused", "resume": "active", "delete": "cancelled"}.get(operation)
                if operation == "run_now":
                    row = db.execute("select * from cron_jobs where owner_id=? and job_id=? and status='active' and revision=?", (owner_id, job_id, revision)).fetchone()
                    if row is None or self._active_run(db, job_id): return None, False
                    self._run(db, row, now, "manual", now)
                elif status is not None:
                    allowed_statuses = "('active','paused')" if operation in ("pause", "resume") else "('active','paused','ended','cancelled')"
                    changed = db.execute(f"update cron_jobs set status=?,revision=revision+1,paused_at=case when ?='paused' then ? else paused_at end,updated_at=? where owner_id=? and job_id=? and revision=? and status in {allowed_statuses} and not exists (select 1 from cron_deliveries d join cron_runs r on r.run_id=d.run_id where r.job_id=cron_jobs.job_id and d.status='sending')", (status, status, _iso(now), _iso(now), owner_id, job_id, revision))
                    if not changed.rowcount: return None, False
                    current = int(db.execute("select revision from cron_jobs where job_id=?", (job_id,)).fetchone()[0])
                    self._invalidate_stale_work(db, job_id, current, now); self._event(db, job_id, operation, now)
                    if status == "cancelled": self._redact_terminal_if_ready(db, job_id)
                else:
                    raise ValueError("invalid mutation")
            current = self._job(db.execute("select * from cron_jobs where job_id=?", (job.job_id if job is not None else job_id,)).fetchone())
            snapshot = self._snapshot(current)
            db.execute("insert into cron_mutation_receipts(owner_id,operation,job_id,idempotency_key_hash,request_hash,response,created_at) values(?,?,?,?,?,?,?)", (owner_id, operation, current.job_id, key_hash, request_hash, json.dumps(snapshot, separators=(",", ":")), _iso(now)))
            return current, False

    def claim_due_run(self, *, now: datetime | None = None, lease_seconds: int = 60) -> CronExecutionClaim | None:
        now = _time(now)
        with self._write() as db:
            pending_rows = db.execute("select r.*,j.owner_id,j.destination,j.intent_text,j.constraints,j.schedule,j.ends_at,j.max_successes,j.successful_runs,j.status job_status,j.revision current_revision from cron_runs r join cron_jobs j on j.job_id=r.job_id where r.status in ('pending','retry_wait') and r.available_at<=? order by r.created_at", (_iso(now),))
            for pending in pending_rows:
                lifecycle_retry = pending["trigger"] == "lifecycle" and pending["revision"] == pending["current_revision"] and pending["job_status"] == "ended"
                if (pending["job_status"] != "active" and not lifecycle_retry) or pending["revision"] != pending["current_revision"]:
                    db.execute("update cron_runs set status='invalidated',lease_until=null,updated_at=? where run_id=? and status in ('pending','retry_wait')", (_iso(now), pending["run_id"]))
                    continue
                token = str(uuid.uuid4())
                claimed = db.execute("""update cron_runs set lease_until=?,lease_token=?,status='leased',updated_at=? where run_id=?
                    and status in ('pending','retry_wait') and revision=? and exists (
                        select 1 from cron_jobs j where j.job_id=cron_runs.job_id and j.revision=cron_runs.revision
                        and (j.status='active' or (cron_runs.trigger='lifecycle' and j.status='ended'))
                    )""", (_iso(now + timedelta(seconds=lease_seconds)), token, _iso(now), pending["run_id"], pending["revision"]))
                if not claimed.rowcount:
                    continue
                run = JobRun(pending["run_id"], pending["job_id"], _dt(pending["scheduled_for"]), pending["trigger"], pending["revision"], "leased")
                return CronExecutionClaim(run, token, pending["owner_id"], pending["destination"], JobIntent(pending["intent_text"], tuple(json.loads(pending["constraints"]))), self._progress(db, pending["job_id"], run.run_id, pending["successful_runs"]), self._will_end_after_run(pending, run.scheduled_for))
            for row in db.execute("select * from cron_jobs where status='active' order by created_at,job_id"):
                if self._active_run(db, row["job_id"]): continue
                schedule, ends = _load_schedule(row["schedule"]), _dt(row["ends_at"])
                trigger = due = None
                if ends and now >= ends:
                    # A normal occurrence before the boundary remains owed even
                    # when the scheduler was down at the boundary.
                    due = self._due(schedule, _dt(row["last_due"]), ends - timedelta(microseconds=1), _dt(row["schedule_anchor"]))
                    if due is not None:
                        trigger = "schedule"
                    else:
                        trigger, due = "lifecycle", now
                        db.execute("update cron_jobs set status='ended',revision=revision+1,updated_at=? where job_id=?", (_iso(now), row["job_id"]))
                        row = db.execute("select * from cron_jobs where job_id=?", (row["job_id"],)).fetchone()
                elif row["paused_at"]:
                    due = catch_up(schedule, _dt(row["paused_at"]), now)
                    trigger = "catchup" if due else None
                    db.execute("update cron_jobs set paused_at=null where job_id=?", (row["job_id"],))
                else:
                    due = self._due(schedule, _dt(row["last_due"]), now, _dt(row["schedule_anchor"]))
                    trigger = "schedule" if due else None
                if trigger:
                    run = self._run(db, row, due, trigger, now)
                    token = str(uuid.uuid4())
                    db.execute("update cron_runs set lease_until=?,lease_token=?,status='leased' where run_id=?", (_iso(now + timedelta(seconds=lease_seconds)), token, run.run_id))
                    fresh = self._job(db.execute("select * from cron_jobs where job_id=?", (run.job_id,)).fetchone())
                    final = self._will_end_after_run(row, due, trigger)
                    leased = JobRun(run.run_id, run.job_id, run.scheduled_for, run.trigger, run.revision, "leased")
                    return CronExecutionClaim(leased, token, fresh.owner_id, fresh.destination, fresh.intent, self._progress(db, run.job_id, run.run_id, row["successful_runs"]), final)
        return None

    def renew_run_lease(self, run_id: str, lease_token: str, *, now: datetime | None = None, lease_seconds: int = 60) -> bool:
        now = _time(now)
        with self._write() as db:
            return bool(db.execute("""update cron_runs set lease_until=?,updated_at=? where run_id=? and lease_token=? and status in ('leased','executing')
                and exists (select 1 from cron_jobs j where j.job_id=cron_runs.job_id and j.revision=cron_runs.revision
                    and (j.status='active' or (cron_runs.trigger='lifecycle' and j.status='ended')))""", (_iso(now + timedelta(seconds=lease_seconds)), _iso(now), run_id, lease_token)).rowcount)

    def begin_run(self, run_id: str, lease_token: str, *, now: datetime | None = None, lease_seconds: float = 180) -> bool:
        """Fence execution before invoking an external sandbox."""
        now = _time(now)
        with self._write() as db:
            return bool(db.execute("""update cron_runs set status='executing',lease_until=?,updated_at=? where run_id=? and lease_token=? and status='leased'
                and exists (select 1 from cron_jobs j where j.job_id=cron_runs.job_id and j.revision=cron_runs.revision
                    and (j.status='active' or (cron_runs.trigger='lifecycle' and j.status='ended')))""", (_iso(now + timedelta(seconds=lease_seconds)), _iso(now), run_id, lease_token)).rowcount)

    def complete_run(self, run_id: str, lease_token: str, outcome: RunOutcome, output: str | tuple[str, ...] | list[str] | None, *, now: datetime | None = None) -> bool:
        now = _time(now)
        with self._write() as db:
            row = db.execute("""select r.*,j.revision current_revision,j.status job_status,j.max_successes,j.successful_runs,j.destination,j.schedule,j.ends_at
                from cron_runs r join cron_jobs j on j.job_id=r.job_id where r.run_id=? and r.lease_token=? and r.status in ('leased','executing')
                and r.revision=j.revision and (j.status='active' or (r.trigger='lifecycle' and j.status='ended'))""", (run_id, lease_token)).fetchone()
            if row is None: return False
            stale = False
            status = "succeeded"
            frames = () if output is None else (output,) if isinstance(output, str) else tuple(output)
            if not 1 <= len(frames) <= 3 or any(not isinstance(frame, str) or not frame.strip() or len(frame) > 4096 for frame in frames):
                raise ValueError("run output must contain one to three non-blank frames of at most 4096 characters")
            db.execute("update cron_runs set status=?,output=?,outcome=?,lease_until=null,updated_at=? where run_id=?", (status, None if stale else "\n".join(frames), outcome.value, _iso(now), run_id))
            delivery_status = "suppressed" if stale else "pending"
            for sequence, frame in enumerate(frames):
                db.execute("insert into cron_deliveries(delivery_id,run_id,sequence,attempts,status,available_at,lease_until,lease_token,created_at,updated_at,output,error,destination,job_revision,telegram_receipt_id) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), run_id, sequence, 0, delivery_status, _iso(now), None, None, _iso(now), _iso(now), None if stale else frame, None, row["destination"], row["revision"], None))
            if not stale:
                successes = row["successful_runs"] + (outcome == RunOutcome.SUCCEEDED)
                ends_at = _dt(row["ends_at"])
                next_due = next_occurrence(_load_schedule(row["schedule"]), _dt(row["scheduled_for"]))
                terminal = self._job_once(db, row["job_id"]) or (ends_at is not None and (next_due is None or next_due >= ends_at))
                terminal = terminal or (row["max_successes"] is not None and successes >= row["max_successes"])
                db.execute("update cron_jobs set successful_runs=?,status=?,updated_at=? where job_id=?", (successes, "ended" if terminal else row["job_status"], _iso(now), row["job_id"]))
            return not stale

    def fail_run(self, run_id: str, lease_token: str, error: str, *, now: datetime | None = None, max_attempts: int = 3) -> bool:
        now = _time(now)
        with self._write() as db:
            row = db.execute("""select r.attempts,r.status,r.job_id,r.revision,r.scheduled_for,r.trigger,j.revision current_revision,j.status job_status,j.schedule,j.ends_at from cron_runs r join cron_jobs j on j.job_id=r.job_id
                where r.run_id=? and r.lease_token=? and r.status in ('leased','executing') and r.revision=j.revision
                and (j.status='active' or (r.trigger='lifecycle' and j.status='ended'))""", (run_id, lease_token)).fetchone()
            if row is None: return False
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= max_attempts else "retry_wait"
            db.execute("update cron_runs set attempts=?,status=?,available_at=?,error=?,lease_until=null,updated_at=? where run_id=?", (attempts,status,_iso(now + timedelta(seconds=2 ** (attempts-1))),error,_iso(now),run_id))
            if status == "failed":
                job = db.execute("select destination,revision from cron_jobs where job_id=?", (row["job_id"],)).fetchone()
                if job is not None and int(job["revision"]) == int(row["revision"]):
                    schedule = _load_schedule(row["schedule"])
                    next_due = next_occurrence(schedule, _dt(row["scheduled_for"]))
                    final_occurrence = row["trigger"] in ("schedule", "catchup") and _dt(row["ends_at"]) is not None and (next_due is None or next_due >= _dt(row["ends_at"]))
                    if self._job_once(db, row["job_id"]) or final_occurrence:
                        db.execute("update cron_jobs set status='ended',updated_at=? where job_id=?", (_iso(now), row["job_id"]))
                    db.execute("update cron_runs set outcome='failed' where run_id=?", (run_id,))
                    db.execute("insert into cron_deliveries(delivery_id,run_id,sequence,attempts,status,available_at,lease_until,lease_token,created_at,updated_at,output,error,destination,job_revision,telegram_receipt_id) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), run_id, 0, 0, "pending", _iso(now), None, None, _iso(now), _iso(now), "Scheduled task could not be completed.", None, job["destination"], job["revision"], None))
            return True

    def defer_run(self, run_id: str, lease_token: str, *, now: datetime | None = None, delay_seconds: int = 2) -> bool:
        """Release a coordination-blocked run without consuming an execution attempt."""
        now = _time(now)
        if not 1 <= delay_seconds <= 60:
            raise ValueError("defer delay must be between one and sixty seconds")
        with self._write() as db:
            return bool(db.execute(
                """update cron_runs set status='retry_wait',available_at=?,lease_until=null,updated_at=? where run_id=? and lease_token=? and status in ('leased','executing')
                and exists (select 1 from cron_jobs j where j.job_id=cron_runs.job_id and j.revision=cron_runs.revision
                    and (j.status='active' or (cron_runs.trigger='lifecycle' and j.status='ended')))""",
                (_iso(now + timedelta(seconds=delay_seconds)), _iso(now), run_id, lease_token),
            ).rowcount)

    def claim_delivery(self, *, now: datetime | None = None, lease_seconds: int = 60) -> DeliveryAttempt | None:
        now = _time(now)
        with self._write() as db:
            while True:
                row = db.execute("select d.*,r.revision run_revision,j.owner_id,j.job_id,j.revision current_revision,j.status job_status from cron_deliveries d join cron_runs r on r.run_id=d.run_id join cron_jobs j on j.job_id=r.job_id where d.status in ('pending','retry_wait') and (d.available_at is null or d.available_at<=?) and not exists (select 1 from cron_deliveries earlier where earlier.run_id=d.run_id and earlier.sequence<d.sequence and earlier.status in ('pending','leased','retry_wait','sending')) order by d.created_at,d.sequence limit 1", (_iso(now),)).fetchone()
                if row is None: return None
                if row["run_revision"] != row["current_revision"] or row["job_status"] == "cancelled":
                    db.execute("update cron_deliveries set status='suppressed',updated_at=? where delivery_id=?", (_iso(now), row["delivery_id"]))
                    self._redact_terminal_if_ready(db, row["job_id"])
                    continue
                break
            token = str(uuid.uuid4())
            db.execute("update cron_deliveries set status='leased',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? where delivery_id=? and status in ('pending','retry_wait')", (token,_iso(now+timedelta(seconds=lease_seconds)),_iso(now),row["delivery_id"]))
            return DeliveryAttempt(row["delivery_id"], row["run_id"], row["attempts"]+1, token, "leased", row["sequence"], row["destination"], row["output"], row["job_revision"], row["owner_id"], row["job_id"])

    def complete_delivery(self, delivery_id: str, lease_token: str, receipt_id: str | None = None, *, now: datetime | None = None, pace_seconds: float = 0) -> bool:
        if pace_seconds < 0:
            raise ValueError("pace_seconds must be non-negative")
        return self._finish_delivery(delivery_id, lease_token, "delivered", now, receipt_id, pace_seconds)

    def run_is_current(self, run_id: str, revision: int, lease_token: str | None = None) -> bool:
        """Whether a run may still contribute visible automation output."""
        with self._read() as db:
            row = db.execute(
                "select r.revision,r.lease_token,r.status run_status,j.revision current_revision,j.status from cron_runs r join cron_jobs j on j.job_id=r.job_id where r.run_id=?",
                (run_id,),
            ).fetchone()
            return (row is not None and row["revision"] == revision and row["current_revision"] == revision
                    and row["status"] in ("active", "ended") and (lease_token is None or (row["lease_token"] == lease_token and row["run_status"] in ("leased", "executing"))))

    def delivery_is_current(self, delivery_id: str) -> bool:
        with self._read() as db:
            row = db.execute("select d.job_revision,j.revision,j.status from cron_deliveries d join cron_runs r on r.run_id=d.run_id join cron_jobs j on j.job_id=r.job_id where d.delivery_id=?", (delivery_id,)).fetchone()
            return row is not None and row["job_revision"] == row["revision"] and row["status"] in ("active", "ended")

    def admit_delivery(self, delivery_id: str, lease_token: str, *, now: datetime | None = None) -> bool:
        """Atomically fence a leased delivery immediately before its connector send."""
        now = _time(now)
        with self._write() as db:
            return bool(db.execute(
                """update cron_deliveries set status='sending',updated_at=?
                where delivery_id=? and lease_token=? and status='leased' and exists (
                    select 1 from cron_runs r join cron_jobs j on j.job_id=r.job_id
                    where r.run_id=cron_deliveries.run_id
                    and cron_deliveries.job_revision=j.revision
                    and j.status in ('active','ended')
                )""",
                (_iso(now), delivery_id, lease_token),
            ).rowcount)

    def suppress_delivery(self, delivery_id: str, lease_token: str, *, now: datetime | None = None) -> bool:
        return self._finish_delivery(delivery_id, lease_token, "suppressed", now)

    def fail_delivery(self, delivery_id: str, lease_token: str, error: str, *, now: datetime | None = None, max_attempts: int = 5, pace_seconds: float = 0) -> bool:
        now = _time(now)
        with self._write() as db:
            row = db.execute("select d.attempts,r.job_id,r.run_id from cron_deliveries d join cron_runs r on r.run_id=d.run_id where d.delivery_id=? and d.lease_token=? and d.status='sending'", (delivery_id, lease_token)).fetchone()
            if row is None: return False
            status = "exhausted" if row["attempts"] >= max_attempts else "retry_wait"
            db.execute("update cron_deliveries set status=?,available_at=?,lease_until=null,error=?,updated_at=? where delivery_id=? and lease_token=? and status='sending'", (status,_iso(now+timedelta(seconds=2 ** max(0,row['attempts']-1))),error,_iso(now),delivery_id,lease_token))
            if status == "exhausted":
                self._pace_next_delivery(db, row["run_id"], now, pace_seconds)
                self._redact_terminal_if_ready(db, row["job_id"])
            return True

    def mark_delivery_unknown(self, delivery_id: str, lease_token: str, *, now: datetime | None = None, pace_seconds: float = 0) -> bool:
        return self._finish_delivery(delivery_id, lease_token, "unknown", now, pace_seconds=pace_seconds)

    def history(self, owner_id: str, job_id: str) -> tuple[dict, ...]:
        with self._read() as db:
            if not db.execute("select 1 from cron_jobs where owner_id=? and job_id=?", (owner_id,job_id)).fetchone(): return ()
            rows = tuple(db.execute("""select action,created_at,output from (
                select action,created_at,null output,0 source_rank,printf('%020d',id) ordinal from cron_events where job_id=?
                union all
                select status action,updated_at created_at,output,1 source_rank,run_id ordinal from cron_runs where job_id=?
            ) order by created_at desc,source_rank desc,ordinal desc limit 64""", (job_id, job_id)))
            return tuple(dict(row) for row in reversed(rows))

    def recover_expired_leases(self, *, now: datetime | None = None, pace_seconds: float = 0) -> int:
        now = _time(now)
        with self._write() as db:
            recovered_runs=db.execute("update cron_runs set status='pending',lease_until=null,updated_at=? where status in ('leased','executing') and lease_until<?", (_iso(now),_iso(now))).rowcount
            expired_sends = tuple(db.execute("select run_id from cron_deliveries where status='sending' and lease_until<?", (_iso(now),)))
            recovered_deliveries=db.execute("update cron_deliveries set status=case when status='sending' then 'unknown' else 'pending' end,lease_until=null,updated_at=? where status in ('leased','sending') and lease_until<?", (_iso(now),_iso(now))).rowcount
            for expired in expired_sends:
                self._pace_next_delivery(db, expired["run_id"], now, pace_seconds)
            for row in db.execute("select distinct r.job_id from cron_deliveries d join cron_runs r on r.run_id=d.run_id where d.status='unknown' and d.updated_at=?", (_iso(now),)):
                self._redact_terminal_if_ready(db, row["job_id"])
            return recovered_runs+recovered_deliveries

    def redact_terminal_job(self, owner_id: str, job_id: str) -> bool:
        with self._write() as db:
            owned = db.execute("select 1 from cron_jobs where owner_id=? and job_id=?", (owner_id, job_id)).fetchone()
            return bool(owned) and self._redact_terminal_if_ready(db, job_id)

    def _change(self, owner, job_id, revision, status, action, now):
        now=_time(now)
        with self._write() as db:
            paused=_iso(now) if status == "paused" else None
            result=db.execute("update cron_jobs set status=?,revision=revision+1,paused_at=case when ?='paused' then ? else paused_at end,updated_at=? where owner_id=? and job_id=? and revision=? and status in ('active','paused') and not exists (select 1 from cron_deliveries d join cron_runs r on r.run_id=d.run_id where r.job_id=cron_jobs.job_id and d.status='sending')",(status,status,paused,_iso(now),owner,job_id,revision))
            if not result.rowcount:
                return False
            current_revision = int(db.execute("select revision from cron_jobs where job_id=?", (job_id,)).fetchone()[0])
            self._invalidate_stale_work(db, job_id, current_revision, now)
            self._event(db,job_id,action,now)
            if status == "cancelled":
                self._redact_terminal_if_ready(db, job_id)
            return True

    def _invalidate_stale_work(self, db, job_id: str, current_revision: int, now: datetime) -> None:
        db.execute(
            "update cron_runs set status='invalidated',lease_until=null,updated_at=? where job_id=? and revision<>? and status in ('pending','leased','executing','retry_wait')",
            (_iso(now), job_id, current_revision),
        )
        db.execute(
            "update cron_deliveries set status='suppressed',lease_until=null,updated_at=? where run_id in (select run_id from cron_runs where job_id=? and revision<>?) and status in ('pending','leased','retry_wait')",
            (_iso(now), job_id, current_revision),
        )

    def _redact_terminal_if_ready(self, db, job_id: str) -> bool:
        job = db.execute("select status from cron_jobs where job_id=?", (job_id,)).fetchone()
        pending = db.execute("select 1 from cron_deliveries d join cron_runs r on r.run_id=d.run_id where r.job_id=? and d.status not in ('delivered','exhausted','suppressed','unknown')", (job_id,)).fetchone()
        if job is None or job["status"] not in ("ended", "cancelled") or pending:
            return False
        db.execute("update cron_runs set output=null,error=null where job_id=?", (job_id,))
        db.execute("update cron_deliveries set output=null,error=null where run_id in (select run_id from cron_runs where job_id=?)", (job_id,))
        db.execute("update cron_jobs set intent_text='',constraints='[]',destination='' where job_id=?", (job_id,))
        tombstone = self._snapshot(self._job(db.execute("select * from cron_jobs where job_id=?", (job_id,)).fetchone()))
        db.execute("update cron_mutation_receipts set response=? where owner_id=? and job_id=?", (json.dumps(tombstone, separators=(",", ":")), self._job_owner(db, job_id), job_id))
        return True

    @staticmethod
    def _job_owner(db, job_id):
        return db.execute("select owner_id from cron_jobs where job_id=?", (job_id,)).fetchone()[0]

    def _run(self, db, job, due, trigger, now):
        run=JobRun(str(uuid.uuid4()),job["job_id"],due,trigger,int(job["revision"]))
        db.execute("""insert into cron_runs(run_id,job_id,scheduled_for,trigger,revision,status,attempts,available_at,lease_until,lease_token,outcome,output,error,created_at,updated_at)
            values(?,?,?,?,?,'pending',0,?,null,null,null,null,null,?,?)""", (run.run_id,run.job_id,_iso(due),trigger,run.revision,_iso(now),_iso(now),_iso(now)))
        if trigger in ('schedule','catchup'): db.execute("update cron_jobs set last_due=?,updated_at=? where job_id=?",(_iso(due),_iso(now),run.job_id))
        return run

    def _due(self,schedule,last,now,created):
        if last: return catch_up(schedule,last,now)
        if schedule.kind == 'once': return schedule.at if schedule.at and schedule.at <= now else None
        if schedule.kind == 'interval':
            origin = schedule.starts_at or created
            if now < origin: return None
            anchored = ScheduleSpec.interval(schedule.every.total_seconds(), starts_at=origin)
            since = origin - timedelta(microseconds=1) if schedule.starts_at is not None else origin
            return catch_up(anchored, since, now)
        return catch_up(schedule, created, now)
    def _will_end_after_run(self,row,due,trigger=None):
        if trigger == "lifecycle": return True
        if "trigger" in row.keys() and row["trigger"] == "lifecycle": return True
        schedule = _load_schedule(row["schedule"])
        if schedule.kind == "once": return True
        ends = _dt(row["ends_at"])
        return ends is not None and ((next_due := next_occurrence(schedule, due)) is None or next_due >= ends)

    def _active_run(self,db,job_id):
        return db.execute("""select 1 from cron_runs r where r.job_id=? and (
            r.status in ('pending','leased','executing','retry_wait') or exists (
                select 1 from cron_deliveries d where d.run_id=r.run_id
                and d.status in ('pending','leased','sending','retry_wait')
            )
        )""", (job_id,)).fetchone()
    def _progress(self, db, job_id, current_run_id, successful_runs):
        row = db.execute("select outcome from cron_runs where job_id=? and run_id<>? and status in ('succeeded','failed') and outcome is not null order by updated_at desc,created_at desc limit 1", (job_id, current_run_id)).fetchone()
        return JobProgress(int(successful_runs), None if row is None else RunOutcome(row["outcome"]))
    def _job_once(self,db,job_id): return _load_schedule(db.execute("select schedule from cron_jobs where job_id=?",(job_id,)).fetchone()[0]).kind == 'once'
    def _lease(self,table,key,value,now,seconds):
        now=_time(now)
        with self._write() as db: return bool(db.execute(f"update {table} set lease_until=? where {key}=? and status='leased'",(_iso(now+timedelta(seconds=seconds)),value)).rowcount)
    def _finish_delivery(self,delivery_id,lease_token,status,now,receipt_id=None,pace_seconds=0):
        now=_time(now)
        with self._write() as db:
            changed = bool(db.execute("update cron_deliveries set status=?,lease_until=null,telegram_receipt_id=?,updated_at=? where delivery_id=? and lease_token=? and status='sending'",(status,receipt_id,_iso(now),delivery_id,lease_token)).rowcount)
            if changed:
                row = db.execute("select d.run_id,d.sequence,j.job_id,j.status from cron_deliveries d join cron_runs r on r.run_id=d.run_id join cron_jobs j on j.job_id=r.job_id where d.delivery_id=?", (delivery_id,)).fetchone()
                if status != "suppressed":
                    self._pace_next_delivery(db, row['run_id'], now, pace_seconds, row['sequence'] + 1)
                self._redact_terminal_if_ready(db, row['job_id'])
            return changed
    @staticmethod
    def _pace_next_delivery(db, run_id, now, pace_seconds, sequence=None):
        if not pace_seconds:
            return
        available_at = _iso(now + timedelta(seconds=pace_seconds))
        if sequence is None:
            db.execute("update cron_deliveries set available_at=case when available_at is null or available_at<? then ? else available_at end,updated_at=? where run_id=? and sequence=(select min(sequence) from cron_deliveries where run_id=? and status in ('pending','retry_wait')) and status in ('pending','retry_wait')", (available_at, available_at, _iso(now), run_id, run_id))
        else:
            db.execute("update cron_deliveries set available_at=case when available_at is null or available_at<? then ? else available_at end,updated_at=? where run_id=? and sequence=? and status in ('pending','retry_wait')", (available_at, available_at, _iso(now), run_id, sequence))
    def _event(self,db,job_id,action,now): db.execute("insert into cron_events(job_id,action,created_at) values(?,?,?)",(job_id,action,_iso(now)))
    def _job(self,row):
        # Tombstones retain identity/status while exposing no stored content.
        destination = row['destination'] or 'telegram:redacted'
        intent = row['intent_text'] or '[redacted]'
        return CronJob(row['job_id'],row['owner_id'],destination,JobIntent(intent,tuple(json.loads(row['constraints']))),_load_schedule(row['schedule']),LifecyclePolicy(_dt(row['ends_at']),row['max_successes']),row['status'],row['revision'])
    @staticmethod
    def _snapshot(job):
        return {"job_id": job.job_id, "owner_id": job.owner_id, "destination": job.destination, "intent": job.intent.text, "constraints": list(job.intent.constraints), "schedule": _dump_schedule(job.schedule), "ends_at": _iso(job.lifecycle.ends_at), "max_successes": job.lifecycle.max_successful_runs, "status": job.status.value, "revision": job.revision}
    @staticmethod
    def _job_snapshot(value):
        return CronJob(value["job_id"], value["owner_id"], value["destination"], JobIntent(value["intent"], tuple(value["constraints"])), _load_schedule(value["schedule"]), LifecyclePolicy(_dt(value["ends_at"]), value["max_successes"]), value["status"], value["revision"])
    def _connect(self):
        db=sqlite3.connect(self.db_path); db.row_factory=sqlite3.Row; db.execute('pragma journal_mode=WAL'); db.execute('pragma synchronous=FULL'); db.execute('pragma foreign_keys=on'); db.execute('pragma busy_timeout=5000'); return db
    def _read(self): return _Transaction(self._connect())
    def _write(self):
        db=self._connect(); db.execute('begin immediate'); return _Transaction(db)
    def _init_db(self):
        db = self._connect()
        try:
            db.execute("begin immediate")
            version = db.execute("select max(version) from schema_migrations").fetchone()[0] if self._table_exists(db, "schema_migrations") else None
            if version not in (None, 1, 2, 3, 4):
                raise RuntimeError("unsupported_schema_version")
            if version is None:
                db.execute("create table schema_migrations(version integer primary key,applied_at text not null)")
                self._migrate_to_v1(db)
                db.execute("insert into schema_migrations values(1,?)", (_iso(_time()),))
                version = 1
            if version == 1:
                self._migrate_v1_to_v2(db)
                db.execute("insert into schema_migrations values(2,?)", (_iso(_time()),))
                version = 2
            if version == 2:
                self._migrate_v2_to_v3(db)
                db.execute("insert into schema_migrations values(3,?)", (_iso(_time()),))
                version = 3
            if version == 3:
                db.execute("create table cron_mutation_receipts(owner_id text not null,operation text not null,job_id text not null,idempotency_key_hash text not null,request_hash text not null,response text not null,created_at text not null,primary key(owner_id,operation,idempotency_key_hash))")
                db.execute("insert into schema_migrations values(4,?)", (_iso(_time()),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _table_exists(db, name: str) -> bool:
        return db.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None

    def _migrate_to_v1(self, db) -> None:
        for statement in (
            "create table if not exists cron_jobs(job_id text primary key,owner_id text not null,destination text not null,intent_text text not null,constraints text not null,schedule text not null,ends_at text,max_successes integer,status text not null,revision integer not null,successful_runs integer not null,last_due text,paused_at text,created_at text not null,updated_at text not null,schedule_anchor text not null)",
            "create table if not exists cron_runs(run_id text primary key,job_id text not null references cron_jobs(job_id),scheduled_for text not null,trigger text not null,revision integer not null,status text not null,attempts integer not null,available_at text,lease_until text,outcome text,output text,error text,created_at text not null,updated_at text not null,unique(job_id,scheduled_for,trigger))",
            "create table if not exists cron_deliveries(delivery_id text primary key,run_id text not null references cron_runs(run_id),sequence integer not null default 0,attempts integer not null,status text not null,available_at text,lease_until text,created_at text not null,updated_at text not null,output text,error text,destination text,job_revision integer,telegram_receipt_id text)",
            "create table if not exists cron_events(id integer primary key,job_id text not null references cron_jobs(job_id),action text not null,created_at text not null)",
        ):
            db.execute(statement)
        columns = {row["name"] for row in db.execute("pragma table_info(cron_deliveries)")}
        for name, definition in {"sequence": "integer not null default 0", "destination": "text", "job_revision": "integer", "telegram_receipt_id": "text"}.items():
            if name not in columns:
                db.execute(f"alter table cron_deliveries add column {name} {definition}")
        next_sequence = db.execute("select coalesce(max(sequence), -1) + 1 from cron_deliveries").fetchone()[0]
        for duplicate in db.execute("select rowid from cron_deliveries where rowid not in (select min(rowid) from cron_deliveries group by run_id,sequence) order by run_id,sequence,rowid"):
            db.execute("update cron_deliveries set sequence=? where rowid=?", (next_sequence, duplicate["rowid"]))
            next_sequence += 1
        db.execute("create unique index if not exists cron_deliveries_run_sequence on cron_deliveries(run_id,sequence)")

    @staticmethod
    def _migrate_v1_to_v2(db) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(cron_runs)")}
        if "lease_token" not in columns:
            db.execute("alter table cron_runs add column lease_token text")

    @staticmethod
    def _migrate_v2_to_v3(db) -> None:
        columns = {row["name"] for row in db.execute("pragma table_info(cron_deliveries)")}
        if "lease_token" not in columns:
            db.execute("alter table cron_deliveries add column lease_token text")


class _Transaction:
    def __init__(self, db): self.db=db
    def __enter__(self): return self.db
    def __exit__(self, typ, value, trace):
        self.db.rollback() if typ else self.db.commit(); self.db.close()
