"""The landing page's statement reach and what is owed (fincl.journey)."""
from __future__ import annotations

import unittest
import uuid
from datetime import date

from fincl.journey import money_journey
from fincl.refresh import refresh_ledger
from tests.test_debts_slice2 import Slice2Base


class MoneyJourneyTests(Slice2Base):
    def _owe(self, direction: str, amount: str) -> None:
        debt = self.open_debt(direction)
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED" if direction == "PAYABLE" else "LENT", "direction": direction,
             "principal_amount": amount, "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True, "expense_category": None,
             "reason": "Opening balance"},
        )

    def test_statement_reach_names_each_account_and_how_far_behind_it_is(self) -> None:
        reach = money_journey(self.database, today=date(2026, 6, 1))["statements"]
        [account] = reach["accounts"]
        self.assertEqual(reach["complete_through"], account["through"])
        self.assertEqual(account["days_behind"], (date(2026, 6, 1) - date.fromisoformat(account["through"])).days)
        self.assertTrue(account["behind"])
        self.assertEqual(reach["behind_count"], 1)

    def test_what_you_owe_is_grouped_and_separate_from_what_you_are_owed(self) -> None:
        self._owe("PAYABLE", "300.00")
        self._owe("PAYABLE", "200.00")
        self._owe("RECEIVABLE", "80.00")
        refresh_ledger(self.database)
        owed = money_journey(self.database, today=date(2026, 6, 1))["owed"]
        debts = next(g for g in owed["groups"] if g["origin"] == "personal_debt")
        self.assertEqual((debts["count"], debts["total"], debts["by_currency"]), (2, "500.00", {"SGD": "500.00"}))
        self.assertEqual(owed["total"], "500.00")
        self.assertEqual((owed["owed_to_you"]["count"], owed["owed_to_you"]["total"]), (1, "80.00"))


if __name__ == "__main__":
    unittest.main()
