"""Stage 5A — immutable forecast snapshots and forecast-versus-actual variance.

The arithmetic tests exercise :mod:`fincl.variance.models` directly so a
regression names the rule it broke rather than the fixture that exposed it. The
comparison tests then run against real ledgers built through the Revolut import
path, because the four failures worth catching are all failures of *evidence*
handling — a delayed receipt read as a lost one, a card purchase read as its own
settlement, an uncovered month read as a zero, and a saved forecast quietly
rewritten by a later recalculation — and none of them are visible without a real
ledger's coverage, channels and generation behind them.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from fincl.backup import backup_ledger
from fincl.db import SCHEMA_VERSION, connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import KNOWN_DECISION_KINDS, replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.forecast.engine import COLUMNS as ENGINE_COLUMNS
from fincl.forecast.engine import ENGINE_VERSION
from fincl.migrations import apply_pending_migrations
from fincl.refresh import refresh_ledger
from fincl.variance import (
    FORECAST_MATCH_RETIRE,
    FORECAST_MATCH_SET,
    FORECAST_SNAPSHOT_ISSUE,
)
from fincl.variance.actuals import ROLE_AMOUNT, cash_movements_page, movement_id
from fincl.variance.models import (
    EVENT_AWAITED,
    EVENT_MATCHED,
    EVENT_MISSING,
    EVENT_PARTIALLY_MATCHED,
    EVENT_UNRESOLVED,
    FLOW_CLASSES,
    FLOW_DEBT_CARD,
    FLOW_INCOME,
    FLOW_LIFESTYLE,
    PERIOD_SCORED,
    PERIOD_UNRESOLVED_COVERAGE,
    PERIOD_UNRESOLVED_FUTURE,
    SNAPSHOT_CONTRACT_VERSION,
    realization,
    snapshot_digest,
    variance_reading,
)
from fincl.variance.queries import forecast_variance_page
from fincl.variance.snapshots import snapshot_payload, stored_snapshot
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

# --- ledger fixtures --------------------------------------------------------

# Salary on the 1st, groceries on the 5th, rent on the 10th. March is the
# interesting month: its rent never leaves, and its salary lands on 3 April.
_CONTIGUOUS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "ACME PAYROLL", "3000.00", "0.00", "SGD", "COMPLETED", "3000.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
     "Groceries", "-800.00", "0.00", "SGD", "COMPLETED", "2200.00"],
    ["Transfer", "Current", "2026-01-10 10:00:00", "2026-01-10 10:00:01",
     "LANDLORD RENT", "-1200.00", "0.00", "SGD", "COMPLETED", "1000.00"],
    ["Topup", "Current", "2026-02-01 10:00:00", "2026-02-01 10:00:01",
     "ACME PAYROLL", "3000.00", "0.00", "SGD", "COMPLETED", "4000.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-800.00", "0.00", "SGD", "COMPLETED", "3200.00"],
    ["Transfer", "Current", "2026-02-10 10:00:00", "2026-02-10 10:00:01",
     "LANDLORD RENT", "-1200.00", "0.00", "SGD", "COMPLETED", "2000.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Groceries", "-800.00", "0.00", "SGD", "COMPLETED", "1200.00"],
    ["Transfer", "Current", "2026-03-20 10:00:00", "2026-03-20 10:00:01",
     "CARD REPAYMENT", "-250.00", "0.00", "SGD", "COMPLETED", "950.00"],
    ["Topup", "Current", "2026-04-03 10:00:00", "2026-04-03 10:00:01",
     "ACME PAYROLL", "3000.00", "0.00", "SGD", "COMPLETED", "3950.00"],
]

def _ledger(runs: list[list[list[str]]]) -> Path:
    """A ledger built from one Revolut export per entry in ``runs``.

    Two exports with a gap between them is how a genuine coverage hole is
    produced: each accepted run contributes its own observed span, and a month
    between them is covered by no evidence at all.
    """
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    service = DocumentImportService.plaintext_phase2(database, root / "evidence")
    for index, rows in enumerate(runs):
        source = root / f"revolut-{index}.csv"
        source.write_bytes(csv_bytes(rows))
        service.import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _movement_ids(database: Path, start: str, end: str) -> dict[str, str]:
    """``{"YYYY-MM-DD|LABEL": movement_id}`` for the fixture's cash movements."""
    page = cash_movements_page(database, currency="SGD", start=start, end=end)
    return {
        f"{item['on']}|{item['label']}": str(item["movement_id"])
        for item in page["movements"]
    }


# --- the hand-written snapshot ----------------------------------------------

_MONTHS = [
    ("2026-01", "2026-01-16", "2026-01-31", True),
    ("2026-02", "2026-02-01", "2026-02-28", False),
    ("2026-03", "2026-03-01", "2026-03-31", False),
    ("2026-04", "2026-04-01", "2026-04-30", False),
]

# (event_id, flow class, direction, date, window earliest, window latest,
#  amount, confidence, recurring salary)
_EVENTS = [
    ("salary:2026-02", FLOW_INCOME, "inflow", "2026-02-01", "2026-02-01",
     "2026-02-03", "3000.00", "likely", True),
    ("salary:2026-03", FLOW_INCOME, "inflow", "2026-03-01", "2026-03-01",
     "2026-03-03", "3200.00", "likely", True),
    ("salary:2026-04", FLOW_INCOME, "inflow", "2026-04-01", "2026-04-01",
     "2026-04-30", "3000.00", "likely", True),
    ("groceries:2026-02", FLOW_LIFESTYLE, "outflow", "2026-02-05", "2026-02-05",
     "2026-02-05", "700.00", "confirmed", False),
    ("rent:2026-02", FLOW_LIFESTYLE, "outflow", "2026-02-10", "2026-02-10",
     "2026-02-10", "1200.00", "confirmed", False),
    ("rent:2026-03", FLOW_LIFESTYLE, "outflow", "2026-03-10", "2026-03-10",
     "2026-03-10", "1200.00", "confirmed", False),
    ("card:2026-03", FLOW_DEBT_CARD, "outflow", "2026-03-20", "2026-03-20",
     "2026-03-20", "250.00", "confirmed", False),
]


