"""M4 step 12: the reported Cash workspace, ``reported_cash_page_from`` (§3.3).

Builds real ledgers through the Revolut import path, mirroring
``tests.test_money_flow``. The aggregate card is checked directly against
``position/reporting.py::cash_at_cutoff`` -- the same function
``money_flow.py``'s Overview card now calls -- so an Overview/Cash
disagreement on the same cutoff/context would fail here.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from fincl.db import connect_readonly, connect_sqlite
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.money_flow import money_flow_page_from
from fincl.position.reporting import (
    ReportedCashPage,
    ReportedCashPocket,
    _pocket_valuation,
    reported_cash_page_from,
)
from fincl.refresh import refresh_ledger
from fincl.reporting_context import (
    STALE_GENERATION_REFRESH_REQUIRED,
    ReportedScope,
    build_reporting_context,
)
from fincl.valuation import ConsolidationStatus
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_api import ApiTestBase

_TODAY = date(2026, 3, 6)

# One continuous statement: both currencies' last movement lands on the same
# day (Mar 5), so a common supported cutoff exists -- the "agreement with
# Overview" and "optional local filtering" cases need this.
_SAME_DAY_ROWS: list[list[str]] = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "360.00"],
    ["Topup", "Current", "2026-03-05 10:01:00", "2026-03-05 10:01:01",
     "USD seed", "200.00", "0.00", "USD", "COMPLETED", "200.00"],
]

# Two separate statements on the same account: one covers only SGD through
# Feb 5 (closing 860.00), the other covers only USD through Mar 5 (closing
# 200.00). Neither statement certifies the other currency's gap in between,
# so the two pockets have genuinely different native balance dates *and* no
# chain of evidence bridges them to one common cutoff.
_SGD_STATEMENT_ROWS: list[list[str]] = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
]
_USD_STATEMENT_ROWS: list[list[str]] = [
    ["Topup", "Current", "2026-03-05 10:01:00", "2026-03-05 10:01:01",
     "USD seed", "200.00", "0.00", "USD", "COMPLETED", "200.00"],
]

_ZERO_ROWS: list[list[str]] = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "100.00", "0.00", "SGD", "COMPLETED", "100.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Spend it all", "-100.00", "0.00", "SGD", "COMPLETED", "0.00"],
]


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _different_dates_ledger() -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    service = DocumentImportService.plaintext_phase2(database, root / "evidence")
    sgd_source = root / "revolut_sgd.csv"
    sgd_source.write_bytes(csv_bytes(_SGD_STATEMENT_ROWS))
    service.import_revolut_pdf(sgd_source)
    usd_source = root / "revolut_usd.csv"
    usd_source.write_bytes(csv_bytes(_USD_STATEMENT_ROWS))
    service.import_revolut_pdf(usd_source)
    refresh_ledger(database)
    return database


def _load_usd_rate(database: Path, *, period: str = "2026-02", rate: str = "1.5000") -> None:
    writer = connect_sqlite(database)
    try:
        load_fx_rates(
            writer,
            {
                "source": "test", "source_version": f"reported-cash-{period}", "quote_currency": "SGD",
                "rates": [{"base_currency": "USD", "period": period, "rate": rate}],
            },
        )
    finally:
        writer.close()
    refresh_ledger(database)


def _cash_page(database: Path, **kwargs: Any) -> ReportedCashPage:
    connection = connect_readonly(database)
    try:
        return reported_cash_page_from(connection, **kwargs)
    finally:
        connection.close()


def _pocket(page: ReportedCashPage, currency: str) -> ReportedCashPocket:
    return next(p for p in page.pockets if p.native["currency_pocket"] == currency)


class SameDayCommonCutoffTests(unittest.TestCase):
    """A common supported cutoff exists: the aggregate card can be AVAILABLE."""

    def setUp(self) -> None:
        self.database = _ledger(_SAME_DAY_ROWS)
        _load_usd_rate(self.database)

    def test_combined_aggregate_matches_overview_at_the_same_cutoff(self) -> None:
        page = _cash_page(self.database, today=_TODAY)
        connection = connect_readonly(self.database)
        try:
            overview = money_flow_page_from(
                connection, today=_TODAY, history_start="2026-01-01", history_end="2026-03-05",
            )
        finally:
            connection.close()

        self.assertEqual(page.combined.result.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(page.combined.result.status, overview.summary.cash_at_cutoff.result.status)
        self.assertEqual(page.combined.result.total, overview.summary.cash_at_cutoff.result.total)
        self.assertEqual(page.combined.balance_as_of, overview.summary.cash_at_cutoff.balance_as_of)
        # SGD 360.00 + USD 200.00 * 1.5000 = SGD 300.00 -> SGD 660.00.
        self.assertEqual(page.combined.result.total, Fraction(660))

    def test_optional_local_pocket_currency_filter_narrows_pockets_and_combined(self) -> None:
        page = _cash_page(self.database, today=_TODAY, pocket_currency="USD")

        self.assertEqual([p.native["currency_pocket"] for p in page.pockets], ["USD"])
        self.assertEqual(page.combined.result.total, Fraction(300))
        self.assertEqual(page.context.reporting_currency, "SGD")  # never changed by the filter

    def test_missing_fx_makes_the_aggregate_partial_not_missing_evidence(self) -> None:
        no_rate_database = _ledger(_SAME_DAY_ROWS)  # no USD rate loaded

        page = _cash_page(no_rate_database, today=_TODAY)

        sgd = _pocket(page, "SGD")
        usd = _pocket(page, "USD")
        self.assertIsNotNone(sgd.reported_balance)
        self.assertIsNone(sgd.reported_balance.unavailable)
        self.assertIsNotNone(usd.reported_balance)
        self.assertIsNotNone(usd.reported_balance.unavailable)
        self.assertEqual(usd.reported_balance.unavailable.reason, "RATE_NOT_IMPORTED")
        self.assertEqual(page.combined.result.status, ConsolidationStatus.PARTIAL)


class DifferentBalanceDatesTests(unittest.TestCase):
    """No chain of evidence bridges the two pockets: no fabricated same-date total."""

    def setUp(self) -> None:
        self.database = _different_dates_ledger()
        _load_usd_rate(self.database)

    def test_each_pocket_is_valued_at_its_own_native_as_of_date(self) -> None:
        page = _cash_page(self.database, today=_TODAY)

        sgd = _pocket(page, "SGD")
        usd = _pocket(page, "USD")
        self.assertEqual(sgd.native["as_of"], "2026-02-05")
        self.assertEqual(usd.native["as_of"], "2026-03-05")
        self.assertIsNotNone(sgd.reported_balance)
        self.assertIsNotNone(usd.reported_balance)
        self.assertEqual(sgd.reported_balance.context.valuation_as_of, date(2026, 2, 5))
        self.assertEqual(usd.reported_balance.context.valuation_as_of, date(2026, 3, 5))
        # SGD == reporting currency (default SGD): identity, no rate needed.
        self.assertEqual(sgd.reported_balance.reported_exact, Fraction(860))
        # USD valued at its own Mar-5 date under the loaded Mar rate.
        self.assertEqual(usd.reported_balance.reported_exact, Fraction(300))

    def test_no_common_cutoff_leaves_the_aggregate_unavailable_not_a_fabricated_sum(self) -> None:
        page = _cash_page(self.database, today=_TODAY)
        connection = connect_readonly(self.database)
        try:
            overview = money_flow_page_from(
                connection, today=_TODAY, history_start="2026-01-01", history_end="2026-03-05",
            )
        finally:
            connection.close()

        # SGD (860 as of Feb 5) and USD (200 as of Mar 5) never independently
        # sum to a purported same-date total: with no certified coverage
        # bridging the gap between them, neither pocket's derived balance is
        # supported, and the aggregate agrees with Overview that it is
        # unavailable -- never SGD 1160 fabricated from two different dates.
        self.assertEqual(page.combined.result.status, ConsolidationStatus.UNAVAILABLE)
        self.assertEqual(page.combined.result.status, overview.summary.cash_at_cutoff.result.status)
        self.assertIsNone(page.combined.result.total)
        self.assertIsNone(page.combined.balance_as_of)

        sgd = _pocket(page, "SGD")
        usd = _pocket(page, "USD")
        self.assertEqual(sgd.native["coverage_status"], "unsupported")
        self.assertEqual(usd.native["coverage_status"], "unsupported")
        self.assertIsNone(sgd.reported_derived_balance)
        self.assertIsNone(usd.reported_derived_balance)
        # Each pocket's own native-date balance is still separately reported.
        self.assertIsNotNone(sgd.reported_balance)
        self.assertIsNotNone(usd.reported_balance)


class MissingObservationTests(unittest.TestCase):
    """No native amount to value at all: missing balance evidence, not FX."""

    def test_pocket_valuation_returns_none_without_a_native_amount(self) -> None:
        database = _different_dates_ledger()
        connection = connect_readonly(database)
        try:
            context = build_reporting_context(connection, scope=ReportedScope(valuation_as_of=_TODAY))
            result = _pocket_valuation(
                connection, context=context, amount=None, currency="SGD", as_of=None,
            )
        finally:
            connection.close()
        self.assertIsNone(result)


class RealZeroBalanceTests(unittest.TestCase):
    def test_a_genuine_zero_balance_reports_as_zero_not_unavailable(self) -> None:
        database = _ledger(_ZERO_ROWS)

        page = _cash_page(database, today=date(2026, 2, 6))

        sgd = _pocket(page, "SGD")
        self.assertEqual(sgd.native["balance"], "0.00")
        self.assertIsNotNone(sgd.reported_balance)
        self.assertIsNone(sgd.reported_balance.unavailable)
        self.assertEqual(sgd.reported_balance.reported_exact, Fraction(0))


class StaleGenerationTests(unittest.TestCase):
    def test_a_manifest_changed_since_the_last_refresh_is_a_typed_refusal(self) -> None:
        database = _different_dates_ledger()
        writer = connect_sqlite(database)
        try:
            load_fx_rates(
                writer,
                {
                    "source": "test", "source_version": "reported-cash-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "EUR", "period": "2026-01", "rate": "0.7000"}],
                },
            )
        finally:
            writer.close()
        # Deliberately not refreshed, so the stored generation manifest cannot
        # be reproduced from a fresh recompute of this same database.
        with self.assertRaises(FinCLError) as caught:
            _cash_page(database, today=_TODAY)
        self.assertTrue(str(caught.exception).startswith(STALE_GENERATION_REFRESH_REQUIRED))


class ApiRouteTests(ApiTestBase):
    """Route wiring only -- backend behavior is covered above directly."""

    def setUp(self) -> None:
        super().setUp()
        self.bootstrap()

    def test_reported_cash_route_returns_the_native_and_reported_shape(self) -> None:
        response = self.client.get("/api/v1/reporting/cash")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("combined", body)
        self.assertIn("pockets", body)
        for pocket in body["pockets"]:
            self.assertIn("balance", pocket)
            self.assertIn("as_of", pocket)
            self.assertIn("reported_balance", pocket)
            self.assertIn("reported_derived_balance", pocket)

    def test_stale_generation_is_a_409(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            load_fx_rates(
                connection,
                {
                    "source": "test", "source_version": "reported-cash-api-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.5000"}],
                },
            )
        finally:
            connection.close()
        response = self.client.get("/api/v1/reporting/cash")
        self.assertEqual(response.status_code, 409, response.text)
