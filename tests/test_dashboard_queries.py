"""M2: the snapshot dashboard query surface in ``fincl.queries``.

These build a real ledger by Revolut import + ``refresh_ledger`` and assert the
contract the UI depends on: exact decimal strings, coverage gaps that withhold
rather than zero, deterministic window selection, and the labelled recurring
heuristic.
"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fincl.application import record_decision
from fincl.errors import FinCLError
from fincl.queries import aggregate, dashboard, money
from fincl.refresh import refresh_ledger
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    FinCLService.plaintext_phase1(database, root / "evidence").import_revolut(source)
    refresh_ledger(database)
    return database


_THREE_MONTHS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01", "Alpha", "-10.00", "0.00", "SGD", "COMPLETED", "890.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01", "Beta", "-20.00", "0.00", "SGD", "COMPLETED", "870.00"],
    ["Card Payment", "Current", "2026-02-18 10:00:00", "2026-02-18 10:00:01", "Gamma", "-7.50", "0.00", "SGD", "COMPLETED", "862.50"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01", "Delta", "-30.00", "0.00", "SGD", "COMPLETED", "832.50"],
]


class MoneyFormatTests(unittest.TestCase):
    def test_money_is_a_plain_decimal_string_with_no_exponent(self) -> None:
        self.assertEqual(money(Decimal("0")), "0")
        self.assertEqual(money(Decimal("12.5")), "12.5")

    def test_large_values_keep_every_digit(self) -> None:
        big = Decimal("123456789012345.67")
        self.assertEqual(money(big), "123456789012345.67")


class AggregateTests(unittest.TestCase):
    def test_totals_are_exact_and_sorted_by_descending_value(self) -> None:
        entries = [
            {"bucket": "spending", "amount": "-10.00", "merchant": "A"},
            {"bucket": "spending", "amount": "-2.50", "merchant": "A"},
            {"bucket": "spending", "amount": "-40.00", "merchant": "B"},
            {"bucket": "income", "amount": "100.00", "merchant": "B"},
        ]
        result = aggregate(entries, "merchant")
        self.assertEqual([(r["name"], r["value"], r["count"]) for r in result],
                         [("B", "40.00", 1), ("A", "12.50", 2)])


class WindowSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger(_THREE_MONTHS)

    def test_default_window_is_the_latest_full_covered_calendar_month(self) -> None:
        data = dashboard(self.database)
        self.assertEqual((data["start"], data["end"]), ("2026-02-01", "2026-02-28"))
        self.assertTrue(data["covered"])

    def test_summary_spending_equals_the_recent_contributor_sum(self) -> None:
        # The Feb window has two spending rows (-20.00, -7.50); `recent` (cap 8)
        # holds both, so their magnitudes reconcile to the summary exactly.
        data = dashboard(self.database)
        total = sum(
            abs(Decimal(e["amount"]))
            for e in data["recent"]
            if e["bucket"] == "spending" and e["currency"] == "SGD"
        )
        self.assertEqual(data["summary"]["spending"], money(total))
        self.assertEqual(data["summary"]["spending"], "27.50")

    def test_an_uncovered_window_withholds_totals_and_analytics(self) -> None:
        data = dashboard(self.database, start="2000-01-01", end="2000-06-30")
        self.assertFalse(data["covered"])
        self.assertIsNone(data["summary"]["spending"])
        self.assertEqual(data["category_totals"], [])
        self.assertEqual(data["merchant_totals"], [])

    def test_trend_marks_uncovered_months_null_never_zero(self) -> None:
        data = dashboard(self.database)
        gap_months = [t for t in data["trend"] if not t["complete"]]
        self.assertTrue(gap_months)
        for month in gap_months:
            self.assertIsNone(month["spending"])
            self.assertIsNone(month["income"])

    def test_mismatched_start_and_end_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            dashboard(self.database, start="2026-02-01", end=None)


_RECURRING = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01", "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-01-10 09:00:00", "2026-01-10 09:00:01", "Streamly", "-15.00", "0.00", "SGD", "COMPLETED", "885.00"],
    ["Card Payment", "Current", "2026-02-10 09:00:00", "2026-02-10 09:00:01", "Streamly", "-15.00", "0.00", "SGD", "COMPLETED", "870.00"],
    ["Card Payment", "Current", "2026-03-10 09:00:00", "2026-03-10 09:00:01", "Streamly", "-15.00", "0.00", "SGD", "COMPLETED", "855.00"],
    ["Card Payment", "Current", "2026-04-10 09:00:00", "2026-04-10 09:00:01", "Streamly", "-15.00", "0.00", "SGD", "COMPLETED", "840.00"],
]


class RecurringHeuristicTests(unittest.TestCase):
    def test_three_monthly_equal_charges_surface_as_a_candidate(self) -> None:
        data = dashboard(_ledger(_RECURRING), start="2026-04-01", end="2026-04-30")
        names = {r["merchant"].upper(): r for r in data["recurring"]}
        self.assertIn("STREAMLY", names)
        self.assertEqual(names["STREAMLY"]["cadence"], "Monthly")
        self.assertEqual(names["STREAMLY"]["amount"], "15.00")
        self.assertIn("verify", names["STREAMLY"]["status"].lower())


class CustomVocabularyTests(unittest.TestCase):
    def test_a_custom_category_and_budget_appear_in_the_snapshot(self) -> None:
        database = _ledger(_THREE_MONTHS)
        record_decision(
            database, request_id="c1", intent_hash="i1", decision_kind="CATEGORY_CREATE",
            anchors={"category": "Lifestyle / Streaming"},
            payload={"category": "Lifestyle / Streaming", "reason": "tracking subscriptions"},
            policy_version="dashboard-v1", snapshot="create Lifestyle / Streaming",
        )
        record_decision(
            database, request_id="p1", intent_hash="i2", decision_kind="PLAN_SET",
            anchors={"plan": "plan-1"},
            payload={"type": "budget", "name": "Streaming", "currency": "SGD",
                     "target": "30.00", "saved": "0", "category": "Lifestyle / Streaming",
                     "reason": "monthly cap"},
            policy_version="dashboard-v1", snapshot="plan-1",
        )
        data = dashboard(database)
        self.assertIn("Lifestyle / Streaming", data["custom_categories"])
        self.assertIn("Lifestyle / Streaming", data["categories"])
        self.assertEqual([p["name"] for p in data["plans"]], ["Streaming"])


if __name__ == "__main__":
    unittest.main()