def _document() -> dict[str, Any]:
    """A snapshot document stated outright, so the expected numbers are readable.

    Building it by hand rather than from a live timeline is deliberate: these
    tests are about what the comparison does with an issued forecast, and a
    forecast whose figures are visible in the test file is one whose variances
    can be checked by eye.
    """
    return {
        "contract_version": SNAPSHOT_CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "issued_at": "2026-01-15T00:00:00+00:00",
        "generation": "sha256:test-generation",
        "currency": "SGD",
        "scale": 2,
        "case": "base",
        "settings": {
            "likely_income_policy": "exclude",
            "strong_income_interruption": False,
        },
        "basis": {
            "supported_cash_cutoff": "2026-01-15",
            "manifest_as_of": "2026-01-15",
            "issued_as_of": "2026-01-15",
        },
        "horizon": {
            "start": "2026-01-15",
            "first_modelled_day": "2026-01-16",
            "end": "2026-04-30",
            "months": 4,
        },
        "reserve": {"available": True, "target": "1000.00", "reason": None},
        "opening_balance": "1000.00",
        "events": [
            {
                "event_id": event_id,
                "source_id": event_id,
                "flow_class": flow_class,
                "direction": direction,
                "on": on,
                "amount": amount,
                "label": event_id.split(":")[0].title(),
                "timing": {
                    "basis": "known" if earliest == latest else "inferred",
                    "earliest": earliest,
                    "latest": latest,
                    "precision": "day" if earliest == latest else "window",
                },
                "confidence": confidence,
                "recurring_salary": salary,
                "conditional": False,
                "condition": None,
            }
            for event_id, flow_class, direction, on, earliest, latest, amount, confidence, salary in _EVENTS
        ],
        "outputs": {
            "months": [
                {
                    "month": month,
                    "first_day": first,
                    "last_day": last,
                    "partial": partial,
                    "opening": "1000.00",
                    "closing": "1000.00",
                }
                for month, first, last, partial in _MONTHS
            ],
            "totals": {"income": "9200.00", "expense": "3350.00", "net": "5850.00"},
            "closing_balance": "6850.00",
            "lowest_balance": {"occurs": True, "on": "2026-01-31", "amount": "1000.00"},
            "first_zero": {"occurs": False, "on": None, "amount": None},
            "first_reserve_breach": {"available": True, "occurs": False},
        },
        "limitations": ["a stated snapshot fixture, not a live forecast"],
    }


def _append(
    database: Path, kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    append_decision(
        default_journal_path(database),
        decision_kind=kind,
        anchors=anchors,
        payload=payload,
        policy_version="stage-5a",
        snapshot="test",
    )


def _replay(database: Path) -> None:
    connection = connect_sqlite(database)
    try:
        initialize_schema(connection)
        replay_journal(connection)
    finally:
        connection.close()


def _issue(database: Path, snapshot: str = "snap-1") -> dict[str, Any]:
    document = _document()
    _append(
        database,
        FORECAST_SNAPSHOT_ISSUE,
        {"snapshot": snapshot},
        {
            "document": document,
            "digest": snapshot_digest(document),
            "reason": "issued for the Stage 5A comparison",
        },
    )
    return document


def _match(
    database: Path,
    *,
    match: str,
    event_id: str,
    movement: str,
    on: str,
    amount: str,
    kind: str = "full",
    snapshot: str = "snap-1",
) -> None:
    _append(
        database,
        FORECAST_MATCH_SET,
        {"snapshot": snapshot, "match": match},
        {
            "event_id": event_id,
            "movement_id": movement,
            "matched_on": on,
            "amount": amount,
            "match_kind": kind,
            "reason": "matched by the operator",
        },
    )


def _cell(month_row: dict[str, Any], flow_class: str) -> dict[str, Any]:
    return next(
        cell for cell in month_row["by_flow_class"] if cell["flow_class"] == flow_class
    )


def _month(page: dict[str, Any], month: str) -> dict[str, Any]:
    return next(row for row in page["months"] if row["month"] == month)


def _event(page: dict[str, Any], event_id: str) -> dict[str, Any]:
    return next(row for row in page["events"] if row["event_id"] == event_id)


# --- the arithmetic ---------------------------------------------------------


class VarianceSignTests(unittest.TestCase):
    """The same difference means opposite things for income and for expense."""

    def test_overspending_and_extra_income_are_never_the_same_result(self) -> None:
        spent_more = variance_reading(
            flow_class=FLOW_LIFESTYLE, expected_minor=100_000,
            actual_minor=120_000, scale=2,
        )
        earned_more = variance_reading(
            flow_class=FLOW_INCOME, expected_minor=100_000,
            actual_minor=120_000, scale=2,
        )

        self.assertEqual(spent_more["actual_minus_expected"], "200.00")
        self.assertEqual(earned_more["actual_minus_expected"], "200.00")
        # Identical differences, opposite cash effects.
        self.assertEqual(spent_more["cash_effect"], "-200.00")
        self.assertEqual(earned_more["cash_effect"], "200.00")
        self.assertFalse(spent_more["favourable"])
        self.assertTrue(earned_more["favourable"])
        self.assertIn("more left", spent_more["explanation"])
        self.assertIn("cash is behind", spent_more["explanation"])
        self.assertIn("more income arrived", earned_more["explanation"])
        self.assertIn("cash is ahead", earned_more["explanation"])

    def test_underspending_is_favourable_and_shortfall_income_is_not(self) -> None:
        underspent = variance_reading(
            flow_class=FLOW_LIFESTYLE, expected_minor=100_000,
            actual_minor=80_000, scale=2,
        )
        short = variance_reading(
            flow_class=FLOW_INCOME, expected_minor=100_000, actual_minor=80_000, scale=2
        )

        self.assertTrue(underspent["favourable"])
        self.assertEqual(underspent["cash_effect"], "200.00")
        self.assertFalse(short["favourable"])
        self.assertEqual(short["cash_effect"], "-200.00")

    def test_an_open_part_of_the_expected_amount_is_held_out_of_the_difference(self) -> None:
        cell = variance_reading(
            flow_class=FLOW_INCOME, expected_minor=500000, actual_minor=300000,
            scale=2, open_minor=200000,
        )

        self.assertEqual(cell["expected"], "5000.00")
        self.assertEqual(cell["open_expected"], "2000.00")
        self.assertEqual(cell["actual_minus_expected"], "0.00")
        self.assertIsNone(cell["favourable"])
        self.assertIn("2000.00 of the issued amount is still open", cell["explanation"])

    def test_an_unknown_actual_is_null_and_never_differenced_against_zero(self) -> None:
        cell = variance_reading(
            flow_class=FLOW_INCOME, expected_minor=300_000, actual_minor=None,
            scale=2, unresolved_reason="no evidence covers this month",
        )

        self.assertEqual(cell["expected"], "3000.00")
        self.assertIsNone(cell["actual"])
        self.assertIsNone(cell["actual_minus_expected"])
        self.assertIsNone(cell["cash_effect"])
        self.assertFalse(cell["resolved"])
        self.assertEqual(cell["explanation"], "no evidence covers this month")

    def test_a_flow_amount_is_a_magnitude(self) -> None:
        with self.assertRaisesRegex(ValueError, "magnitude"):
            variance_reading(
                flow_class=FLOW_INCOME, expected_minor=-1, actual_minor=0, scale=2
            )

    def test_the_flow_classes_are_the_stage_3b_timeline_columns(self) -> None:
        # Restated rather than imported (see fincl.variance.models); this is the
        # check that keeps the two in step.
        self.assertEqual(set(FLOW_CLASSES), set(ENGINE_COLUMNS))


class RealizationTests(unittest.TestCase):
    """A discount is only ever a suggestion, and only ever with its sample."""

    def test_a_discount_carries_its_sample_count_and_is_not_applied(self) -> None:
        summary = realization(
            realized=2, resolved=3, realized_minor=600_000,
            expected_minor=900_000, scale=2,
        )

        self.assertTrue(summary["available"])
        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["occurrence_rate"], "2/3")
        self.assertEqual(summary["suggested_discount"]["amount"], "3000.00")
        self.assertFalse(summary["auto_applied"])
        self.assertIn("never applied", summary["policy"])

    def test_no_resolved_occurrence_yields_no_rate_rather_than_zero(self) -> None:
        summary = realization(
            realized=0, resolved=0, realized_minor=0, expected_minor=0, scale=2
        )

        self.assertFalse(summary["available"])
        self.assertEqual(summary["sample_count"], 0)
        self.assertIsNone(summary["occurrence_rate"])
        self.assertIsNone(summary["suggested_discount"])

    def test_more_realized_than_resolved_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "more occurrences"):
            realization(
                realized=4, resolved=3, realized_minor=0, expected_minor=0, scale=2
            )


