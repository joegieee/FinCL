"""M3b.1 step 03: the shared reporting request context (§3.1, §6.2).

Builds a real ledger through the same seam-test helpers as
``tests.test_money_flow_seam`` so ``build_reporting_context`` is exercised
against an actual committed generation, not a hand-built manifest dict.
"""

from __future__ import annotations

import unittest
from datetime import date

from fincl.db import connect_readonly, connect_sqlite
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.generation import current_generation
from fincl.reporting_context import (
    STALE_GENERATION_REFRESH_REQUIRED,
    ReportedScope,
    build_reporting_context,
)

from .money_flow_seam import build_ledger, publish_new_generation


class BuildReportingContextTests(unittest.TestCase):
    def test_pins_the_current_generation_policy_and_rate_inventory_digest(self) -> None:
        database = build_ledger()
        connection = connect_readonly(database)
        try:
            generation = current_generation(connection)
            assert generation is not None
            scope = ReportedScope(valuation_as_of=date(2026, 3, 6))
            context = build_reporting_context(connection, scope=scope)
        finally:
            connection.close()

        self.assertEqual(context.generation_token, generation["generation_token"])
        self.assertEqual(context.reporting_currency, "SGD")
        self.assertTrue(context.policy.is_default)
        self.assertTrue(context.rate_inventory_digest)
        self.assertIs(context.scope, scope)
        self.assertEqual(context.scope.valuation_as_of, date(2026, 3, 6))

    def test_scope_fields_pass_through_unchanged(self) -> None:
        database = build_ledger()
        connection = connect_readonly(database)
        try:
            scope = ReportedScope(
                valuation_as_of=date(2026, 3, 6), history_start="2026-01-01",
                history_end="2026-03-06", pocket_currency="USD",
                filters={"category_keys": ("Groceries",)},
            )
            context = build_reporting_context(connection, scope=scope)
        finally:
            connection.close()
        self.assertEqual(context.scope.history_start, "2026-01-01")
        self.assertEqual(context.scope.history_end, "2026-03-06")
        self.assertEqual(context.scope.pocket_currency, "USD")
        self.assertEqual(context.scope.filters, {"category_keys": ("Groceries",)})

    def test_a_manifest_changed_since_the_last_refresh_is_a_hard_refusal(self) -> None:
        """§6.2: a stale manifest must refuse, never mix new FX with the old
        generation's derived evidence -- distinct from the legacy soft flag."""
        database = build_ledger()
        # Load one new FX rate without republishing: the stored generation
        # row's manifest no longer matches what a fresh recompute would find.
        writer = connect_sqlite(database)
        try:
            load_fx_rates(
                writer,
                {
                    "source": "test", "source_version": "reporting-context-stale-fixture",
                    "quote_currency": "SGD",
                    "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.5000"}],
                },
            )
        finally:
            writer.close()

        connection = connect_readonly(database)
        try:
            with self.assertRaises(FinCLError) as caught:
                build_reporting_context(
                    connection, scope=ReportedScope(valuation_as_of=date(2026, 3, 6))
                )
        finally:
            connection.close()
        self.assertTrue(str(caught.exception).startswith(STALE_GENERATION_REFRESH_REQUIRED))
        self.assertIn("fincl refresh", str(caught.exception))

    def test_no_write_happens_while_detecting_or_refusing_staleness(self) -> None:
        database = build_ledger()

        def _counts() -> tuple[int, int]:
            probe = connect_readonly(database)
            try:
                entries = probe.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
                generations = probe.execute("SELECT count(*) FROM projection_generation").fetchone()[0]
                return int(entries), int(generations)
            finally:
                probe.close()

        before = _counts()
        connection = connect_readonly(database)
        try:
            build_reporting_context(connection, scope=ReportedScope(valuation_as_of=date(2026, 3, 6)))
        finally:
            connection.close()
        self.assertEqual(before, _counts())

    def test_a_fresh_refresh_after_the_stale_write_is_pinned_correctly(self) -> None:
        database = build_ledger()
        new_token = publish_new_generation(database)

        connection = connect_readonly(database)
        try:
            context = build_reporting_context(
                connection, scope=ReportedScope(valuation_as_of=date(2026, 3, 6))
            )
        finally:
            connection.close()
        self.assertEqual(context.generation_token, new_token)


if __name__ == "__main__":
    unittest.main()
