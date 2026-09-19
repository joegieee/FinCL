"""The classification read model agrees with the reports about debt-owned movements.

`analysis.components` removes personal-debt claims from an entry before
reporting it, so a repayment a debt fully explains is principal, not spending.
`queries._ENTRY_VIEW` -- the transaction list, the classification editor, the
categorization inbox and its completeness figures -- must give the same
answer: such a row is bucket ``debt`` and is not asked for a category,
spending pattern/need or incoming type. A partly allocated row keeps its
ordinary bucket (the reports still count its remainder) and says so.
"""

from __future__ import annotations

import sqlite3
import unittest
import uuid

from fincl.categorization_queries import categorization_candidates, categorization_summary
from fincl.classification import ClassificationDraft, FieldEdit, evaluate_classification_draft
from fincl.db import connect_readonly
from fincl.generation import current_generation
from fincl.queries import transactions_page
from fincl.refresh import refresh_ledger
from tests.test_debts_slice2 import Slice2Base


class DebtAwareClassificationTests(Slice2Base):
    def setUp(self) -> None:
        super().setUp()
        self.full = self.anchor_for(coefficient=11000, direction="OUT")      # Repay Alex -110.00
        self.partial = self.anchor_for(coefficient=4000, direction="OUT")    # Repay Part -40.00
        self.plain = self.anchor_for(coefficient=12000, direction="OUT")     # Shared Flat -120.00
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE",
             "principal_amount": "200.00", "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance owed to Alex"},
        )
        # Principal at scale 0 against a scale-2 entry: "fully allocated" must be
        # an exact comparison at a common scale, not a string or REAL match.
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "110", "effective_on": "2026-03-09",
             "entry_allocations": [self._alloc(self.full, "110")],
             "opening_balance": False, "expense_category": None,
             "reason": "Repaid Alex in full for this transfer"},
        )
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "25.00", "effective_on": "2026-03-12",
             "entry_allocations": [self._alloc(self.partial, "25.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Part of this payment repaid Alex"},
        )
        refresh_ledger(self.database)

    def generation(self) -> str:
        connection = connect_readonly(self.database)
        try:
            return current_generation(connection)["generation_token"]
        finally:
            connection.close()

    def page(self, **filters: str) -> dict[str, dict]:
        result = transactions_page(
            self.database, generation=self.generation(), start="2026-03-01", end="2026-03-31",
            currency="SGD", **filters,
        )
        return {row["anchor"]: row for row in result["entries"]}

    def test_fully_allocated_movement_is_a_debt_row_not_waiting_for_review(self) -> None:
        rows = self.page()
        self.assertEqual(rows[self.full]["bucket"], "debt")
        self.assertEqual(rows[self.full]["debt_allocation"], "full")
        self.assertFalse(rows[self.full]["review"])
        self.assertEqual(rows[self.partial]["bucket"], "spending")
        self.assertEqual(rows[self.partial]["debt_allocation"], "partial")
        self.assertEqual(rows[self.plain]["bucket"], "spending")
        self.assertIsNone(rows[self.plain]["debt_allocation"])

    def test_debt_kind_filter_selects_only_fully_owned_rows(self) -> None:
        self.assertEqual(set(self.page(kind="debt")), {self.full})
        self.assertNotIn(self.full, self.page(kind="review"))

    def test_bucket_agrees_with_the_reported_components(self) -> None:
        """A `debt` row is exactly one whose reported amount component is zero."""
        connection = sqlite3.connect(self.database)
        try:
            reported = dict(connection.execute(
                "SELECT origin_anchor, signed_coefficient FROM analysis_components "
                "WHERE role = 'amount' AND origin_kind = 'ENTRY' AND generation_token = ?",
                (self.generation(),),
            ).fetchall())
        finally:
            connection.close()
        self.assertEqual(reported[self.full], 0)
        self.assertNotEqual(reported[self.partial], 0)
        debt_rows = {anchor for anchor, row in self.page().items() if row["bucket"] == "debt"}
        zeroed = {anchor for anchor, value in reported.items() if value == 0}
        self.assertEqual(debt_rows, zeroed & set(self.page()))

    def test_categorization_inbox_does_not_ask_about_a_debt_owned_movement(self) -> None:
        generation = self.generation()
        cards = categorization_candidates(self.database, generation=generation)["candidates"]
        labels = {card["label"] for card in cards}
        self.assertNotIn("REPAY ALEX", labels)
        self.assertIn("REPAY PART", labels)
        for focus in ("spending_details", "incoming", "conflicts"):
            focused = categorization_candidates(self.database, generation=generation, focus=focus)["candidates"]
            self.assertNotIn("REPAY ALEX", {card["label"] for card in focused}, focus)

    def test_summary_counts_a_debt_owned_movement_as_resolved(self) -> None:
        summary = categorization_summary(self.database, generation=self.generation())
        self.assertEqual(summary["counts"]["debt_allocated"], 1)
        self.assertLessEqual(summary["progress_numerator"], summary["active_categorization_denominator"])
        self.assertEqual(
            summary["active_categorization_denominator"] - summary["progress_numerator"],
            summary["counts"].get("unclassified_active", 0),
        )

    def test_editor_offers_no_field_for_a_debt_owned_movement(self) -> None:
        connection = connect_readonly(self.database)
        try:
            result = evaluate_classification_draft(connection, ClassificationDraft(
                anchors=(self.full,),
                category=FieldEdit("set", "Food & Dining / Groceries"),
                cost_behavior=FieldEdit("set", "variable_routine"),
                reason="should not apply",
            ))
        finally:
            connection.close()
        self.assertEqual(result["commands"], [])
        (row,) = result["rows"]
        self.assertEqual(row["bucket"], "debt")
        for name, outcome in row["fields"].items():
            self.assertFalse(outcome["applicable"], name)
            self.assertIn("personal debt", outcome["reason"], name)


if __name__ == "__main__":
    unittest.main()
