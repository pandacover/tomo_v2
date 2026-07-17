from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class JobStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SENDING = "sending"
    DELIVERED = "delivered"
    RETRY_WAIT = "retry_wait"
    EXHAUSTED = "exhausted"
    SUPPRESSED = "suppressed"
    UNKNOWN = "unknown"


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    APPROVAL_NEEDED = "approval_needed"
    CONTROL_NOTICE = "control_notice"


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not (clean := value.strip()):
        raise ValueError(f"{name} is required")
    return clean


def _bounded_required(value: str, name: str, limit: int) -> str:
    value = _required(value, name)
    if len(value) > limit:
        raise ValueError(f"{name} cannot exceed {limit} characters")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class JobIntent:
    text: str
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        text = _required(self.text, "intent text")
        if len(text) > 4000:
            raise ValueError("intent text cannot exceed 4000 characters")
        if len(self.constraints) > 32:
            raise ValueError("intent constraints cannot exceed 32 items")
        constraints = tuple(_bounded_required(value, "intent constraint", 1000) for value in self.constraints)
        if sum(len(value) for value in constraints) > 8000:
            raise ValueError("intent constraints cannot exceed 8000 characters")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "constraints", constraints)


@dataclass(frozen=True)
class ScheduleSpec:
    kind: str
    at: datetime | None = None
    every: timedelta | None = None
    expression: str | None = None
    timezone_name: str = "UTC"
    starts_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"once", "interval", "cron"}:
            raise ValueError("schedule kind must be once, interval, or cron")
        timezone_name = _bounded_required(self.timezone_name, "timezone_name", 128)
        object.__setattr__(self, "timezone_name", timezone_name)
        if self.timezone_name != "UTC":
            try:
                ZoneInfo(self.timezone_name)
            except (TypeError, ZoneInfoNotFoundError) as error:
                raise ValueError("timezone_name must be an IANA timezone") from error
        if self.kind == "once":
            if self.at is None or self.every is not None or self.expression is not None:
                raise ValueError("one-shot schedules require only at")
            object.__setattr__(self, "at", _utc(self.at, "schedule at"))
        elif self.kind == "interval":
            if self.every is None or self.at is not None or self.expression is not None:
                raise ValueError("interval schedules require only every")
            if self.every <= timedelta(0):
                raise ValueError("interval must be positive")
            if self.starts_at is not None:
                object.__setattr__(self, "starts_at", _utc(self.starts_at, "schedule starts_at"))
        else:
            if not isinstance(self.expression, str) or len(self.expression) > 128 or len(self.expression.split()) != 5 or self.at is not None or self.every is not None:
                raise ValueError("cron schedules require a five-field expression")
            object.__setattr__(self, "expression", self.expression.strip())
            # Keep schedule definitions invalid-free before persistence.
            from .cron_schedule import _cron_fields

            _cron_fields(self.expression)

    @classmethod
    def once(cls, at: datetime) -> ScheduleSpec:
        return cls("once", at=at)

    @classmethod
    def interval(cls, seconds: int | float, *, starts_at: datetime | None = None) -> ScheduleSpec:
        if isinstance(seconds, bool):
            raise ValueError("interval seconds must be positive")
        return cls("interval", every=timedelta(seconds=seconds), starts_at=starts_at)

    @classmethod
    def cron(cls, expression: str, timezone_name: str = "UTC") -> ScheduleSpec:
        return cls("cron", expression=expression, timezone_name=timezone_name)


@dataclass(frozen=True)
class LifecyclePolicy:
    ends_at: datetime | None = None
    max_successful_runs: int | None = None

    def __post_init__(self) -> None:
        if self.ends_at is not None:
            object.__setattr__(self, "ends_at", _utc(self.ends_at, "lifecycle ends_at"))
        if self.max_successful_runs is not None and (isinstance(self.max_successful_runs, bool) or self.max_successful_runs < 1):
            raise ValueError("max_successful_runs must be at least one")