# --- immutability -----------------------------------------------------------


class SnapshotImmutabilityTests(unittest.TestCase):
    """An issued forecast is what it said, whatever later recalculation says."""

    def _live_ledger(self) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp())
        database = root / "fincl.sqlite3"
        service = DocumentImportService.plaintext_phase2(database, root / "evidence")
        rows, balance = [], 0.0
        for month in range(1, 7):
            balance += 3000
            rows.append(["Topup", "Current", f"2026-{month:02d}-01 10:00:00",
                         f"2026-{month:02d}-01 10:00:01", "ACME PAYROLL",
                         "3000.00", "0.00", "SGD", "COMPLETED", f"{balance:.2f}"])
            balance -= 1200
            rows.append(["Transfer", "Current", f"2026-{month:02d}-10 10:00:00",
                         f"2026-{month:02d}-10 10:00:01", "LANDLORD RENT",
                         "-1200.00", "0.00", "SGD", "COMPLETED", f"{balance:.2f}"])
        source = root / "revolut-0.csv"
        source.write_bytes(csv_bytes(rows))
        service.import_revolut_pdf(source)
        refresh_ledger(database)
        return root, database

    def _stored(self, database: Path, snapshot: str) -> tuple[str, str]:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT document_json, document_digest FROM forecast_snapshots "
                "WHERE snapshot = ?",
                (snapshot,),
            ).fetchone()
        finally:
            connection.close()
        return str(row["document_json"]), str(row["document_digest"])

    def test_changing_next_months_plan_leaves_the_snapshot_byte_for_byte(self) -> None:
        root, database = self._live_ledger()

        anchors, payload = snapshot_payload(
            database, snapshot="snap-june", currency="SGD",
            reason="the June plan", today=date(2026, 6, 15),
        )
        _append(database, FORECAST_SNAPSHOT_ISSUE, anchors, payload)
        _replay(database)
        issued_json, issued_digest = self._stored(database, "snap-june")
        issued_cutoff = payload["document"]["basis"]["supported_cash_cutoff"]

        # Next month's plan changes: two more months of evidence arrive, which
        # moves the cutoff, the horizon and every projected figure behind it.
        rows = [
            ["Topup", "Current", "2026-07-01 10:00:00", "2026-07-01 10:00:01",
             "ACME PAYROLL", "3500.00", "0.00", "SGD", "COMPLETED", "14300.00"],
            ["Transfer", "Current", "2026-07-10 10:00:00", "2026-07-10 10:00:01",
             "LANDLORD RENT", "-1500.00", "0.00", "SGD", "COMPLETED", "12800.00"],
        ]
        source = root / "revolut-1.csv"
        source.write_bytes(csv_bytes(rows))
        DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
        refresh_ledger(database)
        _replay(database)

        _, recomputed = snapshot_payload(
            database, snapshot="snap-august", currency="SGD",
            reason="the August plan", today=date(2026, 8, 15),
        )
        # The plan really did change...
        self.assertNotEqual(
            recomputed["document"]["basis"]["supported_cash_cutoff"], issued_cutoff
        )
        # ...and the saved snapshot did not move a byte.
        stored_json, stored_digest = self._stored(database, "snap-june")
        self.assertEqual(stored_json, issued_json)
        self.assertEqual(stored_digest, issued_digest)

        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            read_back = stored_snapshot(connection, "snap-june")
        finally:
            connection.close()
        self.assertTrue(read_back["digest_verified"])
        self.assertEqual(
            read_back["document"]["basis"]["supported_cash_cutoff"], issued_cutoff
        )

    def test_re_issuing_a_snapshot_id_is_refused_rather_than_merged(self) -> None:
        database = _ledger([_CONTIGUOUS])
        _issue(database)
        _replay(database)
        _issue(database)

        with self.assertRaisesRegex(FinCLError, "already been issued"):
            _replay(database)

    def test_a_digest_that_does_not_cover_the_document_is_refused(self) -> None:
        database = _ledger([_CONTIGUOUS])
        _append(
            database,
            FORECAST_SNAPSHOT_ISSUE,
            {"snapshot": "snap-bad"},
            {
                "document": _document(),
                "digest": "sha256:" + "0" * 64,
                "reason": "a digest that proves nothing",
            },
        )

        with self.assertRaisesRegex(FinCLError, "digest does not cover"):
            _replay(database)

    def test_the_kinds_are_in_the_closed_replay_vocabulary(self) -> None:
        for kind in (FORECAST_SNAPSHOT_ISSUE, FORECAST_MATCH_SET, FORECAST_MATCH_RETIRE):
            self.assertIn(kind, KNOWN_DECISION_KINDS)


# --- the comparison ---------------------------------------------------------


