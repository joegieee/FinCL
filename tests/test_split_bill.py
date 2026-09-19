"""Splitting a bill from the Debts page: one atomic batch through ``/api/v1/batch``.

A split records each other person's share as ``EXPENSE_PAID_FOR_OTHER`` on a
receivable (opened in the same batch for someone new) and the owner's share as
the bill's ``ENTRY_RESIDUAL_SET``. When the shares cover the whole bill the
transaction is debt-owned: the inbox stops asking for its category and the
reports count only the owner's share, under the owner's category.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date

from fincl.api import create_app
from fincl.report import economic_flow_report_data
from tests.asgi_client import ASGITestClient
from tests.test_api import _ORIGIN, ApiTestBase


def _alloc(anchor: str, principal: str) -> dict:
    return {"entry_anchor": anchor, "principal_amount": principal, "interest_amount": "0.00",
            "fee_amount": "0.00", "separate_fee_amount": "0.00"}


class SplitBillTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()
        connection = sqlite3.connect(self.database)
        try:
            self.grocer = connection.execute(
                "SELECT natural_anchor FROM ledger_entries WHERE amount_coefficient = -5125"
            ).fetchone()[0]
        finally:
            connection.close()

    def candidates(self, query: str = "") -> list[dict]:
        response = self.client.get(f"/api/v1/debts/split-candidates{query}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["candidates"]

    def split_items(self, *, sam_share: str, my_share: str | None, category: str = "Food & Dining / Groceries") -> list[dict]:
        sam = str(uuid.uuid4())
        items = [
            {"kind": "PERSONAL_DEBT_OPEN", "anchors": {"debt": sam},
             "payload": {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "Sam",
                         "opened_on": "2026-01-20", "due_on": None, "reason": "Split the groceries"}},
            {"kind": "PERSONAL_DEBT_EVENT_SET", "anchors": {"debt": sam, "event": str(uuid.uuid4())},
             "payload": {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
                         "principal_amount": sam_share, "effective_on": "2026-01-20",
                         "entry_allocations": [_alloc(self.grocer, sam_share)], "opening_balance": False,
                         "expense_category": None, "reason": "Split the groceries"}},
        ]
        if my_share is not None:
            items.append({"kind": "ENTRY_RESIDUAL_SET", "anchors": {"entry": self.grocer},
                          "payload": {"expense_amount": my_share, "expense_category": category,
                                      "reason": "Split the groceries"}})
        return items

    def batch(self, items: list[dict]):
        return self.post("/api/v1/batch", {
            "request_id": str(uuid.uuid4()), "expected_generation": self.overview()["generation"],
            "reason": "Split Grocer", "items": items,
        }, self.csrf)

    def test_candidates_are_outgoing_bills_newest_first(self) -> None:
        got = self.candidates()
        self.assertEqual([c["description"] for c in got], ["Grocer", "Bookshop", "Cafe Social"])
        self.assertTrue(all(c["economic_direction"] == "OUT" for c in got))
        self.assertEqual(got[0]["open_amount"], "51.25")
        self.assertEqual([c["description"] for c in self.candidates("?search=book")], ["Bookshop"])

    def test_a_split_saves_in_one_batch_and_leaves_only_my_share_as_spending(self) -> None:
        before = economic_flow_report_data(self.database)["currencies"][0]["spending"]
        response = self.batch(self.split_items(sam_share="30.00", my_share="21.25"))
        self.assertIn(response.status_code, {200, 202}, response.text)

        after = economic_flow_report_data(self.database)["currencies"][0]["spending"]
        self.assertEqual(before, "74.25")
        self.assertEqual(after, "44.25")  # 74.25 less Sam's 30.00

        debts = self.client.get("/api/v1/debts").json()
        (sam,) = debts["owed_to_me"]
        self.assertEqual((sam["counterparty_label"], sam["remaining_principal"]["value"]), ("Sam", "30.00"))

        # Fully explained: no longer offered for a split, and no longer asking for a category.
        self.assertNotIn(self.grocer, {c["entry_anchor"] for c in self.candidates()})
        row = self.client.get(f"/api/v1/transactions/entry?anchor={self.grocer}").json()["entry"]
        self.assertEqual((row["bucket"], row["debt_allocation"]), ("debt", "full"))

    def test_a_bill_i_paid_entirely_for_someone_else_needs_no_share_of_my_own(self) -> None:
        response = self.batch(self.split_items(sam_share="51.25", my_share=None))
        self.assertIn(response.status_code, {200, 202}, response.text)
        self.assertNotIn(self.grocer, {c["entry_anchor"] for c in self.candidates()})

    def test_a_bill_with_its_owner_share_recorded_is_not_offered_again(self) -> None:
        response = self.batch(self.split_items(sam_share="20.00", my_share="21.25"))
        self.assertIn(response.status_code, {200, 202}, response.text)
        # 10.00 is still unclaimed, but a second split would replace my share.
        self.assertNotIn(self.grocer, {c["entry_anchor"] for c in self.candidates()})

    def test_an_invalid_split_records_nothing(self) -> None:
        journal = self.journal_len()
        over = self.batch(self.split_items(sam_share="40.00", my_share="21.25"))
        self.assertEqual(over.status_code, 422, over.text)
        typo = self.batch(self.split_items(sam_share="30.00", my_share="21.25", category="Groceries"))
        self.assertEqual(typo.status_code, 422, typo.text)
        self.assertIn("unknown category", typo.json()["detail"])
        self.assertEqual(self.journal_len(), journal)
        self.assertEqual(self.client.get("/api/v1/debts").json()["owed_to_me"], [])


if __name__ == "__main__":
    import unittest

    unittest.main()


class DebtsClockTests(ApiTestBase):
    def test_debts_are_valued_on_the_apps_today_not_the_hosts(self) -> None:
        """Obligations and the forecast read `clock.today()`; with a pinned clock
        the Debts page must not land on a different day beside them."""
        app = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.root / "evidence", today=date(2031, 1, 15),
        )
        client = ASGITestClient(app, base_url=_ORIGIN)
        self.addCleanup(client.close)
        client.post("/session", json={"secret": "test-secret"},
                    headers={"origin": _ORIGIN, "content-type": "application/json"})

        self.assertEqual(client.get("/api/v1/debts").json()["as_of"], "2031-01-15")
        # An explicit as_of still wins.
        self.assertEqual(client.get("/api/v1/debts?as_of=2030-06-01").json()["as_of"], "2030-06-01")