@dataclass(frozen=True)
class CronJob:
    job_id: str
    owner_id: str
    destination: str
    intent: JobIntent
    schedule: ScheduleSpec
    lifecycle: LifecyclePolicy = field(default_factory=LifecyclePolicy)
    status: JobStatus | str = JobStatus.ACTIVE
    revision: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _required(self.job_id, "job_id"))
        object.__setattr__(self, "owner_id", _required(self.owner_id, "owner_id"))
        object.__setattr__(self, "destination", _required(self.destination, "destination"))
        if not isinstance(self.intent, JobIntent) or not isinstance(self.schedule, ScheduleSpec) or not isinstance(self.lifecycle, LifecyclePolicy):
            raise ValueError("job intent, schedule, and lifecycle are required")
        if not isinstance(self.status, JobStatus):
            try:
                object.__setattr__(self, "status", JobStatus(self.status))
            except ValueError as error:
                raise ValueError("invalid job status") from error
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("job revision must be at least one")


@dataclass(frozen=True)
class JobRun:
    run_id: str
    job_id: str
    scheduled_for: datetime
    trigger: str
    revision: int
    status: RunStatus | str = RunStatus.PENDING

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _required(self.run_id, "run_id"))
        object.__setattr__(self, "job_id", _required(self.job_id, "job_id"))
        object.__setattr__(self, "scheduled_for", _utc(self.scheduled_for, "scheduled_for"))
        if self.trigger not in {"schedule", "catchup", "manual", "lifecycle"}:
            raise ValueError("invalid run trigger")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("run revision must be at least one")
        if not isinstance(self.status, RunStatus):
            object.__setattr__(self, "status", RunStatus(self.status))


@dataclass(frozen=True)
class DeliveryAttempt:
    delivery_id: str
    run_id: str
    attempt: int
    lease_token: str
    status: DeliveryStatus | str = DeliveryStatus.PENDING
    sequence: int = 0
    destination: str | None = None
    payload: str | None = None
    job_revision: int | None = None
    owner_id: str | None = None
    job_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "delivery_id", _required(self.delivery_id, "delivery_id"))
        object.__setattr__(self, "run_id", _required(self.run_id, "run_id"))
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("delivery attempt must be at least one")
        object.__setattr__(self, "lease_token", _required(self.lease_token, "delivery lease_token"))
        if not isinstance(self.status, DeliveryStatus):
            object.__setattr__(self, "status", DeliveryStatus(self.status))
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("delivery sequence must be a non-negative integer")
        if self.destination is not None:
            object.__setattr__(self, "destination", _required(self.destination, "delivery destination"))
        if self.payload is not None:
            object.__setattr__(self, "payload", _required(self.payload, "delivery payload"))
        if self.job_revision is not None and (isinstance(self.job_revision, bool) or not isinstance(self.job_revision, int) or self.job_revision < 1):
            raise ValueError("delivery job revision must be at least one")
        if self.owner_id is not None:
            object.__setattr__(self, "owner_id", _required(self.owner_id, "delivery owner_id"))
        if self.job_id is not None:
            object.__setattr__(self, "job_id", _required(self.job_id, "delivery job_id"))


@dataclass(frozen=True)
class ControlNotice:
    job_id: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _required(self.job_id, "job_id"))
        object.__setattr__(self, "message", _required(self.message, "control notice message"))


@dataclass(frozen=True)
class CronExecutionClaim:
    run: JobRun
    lease_token: str
    owner_id: str
    destination: str
    intent: JobIntent
    progress: JobProgress
    will_end_after_run: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run, JobRun):
            raise ValueError("execution claim requires a job run")
        object.__setattr__(self, "lease_token", _required(self.lease_token, "execution claim lease_token"))
        object.__setattr__(self, "owner_id", _required(self.owner_id, "execution claim owner_id"))
        object.__setattr__(self, "destination", _required(self.destination, "execution claim destination"))
        if not isinstance(self.intent, JobIntent):
            raise ValueError("execution claim requires job intent")
        if not isinstance(self.progress, JobProgress):
            raise ValueError("execution claim requires job progress")


@dataclass(frozen=True)
class JobProgress:
    successful_runs: int = 0
    previous_outcome: RunOutcome | None = None

    def __post_init__(self) -> None:
        if isinstance(self.successful_runs, bool) or not isinstance(self.successful_runs, int) or self.successful_runs < 0:
            raise ValueError("successful_runs cannot be negative")
        if self.previous_outcome is not None and not isinstance(self.previous_outcome, RunOutcome):
            object.__setattr__(self, "previous_outcome", RunOutcome(self.previous_outcome))
