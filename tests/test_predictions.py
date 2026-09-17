"""Prediction improvements: cadence breadth, drift, and observed forecast ranges.

Three separate claims are proven here.

*A cadence that is not weekly or monthly is still a cadence.* Fortnightly,
four-weekly, quarterly, semiannual and annual groups are recognised, and each
one maps to a schedule that actually reproduces it — four-weekly in particular
must not collapse into monthly, because the two drift apart by nearly two weeks
over a year and a forecast that confuses them loses a payment.

*One late payment is not the absence of a schedule.* A single gap outside the
winning band is tolerated and reported as an outlier; two are not, because a
history that disagrees with itself twice has no schedule to find.

*A forecast month states how wide its own estimate is.* The band is observed
dispersion across the baseline window — each unscheduled category repeating its
cheapest or dearest observed month — accumulating into the closing balance, and
it is never presented as a confidence interval.
"""

from __future__ import annotations

import unittest
from datetime import date

from fincl.errors import FinCLError
from fincl.forecast.engine import (
    COLUMN_LIFESTYLE,
    DIRECTION_OUT,
    TIMING_INFERRED,
    ResidualCategory,
    TimelineEvent,
    _manifest_categories,
    run_timeline,
)
from fincl.planning.detection import _cadence, _drift, _suggested_schedule
from fincl.planning.schedules import occurrences, parse_schedule


class CadenceBreadthTests(unittest.TestCase):
    def test_the_original_weekly_and_monthly_bands_are_unchanged(self) -> None:
        self.assertEqual(_cadence([7, 7, 7]), "weekly")
        self.assertEqual(_cadence([30, 31, 31]), "monthly")

    def test_a_fortnightly_group_is_recognised(self) -> None:
        self.assertEqual(_cadence([14, 14, 15]), "fortnightly")

    def test_four_weekly_does_not_collapse_into_monthly(self) -> None:
        """The two drift apart by nearly two weeks a year; conflating them
        would silently drop a payment out of the horizon."""
        self.assertEqual(_cadence([28, 28, 28]), "four-weekly")
        self.assertEqual(_cadence([31, 30, 31]), "monthly")

    def test_quarterly_semiannual_and_annual_groups_are_recognised(self) -> None:
        self.assertEqual(_cadence([91, 92, 90]), "quarterly")
        self.assertEqual(_cadence([182, 183, 184]), "semiannual")
        self.assertEqual(_cadence([365, 366, 365]), "annual")

    def test_one_late_payment_does_not_erase_the_schedule(self) -> None:
        self.assertEqual(_cadence([30, 31, 45, 30, 31]), "monthly")

    def test_two_disagreeing_gaps_are_not_a_schedule_with_a_hiccup(self) -> None:
        self.assertIsNone(_cadence([30, 45, 60, 31]))

    def test_gaps_that_match_nothing_have_no_cadence(self) -> None:
        self.assertIsNone(_cadence([3, 47, 61]))

    def test_an_evenly_split_history_refuses_to_pick_a_winner(self) -> None:
        self.assertIsNone(_cadence([7, 7, 30, 31]))


