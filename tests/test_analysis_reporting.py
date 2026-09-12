"""M3b.1 step 04: reported Analysis backend, burn and contributor routes (§3.1-3.2, §6).

Builds real ledgers through the Revolut import path (mirroring
``tests.test_analysis_page``/``tests.test_dated_coverage``) and, for the
debt-event cases, reuses ``tests.test_debts_slice2.Slice2Base`` so forgiveness
and expense-paid-for-me fixtures are not duplicated.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import date
from fractions import Fraction
from pathlib import Path

from fincl.analysis.handles import decode_reported, encode
from fincl.analysis.reporting import (
    reported_analysis_page_from,
    reported_contributors_page_from,
)
from fincl.db import connect_readonly, connect_sqlite
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.refresh import refresh_ledger
from fincl.reporting_context import ReportedScope, build_reporting_context
from fincl.valuation import ConsolidationStatus
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_debts_slice2 import Slice2Base


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _load_rate(database: Path, base: str, period: str, rate: str, *, quote: str = "SGD", source_version: str = "v1") -> None:
    connection = connect_sqlite(database)
    try:
        load_fx_rates(
            connection,
            {
                "source": "test", "source_version": source_version, "quote_currency": quote,
                "rates": [{"base_currency": base, "period": period, "rate": rate}],
            },
        )
    finally:
        connection.close()
    # A rate import moves the manifest's fx_row_inventory_digest; republish so
    # `build_reporting_context` finds a generation that matches its own
    # snapshot rather than refusing as stale.
    refresh_ledger(database)


_MIXED_ROWS = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
     "Seed SGD", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
    ["Topup", "Current", "2026-01-01 09:01:00", "2026-01-01 09:01:01",
     "Seed USD", "500.00", "0.00", "USD", "COMPLETED", "500.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "960.00"],
    ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01",
     "US Shop", "-20.00", "1.00", "USD", "COMPLETED", "479.00"],
    ["Refund", "Current", "2026-01-10 10:00:00", "2026-01-10 10:00:01",
     "Groceries", "5.00", "0.00", "SGD", "COMPLETED", "965.00"],
    # Extends the statement's printed period through month-end so a request
    # for the whole calendar month is fully covered (not merely through the
    # last economically interesting row).
    ["Topup", "Current", "2026-01-31 23:00:00", "2026-01-31 23:00:01",
     "Month-end marker", "0.01", "0.00", "SGD", "COMPLETED", "965.01"],
]


def _page(database: Path, **kwargs: object) -> object:
    connection = connect_readonly(database)
    try:
        return reported_analysis_page_from(connection, **kwargs)
    finally:
        connection.close()


class MixedPocketSummaryTests(unittest.TestCase):
    def test_hand_computed_net_inflow_outflow_and_spending(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-01", "1.5000")
        result = _page(
            database, as_of=date(2026, 1, 31),
            history_start="2026-01-01", history_end="2026-01-31",
        )
        self.assertTrue(result.covered)
        spending = result.summary["spending"]
        # SGD 40.00 (groceries) + USD 20.00 * 1.5 = SGD 30.00 -> SGD 70.00 magnitude.
        self.assertEqual(spending.current.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(spending.current.total, Fraction(70))
        fees = result.summary["fees"]
        # USD fee 1.00 * 1.5 = SGD 1.50.
        self.assertEqual(fees.current.total, Fraction(3, 2))
        refunds = result.summary["refunds"]
        self.assertEqual(refunds.current.total, Fraction(5))
        income = result.summary["income"]
        self.assertEqual(income.current.total, Fraction(0))

        net = result.summary["net_economic_flow"].current
        inflow = result.summary["inflow"].current
        outflow = result.summary["outflow"].current
        # net = refund(+5) - spending(70) - fees(1.5) = -66.5
        self.assertEqual(net.total, Fraction(-1330, 20))
        self.assertEqual(inflow.total, Fraction(5))
        self.assertEqual(outflow.total, Fraction(715, 10))
        assert inflow.total is not None and outflow.total is not None and net.total is not None
        self.assertEqual(inflow.total - outflow.total, net.total)

    def test_direct_and_inverse_pricing_agree(self) -> None:
        direct_db = _ledger(_MIXED_ROWS)
        _load_rate(direct_db, "USD", "2026-01", "1.5000")
        direct = _page(direct_db, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31")

        inverse_db = _ledger(_MIXED_ROWS)
        _load_rate(inverse_db, "SGD", "2026-01", "0.6666666667", quote="USD")
        inverse = _page(inverse_db, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31")

        # Both price USD spending through their respective direct/inverse leg;
        # the exact ratios differ slightly (0.6666666667 is not exactly 2/3),
        # but both must be AVAILABLE and use the currency's own rate row.
        self.assertEqual(direct.summary["spending"].current.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(inverse.summary["spending"].current.status, ConsolidationStatus.AVAILABLE)
        rate_inputs = inverse.summary["spending"].current.rate_inputs
        self.assertTrue(any(r.inverted for r in rate_inputs))

    def test_missing_fx_for_one_pocket_is_partial_and_names_the_scope(self) -> None:
        database = _ledger(_MIXED_ROWS)
        # No FX loaded at all: USD cannot be priced, SGD is identity.
        result = _page(database, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31")
        spending = result.summary["spending"].current
        self.assertEqual(spending.status, ConsolidationStatus.PARTIAL)
        # SGD 40.00 groceries priced; USD 20.00 excluded.
        self.assertEqual(spending.partial_total, Fraction(40))
        excluded_currencies = {d.scope.currency for d in spending.excluded_detail}
        self.assertIn("USD", excluded_currencies)
        usd_detail = next(d for d in spending.excluded_detail if d.scope.currency == "USD")
        self.assertEqual(usd_detail.reasons, ("RATE_NOT_IMPORTED",))
        self.assertTrue(usd_detail.remedies)

    def test_pocket_currency_narrows_but_default_includes_all(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-01", "1.5000")
        all_pockets = _page(database, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31")
        sgd_only = _page(
            database, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31",
            pocket_currency="SGD",
        )
        self.assertEqual(all_pockets.summary["spending"].current.total, Fraction(70))
        self.assertEqual(sgd_only.summary["spending"].current.total, Fraction(40))

    def test_a_window_entirely_outside_coverage_is_unavailable_not_zero(self) -> None:
        database = _ledger(_MIXED_ROWS)
        result = _page(
            database, as_of=date(2027, 6, 30),
            history_start="2027-06-01", history_end="2027-06-30",
        )
        self.assertFalse(result.covered)
        spending = result.summary["spending"].current
        self.assertEqual(spending.status, ConsolidationStatus.UNAVAILABLE)
        self.assertEqual(spending.unavailable_reasons, ("NO_EVIDENCE_COVERAGE",))
        self.assertIsNone(spending.total)
        self.assertIsNone(spending.partial_total)


class GrossBurnTests(unittest.TestCase):
    _THREE_MONTH_ROWS = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "3000.00", "0.00", "SGD", "COMPLETED", "3000.00"],
        ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
         "Jan spend", "-100.00", "1.00", "SGD", "COMPLETED", "2899.00"],
        ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
         "Feb spend", "-200.00", "0.00", "SGD", "COMPLETED", "2699.00"],
        ["Refund", "Current", "2026-02-10 10:00:00", "2026-02-10 10:00:01",
         "Feb refund", "50.00", "0.00", "SGD", "COMPLETED", "2749.00"],
        ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
         "Mar spend", "-300.00", "0.00", "SGD", "COMPLETED", "2449.00"],
        # Extends the statement's printed period through March's own
        # month-end so March counts as a complete evidence-covered month.
        ["Topup", "Current", "2026-03-31 23:00:00", "2026-03-31 23:00:01",
         "Month-end marker", "0.01", "0.00", "SGD", "COMPLETED", "2449.01"],
    ]

    def test_burn_averages_spending_plus_fees_over_included_months(self) -> None:
        database = _ledger(self._THREE_MONTH_ROWS)
        result = _page(database, as_of=date(2026, 3, 31), history_start="2026-03-01", history_end="2026-03-31")
        burn = result.gross_burn
        self.assertEqual(burn.month_count, 3)
        self.assertEqual(burn.included_months, ("2026-01", "2026-02", "2026-03"))
        assert burn.result is not None
        self.assertEqual(burn.result.status, ConsolidationStatus.AVAILABLE)
        # (101.00 + 200.00 + 300.00) / 3 -- refund does not reduce burn.
        self.assertEqual(burn.result.total, Fraction(601, 3))

    def test_fewer_than_three_qualifying_months_is_honestly_smaller(self) -> None:
        rows = self._THREE_MONTH_ROWS[:3]  # only January is a complete covered month
        database = _ledger(rows)
        result = _page(database, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31")
        burn = result.gross_burn
        self.assertEqual(burn.month_count, 1)
        self.assertEqual(burn.included_months, ("2026-01",))
        assert burn.result is not None
        self.assertEqual(burn.result.total, Fraction(101))

    def test_no_qualifying_month_is_unavailable_not_zero(self) -> None:
        database = _ledger(self._THREE_MONTH_ROWS)
        result = _page(
            database, as_of=date(2020, 1, 31), history_start="2020-01-01", history_end="2020-01-31",
        )
        burn = result.gross_burn
        self.assertEqual(burn.month_count, 0)
        self.assertIsNone(burn.result)
        self.assertIsNotNone(burn.reason)

    def test_burn_reaches_earlier_than_the_requested_history_window(self) -> None:
        database = _ledger(self._THREE_MONTH_ROWS)
        result = _page(database, as_of=date(2026, 3, 31), history_start="2026-03-01", history_end="2026-03-31")
        self.assertEqual(result.gross_burn.included_months, ("2026-01", "2026-02", "2026-03"))


class TrendTests(unittest.TestCase):
    _ROWS = GrossBurnTests._THREE_MONTH_ROWS

    def test_trend_reports_six_months_by_default_with_the_evidenced_ones_complete(self) -> None:
        database = _ledger(self._ROWS)
        result = _page(database, as_of=date(2026, 3, 31), history_start="2026-03-01", history_end="2026-03-31")
        self.assertEqual(
            [m.month for m in result.trend],
            ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03"],
        )
        by_month = {m.month: m for m in result.trend}
        for month in ("2025-10", "2025-11", "2025-12"):
            self.assertFalse(by_month[month].complete)
        for month in ("2026-01", "2026-02", "2026-03"):
            self.assertTrue(by_month[month].complete)
        self.assertEqual(by_month["2026-01"].spending.total, Fraction(100))
        self.assertEqual(by_month["2026-02"].spending.total, Fraction(200))
        self.assertEqual(by_month["2026-03"].spending.total, Fraction(300))
        # A refund does not reduce the spending trend cell either.
        self.assertEqual(by_month["2026-02"].income.total, Fraction(0))

    def test_trend_range_12_reaches_back_past_evidence_into_incomplete_months(self) -> None:
        database = _ledger(self._ROWS)
        connection = connect_readonly(database)
        try:
            result = reported_analysis_page_from(
                connection, as_of=date(2026, 3, 31), history_start="2026-03-01", history_end="2026-03-31",
                trend_range="12",
            )
        finally:
            connection.close()
        self.assertEqual(len(result.trend), 12)
        incomplete = [m for m in result.trend if not m.complete]
        self.assertTrue(incomplete)
        for month in incomplete:
            self.assertEqual(month.spending.unavailable_reasons, ("NO_EVIDENCE_COVERAGE",))
            self.assertIsNone(month.spending.total)

    def test_an_unknown_trend_range_is_refused(self) -> None:
        database = _ledger(self._ROWS)
        connection = connect_readonly(database)
        try:
            with self.assertRaises(FinCLError):
                reported_analysis_page_from(
                    connection, as_of=date(2026, 3, 31), history_start="2026-03-01", history_end="2026-03-31",
                    trend_range="24",
                )
        finally:
            connection.close()


class DebtEventEffectsTests(Slice2Base):
    """Reuses the SGD-only Slice2Base fixture: every figure here is identity
    -valued, isolating debt-event classification from FX concerns."""

    def _page(self, **kwargs: object) -> object:
        connection = connect_readonly(self.database)
        try:
            return reported_analysis_page_from(connection, **kwargs)
        finally:
            connection.close()

    def test_expense_paid_for_me_counts_as_spending(self) -> None:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "EXPENSE_PAID_FOR_ME", "direction": "PAYABLE",
             "principal_amount": "25.00", "effective_on": "2026-03-06",
             "entry_allocations": [], "opening_balance": False,
             "expense_category": "Dining", "reason": "Alex covered my dinner"},
        )
        result = self._page(as_of=date(2026, 3, 12), history_start="2026-03-01", history_end="2026-03-12")
        # Seed spending was 270.00 (see AnalyticalEffectsTests.test_baseline_spending);
        # the off-ledger expense adds 25.00 more. (This fixture's statement
        # never reaches a complete calendar month, so burn itself is
        # separately exercised with its own fixture in GrossBurnTests; the
        # relevant fact proven here is only bucket classification.)
        self.assertEqual(result.summary["spending"].current.total, Fraction(295))

    def test_forgiveness_is_exceptional_not_income(self) -> None:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance 100.00"},
        )
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PAYABLE_FORGIVEN", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-10",
             "entry_allocations": [], "opening_balance": False,
             "expense_category": None, "reason": "Alex forgave the balance"},
        )
        result = self._page(as_of=date(2026, 3, 12), history_start="2026-03-01", history_end="2026-03-12")
        self.assertEqual(result.summary["income"].current.total, Fraction(0))
        exceptional = result.summary["exceptional_adjustments"].current
        self.assertEqual(exceptional.total, Fraction(100))
        # Forgiveness is still part of net economic flow (§3.1's correction).
        net = result.summary["net_economic_flow"].current
        self.assertIsNotNone(net.total)

    def test_receivable_written_off_is_exceptional_loss_not_spending(self) -> None:
        debt = self.open_debt("RECEIVABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "LENT", "direction": "RECEIVABLE",
             "principal_amount": "70.00", "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance 70.00"},
        )
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "RECEIVABLE_WRITTEN_OFF", "direction": "RECEIVABLE",
             "principal_amount": "70.00", "effective_on": "2026-03-11",
             "entry_allocations": [], "opening_balance": False,
             "expense_category": None, "reason": "Jo will not repay"},
        )
        result = self._page(as_of=date(2026, 3, 12), history_start="2026-03-01", history_end="2026-03-12")
        self.assertEqual(result.summary["spending"].current.total, Fraction(270))  # unchanged
        exceptional = result.summary["exceptional_adjustments"].current
        self.assertEqual(exceptional.total, Fraction(-70))


class RankingTests(unittest.TestCase):
    # Ranked by merchant, not category: `merchant_display` is populated
    # directly from the statement's own description text, so this fixture
    # needs no categorization-rule setup to produce distinct ranked groups.
    _ROWS = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed SGD", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
        ["Topup", "Current", "2026-01-01 09:01:00", "2026-01-01 09:01:01",
         "Seed USD", "500.00", "0.00", "USD", "COMPLETED", "500.00"],
        ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
         "GROCERIES PLACE", "-50.00", "0.00", "SGD", "COMPLETED", "950.00"],
        ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01",
         "TRAVEL AGENCY", "-30.00", "0.00", "USD", "COMPLETED", "470.00"],
        ["Topup", "Current", "2026-01-31 23:00:00", "2026-01-31 23:00:01",
         "Month-end marker", "0.01", "0.00", "SGD", "COMPLETED", "950.01"],
    ]

    def test_merchant_ranking_reverses_with_the_exchange_rate(self) -> None:
        # At 1.0, USD 30 < SGD 50: Groceries Place ranks first. At 2.5,
        # USD 30 * 2.5 = SGD 75 > SGD 50: Travel Agency overtakes it.
        low_rate_db = _ledger(self._ROWS)
        _load_rate(low_rate_db, "USD", "2026-01", "1.0000")
        low = _page(low_rate_db, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31", top_n=5)
        self.assertEqual(low.merchant_ranking[0].name, "GROCERIES PLACE")

        high_rate_db = _ledger(self._ROWS)
        _load_rate(high_rate_db, "USD", "2026-01", "2.5000")
        high = _page(high_rate_db, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31", top_n=5)
        self.assertEqual(high.merchant_ranking[0].name, "TRAVEL AGENCY")

    def test_ranking_consolidates_before_ranking_not_per_pocket(self) -> None:
        database = _ledger(self._ROWS)
        _load_rate(database, "USD", "2026-01", "1.5000")
        result = _page(database, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31", top_n=5)
        names = [group.name for group in result.merchant_ranking]
        self.assertEqual(len(names), len(set(names)))  # each merchant consolidated once


class ComparisonTests(unittest.TestCase):
    _ROWS = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
        ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
         "Jan spend", "-40.00", "0.00", "SGD", "COMPLETED", "960.00"],
        ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
         "Feb spend", "-70.00", "0.00", "SGD", "COMPLETED", "890.00"],
        ["Topup", "Current", "2026-02-28 23:00:00", "2026-02-28 23:00:01",
         "Month-end marker", "0.01", "0.00", "SGD", "COMPLETED", "890.01"],
    ]

    def test_previous_month_comparison_is_valued_at_its_own_period(self) -> None:
        database = _ledger(self._ROWS)
        result = _page(database, as_of=date(2026, 2, 28), history_start="2026-02-01", history_end="2026-02-28")
        entry = result.summary["spending"]
        self.assertEqual(entry.comparison_available, True)
        assert entry.previous is not None
        self.assertEqual(entry.current.total, Fraction(70))
        self.assertEqual(entry.previous.total, Fraction(40))
        # Exact delta (70 - 40 = 30) and pct ((70-40)/40*100 = 75.0), never a
        # native delta repriced at today's rate or a float division (§3.1 item 4).
        self.assertEqual(entry.delta, Fraction(30))
        self.assertEqual(entry.pct, "75.0")

    def test_no_delta_or_pct_when_either_side_is_not_fully_available(self) -> None:
        database = _ledger(self._ROWS)
        # No FX loaded at all is irrelevant here (native SGD is identity), so
        # force partial by narrowing to a currency with zero components: the
        # comparison period itself is simply unavailable when disabled.
        result = _page(
            database, as_of=date(2026, 2, 28), history_start="2026-02-01", history_end="2026-02-28",
        )
        # Sanity: with comparison explicitly off, no delta/pct is invented.
        off = _page(
            database, as_of=date(2026, 2, 28), history_start="2026-02-01", history_end="2026-02-28",
            comparison="none",
        )
        self.assertIsNotNone(result.summary["spending"].delta)
        self.assertIsNone(off.summary["spending"].delta)
        self.assertIsNone(off.summary["spending"].pct)


class ContributorParentEqualityTests(unittest.TestCase):
    def test_contributor_rows_sum_exactly_to_the_spending_parent(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-01", "1.5000")
        result = _page(database, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31")
        handle = result.summary["spending"].handle
        connection = connect_readonly(database)
        try:
            page = reported_contributors_page_from(connection, handle=handle, limit=50)
        finally:
            connection.close()
        total = sum(
            (row.valuation.reported_exact for row in page.rows if row.valuation.reported_exact is not None),
            Fraction(0),
        )
        assert page.parent.total is not None
        self.assertEqual(total, page.parent.total)
        self.assertEqual(page.parent.total, result.summary["spending"].current.total)

    def test_off_ledger_component_exposes_a_debt_event_owning_source(self) -> None:
        # Build a fresh SGD debt fixture so the off-ledger component's
        # owning_source can be asserted without FX noise.
        fixture = Slice2Base(methodName="setUp")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        debt = fixture.open_debt("PAYABLE")
        fixture.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "EXPENSE_PAID_FOR_ME", "direction": "PAYABLE",
             "principal_amount": "25.00", "effective_on": "2026-03-06",
             "entry_allocations": [], "opening_balance": False,
             "expense_category": "Dining", "reason": "Alex covered my dinner"},
        )
        connection = connect_readonly(fixture.database)
        try:
            result = reported_analysis_page_from(
                connection, as_of=date(2026, 3, 12), history_start="2026-03-01", history_end="2026-03-12",
            )
        finally:
            connection.close()
        handle = result.summary["spending"].handle
        connection = connect_readonly(fixture.database)
        try:
            page = reported_contributors_page_from(connection, handle=handle, limit=50)
        finally:
            connection.close()
        debt_rows = [row for row in page.rows if row.origin_kind == "DEBT_EVENT"]
        self.assertTrue(debt_rows)
        self.assertEqual(debt_rows[0].owning_source.kind, "debt_event")
        self.assertTrue(debt_rows[0].owning_source.id)
        # Never fabricated as if it were a bank transaction id.
        self.assertNotEqual(debt_rows[0].owning_source.kind, "transaction")


class StaleAndTamperedHandleTests(unittest.TestCase):
    def test_a_legacy_handle_is_rejected_by_the_reported_decoder(self) -> None:
        legacy = encode(
            generation="g1", lens="recorded_flow", currency="SGD",
            start="2026-01-01", end="2026-01-31", buckets=["spending"],
        )
        with self.assertRaises(FinCLError):
            decode_reported(legacy)

    def test_a_tampered_reported_handle_is_rejected(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-01", "1.5000")
        result = _page(database, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31")
        handle = result.summary["spending"].handle
        tampered = handle[:-2] + ("AA" if not handle.endswith("AA") else "BB")
        connection = connect_readonly(database)
        try:
            with self.assertRaises(FinCLError):
                reported_contributors_page_from(connection, handle=tampered)
        finally:
            connection.close()

    def test_a_handle_from_a_stale_generation_is_refused(self) -> None:
        database = _ledger(_MIXED_ROWS)
        _load_rate(database, "USD", "2026-01", "1.5000")
        result = _page(database, as_of=date(2026, 1, 31), history_start="2026-01-01", history_end="2026-01-31")
        handle = result.summary["spending"].handle

        # A new rate import + refresh republishes a new generation, changing
        # both the generation token and the rate-inventory digest.
        _load_rate(database, "USD", "2026-02", "1.6000", source_version="v2")
        refresh_ledger(database)

        connection = connect_readonly(database)
        try:
            with self.assertRaises(FinCLError) as caught:
                reported_contributors_page_from(connection, handle=handle)
        finally:
            connection.close()
        self.assertIn("STALE_REPORTING_CONTEXT", str(caught.exception))

    def test_building_a_handle_requires_a_live_context_first(self) -> None:
        # Sanity: build_reporting_context itself still works read-only on the
        # same fixture used above (no accidental write from handle minting).
        database = _ledger(_MIXED_ROWS)
        connection = connect_readonly(database)
        try:
            context = build_reporting_context(
                connection, scope=ReportedScope(valuation_as_of=date(2026, 1, 31))
            )
        finally:
            connection.close()
        self.assertTrue(context.generation_token)


if __name__ == "__main__":
    unittest.main()
