"""M0 step 02: requirement-based coverage and bounded preflight (§4.3).

Builds real ledgers through the Revolut import path (mirroring
``tests.test_position``/``tests.test_analysis_components``) rather than
synthesizing ``ComponentProjection``/``CoverageContract`` objects by hand, so
these tests exercise the actual read path a preflight would run.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fincl.coverage import (
    CoverageContract,
    dated_reporting_coverage,
    latest_covered_complete_months,
)
from fincl.db import connect_readonly, connect_sqlite
from fincl.document_service import DocumentImportService
from fincl.fx import load_fx_rates
from fincl.intervals import DateInterval
from fincl.money import ExactMoney
from fincl.position.queries import position_page_from
from fincl.refresh import refresh_ledger
from fincl.valuation import (
    FLOW_RATE_V1,
    EntryPrecision,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    value_one,
)
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _load_rate(database: Path, base: str, period: str, rate: str, *, source_version: str = "v1") -> None:
    connection = connect_sqlite(database)
    try:
        load_fx_rates(
            connection,
            {
                "source": "test", "source_version": source_version, "quote_currency": "SGD",
                "rates": [{"base_currency": base, "period": period, "rate": rate}],
            },
        )
    finally:
        connection.close()


_MIXED_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed SGD", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Topup", "Current", "2026-01-01 10:01:00", "2026-01-01 10:01:01",
     "Seed USD", "300.00", "0.00", "USD", "COMPLETED", "300.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
     "Jan USD spend", "-10.00", "0.00", "USD", "COMPLETED", "290.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Feb USD spend", "-20.00", "0.00", "USD", "COMPLETED", "270.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Mar USD spend", "-15.00", "0.00", "USD", "COMPLETED", "255.00"],
]


class DatedRequirementDiscoveryTests(unittest.TestCase):
    """§4.3 items 1-3: settlement currencies/months, burn months, cash currencies."""

    def test_an_existing_pair_with_only_the_wrong_months_is_uncovered(self) -> None:
        database = _ledger(_MIXED_ROWS)
        # A rate is loaded, but only for March -- January's USD spend has no
        # eligible rate even though the USD/SGD pair exists at all.
        _load_rate(database, "USD", "2026-03", "1.5000")
        connection = connect_readonly(database)
        try:
            dated = dated_reporting_coverage(
                connection, as_of=date(2026, 3, 31),
                history_start=date(2026, 1, 1), history_end=date(2026, 3, 31),
            )
        finally:
            connection.close()
        by_scope = {(r.purpose, r.native_currency, r.period): r for r in dated.requirements}
        january = by_scope[("flow", "USD", "2026-01")]
        self.assertEqual(january.status, "UNAVAILABLE")
        self.assertEqual(january.reason, "RATE_NOT_IMPORTED")
        self.assertIn("2026-01", january.remedy or "")
        march = by_scope[("flow", "USD", "2026-03")]
        self.assertEqual(march.status, "AVAILABLE")
        self.assertEqual(march.basis, "monthly_proxy")

    def test_coverage_prediction_matches_actual_valuation_for_every_purpose(self) -> None:
        """The requirement row is not a second ladder: it must reproduce
        exactly what ``value_one`` resolves for the identical context."""
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-02", "1.4000")
        connection = connect_readonly(database)
        try:
            dated = dated_reporting_coverage(
                connection, as_of=date(2026, 3, 15),
                history_start=date(2026, 1, 1), history_end=date(2026, 3, 15),
            )
            by_scope = {(r.purpose, r.native_currency, r.period): r for r in dated.requirements}
            predicted = by_scope[("flow", "USD", "2026-02")]
            self.assertEqual(predicted.status, "AVAILABLE")

            context = ValuationContext(
                reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
                valuation_as_of=date(2026, 3, 15), rate_selection_policy=FLOW_RATE_V1,
            )
            item = ValuationItem(
                native=ExactMoney.parse("20.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                precision=EntryPrecision.MONTH, period_month="2026-02",
            )
            actual = value_one(connection, item, context=context)
        finally:
            connection.close()
        assert actual.basis is not None
        self.assertEqual(predicted.basis, actual.basis.value)
        self.assertEqual(predicted.status, "AVAILABLE")
        self.assertIsNone(actual.unavailable)

    def test_earlier_burn_months_are_included_even_before_history_start(self) -> None:
        database = _ledger(_MIXED_ROWS)
        connection = connect_readonly(database)
        try:
            dated = dated_reporting_coverage(
                connection, as_of=date(2026, 3, 31),
                # The requested history window is March-only...
                history_start=date(2026, 3, 1), history_end=date(2026, 3, 31),
            )
        finally:
            connection.close()
        # March is not a *complete* covered month (the statement's certified
        # period only runs through March 5), so burn's own lookback reaches
        # back past the requested window into January and February instead.
        self.assertEqual(dated.burn.included_months, ("2026-01", "2026-02"))
        gross_burn_scopes = {
            (r.native_currency, r.period)
            for r in dated.requirements
            if "gross_burn" in r.affected_metrics
        }
        self.assertIn(("USD", "2026-01"), gross_burn_scopes)
        self.assertIn(("USD", "2026-02"), gross_burn_scopes)
        analysis_flow_scopes = {
            (r.native_currency, r.period)
            for r in dated.requirements
            if "analysis_flow" in r.affected_metrics
        }
        self.assertNotIn(("USD", "2026-01"), analysis_flow_scopes)

    def test_identity_currency_needs_no_rate_and_is_still_reported(self) -> None:
        database = _ledger(_MIXED_ROWS)
        connection = connect_readonly(database)
        try:
            dated = dated_reporting_coverage(
                connection, as_of=date(2026, 1, 31),
                history_start=date(2026, 1, 1), history_end=date(2026, 1, 31),
            )
        finally:
            connection.close()
        sgd_rows = [r for r in dated.requirements if r.native_currency == "SGD"]
        self.assertTrue(sgd_rows)
        for row in sgd_rows:
            self.assertEqual(row.status, "AVAILABLE")
            self.assertEqual(row.basis, "identity")
            self.assertIsNone(row.policy)

    def test_original_purchase_currency_is_not_an_extra_pocket(self) -> None:
        """A EUR purchase settled in SGD needs no EUR reference rate: only
        ``currency_pocket`` (the settlement currency) is ever scanned."""
        database = _ledger(_MIXED_ROWS)
        connection = connect_sqlite(database)
        try:
            connection.execute(
                "UPDATE ledger_entries SET source_currency = 'EUR', "
                "source_amount_text = '300.00', source_amount_coefficient = 30000, "
                "source_amount_scale = 2 WHERE currency_pocket = 'SGD'"
            )
            connection.commit()
        finally:
            connection.close()
        connection = connect_readonly(database)
        try:
            dated = dated_reporting_coverage(
                connection, as_of=date(2026, 1, 31),
                history_start=date(2026, 1, 1), history_end=date(2026, 1, 31),
            )
        finally:
            connection.close()
        self.assertFalse(any(r.native_currency == "EUR" for r in dated.requirements))

    def test_cash_and_forecast_share_the_same_liquid_currency_requirement(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-01", "1.4000")
        # Month-end: the closed-month POSITION/FORECAST policy may use
        # January's own rate here (mid-month could not; see test_valuation).
        connection = connect_readonly(database)
        try:
            dated = dated_reporting_coverage(
                connection, as_of=date(2026, 1, 31),
                history_start=date(2026, 1, 1), history_end=date(2026, 1, 31),
            )
        finally:
            connection.close()
        by_scope = {(r.purpose, r.native_currency, r.as_of_date): r for r in dated.requirements}
        self.assertIn(("position", "USD", "2026-01-31"), by_scope)
        self.assertIn(("forecast", "USD", "2026-01-31"), by_scope)
        self.assertEqual(by_scope[("position", "USD", "2026-01-31")].status, "AVAILABLE")
        self.assertEqual(by_scope[("forecast", "USD", "2026-01-31")].status, "AVAILABLE")


class UnsupportedCashIsSeparateFromMissingFxTests(unittest.TestCase):
    """§4.3 item 5: evidence readiness and FX readiness must not collapse."""

    def test_a_supplied_rate_does_not_turn_unsupported_evidence_into_a_balance(self) -> None:
        rows = [
            ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
             "Seed SGD", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
            ["Topup", "Current", "2026-01-01 10:01:00", "2026-01-01 10:01:01",
             "Seed USD", "50.00", "0.00", "USD", "COMPLETED", "50.00"],
            ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
             "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
        ]
        database = _ledger(rows)
        _load_rate(database, "USD", "2026-01", "1.4000")

        # As in tests.test_position: an uncertified quiet interval past the
        # last statement means the ledger's own evidence coverage cannot
        # bridge a supported cash balance forward to this later date.
        far_today = date(2026, 3, 10)
        connection = connect_readonly(database)
        try:
            position = position_page_from(connection, today=far_today)
            dated = dated_reporting_coverage(connection, as_of=far_today)
        finally:
            connection.close()

        self.assertFalse(position["current_position_supported"])
        by_scope = {(r.purpose, r.native_currency, r.as_of_date): r for r in dated.requirements}
        usd_position = by_scope.get(("position", "USD", far_today.isoformat()))
        # USD is a liquid pocket with a rate on file, so the *FX* requirement
        # is available -- this must not be read as "USD cash is supported".
        if usd_position is not None:
            self.assertEqual(usd_position.status, "AVAILABLE")
        self.assertFalse(position["current_position_supported"])


class BurnMonthSelectorTests(unittest.TestCase):
    """Direct unit coverage of the shared burn-month selector."""

    def _contract(self, intervals: list[tuple[str, str]]) -> CoverageContract:
        return CoverageContract(
            required_sources=["test"], source_intervals={},
            covered_intervals=[DateInterval(start, end) for start, end in intervals],
            status="COVERED", reasons=[],
        )

    def test_stops_at_the_requested_limit(self) -> None:
        contract = self._contract([("2025-01-01", "2026-12-31")])
        months = latest_covered_complete_months(contract, history_end=date(2026, 8, 15), limit=3)
        self.assertEqual(months, ["2026-05", "2026-06", "2026-07"])

    def test_skips_a_gap_and_keeps_searching_backward(self) -> None:
        contract = self._contract([("2026-01-01", "2026-01-31"), ("2026-04-01", "2026-08-31")])
        months = latest_covered_complete_months(contract, history_end=date(2026, 8, 15), limit=5)
        # April-July are covered and satisfy 4 of the 5 requested months; the
        # 5th does not stop at the February/March gap -- it reaches back into
        # the earlier, separately covered January.
        self.assertEqual(months, ["2026-01", "2026-04", "2026-05", "2026-06", "2026-07"])

    def test_no_qualifying_month_is_an_empty_result(self) -> None:
        contract = self._contract([])
        months = latest_covered_complete_months(contract, history_end=date(2026, 8, 15), limit=3)
        self.assertEqual(months, [])


class PreflightDoesNotWriteTests(unittest.TestCase):
    def test_the_readonly_connection_cannot_write(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-01", "1.4000")
        connection = connect_readonly(database)
        try:
            dated_reporting_coverage(
                connection, as_of=date(2026, 1, 31),
                history_start=date(2026, 1, 1), history_end=date(2026, 1, 31),
            )
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO fx_rates(base_currency, quote_currency, period, "
                                    "rate_numerator, rate_denominator, rate_text, source, "
                                    "source_version, imported_at) VALUES "
                                    "('ZZZ','SGD','2026-01',1,1,'1','x','x','x')")
        finally:
            connection.close()

    def test_row_counts_are_unchanged_after_a_preflight_read(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-01", "1.4000")

        def _counts() -> tuple[int, int]:
            connection = connect_readonly(database)
            try:
                entries = connection.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
                rates = connection.execute("SELECT count(*) FROM fx_rates").fetchone()[0]
                return int(entries), int(rates)
            finally:
                connection.close()

        before = _counts()
        connection = connect_readonly(database)
        try:
            dated_reporting_coverage(
                connection, as_of=date(2026, 3, 31),
                history_start=date(2026, 1, 1), history_end=date(2026, 3, 31),
            )
        finally:
            connection.close()
        after = _counts()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
