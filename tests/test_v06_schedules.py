"""v0.6 slice 3 — recurring schedule date arithmetic."""

from __future__ import annotations

import unittest
from datetime import date

from fincl.errors import FinCLError
from fincl.planning.schedules import occurrences, parse_schedule


class MonthlyClampTests(unittest.TestCase):
    def test_day_31_clamps_per_month_and_returns_to_31(self) -> None:
        schedule = {"kind": "monthly", "day": 31, "anchor_on": "2026-01-31"}
        got = occurrences(schedule, date(2026, 1, 1), date(2026, 4, 30))
        self.assertEqual(got, [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])

    def test_leap_february_uses_29(self) -> None:
        schedule = {"kind": "monthly", "day": 31, "anchor_on": "2028-01-31"}
        got = occurrences(schedule, date(2028, 2, 1), date(2028, 2, 29))
        self.assertEqual(got, [date(2028, 2, 29)])

    def test_window_is_inclusive_on_both_ends(self) -> None:
        schedule = {"kind": "monthly", "day": 15, "anchor_on": "2026-01-15"}
        got = occurrences(schedule, date(2026, 3, 15), date(2026, 5, 15))
        self.assertEqual(got, [date(2026, 3, 15), date(2026, 4, 15), date(2026, 5, 15)])


class WeeklyTests(unittest.TestCase):
    def test_every_seven_days_from_the_anchor_weekday(self) -> None:
        schedule = {"kind": "weekly", "weekday": 2, "anchor_on": "2026-01-07"}  # a Wednesday
        got = occurrences(schedule, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(got, [date(2026, 1, 7), date(2026, 1, 14), date(2026, 1, 21), date(2026, 1, 28)])
        self.assertTrue(all(d.weekday() == 2 for d in got))


class ScheduleValidationTests(unittest.TestCase):
    def test_weekly_anchor_must_match_the_configured_weekday(self) -> None:
        with self.assertRaises(FinCLError):
            parse_schedule({"kind": "weekly", "weekday": 0, "anchor_on": "2026-01-07"})

    def test_monthly_day_out_of_range_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            parse_schedule({"kind": "monthly", "day": 32, "anchor_on": "2026-01-01"})

    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            parse_schedule({"kind": "annual", "anchor_on": "2026-01-01"})