class ForecastVersusActualTests(unittest.TestCase):
    """One ledger and one issued snapshot, compared on 15 April 2026.

    March is where the interesting facts live: its salary arrived on 3 April,
    its rent never left at all, and its groceries were never forecast. The three
    must read as three different things.
    """

    AS_OF = "2026-04-15"

    def setUp(self) -> None:
        self.database = _ledger([_CONTIGUOUS])
        _issue(self.database)
        ids = _movement_ids(self.database, "2026-01-01", "2026-04-30")
        _match(self.database, match="m-feb-salary", event_id="salary:2026-02",
               movement=ids["2026-02-01|ACME PAYROLL"], on="2026-02-01",
               amount="3000.00")
        _match(self.database, match="m-feb-groceries", event_id="groceries:2026-02",
               movement=ids["2026-02-05|GROCERIES"], on="2026-02-05", amount="800.00")
        _match(self.database, match="m-feb-rent", event_id="rent:2026-02",
               movement=ids["2026-02-10|LANDLORD RENT"], on="2026-02-10",
               amount="1200.00")
        # The delayed one: March's salary, bound to the cash that arrived in April.
        _match(self.database, match="m-mar-salary", event_id="salary:2026-03",
               movement=ids["2026-04-03|ACME PAYROLL"], on="2026-04-03",
               amount="3000.00")
        _match(self.database, match="m-mar-card", event_id="card:2026-03",
               movement=ids["2026-03-20|CARD REPAYMENT"], on="2026-03-20",
               amount="250.00")
        _replay(self.database)
        self.page = forecast_variance_page(
            self.database, snapshot="snap-1", as_of=self.AS_OF
        )

    def test_a_delayed_receipt_is_not_a_permanently_missing_one(self) -> None:
        delayed = _event(self.page, "salary:2026-03")
        absent = _event(self.page, "rent:2026-03")

        # The salary is matched, and its lateness is stated as a timing shift.
        self.assertEqual(delayed["status"], EVENT_MATCHED)
        self.assertEqual(delayed["actual"], "3000.00")
        self.assertEqual(delayed["landed_months"], ["2026-04"])
        self.assertEqual(delayed["timing_difference"]["modelled_month"], "2026-03")
        self.assertEqual(delayed["timing_difference"]["landed_months"], ["2026-04"])
        self.assertIn("timing difference", delayed["timing_difference"]["note"])

        # The rent is a different answer entirely: the window closed inside
        # covered evidence and nothing moved.
        self.assertEqual(absent["status"], EVENT_MISSING)
        self.assertIsNone(absent["actual"])
        self.assertIsNone(absent["timing_difference"])
        self.assertIn("closed on 2026-03-10", absent["status_reason"])

        # And the month rows carry the shift both ways round, so the March
        # income shortfall is explained rather than merely reported.
        march = _month(self.page, "2026-03")
        april = _month(self.page, "2026-04")
        self.assertEqual(
            [item["event_id"] for item in march["timing"]["expected_here_landed_elsewhere"]],
            ["salary:2026-03"],
        )
        self.assertEqual(
            [item["event_id"] for item in april["timing"]["expected_elsewhere_landed_here"]],
            ["salary:2026-03"],
        )

    def test_an_open_window_reads_as_awaited_rather_than_missing(self) -> None:
        awaited = _event(self.page, "salary:2026-04")

        self.assertEqual(awaited["status"], EVENT_AWAITED)
        self.assertIsNone(awaited["actual"])
        self.assertIn("has not passed as of 2026-04-15", awaited["status_reason"])
        # An awaited event is not a miss and must never head the misses list.
        self.assertNotIn(
            "salary:2026-04", [item["event_id"] for item in self.page["biggest_misses"]]
        )

    def test_the_month_rows_state_expected_actual_and_the_sign(self) -> None:
        february = _month(self.page, "2026-02")
        self.assertEqual(february["period_status"], PERIOD_SCORED)

        income = _cell(february, FLOW_INCOME)
        self.assertEqual((income["expected"], income["actual"]), ("3000.00", "3000.00"))
        self.assertEqual(income["actual_minus_expected"], "0.00")

        lifestyle = _cell(february, FLOW_LIFESTYLE)
        # 700 groceries + 1200 rent forecast; 800 + 1200 actually left.
        self.assertEqual(lifestyle["expected"], "1900.00")
        self.assertEqual(lifestyle["actual"], "2000.00")
        self.assertEqual(lifestyle["actual_minus_expected"], "100.00")
        self.assertEqual(lifestyle["cash_effect"], "-100.00")
        self.assertFalse(lifestyle["favourable"])
        self.assertEqual(february["net_cash_effect"], "-100.00")

    def test_march_separates_a_timing_shortfall_from_an_unspent_cost(self) -> None:
        march = _month(self.page, "2026-03")

        income = _cell(march, FLOW_INCOME)
        self.assertEqual(income["expected"], "3200.00")
        self.assertEqual(income["actual"], "0.00")
        self.assertEqual(income["cash_effect"], "-3200.00")

        lifestyle = _cell(march, FLOW_LIFESTYLE)
        # The rent did not leave, so cash is *ahead* on this class.
        self.assertEqual((lifestyle["expected"], lifestyle["actual"]), ("1200.00", "0.00"))
        self.assertEqual(lifestyle["cash_effect"], "1200.00")
        self.assertTrue(lifestyle["favourable"])

        card = _cell(march, FLOW_DEBT_CARD)
        self.assertEqual((card["expected"], card["actual"]), ("250.00", "250.00"))

    def test_cash_nobody_matched_is_unclassified_not_folded_into_a_class(self) -> None:
        march = _month(self.page, "2026-03")

        # The March groceries were never forecast and never matched.
        self.assertEqual(march["unclassified_actual"]["payments"], "800.00")
        self.assertEqual(march["unclassified_actual"]["receipts"], "0.00")
        self.assertEqual(
            [item["label"] for item in march["unclassified_actual"]["items"]],
            ["GROCERIES"],
        )
        # ...and it did not silently inflate the lifestyle class.
        self.assertEqual(_cell(march, FLOW_LIFESTYLE)["actual"], "0.00")
        self.assertIn(
            "2026-03-05|GROCERIES",
            {f"{item['on']}|{item['label']}" for item in self.page["unmatched_movements"]},
        )

    def test_the_net_cash_effect_counts_cash_nobody_matched(self) -> None:
        march = _month(self.page, "2026-03")

        # 1050 left the bank against an issued net of +1750: rent never left,
        # the salary was late, and the groceries were never forecast.
        self.assertEqual(march["actual_cash"], {"receipts": "0.00", "payments": "1050.00"})
        self.assertEqual(march["net_cash_effect"], "-2800.00")

    def test_the_biggest_misses_rank_by_cash_effect_and_explain_themselves(self) -> None:
        misses = self.page["biggest_misses"]

        # Ranked by the size of the cash effect, whichever way it points: the
        # 1200 of rent that never left matters more than the 200 of salary that
        # arrived short, and a favourable surprise is still a miss.
        self.assertEqual(
            [item["event_id"] for item in misses],
            ["rent:2026-03", "salary:2026-03", "groceries:2026-02"],
        )
        rent, salary, groceries = misses
        self.assertEqual(rent["cash_effect"], "1200.00")
        self.assertTrue(rent["favourable"])
        self.assertEqual(rent["status"], EVENT_MISSING)
        self.assertEqual(salary["cash_effect"], "-200.00")
        self.assertFalse(salary["favourable"])
        self.assertEqual(groceries["cash_effect"], "-100.00")

    def test_likely_income_realization_states_its_sample_and_suggests_only(self) -> None:
        summary = self.page["likely_income_realization"]

        # Two of the three issued occurrences are resolved; the April one is
        # still awaited and is excluded from the sample rather than counted as
        # a failure.
        self.assertTrue(summary["available"])
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["issued_occurrences"], 3)
        self.assertEqual(summary["occurrence_rate"], "2/2")
        self.assertEqual(summary["expected_amount"], "6200.00")
        self.assertEqual(summary["realized_amount"], "6000.00")
        self.assertEqual(summary["suggested_discount"]["amount"], "200.00")
        self.assertFalse(summary["auto_applied"])
        self.assertEqual(
            [item["event_id"] for item in summary["observations"]],
            ["salary:2026-02", "salary:2026-03"],
        )
        self.assertEqual(
            [item["event_id"] for item in summary["excluded_from_sample"]],
            ["salary:2026-04"],
        )

    def test_a_month_that_has_not_elapsed_is_unresolved_not_zero(self) -> None:
        april = _month(self.page, "2026-04")

        self.assertEqual(april["period_status"], PERIOD_UNRESOLVED_FUTURE)
        self.assertFalse(april["scored"])
        self.assertIsNone(april["net_cash_effect"])
        for cell in april["by_flow_class"]:
            self.assertIsNone(cell["actual"])
            self.assertIsNone(cell["actual_minus_expected"])
            self.assertFalse(cell["resolved"])
        self.assertIsNone(april["unclassified_actual"]["payments"])
        self.assertEqual(self.page["scored_months"], ["2026-01", "2026-02", "2026-03"])

    def test_a_partial_match_leaves_the_remainder_visible(self) -> None:
        _match(self.database, match="m-mar-rent-part", event_id="rent:2026-03",
               movement=_movement_ids(self.database, "2026-03-01", "2026-03-31")[
                   "2026-03-05|GROCERIES"],
               on="2026-03-05", amount="500.00", kind="partial")
        _replay(self.database)
        page = forecast_variance_page(
            self.database, snapshot="snap-1", as_of=self.AS_OF
        )

        rent = _event(page, "rent:2026-03")
        self.assertEqual(rent["status"], EVENT_PARTIALLY_MATCHED)
        self.assertEqual(rent["actual"], "500.00")
        self.assertIn("remainder", rent["status_reason"])
        march = _month(page, "2026-03")
        self.assertEqual(_cell(march, FLOW_LIFESTYLE)["actual"], "500.00")
        # 500 of the 800 movement now carries a class; the other 300 does not
        # and stays visible rather than disappearing behind the matched part.
        self.assertEqual(march["unclassified_actual"]["payments"], "300.00")
        item = march["unclassified_actual"]["items"][0]
        self.assertEqual(
            (item["amount"], item["matched_amount"], item["unclassified_amount"]),
            ("800.00", "500.00", "300.00"),
        )

    def test_the_report_names_the_semantics_it_used(self) -> None:
        semantics = self.page["semantics"]

        self.assertIn("card purchase", semantics["cash_basis"])
        self.assertIn("bank payment that cleared it", semantics["transfers_and_repayments"])
        self.assertIn("nothing here infers a match", semantics["matching"])
        self.assertIn("never reported as a measured zero", semantics["never_zero"])
        self.assertIn("cash effect", semantics["sign"])


