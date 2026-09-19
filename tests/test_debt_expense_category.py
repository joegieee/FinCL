"""A debt's expense category comes from the same taxonomy as every other category.

The owner's share of a split bill (``ENTRY_RESIDUAL_SET``) and an expense
someone paid for me (``EXPENSE_PAID_FOR_ME``) are reported as spending under
their ``expense_category``. That key used to be free text, so a typo or a bare
"Dining" became a report line no category view knew about. New decisions --
single or batched -- must now name a current (or custom) spending category;
journals written before the check still replay.
"""

from __future__ import annotations

import uuid

from fincl.application import record_batch, record_decision
from fincl.errors import FinCLError
from fincl.refresh import refresh_ledger
from fincl.report import economic_flow_report_data
from tests.test_debts_slice2 import Slice2Base, _intent


def _paid_for_me(category: str) -> dict:
    return {"event_kind": "EXPENSE_PAID_FOR_ME", "direction": "PAYABLE",
            "principal_amount": "25.00", "effective_on": "2026-03-06",
            "entry_allocations": [], "opening_balance": False,
            "expense_category": category, "reason": "Alex covered dinner"}


class DebtExpenseCategoryTests(Slice2Base):
    def _spending(self) -> str:
        return economic_flow_report_data(self.database)["currencies"][0]["spending"]

    def test_an_unknown_category_is_refused(self) -> None:
        debt = self.open_debt("PAYABLE")
        with self.assertRaisesRegex(FinCLError, "unknown category 'Dining'"):
            self.record("PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, _paid_for_me("Dining"))

    def test_internal_and_unclassified_are_not_spending_categories(self) -> None:
        debt = self.open_debt("PAYABLE")
        for category in ("Internal / Transfer", "Unclassified / Unclassified"):
            with self.subTest(category), self.assertRaisesRegex(FinCLError, "not a spending category"):
                self.record("PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, _paid_for_me(category))

    def test_a_residual_needs_a_taxonomy_category_too(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        with self.assertRaisesRegex(FinCLError, "unknown category"):
            self.record("ENTRY_RESIDUAL_SET", {"entry": anchor},
                        {"expense_amount": "40.00", "expense_category": "Housing", "reason": "my share"})

    def test_a_batch_is_held_to_the_same_rule(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        items = [{"kind": "ENTRY_RESIDUAL_SET", "anchors": {"entry": anchor},
                  "payload": {"expense_amount": "40.00", "expense_category": "Housing", "reason": "mine"}}]
        with self.assertRaisesRegex(FinCLError, "unknown category"):
            record_batch(self.database, request_id=str(uuid.uuid4()), intent_hash="b1", items=items, reason="split")

    def test_a_custom_category_is_accepted(self) -> None:
        record_decision(
            self.database, request_id="c1", intent_hash="c1", decision_kind="CATEGORY_CREATE",
            anchors={"category": "Social / Dinners Out"},
            payload={"category": "Social / Dinners Out", "reason": "tracking dinners with friends"},
            policy_version="test", snapshot="create",
        )
        debt = self.open_debt("PAYABLE")
        self.record("PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())},
                    _paid_for_me("Social / Dinners Out"))
        (event,) = [row for row in self.rows("personal_debt_events") if row["event_kind"] == "EXPENSE_PAID_FOR_ME"]
        self.assertEqual(event["expense_category"], "Social / Dinners Out")

    def test_a_journal_written_with_free_text_still_replays(self) -> None:
        debt = self.open_debt("PAYABLE")
        payload = _paid_for_me("Dining")
        anchors = {"debt": debt, "event": str(uuid.uuid4())}
        # Appended directly, as a record from before the check would have been.
        record_decision(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent("PERSONAL_DEBT_EVENT_SET", anchors, payload),
            decision_kind="PERSONAL_DEBT_EVENT_SET", anchors=anchors, payload=payload,
            policy_version="personal-debt-v1", snapshot="legacy",
        )
        refresh_ledger(self.database)
        self.assertEqual(self._spending(), "295.00")


if __name__ == "__main__":
    import unittest

    unittest.main()
