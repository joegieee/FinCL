"""Backend support for the classification editor's "What is this?" hand-offs.

The editor hands one transaction to a debt event (or an installment plan) and
then re-reads that transaction to learn what the decision made of it. Both
reads answer for exactly one anchor and apply the same rules the full pickers
and the transaction list do.
"""

from __future__ import annotations

import unittest
import uuid

from fincl.debts.queries import allocation_candidates
from fincl.errors import FinCLError
from fincl.queries import transaction_entry
from fincl.refresh import refresh_ledger
from tests.test_debts_slice2 import Slice2Base


class DebtHandoffTests(Slice2Base):
    def setUp(self) -> None:
        super().setUp()
        self.outflow = self.anchor_for(coefficient=4000, direction="OUT")   # -40.00
        self.inflow = self.anchor_for(coefficient=200000, direction="IN")   # +2000.00
        self.debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": self.debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance owed to Alex"},
        )

    def candidates(self, anchor: str, kind: str = "PRINCIPAL_REPAID") -> list[dict]:
        return allocation_candidates(self.database, debt_id=self.debt, event_kind=kind, anchor=anchor)["candidates"]

    def test_anchor_returns_exactly_that_transaction_with_its_open_amount(self) -> None:
        (row,) = self.candidates(self.outflow)
        self.assertEqual(row["entry_anchor"], self.outflow)
        self.assertEqual(row["open_amount"], "40.00")

    def test_a_transaction_in_the_wrong_direction_for_the_kind_is_absent(self) -> None:
        # Repaying a payable is an outflow; an inflow cannot back it.
        self.assertEqual(self.candidates(self.inflow), [])

    def test_open_amount_shrinks_once_part_is_allocated(self) -> None:
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": self.debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "15.00", "effective_on": "2026-03-12",
             "entry_allocations": [self._alloc(self.outflow, "15.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Part of this repaid Alex"},
        )
        (row,) = self.candidates(self.outflow)
        self.assertEqual(row["open_amount"], "25.00")


class TransactionEntryTests(Slice2Base):
    def test_rereads_one_row_at_the_current_generation_after_a_debt_decision(self) -> None:
        anchor = self.anchor_for(coefficient=4000, direction="OUT")
        before = transaction_entry(self.database, anchor)
        self.assertEqual(before["entry"]["bucket"], "spending")
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance owed to Alex"},
        )
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "40.00", "effective_on": "2026-03-12",
             "entry_allocations": [self._alloc(anchor, "40.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Repaid Alex"},
        )
        refresh_ledger(self.database)
        after = transaction_entry(self.database, anchor)
        self.assertNotEqual(after["generation"], before["generation"])
        self.assertEqual(after["entry"]["anchor"], anchor)
        self.assertEqual(after["entry"]["bucket"], "debt")
        self.assertEqual(after["entry"]["debt_allocation"], "full")

    def test_an_unknown_anchor_is_refused(self) -> None:
        with self.assertRaisesRegex(FinCLError, "not in the selected ledger"):
            transaction_entry(self.database, "no-such-anchor")


if __name__ == "__main__":
    unittest.main()
