import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tomo_core.cron_models import ScheduleSpec
from tomo_core.cron_schedule import catch_up, next_occurrence


UTC = timezone.utc


class CronScheduleTests(unittest.TestCase):
    def test_interval_catch_up_handles_multi_year_downtime(self):
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        schedule = ScheduleSpec.interval(60, starts_at=since)

        self.assertEqual(catch_up(schedule, since, now), now)
    def test_one_shot_and_interval_next_occurrences(self):
        due = datetime(2026, 1, 1, 12, tzinfo=UTC)
        self.assertEqual(next_occurrence(ScheduleSpec.once(due), due - timedelta(seconds=1)), due)
        self.assertIsNone(next_occurrence(ScheduleSpec.once(due), due))
        interval = ScheduleSpec.interval(300, starts_at=due)
        self.assertEqual(next_occurrence(interval, due + timedelta(seconds=1)), due + timedelta(minutes=5))

    def test_cron_supports_ranges_lists_steps_and_finds_next_utc_occurrence(self):
        schedule = ScheduleSpec.cron("*/15 9-10 1,15 * 1-5", "UTC")
        self.assertEqual(next_occurrence(schedule, datetime(2026, 1, 1, 8, 59, tzinfo=UTC)), datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
        self.assertEqual(next_occurrence(schedule, datetime(2026, 1, 1, 9, 0, tzinfo=UTC)), datetime(2026, 1, 1, 9, 15, tzinfo=UTC))
        with self.assertRaises(ValueError):
            ScheduleSpec.cron("* * * *", "UTC")

    def test_cron_uses_standard_day_matching_and_numbered_steps_extend_to_field_maximum(self):
        either_day = ScheduleSpec.cron("0 9 13 * 1", "UTC")
        self.assertEqual(next_occurrence(either_day, datetime(2026, 1, 4, 12, tzinfo=UTC)), datetime(2026, 1, 5, 9, tzinfo=UTC))
        self.assertEqual(next_occurrence(either_day, datetime(2026, 1, 5, 9, tzinfo=UTC)), datetime(2026, 1, 12, 9, tzinfo=UTC))

        weekday_only = ScheduleSpec.cron("0 9 * * 1", "UTC")
        self.assertEqual(next_occurrence(weekday_only, datetime(2026, 1, 13, 9, tzinfo=UTC)), datetime(2026, 1, 19, 9, tzinfo=UTC))

        stepped = ScheduleSpec.cron("5/15 * * * *", "UTC")
        self.assertEqual(next_occurrence(stepped, datetime(2026, 1, 1, 0, 5, tzinfo=UTC)), datetime(2026, 1, 1, 0, 20, tzinfo=UTC))

    def test_cron_fold_fires_once_and_gap_is_skipped(self):
        try:
            ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            self.skipTest("host does not provide an IANA zoneinfo database")
        schedule = ScheduleSpec.cron("30 1 * * *", "America/New_York")
        self.assertEqual(next_occurrence(schedule, datetime(2026, 11, 1, 5, 0, tzinfo=UTC)), datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        self.assertEqual(next_occurrence(schedule, datetime(2026, 11, 1, 5, 30, tzinfo=UTC)), datetime(2026, 11, 2, 6, 30, tzinfo=UTC))
        gap = ScheduleSpec.cron("30 2 * * *", "America/New_York")
        self.assertEqual(next_occurrence(gap, datetime(2026, 3, 8, 6, tzinfo=UTC)), datetime(2026, 3, 9, 6, 30, tzinfo=UTC))

    def test_catch_up_coalesces_all_missed_occurrences_to_one_latest_due_time(self):
        schedule = ScheduleSpec.interval(60, starts_at=datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(catch_up(schedule, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 3, 30, tzinfo=UTC)), datetime(2026, 1, 1, 0, 3, tzinfo=UTC))
        self.assertIsNone(catch_up(schedule, datetime(2026, 1, 1, 0, 3, 30, tzinfo=UTC), datetime(2026, 1, 1, 0, 3, 45, tzinfo=UTC)))


if __name__ == "__main__":
    unittest.main()
