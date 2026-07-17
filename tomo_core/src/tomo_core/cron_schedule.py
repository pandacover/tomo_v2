from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from .cron_models import ScheduleSpec


_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _field_values(source: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in source.split(","):
        base, separator, step_text = part.partition("/")
        step = int(step_text) if separator and step_text.isdigit() else 1
        if separator and (not step_text.isdigit() or step < 1):
            raise ValueError("cron step must be a positive integer")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError("invalid cron range")
            start, end = int(start_text), int(end_text)
        elif base.isdigit():
            start = int(base)
            end = high if separator else start
        else:
            raise ValueError("invalid cron field")
        if not low <= start <= end <= high:
            raise ValueError("cron field is out of range")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class _CronFields:
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    day_is_wildcard: bool
    weekday_is_wildcard: bool


@lru_cache(maxsize=256)
def _cron_fields(expression: str) -> _CronFields:
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("cron expression must contain five fields")
    fields = tuple(_field_values(part, *bounds) for part, bounds in zip(parts, _FIELDS))
    return _CronFields(*fields, parts[2] == "*", parts[4] == "*")


def _matches(schedule: ScheduleSpec, candidate: datetime) -> bool:
    fields = _cron_fields(schedule.expression or "")
    zone = timezone.utc if schedule.timezone_name == "UTC" else ZoneInfo(schedule.timezone_name)
    local = candidate.astimezone(zone)
    # A repeated local minute is represented twice in UTC. The first is canonical.
    day_matches = local.day in fields.day
    weekday_matches = ((local.weekday() + 1) % 7) in fields.weekday
    if fields.day_is_wildcard:
        day_matches = weekday_matches
    elif fields.weekday_is_wildcard:
        day_matches = day_matches
    else:
        day_matches = day_matches or weekday_matches
    return local.fold == 0 and (local.minute in fields.minute and local.hour in fields.hour and day_matches and local.month in fields.month)


def next_occurrence(schedule: ScheduleSpec, after: datetime) -> datetime | None:
    """Return the first occurrence strictly after ``after`` as a UTC datetime."""
    if after.tzinfo is None or after.utcoffset() is None:
        raise ValueError("after must be timezone-aware")
    after = after.astimezone(timezone.utc)
    if schedule.kind == "once":
        return schedule.at if schedule.at and schedule.at > after else None
    if schedule.kind == "interval":
        origin = schedule.starts_at or after.replace(second=0, microsecond=0)
        if after < origin:
            return origin
        elapsed = after - origin
        periods = elapsed // schedule.every + 1
        return origin + periods * schedule.every
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Five years bounds the search while covering every valid five-field expression.
    for _ in range(5 * 366 * 24 * 60):
        if _matches(schedule, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron expression has no occurrence in the next five years")


def catch_up(schedule: ScheduleSpec, since: datetime, now: datetime) -> datetime | None:
    """Coalesce occurrences in (since, now] into their latest scheduled time."""
    if since.tzinfo is None or now.tzinfo is None:
        raise ValueError("catch-up bounds must be timezone-aware")
    if now < since:
        raise ValueError("now cannot precede since")
    if schedule.kind == "interval":
        origin = schedule.starts_at
        if origin is None:
            raise ValueError("interval catch-up requires starts_at")
        if now < origin:
            return None
        first_period = max(0, (since - origin) // schedule.every + 1)
        first = origin + first_period * schedule.every
        if first > now:
            return None
        return origin + ((now - origin) // schedule.every) * schedule.every

    occurrence = next_occurrence(schedule, since)
    latest = None
    while occurrence is not None and occurrence <= now:
        latest = occurrence
        occurrence = next_occurrence(schedule, occurrence)
    return latest
