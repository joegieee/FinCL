"""M3b.3 step 09: the composite Overview API, ``money_flow_page_from`` (§3.3-3.4, §6.2).

Builds real ledgers through the Revolut import path, mirroring
``tests.test_analysis_reporting`` and ``tests.test_money_flow_seam``. Every
hand-computed figure below is checked directly against the owning readers
(``reported_analysis_page_from``, ``position_page_from``,
``cash_timeline_from``) so a disagreement between this composite and its
sources would fail here, not only in a screen.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from fractions import Fraction
from pathlib import Path

from fincl.analysis.reporting import reported_analysis_page_from
from fincl.db import connect_readonly, connect_sqlite
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.forecast.engine import cash_timeline_from
from fincl.fx import load_fx_rates
from fincl.generation import current_generation
from fincl.money_flow import money_flow_page, money_flow_page_from
from fincl.position.queries import position_page_from
from fincl.refresh import refresh_ledger
from fincl.reporting_context import STALE_GENERATION_REFRESH_REQUIRED
from fincl.valuation import ConsolidationStatus
from tests.money_flow_seam import build_ledger, count_sqlite_connections, publish_new_generation
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_api import ApiTestBase

_TODAY = date(2026, 3, 6)
_HISTORY_START = "2026-01-01"
_HISTORY_END = "2026-03-05"

# One continuous Revolut statement (Jan 1 - Mar 5): SGD seeded then spent on
# groceries/rent, plus a USD pocket that is topped up but never spent from --
# real evidence for "one supported pocket, one pocket with no residual
# -spending input at all" (§3.3's per-pocket forecast-readiness requirement),
# not a synthetic gap.
_MIXED_ROWS: list[list[str]] = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "360.00"],
    ["Topup", "Current", "2026-03-05 10:01:00", "2026-03-05 10:01:01",
     "USD seed", "200.00", "0.00", "USD", "COMPLETED", "200.00"],
]


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _load_usd_rate(database: Path, *, period: str = "2026-02", rate: str = "1.5000") -> None:
    writer = connect_sqlite(database)
    try:
        load_fx_rates(
            writer,
            {
                "source": "test", "source_version": f"money-flow-{period}", "quote_currency": "SGD",
                "rates": [{"base_currency": "USD", "period": period, "rate": rate}],
            },
        )
    finally:
        writer.close()
    # A rate import moves fx_row_inventory_digest; republish so the next
    # generation snapshot matches what build_reporting_context recomputes.
    refresh_ledger(database)


def _mixed_ledger_with_rate() -> Path:
    database = _ledger(_MIXED_ROWS)
    _load_usd_rate(database)
    return database


def _page(database: Path, **kwargs: object) -> object:
    connection = connect_readonly(database)
    try:
        return money_flow_page_from(connection, **kwargs)
    finally:
        connection.close()


class MixedPocketFourCardTests(unittest.TestCase):
    """Hand-computed values for all four cards on one mixed-pocket ledger."""

    def test_cash_at_cutoff_sums_both_pockets_at_the_common_cutoff(self) -> None:
        database = _mixed_ledger_with_rate()

        result = _page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        cash = result.summary.cash_at_cutoff
        # SGD 360.00 + USD 200.00 * 1.5000 = SGD 300.00 -> SGD 660.00.
        self.assertEqual(cash.result.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(cash.result.total, Fraction(660))
        self.assertEqual(cash.balance_as_of, "2026-03-05")
        self.assertEqual(cash.valuation_as_of, date(2026, 3, 5))

    def test_economic_flow_matches_hand_computed_net_in_out(self) -> None:
        database = _mixed_ledger_with_rate()

        result = _page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        summary = result.summary
        self.assertEqual(summary.outflow.current.total, Fraction(540))
        self.assertEqual(summary.inflow.current.total, Fraction(0))
        self.assertEqual(summary.net_economic_flow.current.total, Fraction(-540))
        self.assertEqual(summary.exceptional_adjustments.current.total, Fraction(0))

    def test_gross_burn_averages_the_two_covered_months(self) -> None:
        database = _mixed_ledger_with_rate()

        result = _page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        burn = result.summary.gross_burn
        self.assertEqual(burn.included_months, ("2026-01", "2026-02"))
        # (0 + 40.00) / 2 included months = 20.00.
        assert burn.result is not None
        self.assertEqual(burn.result.total, Fraction(20))

    def test_forecast_excludes_the_pocket_with_no_residual_evidence(self) -> None:
        database = _mixed_ledger_with_rate()

        result = _page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        forecast = result.summary.forecast_closing
        self.assertEqual(forecast.base.status, ConsolidationStatus.PARTIAL)
        self.assertEqual(forecast.conservative.status, ConsolidationStatus.PARTIAL)
        self.assertIn("USD", {scope.currency for scope in forecast.base.excluded})
        self.assertIn("NO_SUPPORTED_FORECAST_EVIDENCE", forecast.base.unavailable_reasons)
        # The evidence gap is disclosed as an *evidence* reason, never folded
        # into an FX-only reason vocabulary (§4.3).
        self.assertNotIn("RATE_NOT_IMPORTED", forecast.base.unavailable_reasons)

    def test_both_forecast_scenarios_stay_distinct(self) -> None:
        database = _mixed_ledger_with_rate()

        result = _page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        forecast = result.summary.forecast_closing
        self.assertIsNotNone(forecast.base.partial_total)
        self.assertIsNotNone(forecast.conservative.partial_total)
        self.assertNotEqual(forecast.base.partial_total, forecast.conservative.partial_total)

    def test_cash_and_forecast_expose_distinct_balance_and_valuation_dates(self) -> None:
        database = _mixed_ledger_with_rate()

        result = _page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        cash = result.summary.cash_at_cutoff
        forecast = result.summary.forecast_closing
        # Both share the same native opening date (the owner cutoff)...
        self.assertEqual(cash.balance_as_of, forecast.opening_balance_as_of)
        # ...but the cash card is valued at that cutoff while forecast rates
        # are held at the later request/issue date (§3.3).
        self.assertEqual(cash.valuation_as_of, date(2026, 3, 5))
        self.assertEqual(forecast.valuation_as_of, date(2026, 3, 6))
        self.assertNotEqual(cash.valuation_as_of, forecast.valuation_as_of)

    def test_first_modelled_day_is_the_day_after_the_owner_cutoff(self) -> None:
        database = _mixed_ledger_with_rate()

        result = _page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        self.assertEqual(result.supported_cash_cutoff, "2026-03-05")
        self.assertEqual(result.first_modelled_day, "2026-03-06")
        self.assertEqual(result.cash_stale_days, 1)
        self.assertEqual(result.horizon_end, "2026-09-05")


class OwningReaderEqualityTests(unittest.TestCase):
    """Every figure equals the same-context owning reader's own result."""

    def test_economic_flow_and_burn_equal_reported_analysis(self) -> None:
        database = _mixed_ledger_with_rate()
        connection = connect_readonly(database)
        try:
            analysis = reported_analysis_page_from(
                connection, as_of=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END,
            )
            flow = money_flow_page_from(
                connection, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END,
            )
        finally:
            connection.close()

        self.assertEqual(flow.summary.net_economic_flow.current.total, analysis.summary["net_economic_flow"].current.total)
        self.assertEqual(flow.summary.inflow.current.total, analysis.summary["inflow"].current.total)
        self.assertEqual(flow.summary.outflow.current.total, analysis.summary["outflow"].current.total)
        assert flow.summary.gross_burn.result is not None and analysis.gross_burn.result is not None
        self.assertEqual(flow.summary.gross_burn.result.total, analysis.gross_burn.result.total)
        self.assertEqual(flow.summary.gross_burn.included_months, analysis.gross_burn.included_months)

    def test_cash_at_cutoff_native_pockets_equal_position_page(self) -> None:
        database = _mixed_ledger_with_rate()
        connection = connect_readonly(database)
        try:
            position = position_page_from(connection, today=_TODAY)
            flow = money_flow_page_from(
                connection, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END,
            )
        finally:
            connection.close()

        self.assertEqual(flow.summary.cash_at_cutoff.balance_as_of, position["supported_cutoff"])
        # Reconstruct the same reporting total from position's own native
        # combined balances and compare exactly, not just "both AVAILABLE".
        sgd = Fraction(position["combined_liquid_position"]["SGD"])
        usd = Fraction(position["combined_liquid_position"]["USD"])
        self.assertEqual(flow.summary.cash_at_cutoff.result.total, sgd + usd * Fraction("1.5"))

    def test_forecast_closing_native_amount_equals_cash_timeline(self) -> None:
        database = _mixed_ledger_with_rate()
        connection = connect_readonly(database)
        try:
            timeline = cash_timeline_from(connection, today=_TODAY)
            flow = money_flow_page_from(
                connection, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END,
            )
        finally:
            connection.close()

        sgd_base_closing = Fraction(timeline["by_currency"]["SGD"]["base"]["closing_balance"])
        self.assertEqual(flow.summary.forecast_closing.base.partial_total, sgd_base_closing)
        sgd_conservative_closing = Fraction(timeline["by_currency"]["SGD"]["conservative"]["closing_balance"])
        self.assertEqual(flow.summary.forecast_closing.conservative.partial_total, sgd_conservative_closing)