class UnmatchedMonthTests(unittest.TestCase):
    """Before anything is matched, the month still has to tell the truth."""

    def test_the_net_cash_effect_does_not_wait_for_matches(self) -> None:
        database = _ledger([_CONTIGUOUS])
        _issue(database)
        _replay(database)

        page = forecast_variance_page(database, snapshot="snap-1", as_of="2026-04-15")

        # In 3000, out 2000, against an issued 3000 in and 1900 out. Summing the
        # unmatched classes would have reported the issued net, negated.
        self.assertEqual(_month(page, "2026-02")["net_cash_effect"], "-100.00")


class OpenEventInClosedMonthTests(unittest.TestCase):
    """A month can close while an event issued in it can still be answered."""

    def setUp(self) -> None:
        self.database = _ledger([_CONTIGUOUS])
        document = _document()
        salary = next(
            item for item in document["events"] if item["event_id"] == "salary:2026-02"
        )
        salary["on"] = "2026-02-28"
        salary["timing"].update(
            earliest="2026-02-26", latest="2026-03-04", basis="inferred", precision="window"
        )
        _append(
            self.database,
            FORECAST_SNAPSHOT_ISSUE,
            {"snapshot": "snap-open"},
            {
                "document": document,
                "digest": snapshot_digest(document),
                "reason": "a salary window that crosses the month end",
            },
        )
        _replay(self.database)
        self.page = forecast_variance_page(
            self.database, snapshot="snap-open", as_of="2026-03-02"
        )
        self.february = _month(self.page, "2026-02")

    def test_a_class_with_only_open_events_has_no_answer_yet(self) -> None:
        self.assertEqual(_event(self.page, "salary:2026-02")["status"], EVENT_AWAITED)
        self.assertEqual(self.february["period_status"], PERIOD_SCORED)

        income = _cell(self.february, FLOW_INCOME)
        self.assertIsNone(income["actual"])
        self.assertIsNone(income["cash_effect"])
        self.assertIsNone(income["favourable"])
        self.assertFalse(income["resolved"])
        self.assertEqual(income["open_expected"], "3000.00")

    def test_the_answered_classes_are_still_scored(self) -> None:
        lifestyle = _cell(self.february, FLOW_LIFESTYLE)

        self.assertTrue(lifestyle["resolved"])
        self.assertEqual(lifestyle["open_expected"], "0.00")

    def test_the_month_says_how_much_of_its_net_is_still_open(self) -> None:
        self.assertEqual(
            self.february["open_expected"], {"receipts": "3000.00", "payments": "0.00"}
        )
        self.assertEqual(self.february["net_cash_effect"], "-100.00")


