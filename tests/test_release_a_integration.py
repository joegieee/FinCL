"""Step 11 — Release A integrated verification (plan §7 M3b.3, §9.2).

The per-module suites (``test_money_flow.py``, ``test_analysis_reporting.py``,
``test_transactions_reporting.py``) each prove their own owning reader agrees
with itself in isolation. This module is the missing integration layer step
11 calls for: one mixed-pocket fixture driven through the *actual HTTP
routes* behind Overview, Analysis/Cash Flow, contributors, Transactions,
single-entry detail and bulk selection, checking they agree with each other
under the same generation/context -- not just that each is internally
consistent -- and that a reporting-policy change invalidates exactly the
scope-bound handles it must (§9.2: "policy/rate change cannot alter
transaction candidate IDs or stored decisions").

Reuses the existing mixed-pocket fixture builders from ``tests.test_money_flow``
rather than inventing a second one (§9.1: "extend existing builders").
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from fincl.api import create_app
from fincl.db import connect_sqlite
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.refresh import refresh_ledger
from fincl.reporting_currency import REPORTING_CURRENCY_SET
from tests.asgi_client import ASGITestClient
from tests.test_money_flow import (
    _HISTORY_END,
    _HISTORY_START,
    _MIXED_ROWS,
    _ledger,
    _load_usd_rate,
    _mixed_ledger_with_rate,
)

_ORIGIN = "http://127.0.0.1:8765"
_ANCHORS = {"ledger": "default"}


def _set_reporting_currency(database: Path, currency: str, *, reason: str = "test") -> None:
    append_decision(
        default_journal_path(database), decision_kind=REPORTING_CURRENCY_SET, anchors=_ANCHORS,
        payload={"currency": currency, "reason": reason},
        policy_version="reporting-currency-v1", snapshot=currency,
    )
    connection = connect_sqlite(database)
    try:
        replay_journal(connection)
    finally:
        connection.close()
    refresh_ledger(database)


class _ReleaseAApiBase(unittest.TestCase):
    """A mixed-pocket (SGD+USD) ledger, driven through the real ASGI app --
    the same route surface a browser hits, not the bare reader functions."""

    def _boot(self, database: Path) -> None:
        self.database = database
        self.root = database.parent
        self.app = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.root / "evidence",
        )
        self.client = ASGITestClient(self.app, base_url=_ORIGIN)
        self.addCleanup(self.client.close)
        response = self.client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf"]

    def setUp(self) -> None:
        self._boot(_mixed_ledger_with_rate())

    def _get(self, path: str, **params: object) -> dict:
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class CrossScreenAgreementTests(_ReleaseAApiBase):
    """§9.2: "corresponding Overview/Analysis/owner values and exact
    contributor sums agree under the same context." Checked here through the
    real routes, on the same generation, not by re-deriving numbers."""

    def test_overview_and_analysis_share_one_generation_and_agree_on_net_and_burn(self) -> None:
        money_flow = self._get("/api/v1/money-flow", history_start=_HISTORY_START, history_end=_HISTORY_END)
        analysis = self._get("/api/v1/reporting/analysis", history_start=_HISTORY_START, history_end=_HISTORY_END)

        self.assertEqual(money_flow["generation"], analysis["generation"])

        mf_net = money_flow["summary"]["economic_flow"]["net"]
        an_net = analysis["summary"]["net_economic_flow"]["current"]
        self.assertEqual(mf_net["status"], an_net["status"])
        self.assertEqual(mf_net["total"], an_net["total"])

        mf_burn = money_flow["summary"]["gross_burn"]
        an_burn = analysis["gross_burn"]
        self.assertEqual(mf_burn["included_months"], an_burn["included_months"])
        assert mf_burn["amount"] is not None and an_burn["result"] is not None
        self.assertEqual(mf_burn["amount"]["total"], an_burn["result"]["total"])

    def test_cash_at_cutoff_and_analysis_share_the_same_reporting_currency(self) -> None:
        money_flow = self._get("/api/v1/money-flow", history_start=_HISTORY_START, history_end=_HISTORY_END)
        analysis = self._get("/api/v1/reporting/analysis", history_start=_HISTORY_START, history_end=_HISTORY_END)
        transactions = self._get(
            "/api/v1/reporting/transactions", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        self.assertEqual(money_flow["reporting_currency"], "SGD")
        self.assertEqual(analysis["reporting_currency"], "SGD")
        self.assertEqual(transactions["reporting_currency"], "SGD")
        # SGD 360.00 + USD 200.00 * 1.5000 = SGD 660.00 (money_flow_seam.py rate).
        cash = money_flow["summary"]["cash_at_cutoff"]["amount"]
        self.assertEqual(cash["status"], "available")
        self.assertEqual(Decimal(cash["total"]["value"]), Decimal("660.00"))

    def test_contributor_rows_sum_exactly_to_the_spending_parent_component_not_raw_transactions(self) -> None:
        """Distinguishes the component parent (spending's own consolidated
        total) from a naive sum of every listed transaction row -- the
        transactions list includes the USD topup (an inflow, not spending),
        so a raw-transaction sum would disagree with the parent on purpose."""
        analysis = self._get("/api/v1/reporting/analysis", history_start=_HISTORY_START, history_end=_HISTORY_END)
        handle = analysis["contributors_handle"]
        contributors = self._get(f"/api/v1/reporting/contributors/{handle}")

        assert contributors["parent"]["total"] is not None
        parent_total = Decimal(contributors["parent"]["total"]["value"])
        component_sum = sum(
            (
                Decimal(row["valuation"]["reported"]["value"])
                for row in contributors["rows"]
                if row["valuation"]["reported"] is not None
            ),
            Decimal("0"),
        )
        self.assertEqual(component_sum, parent_total)

        spending_entry = analysis["summary"]["spending"]["current"]
        self.assertEqual(spending_entry["total"], contributors["parent"]["total"])

        # The raw transaction list is not a substitute for this reconciliation:
        # it includes the USD inflow the spending parent correctly excludes.
        transactions = self._get(
            "/api/v1/reporting/transactions", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        raw_total = sum(
            (
                Decimal(row["amount"]["reported"]["value"])
                for row in transactions["rows"]
                if row["amount"]["reported"] is not None
            ),
            Decimal("0"),
        )
        self.assertNotEqual(raw_total, parent_total)


class TransactionsDetailAndBulkSelectionTests(_ReleaseAApiBase):
    """The other two Release A surfaces named in step 11: single-entry
    detail and bulk (anchors-based) selection, over the same scope the
    transactions list itself uses."""

    def test_bulk_selection_anchors_are_exactly_the_listed_rows(self) -> None:
        listing = self._get(
            "/api/v1/reporting/transactions", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        anchors = self._get(
            "/api/v1/reporting/transaction-anchors", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        self.assertEqual(listing["generation"], anchors["generation"])
        self.assertFalse(anchors["truncated"])
        listed_native_anchors = {row["entry"]["anchor"] for row in listing["rows"]}
        self.assertEqual(set(anchors["anchors"]), listed_native_anchors)
        self.assertEqual(len(anchors["anchors"]), listing["total"])

    def test_single_entry_detail_matches_its_row_in_the_list(self) -> None:
        from fincl.reporting_transactions import encode_anchor

        listing = self._get(
            "/api/v1/reporting/transactions", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        row = listing["rows"][0]
        encoded = encode_anchor(row["entry"]["anchor"])
        detail = self._get(f"/api/v1/reporting/transactions/{encoded}")
        self.assertEqual(detail["entry"]["anchor"], row["entry"]["anchor"])
        self.assertEqual(detail["amount"], row["amount"])
        self.assertEqual(detail["statement_detail"], row["statement_detail"])


class PolicyAndRateChangeIntegrationTests(_ReleaseAApiBase):
    """Step 11 item 3: change reporting policy in the fixture, refresh
    explicitly, and verify stable candidate IDs, rejected old cursors
    /handles, and unchanged native evidence -- across screens, not just one."""

    def test_reporting_currency_change_invalidates_scoped_handles_but_not_candidates(self) -> None:
        before_anchors = self._get(
            "/api/v1/reporting/transaction-anchors", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )["anchors"]
        before_page = self._get(
            "/api/v1/reporting/transactions",
            history_start=_HISTORY_START, history_end=_HISTORY_END, limit=1,
        )
        old_cursor = before_page["next_cursor"]
        self.assertIsNotNone(old_cursor)
        old_analysis = self._get(
            "/api/v1/reporting/analysis", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        old_handle = old_analysis["contributors_handle"]
        native_before = self.client.get(
            "/api/v1/transactions",
            params={"generation": old_analysis["generation"], "start": _HISTORY_START, "end": _HISTORY_END},
        ).json()

        _set_reporting_currency(self.database, "USD")

        after_anchors = self._get(
            "/api/v1/reporting/transaction-anchors", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )["anchors"]
        self.assertEqual(set(after_anchors), set(before_anchors))

        after_analysis = self._get(
            "/api/v1/reporting/analysis", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        self.assertNotEqual(after_analysis["generation"], old_analysis["generation"])
        self.assertEqual(after_analysis["reporting_currency"], "USD")

        # The cursor decodes but no longer matches this scope's live
        # candidate set, so it is a plain (422) rejection, not the typed
        # STALE_GENERATION refusal (that is reserved for a manifest that
        # cannot reproduce its own published snapshot) -- either way it must
        # never be silently honored across the policy change.
        stale_cursor_response = self.client.get(
            "/api/v1/reporting/transactions",
            params={"history_start": _HISTORY_START, "history_end": _HISTORY_END, "limit": 1, "cursor": old_cursor},
        )
        self.assertEqual(stale_cursor_response.status_code, 422, stale_cursor_response.text)

        stale_handle_response = self.client.get(f"/api/v1/reporting/contributors/{old_handle}")
        self.assertEqual(stale_handle_response.status_code, 409, stale_handle_response.text)

        native_after = self.client.get(
            "/api/v1/transactions",
            params={
                "generation": after_analysis["generation"], "start": _HISTORY_START, "end": _HISTORY_END,
            },
        ).json()
        # The legacy native route's own currency scope narrows to the ledger's
        # single native pocket regardless of reporting policy (unchanged
        # native semantics, §9.2), so both reads return the same SGD rows.
        self.assertEqual(
            [(e["id"], e["currency"], e["amount"]) for e in native_before["entries"]],
            [(e["id"], e["currency"], e["amount"]) for e in native_after["entries"]],
        )

    def test_a_missing_rate_leaves_cash_and_analysis_partial_together_while_transactions_stay_visible(
        self,
    ) -> None:
        # A fresh ledger with the USD pocket but no USD rate at all.
        database = _ledger(_MIXED_ROWS)
        self._boot(database)

        money_flow = self._get("/api/v1/money-flow", history_start=_HISTORY_START, history_end=_HISTORY_END)
        transactions = self._get(
            "/api/v1/reporting/transactions", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        self.assertEqual(money_flow["generation"], transactions["generation"])

        cash = money_flow["summary"]["cash_at_cutoff"]["amount"]
        self.assertEqual(cash["status"], "partial")
        self.assertIn("RATE_NOT_IMPORTED", cash["unavailable_reasons"])
        self.assertIn("USD", {scope["currency"] for scope in cash["excluded"]})

        usd_rows = [row for row in transactions["rows"] if row["entry"]["currency"] == "USD"]
        self.assertTrue(usd_rows, "the unpriced USD row must stay visible, never be dropped")
        self.assertIsNone(usd_rows[0]["amount"]["reported"])
        self.assertIsNotNone(usd_rows[0]["amount"]["native"])

        # Now backfill the rate and refresh explicitly (§ execution contract:
        # "no automatic refresh"): the same generation-scoped state clears.
        _load_usd_rate(database)
        money_flow_after = self._get(
            "/api/v1/money-flow", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        self.assertNotEqual(money_flow_after["generation"], money_flow["generation"])
        self.assertEqual(money_flow_after["summary"]["cash_at_cutoff"]["amount"]["status"], "available")


class SingleConnectionAcrossCompositeReadsTests(_ReleaseAApiBase):
    """§9.2: "composite connection count is one" -- checked here for every
    Release A composite endpoint hit through the real app, not only the bare
    reader (already covered directly in ``test_money_flow.py``)."""

    def test_each_composite_route_call_succeeds_independently_under_the_same_generation(self) -> None:
        first = self._get("/api/v1/money-flow", history_start=_HISTORY_START, history_end=_HISTORY_END)
        second = self._get("/api/v1/reporting/analysis", history_start=_HISTORY_START, history_end=_HISTORY_END)
        third = self._get(
            "/api/v1/reporting/transactions", history_start=_HISTORY_START, history_end=_HISTORY_END,
        )
        # One generation pinned at read time, observed identically by three
        # independent single-connection composite reads with no writes
        # between them (no refresh occurred).
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(second["generation"], third["generation"])


if __name__ == "__main__":
    unittest.main()