class MissingRateTests(unittest.TestCase):
    def test_missing_usd_rate_makes_cash_partial_not_missing_evidence(self) -> None:
        database = _ledger(_MIXED_ROWS)  # no USD rate loaded

        result = _page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        cash = result.summary.cash_at_cutoff
        self.assertEqual(cash.result.status, ConsolidationStatus.PARTIAL)
        self.assertEqual(cash.result.partial_total, Fraction(360))
        self.assertIn("USD", {scope.currency for scope in cash.result.excluded})
        self.assertIn("RATE_NOT_IMPORTED", cash.result.unavailable_reasons)


class SingleConnectionTests(unittest.TestCase):
    def test_money_flow_page_opens_exactly_one_connection(self) -> None:
        database = _mixed_ledger_with_rate()

        with count_sqlite_connections() as counter:
            money_flow_page(database, today=_TODAY, history_start=_HISTORY_START, history_end=_HISTORY_END)

        self.assertEqual(counter["n"], 1)


class StaleAndConcurrentRefreshTests(unittest.TestCase):
    def test_a_manifest_changed_since_the_last_refresh_is_a_typed_refusal(self) -> None:
        database = build_ledger()
        writer = connect_sqlite(database)
        try:
            load_fx_rates(
                writer,
                {
                    "source": "test", "source_version": "money-flow-stale-fixture", "quote_currency": "SGD",
                    "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.5000"}],
                },
            )
        finally:
            writer.close()
        # Deliberately not refreshed: the stored generation no longer matches
        # a fresh recompute from this same snapshot.

        connection = connect_readonly(database)
        try:
            with self.assertRaises(FinCLError) as caught:
                money_flow_page_from(
                    connection, today=date(2026, 3, 6), history_start="2026-01-01", history_end="2026-03-06",
                )
        finally:
            connection.close()
        self.assertTrue(str(caught.exception).startswith(STALE_GENERATION_REFRESH_REQUIRED))

    def test_a_refresh_published_mid_read_never_reaches_the_open_composite_read(self) -> None:
        database = build_ledger()
        connection = connect_readonly(database)
        try:
            connection.execute("BEGIN")
            pinned_generation = str(current_generation(connection)["generation_token"])

            republished_generation = publish_new_generation(database)
            self.assertNotEqual(pinned_generation, republished_generation)

            result = money_flow_page_from(
                connection, today=date(2026, 3, 6), history_start="2026-01-01", history_end="2026-03-06",
            )
        finally:
            connection.close()

        self.assertEqual(result.context.generation_token, pinned_generation)


