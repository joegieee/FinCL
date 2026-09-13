"""M4 step 15: the reported Income (Inflows) workspace,
``reported_income_candidates_page_from`` (§3.1, §7 M4).

Builds real ledgers through the Revolut import path, reusing
``tests.test_obligations``'s own "Acme Payroll" monthly-income fixture
pattern so income-candidate detection (3+ similar receipts, a stable
cadence) fires exactly as it does for that module's own tests.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from fincl.db import connect_readonly, connect_sqlite
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.forecast.reporting import reported_forecast_page_from
from fincl.fx import load_fx_rates
from fincl.money_flow import money_flow_page_from
from fincl.planning.queries import recurring_page_from
from fincl.planning.reporting import (
    ReportedOccurrence,
    _occurrence_valuation,
    _reported_candidate,
    reported_income_candidates_page_from,
)
from fincl.refresh import refresh_ledger
from fincl.reporting_context import STALE_GENERATION_REFRESH_REQUIRED
from fincl.valuation import ValuationContext, ValuationPurpose
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_api import ApiTestBase

_TODAY = date(2026, 5, 1)


def _mixed_rows() -> list[list[str]]:
    """Four months of an SGD and a USD monthly payer, 25th of each month --
    enough sources (>= 3) and a stable ~30-day cadence for the detector."""
    rows = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "50000.00", "0.00", "SGD", "COMPLETED", "50000.00"],
        ["Topup", "Current", "2026-01-01 09:01:00", "2026-01-01 09:01:01",
         "Seed USD", "5000.00", "0.00", "USD", "COMPLETED", "5000.00"],
    ]
    sgd_balance = Decimal("50000.00")
    usd_balance = Decimal("5000.00")
    for month in ("01", "02", "03", "04"):
        sgd_balance += Decimal("5000.00")
        rows.append([
            "Topup", "Current", f"2026-{month}-25 10:00:00", f"2026-{month}-25 10:00:01",
            "Acme Payroll", "5000.00", "0.00", "SGD", "COMPLETED", f"{sgd_balance}",
        ])
        usd_balance += Decimal("2000.00")
        rows.append([
            "Topup", "Current", f"2026-{month}-25 11:00:00", f"2026-{month}-25 11:00:01",
            "Acme Payroll", "2000.00", "0.00", "USD", "COMPLETED", f"{usd_balance}",
        ])
    return rows


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _load_usd_rate(database: Path, *, periods: tuple[str, ...] = ("2026-01", "2026-02", "2026-03", "2026-04"), rate: str = "1.5000") -> None:
    writer = connect_sqlite(database)
    try:
        load_fx_rates(
            writer,
            {
                "source": "test", "source_version": f"reported-income-{periods[0]}", "quote_currency": "SGD",
                "rates": [{"base_currency": "USD", "period": period, "rate": rate} for period in periods],
            },
        )
    finally:
        writer.close()
    refresh_ledger(database)


def _candidate(page: object, currency: str) -> object:
    return next(c for c in page.candidates if c.native["currency"] == currency)  # type: ignore[attr-defined]


class MixedCurrencyPayerGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger(_mixed_rows())
        _load_usd_rate(self.database)

    def test_each_currency_forms_its_own_payer_group_with_reported_amounts(self) -> None:
        connection = connect_readonly(self.database)
        try:
            page = reported_income_candidates_page_from(connection, today=_TODAY)
        finally:
            connection.close()

        self.assertEqual(sorted(c.native["currency"] for c in page.candidates), ["SGD", "USD"])
        sgd = _candidate(page, "SGD")
        usd = _candidate(page, "USD")
        self.assertEqual(sgd.native["merchant_key"], "Acme Payroll")
        # SGD is identity (default reporting currency); USD converts at 1.5000.
        self.assertEqual(sgd.variation.latest.reported_exact, Fraction(5000))
        self.assertEqual(usd.variation.latest.reported_exact, Fraction(3000))
        self.assertEqual(usd.variation.priced_count, usd.variation.total_count)

    def test_group_rank_only_after_valuation_min_max_mean_from_priced_amounts(self) -> None:
        connection = connect_readonly(self.database)
        try:
            page = reported_income_candidates_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        usd = _candidate(page, "USD")
        # All four USD receipts are 2000.00 -> reported 3000 each; min==max==mean.
        self.assertEqual(usd.variation.minimum.reported_exact, Fraction(3000))
        self.assertEqual(usd.variation.maximum.reported_exact, Fraction(3000))
        self.assertEqual(usd.variation.mean, Fraction(3000))


class MissingFxStableCountsTests(unittest.TestCase):
    def test_missing_usd_rate_keeps_the_candidate_and_its_occurrence_count(self) -> None:
        database = _ledger(_mixed_rows())  # no USD rate loaded

        connection = connect_readonly(database)
        try:
            page = reported_income_candidates_page_from(connection, today=_TODAY)
        finally:
            connection.close()

        self.assertEqual(sorted(c.native["currency"] for c in page.candidates), ["SGD", "USD"])
        usd = _candidate(page, "USD")
        sgd = _candidate(page, "SGD")
        self.assertEqual(usd.variation.total_count, 4)
        self.assertEqual(usd.variation.priced_count, 0)
        self.assertIsNone(usd.variation.minimum)
        self.assertIsNone(usd.variation.mean)
        for occurrence in usd.occurrences:
            self.assertIsNotNone(occurrence.reported)
            self.assertIsNotNone(occurrence.reported.unavailable)
        self.assertEqual(sgd.variation.priced_count, sgd.variation.total_count)


class UnknownDateTests(unittest.TestCase):
    def test_an_occurrence_with_no_date_values_to_none_without_dropping_it(self) -> None:
        database = _ledger(_mixed_rows())
        connection = connect_readonly(database)
        try:
            context = ValuationContext(
                reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
                valuation_as_of=_TODAY, rate_selection_policy="flow-rate-v1",
            )
            result = _occurrence_valuation(
                connection, context=context, amount="100.00", currency="SGD", occurrence_date=None,
            )
        finally:
            connection.close()
        self.assertIsNone(result)

    def test_a_candidate_with_one_undated_occurrence_still_counts_it(self) -> None:
        database = _ledger(_mixed_rows())
        connection = connect_readonly(database)
        try:
            context = ValuationContext(
                reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
                valuation_as_of=_TODAY, rate_selection_policy="flow-rate-v1",
            )
            candidate = {
                "candidate": "synthetic", "currency": "SGD",
                "occurrences": [
                    {"anchor": "a1", "date": "2026-01-25", "amount": "100.00"},
                    {"anchor": "a2", "date": None, "amount": "100.00"},
                ],
            }
            reported = _reported_candidate(connection, candidate, context=context)
        finally:
            connection.close()
        self.assertEqual(reported.variation.total_count, 2)
        self.assertEqual(reported.variation.priced_count, 1)
        undated = next(o for o in reported.occurrences if o.native["anchor"] == "a2")
        self.assertIsInstance(undated, ReportedOccurrence)
        self.assertIsNone(undated.reported)


class QueueScopeIndependentOfHistoryTests(unittest.TestCase):
    def test_different_valuation_dates_leave_the_candidate_queue_unchanged(self) -> None:
        database = _ledger(_mixed_rows())
        _load_usd_rate(database)
        connection = connect_readonly(database)
        try:
            early = reported_income_candidates_page_from(connection, today=date(2026, 4, 26))
        finally:
            connection.close()
        connection = connect_readonly(database)
        try:
            late = reported_income_candidates_page_from(connection, today=date(2026, 12, 31))
        finally:
            connection.close()

        early_ids = sorted(c.native["candidate"] for c in early.candidates)
        late_ids = sorted(c.native["candidate"] for c in late.candidates)
        self.assertEqual(early_ids, late_ids)
        self.assertEqual(
            [o.native["anchor"] for c in early.candidates for o in c.occurrences],
            [o.native["anchor"] for c in late.candidates for o in c.occurrences],
        )

    def test_overviews_history_range_does_not_affect_the_income_queue(self) -> None:
        database = _ledger(_mixed_rows())
        _load_usd_rate(database)
        connection = connect_readonly(database)
        try:
            money_flow_page_from(
                connection, today=_TODAY, history_start="2026-01-01", history_end="2026-01-31",
            )
        except FinCLError:
            pass  # Overview may be partial/unavailable on this fixture; irrelevant here.
        finally:
            connection.close()
        connection = connect_readonly(database)
        try:
            narrow_income = reported_income_candidates_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        connection = connect_readonly(database)
        try:
            money_flow_page_from(
                connection, today=_TODAY, history_start="2020-01-01", history_end="2026-04-30",
            )
        except FinCLError:
            pass
        finally:
            connection.close()
        connection = connect_readonly(database)
        try:
            wide_income = reported_income_candidates_page_from(connection, today=_TODAY)
        finally:
            connection.close()

        self.assertEqual(
            sorted(c.native["candidate"] for c in narrow_income.candidates),
            sorted(c.native["candidate"] for c in wide_income.candidates),
        )


class NativeIdentityUnchangedTests(unittest.TestCase):
    """Confidence/classification/rule payloads stay exactly native; nothing
    here promotes a candidate to confirmed income."""

    def setUp(self) -> None:
        self.database = _ledger(_mixed_rows())
        _load_usd_rate(self.database)

    def test_candidates_stay_likely_never_rewritten_to_confirmed(self) -> None:
        connection = connect_readonly(self.database)
        try:
            page = reported_income_candidates_page_from(connection, today=_TODAY)
            direct = recurring_page_from(connection)
        finally:
            connection.close()
        for candidate in page.candidates:
            self.assertEqual(candidate.native["confidence"], "likely")
            self.assertFalse(candidate.native["auto_confirmable"])
        self.assertEqual(page.native["candidates"], direct["income_candidates"])
        self.assertEqual(page.native["exclusions"], direct["income_exclusions"])

    def test_no_write_occurs_reading_the_reported_page(self) -> None:
        def entry_count() -> int:
            connection = connect_sqlite(self.database)
            try:
                return connection.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
            finally:
                connection.close()

        before = entry_count()
        connection = connect_readonly(self.database)
        try:
            # A read-only connection physically cannot write; reading the
            # reported page through it, then confirming the entry count is
            # byte-identical, proves no write occurred either way.
            reported_income_candidates_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        self.assertEqual(before, entry_count())


class ReceivedVersusFutureValuationPurposeTests(unittest.TestCase):
    """Income candidate receipts are dated FLOW facts; the *same* candidate's
    forecast-modelled future occurrences (in the reported Forecast workspace,
    step 14) are FORECAST facts -- distinct purposes for the same identity."""

    def test_income_uses_flow_while_the_forecasts_own_event_uses_forecast(self) -> None:
        database = _ledger(_mixed_rows())
        _load_usd_rate(database)
        connection = connect_readonly(database)
        try:
            income = reported_income_candidates_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        sgd = _candidate(income, "SGD")
        for occurrence in sgd.occurrences:
            self.assertIsNotNone(occurrence.reported)
            self.assertEqual(occurrence.reported.context.purpose, ValuationPurpose.FLOW)

        connection = connect_readonly(database)
        try:
            forecast = reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        sgd_forecast = next(c for c in forecast.currencies if c.currency == "SGD")
        matching = [
            event for event in sgd_forecast.base.events
            if event.source_id.startswith(str(sgd.native["candidate"]))
        ]
        # The same candidate identity genuinely produces forecast-modelled
        # future occurrences -- this is not a vacuous check.
        self.assertGreater(len(matching), 0)
        for event in matching:
            self.assertEqual(event.amount.context.purpose, ValuationPurpose.FORECAST)


class StaleGenerationTests(unittest.TestCase):
    def test_a_manifest_changed_since_the_last_refresh_is_a_typed_refusal(self) -> None:
        database = _ledger(_mixed_rows())
        writer = connect_sqlite(database)
        try:
            load_fx_rates(
                writer,
                {
                    "source": "test", "source_version": "reported-income-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "EUR", "period": "2026-01", "rate": "0.7000"}],
                },
            )
        finally:
            writer.close()
        connection = connect_readonly(database)
        try:
            with self.assertRaises(FinCLError) as caught:
                reported_income_candidates_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        self.assertTrue(str(caught.exception).startswith(STALE_GENERATION_REFRESH_REQUIRED))


class ApiRouteTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.bootstrap()

    def test_reported_income_route_returns_the_shape(self) -> None:
        response = self.client.get("/api/v1/reporting/inflows/candidates")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("candidates", body)
        self.assertIn("native", body)
        self.assertIn("candidates", body["native"])

    def test_stale_generation_is_a_409(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            load_fx_rates(
                connection,
                {
                    "source": "test", "source_version": "reported-income-api-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.5000"}],
                },
            )
        finally:
            connection.close()
        response = self.client.get("/api/v1/reporting/inflows/candidates")
        self.assertEqual(response.status_code, 409, response.text)
