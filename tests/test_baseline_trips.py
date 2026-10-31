"""Trip spending as its own baseline term, and the forecast allowance it feeds."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fincl.application import record_decision
from fincl.baseline import baseline_contributors, baseline_estimate
from fincl.document_service import DocumentImportService
from fincl.forecast.engine import (
    COLUMN_IRREGULAR,
    DIRECTION_OUT,
    ForecastEvent,
    build_cash_timeline,
)
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes
from tests.test_forecast_engine import _manifest

TODAY = date(2026, 5, 10)


def _ledger(trip_spend: list[tuple[str, str, str]], *, currency: str = "SGD", home: bool = True) -> Path:
    """Four months of a corner shop (the last one still open), the given trip spending and a March one-off."""
    rows: list[list[str]] = []
    balance = Decimal("10000.00")

    def row(day: str, description: str, amount: str, kind: str = "Card Payment") -> None:
        nonlocal balance
        balance += Decimal(amount)
        stamp = f"{day} 10:00:00"
        rows.append([kind, "Current", stamp, stamp, description, amount, "0.00", currency, "COMPLETED", f"{balance:.2f}"])

    row("2026-01-01", "Seed", "10000.00", "Topup")
    spending = [(f"2026-{month}-{day}", "Corner Shop", "-20.00") for month in ("01", "02", "03", "04")
                for day in ("05", "12", "19")] if home else []
    if home:
        spending.append(("2026-03-26", "Annual Thing", "-400.00"))
    for day, description, amount in sorted([*spending, *trip_spend]):
        row(day, description, amount)
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "statement.pdf"
    source.write_bytes(revolut_pdf_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _decide(database: Path, kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    record_decision(database, request_id=str(uuid.uuid4()), intent_hash=uuid.uuid4().hex, decision_kind=kind,
                    anchors=anchors, payload=payload, policy_version="trip-context-v1", snapshot=kind)


def _send_on_trips(database: Path, description: str, country: str = "FR") -> None:
    connection = sqlite3.connect(database)
    try:
        anchors = [str(r[0]) for r in connection.execute(
            "SELECT e.natural_anchor FROM ledger_entries e JOIN document_records d ON d.id = e.document_record_id "
            "WHERE d.description_raw = ?", (description,))]
    finally:
        connection.close()
    for anchor in anchors:
        _decide(database, "ENTRY_COUNTRY_SET", {"entry": anchor}, {"country": country, "reason": "Receipt"})


def _trip(database: Path, start: str, end: str, country: str = "FR") -> None:
    _decide(database, "TRIP_SET", {"trip": str(uuid.uuid4())},
            {"name": f"Trip {start}", "start_on": start, "end_on": end, "countries": [country], "reason": "Holiday"})


BISTRO = [("2026-01-24", "Bistro", "-200.00"), ("2026-02-21", "Bistro", "-200.00"), ("2026-03-21", "Bistro", "-200.00")]


class BaselineTripTermTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger(BISTRO)
        before = baseline_estimate(self.database, today=TODAY)
        self.before = before["by_currency"]["SGD"]
        for start, end in (("2026-01-23", "2026-01-25"), ("2026-02-20", "2026-02-22"), ("2026-03-20", "2026-03-22")):
            _trip(self.database, start, end)
        _send_on_trips(self.database, "Bistro")
        self.estimate = baseline_estimate(self.database, today=TODAY)
        self.sgd = self.estimate["by_currency"]["SGD"]

    def test_trip_spending_is_its_own_term_and_every_month_still_reconciles(self) -> None:
        january = next(row for row in self.sgd["months"] if row["month"] == "2026-01")
        self.assertEqual((january["trip"], january["routine_variable_residual"], january["unresolved_irregular"]),
                         ("200.00", "60.00", "0.00"))
        self.assertTrue(all(row["reconciles"] for row in self.sgd["months"]))
        self.assertEqual(self.sgd["trip_allowance_monthly"], "200.00")
        self.assertTrue(self.sgd["trip_allowance_projected"])
        # The same money, moved: the observed total is unchanged.
        self.assertEqual(self.sgd["total_observed_monthly"], self.before["total_observed_monthly"])

    def test_trip_meals_leave_the_outlier_median_so_a_one_off_at_home_is_still_held_out(self) -> None:
        # With the meals abroad in the median, three months of 260 hid the 400.
        self.assertEqual([e["amount"] for e in self.before["irregular_risk_entries"]], [])
        self.assertEqual([e["amount"] for e in self.sgd["irregular_risk_entries"]], ["400.00"])

    def test_contributors_list_the_trip_and_reconcile(self) -> None:
        read = baseline_contributors(self.database, today=TODAY, currency="SGD", month="2026-02")
        self.assertTrue(read["reconciles"])
        trip = next(part for part in read["parts"] if part["part"] == "trip")
        self.assertEqual([(c["merchant"].lower(), c["amount"]) for c in trip["contributors"]], [("bistro", "200.00")])
        self.assertIsNotNone(trip["contributors"][0]["trip_id"])

    def test_a_planned_trip_replaces_the_allowance_in_its_month_and_does_not_add_to_it(self) -> None:
        def march_expense(events: list[ForecastEvent]) -> Decimal:
            cell = build_cash_timeline(_manifest(balance="50000.00"), sourced_events=events,
                                       estimate=self.estimate)["by_currency"]["SGD"]
            return Decimal(next(row for row in cell["base"]["months"] if row["month"] == "2026-03")["expense"])

        planned = ForecastEvent(
            event_id="planned:rome:1", source_id="planned:rome:1", currency="SGD", amount="500.00",
            column=COLUMN_IRREGULAR, direction=DIRECTION_OUT, label="Rome", on="2026-03-10",
            residual_category="manifest:trip:SGD", residual_on="2026-03-10", residual_amount="500.00",
            removes_from_residual=True,
        )
        # Past trips already put 200 a month in; the planned 500 adds only what it exceeds that by.
        self.assertEqual(march_expense([planned]) - march_expense([]), Decimal("300.00"))


class NoTripLedgerTests(unittest.TestCase):
    def test_a_ledger_without_trip_spending_forecasts_exactly_as_before(self) -> None:
        database = _ledger(BISTRO)
        before = baseline_estimate(database, today=TODAY)
        # A trip nothing joins (no transaction has a country) changes nothing.
        _trip(database, "2026-01-23", "2026-01-25")
        after = baseline_estimate(database, today=TODAY)
        sgd = after["by_currency"]["SGD"]
        self.assertFalse(sgd["trip_allowance_projected"])
        self.assertEqual(sgd["trip_allowance_monthly"], "0.00")
        for field in ("variable_residual_monthly", "variable_residual_max", "irregular_allowance_monthly",
                      "total_observed_monthly", "irregular_risk_entries"):
            self.assertEqual(sgd[field], before["by_currency"]["SGD"][field], field)
        timelines = [build_cash_timeline(_manifest(), estimate=estimate)["by_currency"]["SGD"] for estimate in (before, after)]
        for case in ("base", "conservative"):
            self.assertEqual(timelines[0][case]["months"], timelines[1][case]["months"])
            self.assertNotIn("manifest:trip:SGD", {row["category_id"] for row in timelines[1][case]["allocation_metadata"]})


class TripOnlyPocketTests(unittest.TestCase):
    def test_a_pocket_used_only_on_trips_projects_no_trip_allowance(self) -> None:
        database = _ledger([("2026-01-21", "Ramen", "-3000"), ("2026-02-21", "Ramen", "-2000")], currency="JPY", home=False)
        _trip(database, "2026-01-20", "2026-01-23", "JP")
        _trip(database, "2026-02-20", "2026-02-23", "JP")
        _send_on_trips(database, "Ramen", "JP")
        estimate = baseline_estimate(database, today=TODAY)
        jpy = estimate["by_currency"]["JPY"]
        self.assertNotEqual(jpy["trip_allowance_monthly"], "0.00")
        self.assertFalse(jpy["trip_allowance_projected"])
        self.assertTrue(any("every JPY payment in the window was on a trip" in item for item in estimate["limitations"]))


if __name__ == "__main__":
    unittest.main()
