"""v0.2 analysis slice 2: the GET /api/v1/analysis panel assembly.

Every panel is read from the committed component projection, so the summary
reconciles with the economic-flow report and each drill-down handle reconciles
with the total it sits under. Comparison windows follow the design rules
(previous complete month, else equal-duration preceding; never 31 vs 28).
"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fincl.analysis.scopes import (
    is_full_calendar_month,
    normalize_scope,
    resolve_comparison,
)
from fincl.errors import FinCLError
from fincl.intervals import DateInterval
from fincl.queries import analysis_page, contributors_page
from fincl.refresh import refresh_ledger
from fincl.report import economic_flow_report_data
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01", "Alpha", "-10.00", "0.50", "SGD", "COMPLETED", "889.50"],
    ["Card Payment", "Current", "2026-01-20 10:00:00", "2026-01-20 10:00:01", "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "849.50"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01", "Alpha", "-20.00", "0.00", "SGD", "COMPLETED", "829.50"],
    ["Card Payment", "Current", "2026-02-18 10:00:00", "2026-02-18 10:00:01", "Gamma", "-7.50", "0.00", "SGD", "COMPLETED", "822.00"],
    ["Refund", "Current", "2026-02-20 10:00:00", "2026-02-20 10:00:01", "Alpha", "5.00", "0.00", "SGD", "COMPLETED", "827.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01", "Delta", "-30.00", "0.00", "SGD", "COMPLETED", "797.00"],
]


def _ledger(rows: list[list[str]] = _ROWS) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    FinCLService.plaintext_phase1(database, root / "evidence").import_revolut(source)
    refresh_ledger(database)
    return database


class ScopeAndComparisonTests(unittest.TestCase):
    def test_full_calendar_month_detection(self) -> None:
        self.assertTrue(is_full_calendar_month("2026-02-01", "2026-02-28"))
        self.assertFalse(is_full_calendar_month("2026-02-01", "2026-02-20"))
        self.assertFalse(is_full_calendar_month("2026-02-03", "2026-03-02"))

    def test_a_month_compares_against_the_previous_complete_month(self) -> None:
        scope = normalize_scope(start="2026-02-01", end="2026-02-28")
        covered = [DateInterval("2026-01-01", "2026-03-31")]
        comp = resolve_comparison(scope, covered, requested=None, main_covered=True)
        self.assertEqual((comp.kind, comp.start, comp.end), ("previous_month", "2026-01-01", "2026-01-31"))
        self.assertTrue(comp.available)

    def test_an_arbitrary_range_compares_against_an_equal_duration_before_it(self) -> None:
        from datetime import date

        scope = normalize_scope(start="2026-02-10", end="2026-02-20")  # 11 days
        covered = [DateInterval("2026-01-01", "2026-03-31")]
        comp = resolve_comparison(scope, covered, requested=None, main_covered=True)
        self.assertEqual(comp.kind, "preceding_equal_duration")
        self.assertEqual((comp.start, comp.end), ("2026-01-30", "2026-02-09"))
        # Equal length, and immediately before the selected range.
        self.assertEqual(
            (date.fromisoformat(comp.end) - date.fromisoformat(comp.start)).days,
            (date.fromisoformat(scope.end) - date.fromisoformat(scope.start)).days,
        )
        self.assertEqual(date.fromisoformat(comp.end), date.fromisoformat(scope.start) - _one_day())

    def test_a_previous_month_comparison_needs_a_whole_month(self) -> None:
        scope = normalize_scope(start="2026-02-05", end="2026-02-20")
        with self.assertRaises(FinCLError):
            resolve_comparison(scope, [], requested="previous_month", main_covered=True)

    def test_comparison_outside_coverage_is_unavailable_with_a_reason(self) -> None:
        scope = normalize_scope(start="2026-02-01", end="2026-02-28")
        covered = [DateInterval("2026-02-01", "2026-03-31")]  # January not covered
        comp = resolve_comparison(scope, covered, requested=None, main_covered=True)
        self.assertFalse(comp.available)
        self.assertTrue(comp.reasons)


class AnalysisPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger()

    def test_summary_reconciles_with_the_economic_flow_report(self) -> None:
        page = analysis_page(self.database, start="2026-02-01", end="2026-02-28")
        report = economic_flow_report_data(self.database, start="2026-02-01", end="2026-02-28")
        rc = report["currencies"][0]
        self.assertEqual(page["summary"]["spending"]["value"]["value"], rc["spending"])
        self.assertEqual(page["summary"]["refunds"]["value"]["value"], rc["refunds"])
        self.assertEqual(page["summary"]["fees"]["value"]["value"], rc["fees"])
        self.assertEqual(
            page["summary"]["net_economic_flow"]["value"]["value"], rc["net_economic_flow"]
        )

    def test_month_comparison_carries_previous_delta_and_percentage(self) -> None:
        page = analysis_page(self.database, start="2026-02-01", end="2026-02-28")
        spending = page["summary"]["spending"]
        self.assertEqual(spending["value"]["value"], "27.50")
        self.assertEqual(spending["previous"]["value"], "50.00")
        self.assertEqual(spending["delta"]["value"], "-22.50")
        self.assertEqual(spending["pct"], "-45.0")

    def test_percentage_is_null_when_the_comparison_value_is_zero(self) -> None:
        page = analysis_page(self.database, start="2026-02-01", end="2026-02-28")
        self.assertEqual(page["summary"]["refunds"]["previous"]["value"], "0")
        self.assertIsNone(page["summary"]["refunds"]["pct"])

    def test_category_changes_are_split_and_ordered_by_absolute_delta(self) -> None:
        page = analysis_page(self.database, start="2026-02-01", end="2026-02-28")
        changes = page["category_changes"]
        self.assertTrue(changes["restated_using_current_categories"])
        deltas = [Decimal(r["delta"]["value"]) for r in changes["decreases"]]
        self.assertEqual(deltas, sorted(deltas))
        self.assertTrue(all(Decimal(r["delta"]["value"]) > 0 for r in changes["increases"]))

    def test_merchant_ranking_is_descending_and_drills_to_the_same_total(self) -> None:
        page = analysis_page(self.database, start="2026-02-01", end="2026-02-28")
        ranking = page["merchant_ranking"]
        values = [Decimal(r["value"]["value"]) for r in ranking]
        self.assertEqual(values, sorted(values, reverse=True))
        top = ranking[0]
        drilled = _drain(self.database, top["handle"])
        self.assertEqual(
            sum(abs(Decimal(c["value"])) for c in drilled), Decimal(top["value"]["value"])
        )

    def test_trend_has_six_points_ending_at_the_selected_month(self) -> None:
        page = analysis_page(self.database, start="2026-02-01", end="2026-02-28")
        months = [p["month"] for p in page["trend"]]
        self.assertEqual(len(months), 6)
        self.assertEqual(months[-1], "2026-02")
        self.assertTrue(all(p["spending"] is None for p in page["trend"] if not p["complete"]))

    def test_an_uncovered_window_withholds_values_but_still_answers(self) -> None:
        page = analysis_page(self.database, start="2000-01-01", end="2000-01-31")
        self.assertFalse(page["covered"])
        self.assertIsNone(page["summary"]["spending"]["value"])
        self.assertIsNone(page["contributors_handle"])
        self.assertEqual(page["merchant_ranking"], [])

    def test_comparison_none_suppresses_the_previous_columns(self) -> None:
        page = analysis_page(
            self.database, start="2026-02-01", end="2026-02-28", comparison="none"
        )
        self.assertEqual(page["comparison"]["kind"], "none")
        self.assertIsNone(page["summary"]["spending"]["previous"])
        self.assertEqual(page["category_changes"]["increases"], [])

    def test_a_stale_expected_generation_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            analysis_page(
                self.database, start="2026-02-01", end="2026-02-28",
                expected="sha256:not-the-current-one",
            )

    def test_a_category_filter_narrows_every_panel(self) -> None:
        page = analysis_page(
            self.database, start="2026-02-01", end="2026-02-28",
            category_keys=["Unclassified / Unclassified"],
        )
        self.assertEqual(page["scope"]["category_keys"], ["Unclassified / Unclassified"])
        # Only the filtered category can appear in the ranking / changes.
        for row in page["category_changes"]["decreases"] + page["category_changes"]["increases"]:
            self.assertEqual(row["category"], "Unclassified / Unclassified")


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


def _drain(database: Path, handle: str) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        page = contributors_page(database, handle=handle, cursor=cursor, limit=50)
        out.extend(page["components"])
        cursor = page["next_cursor"]
        if not cursor:
            return out


if __name__ == "__main__":
    unittest.main()
