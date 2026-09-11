"""M3a acceptance (§8): the snapshot-safe reader seam over the money-flow graph.

No behaviour changed — every ``foo(database: Path, ...)`` in
``docs/M3A-money-flow-call-graph.md`` became a three-line wrapper delegating to
``foo_from(connection, ...)``, and every reader that used to open its own
`Path`-taking sub-reads now passes its own connection down instead. These
tests discharge the four M3a-specific acceptance items that are not just
"existing tests still pass":

* one composite read opens exactly one connection and one read transaction;
* a concurrent refresh published mid-read never reaches an already-open
  composite read (Invariant 11 — one generation per composite read);
* net worth composed through the seam matches net worth composed before the
  seam existed, on a fixture where no refresh intervenes;
* (p95 latency against the §5.5 budget is recorded separately by
  ``scripts/bench_money_flow.py`` — a latency measurement is a benchmark, not
  a pass/fail unit test, since CI hardware speed is not a contract.)
"""

from __future__ import annotations

import unittest
from datetime import date

from fincl.db import connect_readonly
from fincl.forecast import cash_timeline
from fincl.generation import current_generation
from fincl.networth import net_worth_page
from fincl.networth.queries import net_worth_page_from

from .money_flow_seam import build_ledger, count_sqlite_connections, publish_new_generation

_TODAY = date(2026, 3, 6)


class ConnectionCountingTests(unittest.TestCase):
    """One composite read opens exactly one connection (§8 M3a)."""

    def test_cash_timeline_opens_exactly_one_connection(self) -> None:
        database = build_ledger()

        with count_sqlite_connections() as counter:
            cash_timeline(database, today=_TODAY)

        self.assertEqual(counter["n"], 1)

    def test_net_worth_page_with_a_trend_opens_exactly_one_connection(self) -> None:
        # The two-observation case (§4.1 of the enumeration): net_worth_page
        # recurses into itself for ``prior_as_of``, which is exactly the case
        # the doc calls out as doubling the connection count under the old code.
        database = build_ledger()

        with count_sqlite_connections() as counter:
            net_worth_page(
                database, as_of="2026-03-06", prior_as_of="2026-02-06", today=_TODAY
            )

        self.assertEqual(counter["n"], 1)


class SeamEquivalenceTests(unittest.TestCase):
    """Net worth composed through the seam equals net worth composed today,
    on a fixture where no refresh intervenes (§8 M3a)."""

    def test_net_worth_trend_matches_the_pre_seam_expected_values(self) -> None:
        database = build_ledger()

        page = net_worth_page(
            database, as_of="2026-03-06", prior_as_of="2026-02-06", today=_TODAY
        )

        trend = page["trend"]
        self.assertEqual(trend["prior_as_of"], "2026-02-06")
        self.assertEqual(trend["current_as_of"], "2026-03-06")
        change = trend["change_by_currency"]["SGD"]
        self.assertEqual(change["prior_tracked_net_worth"], "860.00")
        self.assertEqual(change["current_tracked_net_worth"], "360.00")
        self.assertEqual(change["delta"], "-500.00")
        self.assertTrue(change["both_complete"])
        self.assertFalse(trend["scope_changed"])
        self.assertEqual(page["per_currency"]["SGD"]["tracked_assets"], "360.00")


class ConcurrentRefreshTests(unittest.TestCase):
    """``concurrent_refresh`` (§9.1): a refresh published mid-read proves
    every figure in one response comes from a single generation (§8 M3a)."""

    def test_a_refresh_published_mid_read_never_reaches_the_open_composite_read(
        self,
    ) -> None:
        database = build_ledger()
        connection = connect_readonly(database)
        try:
            connection.execute("BEGIN")
            # One real read pins this connection's snapshot (verified against
            # SQLite's own WAL semantics before this fixture was written).
            pinned_generation = str(current_generation(connection)["generation_token"])

            republished_generation = publish_new_generation(database)
            self.assertNotEqual(pinned_generation, republished_generation)

            page = net_worth_page_from(
                connection, as_of="2026-03-06", prior_as_of="2026-02-06", today=_TODAY
            )
        finally:
            connection.close()

        # Every figure in the response — the current observation, the prior
        # observation folded into the same transaction, and the trend built
        # from both — comes from the generation that was current when this
        # composite read's connection first read, not the one published
        # underneath it mid-read.
        self.assertEqual(page["generation"], pinned_generation)
        change = page["trend"]["change_by_currency"]["SGD"]
        self.assertEqual(change["prior_tracked_net_worth"], "860.00")
        self.assertEqual(change["current_tracked_net_worth"], "360.00")
        self.assertTrue(change["both_complete"])

    def test_a_fresh_read_after_the_refresh_observes_the_new_generation(self) -> None:
        # The other half of the invariant: nothing here holds a snapshot open
        # forever — a *new* composite read started after the refresh commits
        # sees it immediately.
        database = build_ledger()
        before = connect_readonly(database)
        try:
            original_generation = str(current_generation(before)["generation_token"])
        finally:
            before.close()

        republished_generation = publish_new_generation(database)

        after = connect_readonly(database)
        try:
            after.execute("BEGIN")
            observed = str(current_generation(after)["generation_token"])
        finally:
            after.close()

        self.assertNotEqual(original_generation, republished_generation)
        self.assertEqual(observed, republished_generation)


if __name__ == "__main__":
    unittest.main()
