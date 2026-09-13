"""M4 step 14: the reported Forecast workspace, ``reported_forecast_page_from``
(§3.3, §7 M4 extrema rule).

Two test styles:

* A pure unit test feeds a hand-built ``by_currency`` shape straight into
  ``_combined_minimum`` -- exact, hand-checkable amounts, no ledger import
  required -- to prove the combined minimum is aligned by calendar day, not a
  sum of each currency's own independently timed low.
* Ledger/API tests exercise ``reported_forecast_page_from``/the HTTP route
  end to end through the real Revolut import path, reusing
  ``tests.test_money_flow``'s fixtures so the cross-check against Overview's
  own forecast closings is against the exact same ledger shape.
"""

from __future__ import annotations

import unittest
from fractions import Fraction
from pathlib import Path

from fincl.db import connect_readonly, connect_sqlite
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.errors import FinCLError
from fincl.forecast.engine import cash_timeline_from
from fincl.forecast.reporting import (
    _combined_minimum,
    reported_forecast_page_from,
)
from fincl.fx import load_fx_rates
from fincl.money import ExactMoney
from fincl.money_flow import money_flow_page_from
from fincl.refresh import refresh_ledger
from fincl.reporting_context import (
    STALE_GENERATION_REFRESH_REQUIRED,
    ReportedScope,
    build_reporting_context,
)
from fincl.reporting_currency import REPORTING_CURRENCY_SET
from fincl.valuation import (
    ConsolidationStatus,
    ExactValuation,
    RateBasis,
    ValuationContext,
    ValuationPurpose,
)
from tests.test_api import ApiTestBase
from tests.test_money_flow import (
    _HISTORY_END,
    _HISTORY_START,
    _MIXED_ROWS,
    _TODAY,
    _ledger,
    _load_usd_rate,
    _mixed_ledger_with_rate,
)

_ANCHORS = {"ledger": "default"}


def _set_reporting_currency(database: Path, currency: str) -> None:
    append_decision(
        default_journal_path(database), decision_kind=REPORTING_CURRENCY_SET, anchors=_ANCHORS,
        payload={"currency": currency, "reason": "test"},
        policy_version="reporting-currency-v1", snapshot=currency,
    )
    connection = connect_sqlite(database)
    try:
        replay_journal(connection)
    finally:
        connection.close()
    refresh_ledger(database)


def _display_context(connection) -> ValuationContext:
    context = build_reporting_context(connection, scope=ReportedScope(valuation_as_of=_TODAY))
    return ValuationContext(
        reporting_currency=context.reporting_currency, purpose=ValuationPurpose.FORECAST,
        valuation_as_of=_TODAY, rate_selection_policy="forward-monthly-closed-v1",
    )


def _identity_basis(currency: str, *, context: ValuationContext) -> ExactValuation:
    return ExactValuation(
        native=ExactMoney.parse("0.00"), native_currency=currency, reported_exact=Fraction(0),
        context=context, basis=RateBasis.IDENTITY, rate_path=(), unavailable=None,
    )