class UncoveredPeriodTests(unittest.TestCase):
    """A month with no evidence is unresolved, never a measured zero."""

    def setUp(self) -> None:
        # Two exports: one running to the end of February, one starting in
        # April. March is covered by no accepted evidence at all.
        through_february = [
            *_CONTIGUOUS[:6],
            ["Card Payment", "Current", "2026-02-28 10:00:00", "2026-02-28 10:00:01",
             "Coffee", "-5.00", "0.00", "SGD", "COMPLETED", "1995.00"],
        ]
        april = _CONTIGUOUS[8:]
        self.database = _ledger([through_february, april])
        _issue(self.database)
        _replay(self.database)
        self.page = forecast_variance_page(
            self.database, snapshot="snap-1", as_of="2026-05-10"
        )

    def test_the_uncovered_month_reports_null_actuals_and_says_why(self) -> None:
        march = _month(self.page, "2026-03")

        self.assertEqual(march["period_status"], PERIOD_UNRESOLVED_COVERAGE)
        self.assertFalse(march["scored"])
        self.assertIn("does not cover all of 2026-03-01..2026-03-31", march["period_reason"])
        for cell in march["by_flow_class"]:
            self.assertIsNone(cell["actual"])
            self.assertIsNone(cell["actual_minus_expected"])
        self.assertNotIn("2026-03", self.page["scored_months"])

    def test_an_event_in_the_gap_is_unresolved_rather_than_missing(self) -> None:
        rent = _event(self.page, "rent:2026-03")

        self.assertEqual(rent["status"], EVENT_UNRESOLVED)
        self.assertIsNone(rent["actual"])
        self.assertIn("does not cover", rent["status_reason"])
        self.assertNotIn(
            "rent:2026-03", [item["event_id"] for item in self.page["biggest_misses"]]
        )

    def test_the_covered_months_either_side_are_still_scored(self) -> None:
        self.assertEqual(_month(self.page, "2026-02")["period_status"], PERIOD_SCORED)
        self.assertFalse(self.page["coverage"]["covered"])
        self.assertEqual(self.page["coverage"]["status"], "GAPPED")
        self.assertTrue(self.page["coverage"]["uncovered"])


# --- cash basis -------------------------------------------------------------


def _evidence(name: str) -> str:
    return name.encode().hex().ljust(64, "0")[:64]


def _card_ledger(root: Path) -> Path:
    """A card purchase in March and the bank payment that settles it in April.

    Built directly against the schema: the point is the *channel*, and a card
    account with a statement fact behind it is the shortest honest way to state
    one.
    """
    database = root / "fincl.sqlite3"
    connection = connect_sqlite(database)
    try:
        initialize_schema(connection)
        for account, institution, source_type, name in (
            ("card-1", "Mari", "MARI_CARD", "Mari Card"),
            ("bank-1", "Revolut", "REVOLUT_CSV", "Revolut Current"),
        ):
            connection.execute(
                "INSERT INTO accounts(id, institution, source_type, display_name, "
                "masked_identifier) VALUES (?, ?, ?, ?, ?)",
                (account, institution, source_type, name, f"**{account[-1]}"),
            )
        for blob in ("stmt", "csv"):
            connection.execute(
                "INSERT INTO source_content(sha256, byte_size, media_type, evidence_key, "
                "storage_kind, original_path, last_observed_path, first_seen_at, "
                "last_seen_at, integrity_status) VALUES (?, 10, 'application/pdf', ?, "
                "'PLAINTEXT', ?, ?, '2026-04-10T00:00:00Z', '2026-04-10T00:00:00Z', "
                "'VERIFIED')",
                (_evidence(blob), f"key-{blob}", f"{blob}.pdf", f"{blob}.pdf"),
            )
        connection.execute(
            "INSERT INTO extraction_runs(id, evidence_sha256, source_profile_version, "
            "parser_version, extraction_policy_version, started_at, completed_at, "
            "status, selection_state) VALUES ('run-stmt', ?, 'mari-card-v1', 'p1', "
            "'x1', '2026-04-10T00:00:00Z', '2026-04-10T00:00:01Z', 'ACCEPTED', "
            "'SELECTED')",
            (_evidence("stmt"),),
        )
        connection.execute(
            "INSERT INTO statements(id, extraction_run_id, evidence_sha256, account_id, "
            "institution, source_type, statement_date, period_start, period_end, "
            "statement_identity, selection_state, conflict_state) "
            "VALUES ('stmt-a', 'run-stmt', ?, 'card-1', 'Mari', 'MARI_CARD', "
            "'2026-03-28', '2026-03-01', '2026-03-28', 'stmt-a', 'SELECTED', 'NONE')",
            (_evidence("stmt"),),
        )
        connection.execute(
            "INSERT INTO card_statement_facts(statement_id, extraction_run_id, "
            "currency_pocket, field_role, value_kind, value_text, value_coefficient, "
            "value_scale, source_locator, parser_version, role_version) "
            "VALUES ('stmt-a', 'run-stmt', 'SGD', 'payment_due', 'money', '250.00', "
            "25000, 2, 'p1', 'v1', 'r1')"
        )
        connection.execute(
            "INSERT INTO extraction_runs(id, evidence_sha256, source_profile_version, "
            "parser_version, extraction_policy_version, started_at, completed_at, "
            "status, selection_state) VALUES ('run-csv', ?, 'revolut-csv-v1', 'p2', "
            "'x1', '2026-04-10T00:00:00Z', '2026-04-10T00:00:01Z', 'ACCEPTED', "
            "'SELECTED')",
            (_evidence("csv"),),
        )
        # The purchase on the card, then the bank payment that clears it.
        activity = (
            ("card-1", "2026-03-15", "250.00", "Furniture shop", "CARD_PAYMENT"),
            ("bank-1", "2026-04-12", "250.00", "MARI CARD PAYMENT", "TRANSFER"),
        )
        for index, (account, day, amount, label, kind) in enumerate(activity):
            connection.execute(
                "INSERT INTO raw_records(id, extraction_run_id, evidence_sha256, "
                "source_profile_version, parser_version, header_sha256, source_anchor, "
                "extraction_ordinal, physical_line_start, physical_line_end, "
                "record_kind, raw_record_text, raw_cells_json, description_raw, "
                "confidence, required_fields_complete) "
                "VALUES (?, 'run-csv', ?, 'revolut-csv-v1', 'p2', 'h1', ?, ?, ?, ?, "
                "'TRANSACTION', ?, '[]', ?, 'HIGH', 1)",
                (f"raw-{index}", _evidence("csv"), f"anchor-{index}", index + 1,
                 index + 1, index + 1, label, label),
            )
            connection.execute(
                "INSERT INTO ledger_entries(id, extraction_run_id, statement_id, "
                "raw_record_id, document_record_id, account_id, source_family, "
                "natural_anchor, effective_at, effective_precision, currency_pocket, "
                "amount_text, amount_coefficient, amount_scale, economic_direction, "
                "event_kind, evidence_sha256, analytics_eligible, lifecycle_state) "
                "VALUES (?, 'run-csv', NULL, ?, NULL, ?, 'CSV', ?, ?, 'DAY', 'SGD', "
                "?, ?, 2, 'OUT', ?, ?, 1, 'COMPLETED')",
                (f"entry-{index}", f"raw-{index}", account, f"anchor-{index}",
                 f"{day}T10:00:00Z", amount, -int(float(amount) * 100), kind,
                 _evidence("csv")),
            )
        connection.commit()
    finally:
        connection.close()
    return database


