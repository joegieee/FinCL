"""Debts in the forecast: money owed back is upside, settled debts ask for nothing."""

from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from fincl.forecast.engine import cash_timeline
from fincl.planning.sufficiency import sufficiency_page
from tests.test_forecast_sources import TODAY, decide, rent_ledger


def _debt(
    database: Path, *, direction: str, due_on: str | None, amount: str = "800.00", label: str = "Jo",
) -> str:
    debt = str(uuid.uuid4())
    decide(database, "PERSONAL_DEBT_OPEN", {"debt": debt}, {
        "direction": direction, "currency": "SGD", "counterparty_label": label,
        "opened_on": "2026-02-01", "due_on": due_on, "reason": "settling later",
    })
    decide(database, "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, {
        "event_kind": "LENT" if direction == "RECEIVABLE" else "BORROWED",
        "direction": direction, "principal_amount": amount, "effective_on": "2026-02-01",
        "entry_allocations": [], "opening_balance": True, "expense_category": None,
        "reason": "opening balance",
    })
    return debt


class ReceivableTests(unittest.TestCase):
    def test_money_owed_back_is_listed_as_upside_and_never_counted(self) -> None:
        database = rent_ledger()
        debt = _debt(database, direction="RECEIVABLE", due_on="2026-09-01")

        timeline = cash_timeline(database, today=TODAY)
        answer = sufficiency_page(database, today=TODAY)["combined_answer"]

        cell = timeline["by_currency"]["SGD"]
        for case in ("base", "conservative"):
            self.assertNotIn(f"debt:{debt}", [e["source_id"] for e in cell[case]["events"]["items"]])
        self.assertEqual(
            [(e["source_id"], e["amount"]) for e in cell["possible_income_upside"]["events"]],
            [(f"debt:{debt}", "800.00")],
        )
        self.assertEqual(
            answer["upside"],
            [{"label": "Jo", "currency": "SGD", "amount": "800.00", "on": "2026-09-01"}],
        )

    def test_money_past_its_date_back_asks_whether_it_came(self) -> None:
        database = rent_ledger()
        _debt(database, direction="RECEIVABLE", due_on="2026-06-01")

        timeline = cash_timeline(database, today=TODAY)
        todos = sufficiency_page(database, today=TODAY)["combined_answer"]["todos"]

        for case in ("base", "conservative"):
            self.assertEqual(timeline["by_currency"]["SGD"][case]["events"]["repositioned"], [])
        overdue = [todo for todo in todos if todo["key"] == "receivable-overdue"]
        self.assertEqual([todo["page"] for todo in overdue], ["Debts"])
        self.assertIn("Jo was due back on 2026-06-01", overdue[0]["detail"])

    def test_only_open_payables_ask_for_a_due_date(self) -> None:
        database = rent_ledger()
        _debt(database, direction="RECEIVABLE", due_on=None)
        payable = _debt(database, direction="PAYABLE", due_on=None, label="Sam")
        decide(database, "PERSONAL_DEBT_EVENT_SET", {"debt": payable, "event": str(uuid.uuid4())}, {
            "event_kind": "PAYABLE_FORGIVEN", "direction": "PAYABLE", "principal_amount": "800.00",
            "effective_on": "2026-03-01", "entry_allocations": [], "opening_balance": False,
            "expense_category": None, "reason": "forgiven",
        })
        decide(database, "PERSONAL_DEBT_CLOSE", {"debt": payable},
               {"effective_on": "2026-03-01", "reason": "forgiven"})
        _debt(database, direction="PAYABLE", due_on=None, amount="50.00", label="Alex")

        todos = sufficiency_page(database, today=TODAY)["combined_answer"]["todos"]

        dates = [todo for todo in todos if todo["key"] == "debt-dates"]
        self.assertEqual(len(dates), 1)
        self.assertEqual(dates[0]["detail"], "Without a date they are not in the answer: Alex.")


if __name__ == "__main__":
    unittest.main()