class SuggestedScheduleTests(unittest.TestCase):
    """Every detected cadence must map to a schedule that reproduces it."""

    def test_each_cadence_yields_a_schedule_that_parses(self) -> None:
        days = [date(2026, 1, 12), date(2026, 2, 9), date(2026, 3, 9)]
        for cadence in (
            "weekly", "fortnightly", "four-weekly", "monthly",
            "quarterly", "semiannual", "annual",
        ):
            with self.subTest(cadence=cadence):
                schedule = _suggested_schedule(cadence, days)
                self.assertIsNotNone(schedule)
                parse_schedule(schedule)

    def test_a_four_weekly_schedule_fires_thirteen_times_a_year(self) -> None:
        schedule = parse_schedule(
            {"kind": "interval_weeks", "weeks": 4, "anchor_on": "2026-01-05"}
        )
        fired = occurrences(schedule, date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(len(fired), 13)
        self.assertEqual(fired[0], date(2026, 1, 5))
        self.assertEqual(fired[1], date(2026, 2, 2))

    def test_a_quarterly_schedule_fires_on_its_anchor_phase(self) -> None:
        schedule = parse_schedule(
            {"kind": "interval_months", "months": 3, "day": 15, "anchor_on": "2026-02-15"}
        )
        fired = occurrences(schedule, date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(
            fired,
            [date(2026, 2, 15), date(2026, 5, 15), date(2026, 8, 15), date(2026, 11, 15)],
        )

    def test_an_annual_schedule_clamps_a_short_month(self) -> None:
        schedule = parse_schedule(
            {"kind": "interval_months", "months": 12, "day": 31, "anchor_on": "2024-02-29"}
        )
        fired = occurrences(schedule, date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(fired, [date(2026, 2, 28)])

    def test_an_unknown_interval_kind_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            parse_schedule({"kind": "interval_days", "days": 10, "anchor_on": "2026-01-01"})

    def test_a_zero_interval_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            parse_schedule({"kind": "interval_weeks", "weeks": 0, "anchor_on": "2026-01-05"})


class DriftTests(unittest.TestCase):
    def _amounts(self, values: list[str]) -> list:
        from decimal import Decimal
        return [Decimal(value) for value in values]

    def test_a_climbing_bill_is_reported_as_rising(self) -> None:
        drift = _drift(self._amounts(["100.00", "130.00", "160.00"]))

        self.assertTrue(drift["available"])
        self.assertEqual(drift["direction"], "rising")
        self.assertEqual(drift["total_change"], "60.00")
        self.assertEqual(drift["per_occurrence"], "30.00")

    def test_ordinary_noise_is_not_dressed_up_as_a_trend(self) -> None:
        drift = _drift(self._amounts(["100.00", "108.00", "103.00"]))

        self.assertEqual(drift["direction"], "flat")

    def test_a_trend_is_never_applied_to_the_expected_amount(self) -> None:
        drift = _drift(self._amounts(["100.00", "130.00", "160.00"]))

        self.assertFalse(drift["applied"])

    def test_two_observations_cannot_establish_a_trend(self) -> None:
        drift = _drift(self._amounts(["100.00", "160.00"]))

        self.assertFalse(drift["available"])


def _residual_events(monthly: str, days: list[str], scale: int = 2) -> list[TimelineEvent]:
    from fincl.forecast.engine import allocate_daily_cents
    from fincl.money import ExactMoney

    minor = ExactMoney.parse(monthly).rescaled_coefficient(scale)
    slices = allocate_daily_cents(minor, len(days))
    return [
        TimelineEvent(
            event_id=f"residual:{day}", source_id=f"residual:{day}",
            column=COLUMN_LIFESTYLE, direction=DIRECTION_OUT, on=day,
            amount_minor=amount, label="Routine residual",
            timing_basis=TIMING_INFERRED, window_earliest=days[0], window_latest=days[-1],
        )
        for day, amount in zip(days, slices, strict=True)
        if amount
    ]


class ObservedRangeTests(unittest.TestCase):
    def _run(self, dispersion: dict[str, dict[str, int]]) -> list[dict]:
        days = [f"2026-02-{day:02d}" for day in range(1, 29)]
        return run_timeline(
            currency="SGD", scale=2, opening_minor=500_00,
            start="2026-01-31", end="2026-02-28",
            events=_residual_events("280.00", days),
            dispersion=dispersion,
        )["months"]

    def test_a_month_with_no_dispersion_carries_no_band(self) -> None:
        self.assertIsNone(self._run({})[0]["observed_range"])

    def test_spending_low_raises_the_closing_balance_and_high_lowers_it(self) -> None:
        """The two bands cross over: the cheap case is the rich one."""
        row = self._run({"2026-02": {"down": 80_00, "up": 120_00, "base": 280_00}})[0]

        band = row["observed_range"]
        self.assertEqual(row["closing"], "220.00")
        self.assertEqual(band["closing"]["high"], "300.00")
        self.assertEqual(band["closing"]["low"], "100.00")
        self.assertEqual(band["expense"]["low"], "200.00")
        self.assertEqual(band["expense"]["high"], "400.00")

    def test_the_band_is_never_presented_as_a_confidence_interval(self) -> None:
        band = self._run({"2026-02": {"down": 80_00, "up": 120_00, "base": 280_00}})[0]["observed_range"]

        self.assertTrue(band["not_a_confidence_interval"])
        self.assertIn("no precedent", band["note"])

    def test_the_closing_band_widens_across_the_horizon(self) -> None:
        days = [f"2026-02-{day:02d}" for day in range(1, 29)]
        days += [f"2026-03-{day:02d}" for day in range(1, 32)]
        months = run_timeline(
            currency="SGD", scale=2, opening_minor=2000_00,
            start="2026-01-31", end="2026-03-31",
            events=_residual_events("280.00", days),
            dispersion={
                "2026-02": {"down": 50_00, "up": 60_00, "base": 280_00},
                "2026-03": {"down": 50_00, "up": 60_00, "base": 280_00},
            },
        )["months"]

        first, second = months[0]["observed_range"], months[1]["observed_range"]
        first_width = float(first["closing"]["high"]) - float(first["closing"]["low"])
        second_width = float(second["closing"]["high"]) - float(second["closing"]["low"])
        self.assertGreater(second_width, first_width)
        self.assertTrue(second["cumulative"])


class ResidualCategoryLowTests(unittest.TestCase):
    def test_the_manifest_category_carries_the_observed_low(self) -> None:
        estimate = {
            "available": True,
            "by_currency": {
                "SGD": {
                    "variable_residual_monthly": "300.00",
                    "variable_residual_min": "210.00",
                    "variable_residual_max": "450.00",
                    "irregular_allowance_monthly": "50.00",
                }
            },
        }

        routine = _manifest_categories({"methods": {}}, estimate)[0]

        self.assertEqual(routine.lowest_observed_monthly, "210.00")

    def test_an_estimate_without_an_observed_low_gets_a_one_sided_band(self) -> None:
        """The low is never fabricated from the mean."""
        estimate = {
            "available": True,
            "by_currency": {
                "SGD": {
                    "variable_residual_monthly": "300.00",
                    "variable_residual_max": "450.00",
                    "irregular_allowance_monthly": "50.00",
                }
            },
        }

        routine = _manifest_categories({"methods": {}}, estimate)[0]

        self.assertIsNone(routine.lowest_observed_monthly)

    def test_a_negative_observed_low_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            ResidualCategory(
                category_id="x", currency="SGD", mean_monthly="300.00",
                highest_observed_monthly="450.00", lowest_observed_monthly="-1.00",
                label="x",
            )


if __name__ == "__main__":
    unittest.main()