class CashBasisTests(unittest.TestCase):
    """A card purchase is not the bank payment that later settles it."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.database = _card_ledger(self.root)
        self.page = cash_movements_page(
            self.database, currency="SGD", start="2026-03-01", end="2026-04-30"
        )

    def test_the_card_purchase_is_not_a_cash_movement(self) -> None:
        cash = {item["label"]: item for item in self.page["movements"]}
        excluded = {item["label"]: item for item in self.page["excluded_card_channel"]}

        self.assertNotIn("Furniture shop", cash)
        self.assertIn("Furniture shop", excluded)
        self.assertIn("moved a liability", excluded["Furniture shop"]["excluded_because"])
        self.assertEqual(excluded["Furniture shop"]["amount"], "250.00")

    def test_the_bank_payment_that_settles_it_is_the_cash_movement(self) -> None:
        cash = {item["label"]: item for item in self.page["movements"]}

        self.assertIn("MARI CARD PAYMENT", cash)
        payment = cash["MARI CARD PAYMENT"]
        self.assertEqual(payment["direction"], "outflow")
        self.assertEqual(payment["amount"], "250.00")
        self.assertEqual(payment["channel"], "bank")
        self.assertEqual(payment["on"], "2026-04-12")

    def test_the_two_are_never_the_same_movement_identity(self) -> None:
        # The single settlement the forecast modelled must be answerable by
        # exactly one of them, so their identities cannot collide.
        purchase = self.page["excluded_card_channel"][0]["movement_id"]
        payment = self.page["movements"][0]["movement_id"]
        self.assertNotEqual(purchase, payment)
        self.assertEqual(payment, movement_id("anchor-1", ROLE_AMOUNT))


# --- matching rules ---------------------------------------------------------


class MatchProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger([_CONTIGUOUS])
        _issue(self.database)
        _replay(self.database)
        self.movements = _movement_ids(self.database, "2026-02-01", "2026-02-28")

    def test_a_match_against_an_unissued_event_is_refused(self) -> None:
        _match(self.database, match="m-1", event_id="salary:2027-01",
               movement=self.movements["2026-02-01|ACME PAYROLL"],
               on="2026-02-01", amount="3000.00")

        with self.assertRaisesRegex(FinCLError, "issued no cash event"):
            _replay(self.database)

    def test_a_match_against_an_unknown_snapshot_is_refused(self) -> None:
        _match(self.database, match="m-1", event_id="salary:2026-02",
               movement=self.movements["2026-02-01|ACME PAYROLL"],
               on="2026-02-01", amount="3000.00", snapshot="snap-missing")

        with self.assertRaisesRegex(FinCLError, "unknown forecast snapshot"):
            _replay(self.database)

    def test_binding_one_movement_to_one_event_twice_is_refused(self) -> None:
        movement = self.movements["2026-02-01|ACME PAYROLL"]
        _match(self.database, match="m-1", event_id="salary:2026-02",
               movement=movement, on="2026-02-01", amount="1500.00", kind="partial")
        _match(self.database, match="m-2", event_id="salary:2026-02",
               movement=movement, on="2026-02-01", amount="1500.00", kind="partial")

        with self.assertRaisesRegex(FinCLError, "would count it twice"):
            _replay(self.database)

    def test_a_retired_match_frees_the_movement_and_leaves_no_actual(self) -> None:
        movement = self.movements["2026-02-01|ACME PAYROLL"]
        _match(self.database, match="m-1", event_id="salary:2026-02",
               movement=movement, on="2026-02-01", amount="3000.00")
        _append(self.database, FORECAST_MATCH_RETIRE,
                {"snapshot": "snap-1", "match": "m-1"}, {"reason": "matched in error"})
        # The same pair can be bound again once the first binding is retired.
        _match(self.database, match="m-2", event_id="salary:2026-02",
               movement=movement, on="2026-02-01", amount="2500.00", kind="partial")
        _replay(self.database)

        page = forecast_variance_page(
            self.database, snapshot="snap-1", as_of="2026-04-15"
        )
        salary = _event(page, "salary:2026-02")
        self.assertEqual(salary["status"], EVENT_PARTIALLY_MATCHED)
        self.assertEqual(salary["actual"], "2500.00")
        self.assertEqual([item["match"] for item in salary["matches"]], ["m-2"])

    def test_retiring_a_match_that_was_never_recorded_is_refused(self) -> None:
        _append(self.database, FORECAST_MATCH_RETIRE,
                {"snapshot": "snap-1", "match": "ghost"}, {"reason": "never existed"})

        with self.assertRaisesRegex(FinCLError, "retired before it was recorded"):
            _replay(self.database)


# --- migration --------------------------------------------------------------


class MatchSuggestionTests(unittest.TestCase):
    """The shortlist that makes the matching workflow usable.

    Nothing here binds anything: a suggestion is a ranked shortlist of movements
    that could answer an open event, and the operator still confirms each one.
    The ranking is published alongside so it can be disagreed with.
    """

    AS_OF = "2026-04-15"

    def setUp(self) -> None:
        self.database = _ledger([_CONTIGUOUS])
        _issue(self.database)
        _replay(self.database)
        self.page = forecast_variance_page(
            self.database, snapshot="snap-1", as_of=self.AS_OF
        )
        self.suggestions = {
            item["event_id"]: item for item in self.page["match_suggestions"]
        }

    def test_an_open_event_is_offered_candidates_of_its_own_direction(self) -> None:
        offered = self.suggestions["salary:2026-02"]

        self.assertTrue(offered["candidates"])
        for candidate in offered["candidates"]:
            with self.subTest(movement=candidate["movement_id"]):
                self.assertIn("amount_difference", candidate)
                self.assertIn("days_from_window", candidate)

    def test_the_exact_amount_inside_the_window_ranks_first(self) -> None:
        best = self.suggestions["salary:2026-02"]["candidates"][0]

        self.assertTrue(best["exact_amount"])
        self.assertEqual(best["days_from_window"], 0)
        self.assertEqual(best["match_kind"], "full")

    def test_a_full_suggestion_binds_the_whole_movement(self) -> None:
        """Capped at the 700 forecast, the 800 paid would score as an exact match."""
        best = self.suggestions["groceries:2026-02"]["candidates"][0]
        self.assertEqual((best["amount"], best["match_kind"]), ("800.00", "full"))
        self.assertEqual(best["suggested_amount"], "800.00")

        _match(self.database, match="m-groceries", event_id="groceries:2026-02",
               movement=best["movement_id"], on=best["on"],
               amount=best["suggested_amount"], kind=best["match_kind"])
        _replay(self.database)
        page = forecast_variance_page(self.database, snapshot="snap-1", as_of=self.AS_OF)

        self.assertEqual(_event(page, "groceries:2026-02")["actual"], "800.00")
        self.assertEqual(_cell(_month(page, "2026-02"), FLOW_LIFESTYLE)["actual"], "800.00")

    def test_a_late_movement_is_still_offered_with_its_distance_stated(self) -> None:
        """A late salary is exactly the case worth surfacing, not hiding."""
        offered = self.suggestions["salary:2026-03"]
        late = [c for c in offered["candidates"] if c["on"].startswith("2026-04")]

        self.assertTrue(late)
        self.assertGreater(late[0]["days_from_window"], 0)

    def test_a_suggestion_changes_no_status_and_no_figure(self) -> None:
        for event in self.page["events"]:
            with self.subTest(event=event["event_id"]):
                self.assertEqual(event["matches"], [])
                self.assertIsNone(event["actual"])

    def test_an_already_matched_event_is_not_offered_candidates(self) -> None:
        ids = _movement_ids(self.database, "2026-01-01", "2026-04-30")
        _match(self.database, match="m-feb-salary", event_id="salary:2026-02",
               movement=ids["2026-02-01|ACME PAYROLL"], on="2026-02-01",
               amount="3000.00")
        _replay(self.database)

        page = forecast_variance_page(self.database, snapshot="snap-1", as_of=self.AS_OF)

        self.assertNotIn(
            "salary:2026-02", {item["event_id"] for item in page["match_suggestions"]}
        )

    def test_a_bound_movement_is_never_offered_to_another_event(self) -> None:
        ids = _movement_ids(self.database, "2026-01-01", "2026-04-30")
        bound = ids["2026-02-01|ACME PAYROLL"]
        _match(self.database, match="m-feb-salary", event_id="salary:2026-02",
               movement=bound, on="2026-02-01", amount="3000.00")
        _replay(self.database)

        page = forecast_variance_page(self.database, snapshot="snap-1", as_of=self.AS_OF)

        offered = {
            candidate["movement_id"]
            for item in page["match_suggestions"]
            for candidate in item["candidates"]
        }
        self.assertNotIn(bound, offered)


class Schema22MigrationTests(unittest.TestCase):
    """Fresh init, upgrade and replay, each checked on its own.

    The comparison is the resulting *schema* and the *semantic projection* — the
    rows a replay produces — not the SQLite files themselves. Two byte-identical
    databases would be a stronger claim than the migration makes or needs: page
    layout, free lists and rowid allocation legitimately differ between a table
    created at init and one created by an upgrade.
    """

    def _schema(self, database: Path) -> set[tuple[str, str, str]]:
        connection = sqlite3.connect(database)
        try:
            return {
                (str(row[0]), str(row[1]), " ".join(str(row[2] or "").split()))
                for row in connection.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE name LIKE 'forecast_%'"
                )
            }
        finally:
            connection.close()

    def _projection(self, database: Path) -> tuple[list[Any], list[Any]]:
        connection = sqlite3.connect(database)
        try:
            snapshots = connection.execute(
                "SELECT snapshot, issued_at, document_json, document_digest, journal_seq "
                "FROM forecast_snapshots ORDER BY snapshot"
            ).fetchall()
            matches = connection.execute(
                "SELECT snapshot, match, event_id, movement_id, matched_on, "
                "amount_coefficient, amount_scale, match_kind, retired "
                "FROM forecast_actual_matches ORDER BY match"
            ).fetchall()
            return snapshots, matches
        finally:
            connection.close()

    def _seed(self, database: Path) -> None:
        _issue(database)
        _match(database, match="m-1", event_id="salary:2026-02",
               movement=_movement_ids(database, "2026-02-01", "2026-02-28")[
                   "2026-02-01|ACME PAYROLL"],
               on="2026-02-01", amount="3000.00")

    def test_a_fresh_ledger_initializes_at_schema_22(self) -> None:
        database = _ledger([_CONTIGUOUS])

        connection = sqlite3.connect(database)
        try:
            version = int(
                connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(
            {name for _, name, _ in self._schema(database)},
            {"forecast_snapshots", "forecast_actual_matches",
             "forecast_actual_matches_event"},
        )

    def test_an_upgraded_ledger_reaches_the_same_schema_as_a_fresh_one(self) -> None:
        fresh = _ledger([_CONTIGUOUS])
        upgraded = _ledger([_CONTIGUOUS])

        connection = sqlite3.connect(upgraded)
        try:
            connection.execute("DROP TABLE forecast_actual_matches")
            connection.execute("DROP TABLE forecast_snapshots")
            connection.execute(
                "UPDATE schema_metadata SET value = '21' WHERE key = 'schema_version'"
            )
            connection.commit()
        finally:
            connection.close()
        bundle = upgraded.parent / "bundle"
        backup_ledger(
            upgraded, upgraded.parent / "evidence", bundle, allow_stale_schema=True
        )
        result = apply_pending_migrations(upgraded, backup_dir=bundle)

        self.assertEqual((result.from_version, result.to_version), (21, SCHEMA_VERSION))
        self.assertEqual(result.steps, list(range(22, SCHEMA_VERSION + 1)))
        self.assertEqual(self._schema(upgraded), self._schema(fresh))

    def test_replay_projects_the_same_rows_on_a_fresh_and_an_upgraded_ledger(self) -> None:
        fresh = _ledger([_CONTIGUOUS])
        upgraded = _ledger([_CONTIGUOUS])

        connection = sqlite3.connect(upgraded)
        try:
            connection.execute("DROP TABLE forecast_actual_matches")
            connection.execute("DROP TABLE forecast_snapshots")
            connection.execute(
                "UPDATE schema_metadata SET value = '21' WHERE key = 'schema_version'"
            )
            connection.commit()
        finally:
            connection.close()
        bundle = upgraded.parent / "bundle"
        backup_ledger(
            upgraded, upgraded.parent / "evidence", bundle, allow_stale_schema=True
        )
        apply_pending_migrations(upgraded, backup_dir=bundle)

        # Independently of the upgrade: the same journal replays into the same
        # semantic projection on both.
        for database in (fresh, upgraded):
            self._seed(database)
            _replay(database)
        self.assertEqual(self._projection(fresh), self._projection(upgraded))
        snapshots, matches = self._projection(fresh)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(matches), 1)

    def test_a_migrated_ledger_carries_no_snapshot_until_one_is_issued(self) -> None:
        database = _ledger([_CONTIGUOUS])

        snapshots, matches = self._projection(database)
        self.assertEqual((snapshots, matches), ([], []))
        with self.assertRaisesRegex(FinCLError, "no forecast snapshot"):
            forecast_variance_page(database, snapshot="snap-1", as_of="2026-04-15")


if __name__ == "__main__":
    unittest.main()
