"""M4 step 13: the reported Net worth workspace, ``reported_net_worth_page_from``
(§3.3, §7 M4).

Two test styles, mirroring ``tests.test_networth``:

* Pure tests feed hand-built native ``net_worth_page_from``-shaped dicts
  straight into ``_value_components``/``_build_trend`` against a minimal
  real connection (for FX resolution only) -- exact, hand-checkable amounts,
  no ledger import required for these.
* Ledger tests exercise ``reported_net_worth_page_from``/the HTTP route end
  to end through the real Revolut import path, mirroring
  ``tests.test_reported_cash``.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import date
from fractions import Fraction
from pathlib import Path

from fincl.db import connect_readonly, connect_sqlite
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.networth.queries import net_worth_page_from
from fincl.networth.reporting import (
    _build_trend,
    _value_components,
    reported_net_worth_page_from,
)
from fincl.position.reporting import reported_cash_page_from
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

_ROWS: list[list[str]] = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "360.00"],
]


def _ledger(rows: list[list[str]] = _ROWS) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _load_usd_rate(database: Path, *, period: str, rate: str) -> None:
    writer = connect_sqlite(database)
    try:
        load_fx_rates(
            writer,
            {
                "source": "test", "source_version": f"reported-networth-{period}-{uuid.uuid4().hex}",
                "quote_currency": "SGD", "rates": [{"base_currency": "USD", "period": period, "rate": rate}],
            },
        )
    finally:
        writer.close()
    refresh_ledger(database)


def _component(**over: object) -> dict:
    base = {
        "component_id": "cash:acc:SGD", "kind": "cash", "side": "asset", "currency": "SGD",
        "amount": "100.00", "as_of": "2026-02-05", "basis": "evidence", "origin": "position · x",
        "label": "Everyday", "included": True, "confidence": None, "note": None,
    }
    base.update(over)
    return base


def _native_page(components: list[dict], *, as_of: str = "2026-02-05") -> dict:
    return {
        "as_of": as_of, "generation": "gen-x", "label": "tracked net worth",
        "is_total_personal_wealth": False, "disclaimer": "d", "components": components,
        "notes": [], "per_currency": {},
    }


def _context(connection, *, today: date = _TODAY):
    scope = ReportedScope(valuation_as_of=today)
    return build_reporting_context(connection, scope=scope)


class MixedCurrencyAndSignTests(unittest.TestCase):
    """Pure: hand-built components against a real connection (FX only)."""

    def setUp(self) -> None:
        self.database = _ledger()
        _load_usd_rate(self.database, period="2026-01", rate="1.5000")
        self.connection = connect_readonly(self.database)
        self.addCleanup(self.connection.close)
        self.context = _context(self.connection)

    def test_mixed_currency_assets_and_liabilities_consolidate_into_reporting_currency(self) -> None:
        native = _native_page([
            _component(component_id="cash:a:SGD", kind="cash", side="asset", currency="SGD",
                       amount="100.00", as_of="2026-01-31"),
            _component(component_id="cash:b:USD", kind="cash", side="asset", currency="USD",
                       amount="100.00", as_of="2026-01-31"),
            _component(component_id="debt:1", kind="debt", side="liability", currency="SGD",
                       amount="40.00", as_of="2026-01-31"),
        ])
        _components, totals = _value_components(self.connection, native, context=self.context)

        # SGD 100 + USD 100 * 1.5000 = SGD 150 -> assets 250.
        self.assertEqual(totals.assets.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(totals.assets.total, Fraction(250))
        self.assertEqual(totals.liabilities.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(totals.liabilities.total, Fraction(40))
        # Net = assets - liabilities, computed from the same exact Fractions.
        self.assertEqual(totals.net_worth.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(totals.net_worth.total, Fraction(210))

    def test_no_double_counting_when_a_component_is_structurally_excluded(self) -> None:
        native = _native_page([
            _component(component_id="cash:a:SGD", side="asset", amount="100.00", as_of="2026-01-31"),
            _component(
                component_id="debt:linked", kind="debt", side="liability", currency="SGD",
                amount="50.00", as_of="2026-01-31", included=False,
                note="linked to a tracked card facility; counted once in card liability",
            ),
        ])
        _components, totals = _value_components(self.connection, native, context=self.context)

        # The card-linked debt is priced for display but never enters totals.
        self.assertEqual(totals.liabilities.total, Fraction(0))
        self.assertEqual(totals.net_worth.total, Fraction(100))
        priced = next(c for c in _components if c.native["component_id"] == "debt:linked")
        self.assertIsNotNone(priced.reported)
        # And it does not register as a reported evidence gap either.
        self.assertEqual(totals.liabilities.excluded_entry_count, 0)

    def test_manual_basis_stays_separate_from_tracked_but_joins_net_worth_including_manual(self) -> None:
        native = _native_page([
            _component(component_id="cash:a:SGD", side="asset", amount="100.00", as_of="2026-01-31", basis="evidence"),
            _component(component_id="debt:manual", kind="debt", side="liability", currency="SGD",
                       amount="40.00", as_of="2026-01-31", basis="manual"),
        ])
        _components, totals = _value_components(self.connection, native, context=self.context)

        self.assertEqual(totals.liabilities.total, Fraction(0))  # tracked-only: manual excluded
        self.assertEqual(totals.net_worth.total, Fraction(100))  # tracked-only net
        self.assertEqual(totals.manual_liabilities.total, Fraction(40))
        self.assertEqual(totals.net_worth_including_manual.total, Fraction(60))  # 100 - 40


class MissingEvidenceVsMissingFxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger()  # no USD rate loaded
        self.connection = connect_readonly(self.database)
        self.addCleanup(self.connection.close)
        self.context = _context(self.connection)

    def test_missing_amount_folds_as_evidence_gap_not_zero(self) -> None:
        native = _native_page([
            _component(component_id="cash:a:SGD", side="asset", amount="100.00", as_of="2026-01-31"),
            _component(
                component_id="cash:b:SGD", side="asset", amount=None, as_of=None, included=False,
                note="no supported balance boundary",
            ),
        ])
        _components, totals = _value_components(self.connection, native, context=self.context)

        self.assertEqual(totals.assets.status, ConsolidationStatus.PARTIAL)
        self.assertEqual(totals.assets.partial_total, Fraction(100))
        self.assertIn("NO_NET_WORTH_EVIDENCE", totals.assets.unavailable_reasons)
        missing = next(c for c in _components if c.native["component_id"] == "cash:b:SGD")
        self.assertIsNone(missing.reported)

    def test_missing_fx_folds_as_a_distinct_partial_reason(self) -> None:
        native = _native_page([
            _component(component_id="cash:a:SGD", side="asset", amount="100.00", as_of="2026-01-31"),
            _component(component_id="cash:b:USD", side="asset", currency="USD",
                       amount="50.00", as_of="2026-01-31"),
        ])
        _components, totals = _value_components(self.connection, native, context=self.context)

        self.assertEqual(totals.assets.status, ConsolidationStatus.PARTIAL)
        self.assertEqual(totals.assets.partial_total, Fraction(100))
        self.assertIn("RATE_NOT_IMPORTED", totals.assets.unavailable_reasons)
        self.assertNotIn("NO_NET_WORTH_EVIDENCE", totals.assets.unavailable_reasons)
        priced = next(c for c in _components if c.native["component_id"] == "cash:b:USD")
        self.assertIsNotNone(priced.reported)
        self.assertIsNotNone(priced.reported.unavailable)

    def test_no_components_at_all_is_a_genuine_available_zero(self) -> None:
        native = _native_page([])
        _components, totals = _value_components(self.connection, native, context=self.context)
        self.assertEqual(totals.liabilities.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(totals.liabilities.total, Fraction(0))


class TrendTests(unittest.TestCase):
    """Prior and current valued separately at their own dates, then subtracted."""

    def setUp(self) -> None:
        self.database = _ledger()
        # Different rates for the two months proves the delta is not repriced
        # at one shared/"today" rate.
        _load_usd_rate(self.database, period="2026-01", rate="1.4000")
        _load_usd_rate(self.database, period="2026-02", rate="1.6000")
        self.connection = connect_readonly(self.database)
        self.addCleanup(self.connection.close)
        self.context = _context(self.connection)

    def test_delta_uses_each_sides_own_rate_not_a_shared_one(self) -> None:
        prior_native = _native_page(
            [_component(component_id="cash:a:USD", currency="USD", amount="100.00", as_of="2026-01-31")],
            as_of="2026-01-31",
        )
        current_native = _native_page(
            [_component(component_id="cash:a:USD", currency="USD", amount="100.00", as_of="2026-02-28")],
            as_of="2026-02-28",
        )
        _prior_components, prior_totals = _value_components(self.connection, prior_native, context=self.context)
        _current_components, current_totals = _value_components(self.connection, current_native, context=self.context)

        native_trend = {
            "prior_as_of": "2026-01-31", "current_as_of": "2026-02-28",
            "scope_changed": False, "scope_changes": [], "comparability": "same tracked components",
        }
        trend = _build_trend(native_trend, prior_totals=prior_totals, current_totals=current_totals)

        # USD 100 at 1.4000 -> SGD 140; USD 100 at 1.6000 -> SGD 160.
        self.assertEqual(prior_totals.net_worth.total, Fraction(140))
        self.assertEqual(current_totals.net_worth.total, Fraction(160))
        self.assertEqual(trend.delta, Fraction(20))
        self.assertTrue(trend.both_complete)

    def test_missing_side_leaves_delta_none_not_a_zero_substitution(self) -> None:
        prior_native = _native_page([
            _component(component_id="cash:x:SGD", amount=None, as_of=None, included=False,
                       note="no supported balance boundary"),
        ], as_of="2026-01-31")
        current_native = _native_page(
            [_component(component_id="cash:a:SGD", amount="100.00", as_of="2026-02-28")], as_of="2026-02-28",
        )
        _p, prior_totals = _value_components(self.connection, prior_native, context=self.context)
        _c, current_totals = _value_components(self.connection, current_native, context=self.context)

        native_trend = {
            "prior_as_of": "2026-01-31", "current_as_of": "2026-02-28",
            "scope_changed": True, "scope_changes": [{"component_id": "cash:a:SGD", "change": "added_since_prior"}],
            "comparability": "the tracked scope changed",
        }
        trend = _build_trend(native_trend, prior_totals=prior_totals, current_totals=current_totals)

        self.assertEqual(prior_totals.net_worth.status, ConsolidationStatus.UNAVAILABLE)
        self.assertIsNone(trend.delta)
        self.assertFalse(trend.both_complete)


class LedgerIntegrationTests(unittest.TestCase):
    def test_native_detail_is_unchanged_by_the_reported_reader(self) -> None:
        database = _ledger()
        connection = connect_readonly(database)
        try:
            page = reported_net_worth_page_from(connection, as_of="2026-03-06", today=_TODAY)
        finally:
            connection.close()
        connection = connect_readonly(database)
        try:
            direct_native = net_worth_page_from(connection, as_of="2026-03-06", today=_TODAY)
        finally:
            connection.close()
        self.assertEqual(page.native["components"], direct_native["components"])
        self.assertEqual(page.native["per_currency"], direct_native["per_currency"])
        self.assertEqual(page.native["disclaimer"], direct_native["disclaimer"])

    def test_cash_component_matches_the_cash_workspaces_own_reported_balance(self) -> None:
        database = _ledger()
        connection = connect_readonly(database)
        try:
            net_worth = reported_net_worth_page_from(connection, as_of="2026-03-06", today=_TODAY)
        finally:
            connection.close()
        connection = connect_readonly(database)
        try:
            cash = reported_cash_page_from(connection, today=_TODAY)
        finally:
            connection.close()

        cash_component = next(c for c in net_worth.components if c.native["kind"] == "cash")
        cash_pocket = next(
            p for p in cash.pockets
            if p.native["account_id"] == cash_component.native["component_id"].split(":")[1]
        )
        self.assertIsNotNone(cash_component.reported)
        self.assertIsNotNone(cash_pocket.reported_balance)
        self.assertEqual(cash_component.reported.reported_exact, cash_pocket.reported_balance.reported_exact)

    def test_two_dated_observations_produce_an_exact_reported_delta(self) -> None:
        database = _ledger()
        connection = connect_readonly(database)
        try:
            page = reported_net_worth_page_from(
                connection, as_of="2026-03-06", prior_as_of="2026-02-06", today=_TODAY,
            )
        finally:
            connection.close()
        self.assertIsNotNone(page.trend)
        assert page.trend is not None
        self.assertEqual(page.trend.prior_as_of, "2026-02-06")
        self.assertEqual(page.trend.current_as_of, "2026-03-06")
        # SGD 860 (prior, Feb 6 cutoff) -> SGD 360 (current, Mar 6 cutoff): -500.
        self.assertEqual(page.trend.delta, Fraction(-500))
        self.assertFalse(page.trend.scope_changed)


class StaleGenerationTests(unittest.TestCase):
    def test_a_manifest_changed_since_the_last_refresh_is_a_typed_refusal(self) -> None:
        database = _ledger()
        writer = connect_sqlite(database)
        try:
            load_fx_rates(
                writer,
                {
                    "source": "test", "source_version": "reported-networth-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "EUR", "period": "2026-01", "rate": "0.7000"}],
                },
            )
        finally:
            writer.close()
        connection = connect_readonly(database)
        try:
            with self.assertRaises(FinCLError) as caught:
                reported_net_worth_page_from(connection, as_of="2026-03-06", today=_TODAY)
        finally:
            connection.close()
        self.assertTrue(str(caught.exception).startswith(STALE_GENERATION_REFRESH_REQUIRED))


class ApiRouteTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.bootstrap()

    def test_reported_net_worth_route_returns_the_shape(self) -> None:
        response = self.client.get("/api/v1/reporting/net-worth")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("totals", body)
        self.assertIn("components", body)
        for key in ("assets", "liabilities", "net_worth", "manual_assets", "manual_liabilities", "net_worth_including_manual"):
            self.assertIn(key, body["totals"])
            self.assertIn("status", body["totals"][key])

    def test_stale_generation_is_a_409(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            load_fx_rates(
                connection,
                {
                    "source": "test", "source_version": "reported-networth-api-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.5000"}],
                },
            )
        finally:
            connection.close()
        response = self.client.get("/api/v1/reporting/net-worth")
        self.assertEqual(response.status_code, 409, response.text)