class CombinedMinimumAlignmentTests(unittest.TestCase):
    """Pure: the combined minimum is aligned by calendar day, not summed
    from each currency's own independently timed lowest day."""

    def setUp(self) -> None:
        self.database = _ledger(_MIXED_ROWS)
        self.connection = connect_readonly(self.database)
        self.addCleanup(self.connection.close)
        self.context = _display_context(self.connection)
        self.bases = {
            "AAA": _identity_basis("AAA", context=self.context),
            "BBB": _identity_basis("BBB", context=self.context),
        }

    def test_combined_minimum_differs_from_the_sum_of_independent_minima(self) -> None:
        by_currency = {
            "AAA": {"available": True, "base": {"days": [
                {"date": "2026-03-06", "low_point": "100.00"},
                {"date": "2026-03-07", "low_point": "20.00"},
                {"date": "2026-03-08", "low_point": "90.00"},
            ]}},
            "BBB": {"available": True, "base": {"days": [
                {"date": "2026-03-06", "low_point": "100.00"},
                {"date": "2026-03-07", "low_point": "90.00"},
                {"date": "2026-03-08", "low_point": "10.00"},
            ]}},
        }
        minimum = _combined_minimum(
            by_currency, name="base", all_currencies=["AAA", "BBB"], bases=self.bases, display_context=self.context,
        )

        # AAA's own worst day is Mar 7 (20); BBB's own worst day is Mar 8
        # (10) -- summing those independently timed minima gives 30. But on
        # no single calendar day do both currencies sit at their own worst
        # point together: Mar 7 combined is 20+90=110, Mar 8 is 90+10=100.
        # The true (day-aligned) combined minimum is 100 on Mar 8 -- more
        # than triple the naive (wrong) sum of 30.
        self.assertEqual(minimum.result.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(minimum.result.total, Fraction(100))
        self.assertEqual(minimum.month, "2026-03")
        naive_sum_of_independent_minima = Fraction(20) + Fraction(10)
        self.assertNotEqual(minimum.result.total, naive_sum_of_independent_minima)

    def test_one_unusable_currency_makes_the_combined_minimum_unavailable(self) -> None:
        by_currency = {
            "AAA": {"available": True, "base": {"days": [{"date": "2026-03-06", "low_point": "100.00"}]}},
            "BBB": {"available": False, "reason": "no supported opening balance"},
        }
        bases = {"AAA": self.bases["AAA"]}  # BBB never priced: no native amount to value at all.

        minimum = _combined_minimum(
            by_currency, name="base", all_currencies=["AAA", "BBB"], bases=bases, display_context=self.context,
        )

        self.assertEqual(minimum.result.status, ConsolidationStatus.UNAVAILABLE)
        self.assertIsNone(minimum.result.total)
        self.assertIsNone(minimum.month)
        self.assertIn("NO_COMBINED_MINIMUM_EVIDENCE", minimum.result.unavailable_reasons)

    def test_no_currencies_at_all_is_unavailable_not_a_vacuous_zero(self) -> None:
        minimum = _combined_minimum({}, name="base", all_currencies=[], bases={}, display_context=self.context)
        self.assertEqual(minimum.result.status, ConsolidationStatus.UNAVAILABLE)


class LedgerIntegrationTests(unittest.TestCase):
    """SGD is forecastable; USD has no residual-spending evidence, so it is
    natively unavailable -- exactly the "one missing pocket" acceptance case."""

    def setUp(self) -> None:
        self.database = _mixed_ledger_with_rate()

    def test_only_the_forecastable_currency_gets_a_reported_cell(self) -> None:
        connection = connect_readonly(self.database)
        try:
            page = reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        self.assertEqual([c.currency for c in page.currencies], ["SGD"])

    def test_closings_match_overview_exactly(self) -> None:
        connection = connect_readonly(self.database)
        try:
            page = reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        connection = connect_readonly(self.database)
        try:
            overview = money_flow_page_from(
                connection, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END,
            )
        finally:
            connection.close()

        self.assertEqual(page.totals.base_closing.status, overview.summary.forecast_closing.base.status)
        self.assertEqual(page.totals.base_closing.total, overview.summary.forecast_closing.base.total)
        self.assertEqual(
            page.totals.conservative_closing.status, overview.summary.forecast_closing.conservative.status,
        )
        self.assertEqual(
            page.totals.conservative_closing.total, overview.summary.forecast_closing.conservative.total,
        )
        self.assertEqual(page.opening_balance_as_of, overview.summary.forecast_closing.opening_balance_as_of)
        self.assertEqual(page.horizon_end, overview.summary.forecast_closing.horizon_end)

    def test_combined_minimum_is_unavailable_because_usd_has_no_forecast(self) -> None:
        connection = connect_readonly(self.database)
        try:
            page = reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        self.assertEqual(page.totals.base_combined_minimum.result.status, ConsolidationStatus.UNAVAILABLE)
        self.assertEqual(page.totals.conservative_combined_minimum.result.status, ConsolidationStatus.UNAVAILABLE)
        # The closing total may still be a valid (partial) figure -- the
        # extrema rule withholds the minimum without hiding valid closings.
        self.assertIn(page.totals.base_closing.status, (ConsolidationStatus.PARTIAL, ConsolidationStatus.AVAILABLE))

    def test_identity_currency_reports_the_exact_native_amount(self) -> None:
        connection = connect_readonly(self.database)
        try:
            page = reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        sgd = next(c for c in page.currencies if c.currency == "SGD")
        self.assertEqual(sgd.starting_balance.basis, RateBasis.IDENTITY)
        self.assertEqual(sgd.starting_balance.reported_exact, Fraction(360))
        # Every base-case month's reported closing equals its native closing
        # exactly (identity conversion, reporting currency already SGD).
        native_cell = page.native["by_currency"]["SGD"]["base"]["months"]
        for reported_row, native_row in zip(sgd.base.months, native_cell, strict=True):
            native_money = ExactMoney.parse(native_row["closing"])
            expected = Fraction(native_money.coefficient, 10 ** native_money.scale)
            self.assertEqual(reported_row.closing.reported_exact, expected)

    def test_native_detail_is_unchanged_by_the_reported_reader(self) -> None:
        connection = connect_readonly(self.database)
        try:
            page = reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        connection = connect_readonly(self.database)
        try:
            direct_native = cash_timeline_from(connection, today=_TODAY)
        finally:
            connection.close()
        self.assertEqual(page.native, direct_native)

    def test_history_navigation_leaves_the_horizon_unchanged(self) -> None:
        connection = connect_readonly(self.database)
        try:
            narrow = money_flow_page_from(
                connection, today=_TODAY, history_start="2026-03-01", history_end="2026-03-05",
            )
        finally:
            connection.close()
        connection = connect_readonly(self.database)
        try:
            wide = money_flow_page_from(
                connection, today=_TODAY, history_start="2025-01-01", history_end=_HISTORY_END,
            )
        finally:
            connection.close()
        connection = connect_readonly(self.database)
        try:
            page = reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()

        self.assertEqual(narrow.summary.forecast_closing.horizon_end, wide.summary.forecast_closing.horizon_end)
        self.assertEqual(narrow.summary.forecast_closing.opening_balance_as_of, wide.summary.forecast_closing.opening_balance_as_of)
        self.assertEqual(page.horizon_end, wide.summary.forecast_closing.horizon_end)
        self.assertEqual(page.opening_balance_as_of, wide.summary.forecast_closing.opening_balance_as_of)


class HeldRateVisibilityTests(unittest.TestCase):
    def test_a_converted_currency_exposes_its_held_rate_and_date(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_usd_rate(database, period="2026-02", rate="1.5000")
        _set_reporting_currency(database, "USD")  # SGD now needs conversion.

        connection = connect_readonly(database)
        try:
            page = reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()

        self.assertEqual(page.context.reporting_currency, "USD")
        sgd = next(c for c in page.currencies if c.currency == "SGD")
        self.assertNotEqual(sgd.starting_balance.basis, RateBasis.IDENTITY)
        self.assertEqual(len(sgd.starting_balance.rate_path), 1)
        self.assertEqual(page.valuation_as_of, _TODAY)
        # SGD 360.00 / 1.5000 = USD 240.00 (inverse of the stored USD->SGD rate).
        self.assertEqual(sgd.starting_balance.reported_exact, Fraction(240))


class StaleGenerationTests(unittest.TestCase):
    def test_a_manifest_changed_since_the_last_refresh_is_a_typed_refusal(self) -> None:
        database = _ledger(_MIXED_ROWS)
        writer = connect_sqlite(database)
        try:
            load_fx_rates(
                writer,
                {
                    "source": "test", "source_version": "reported-forecast-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "EUR", "period": "2026-01", "rate": "0.7000"}],
                },
            )
        finally:
            writer.close()
        connection = connect_readonly(database)
        try:
            with self.assertRaises(FinCLError) as caught:
                reported_forecast_page_from(connection, today=_TODAY)
        finally:
            connection.close()
        self.assertTrue(str(caught.exception).startswith(STALE_GENERATION_REFRESH_REQUIRED))


class ApiRouteTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.bootstrap()

    def test_reported_forecast_route_returns_the_shape(self) -> None:
        response = self.client.get("/api/v1/reporting/forecast")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("totals", body)
        self.assertIn("currencies", body)
        self.assertIn("native", body)
        for key in ("base_closing", "conservative_closing", "base_combined_minimum", "conservative_combined_minimum"):
            self.assertIn(key, body["totals"])

    def test_stale_generation_is_a_409(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            load_fx_rates(
                connection,
                {
                    "source": "test", "source_version": "reported-forecast-api-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.5000"}],
                },
            )
        finally:
            connection.close()
        response = self.client.get("/api/v1/reporting/forecast")
        self.assertEqual(response.status_code, 409, response.text)