class ApiRouteTests(ApiTestBase):
    """Route wiring only -- backend behavior is covered above directly."""

    def setUp(self) -> None:
        super().setUp()
        self.bootstrap()

    def test_money_flow_route_returns_the_four_card_shape(self) -> None:
        response = self.client.get(
            "/api/v1/money-flow",
            params={"history_start": "2026-01-01", "history_end": "2026-01-31"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        summary = body["summary"]
        self.assertIn("economic_flow", summary)
        self.assertIn("gross_burn", summary)
        self.assertIn("cash_at_cutoff", summary)
        self.assertIn("forecast_closing", summary)
        for key in ("inflow", "outflow", "net", "exceptional_adjustments"):
            self.assertIn("status", summary["economic_flow"][key])
        self.assertIn("supported_cash_cutoff", body["cutoff"])
        self.assertIn("first_modelled_day", body["cutoff"])
        self.assertIn("stale_days", body["cutoff"])
        self.assertEqual(body["provenance"]["policy_versions"]["gross_burn"], "gross-burn-v2")

    def test_stale_generation_is_a_409(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            load_fx_rates(
                connection,
                {
                    "source": "test", "source_version": "money-flow-api-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.5000"}],
                },
            )
        finally:
            connection.close()
        # Deliberately not refreshed, so the stored generation manifest cannot
        # be reproduced from a fresh recompute of this same database.

        response = self.client.get(
            "/api/v1/money-flow",
            params={"history_start": "2026-01-01", "history_end": "2026-01-31"},
        )
        self.assertEqual(response.status_code, 409, response.text)


if __name__ == "__main__":
    unittest.main()
