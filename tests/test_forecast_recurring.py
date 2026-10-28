"""Recurring commitments against the routine residual: counted once, on schedule."""

from __future__ import annotations

import unittest

from fincl.baseline.estimators import baseline_estimate
from fincl.forecast.engine import cash_timeline
from fincl.planning.sufficiency import sufficiency_page
from tests.test_forecast_sources import TODAY, base_events, decide, rent_ledger


class RecurringResidualTests(unittest.TestCase):
    def test_a_linked_commitment_leaves_the_rest_of_routine_spending_alone(self) -> None:
        database = rent_ledger()

        timeline = cash_timeline(database, today=TODAY)

        cell = timeline["by_currency"]["SGD"]
        july = next(
            row for row in cell["base"]["allocation_metadata"]
            if row["category_id"] == "manifest:routine:SGD" and row["month"] == "2026-07"
        )
        self.assertEqual((july["monthly_assumption"], july["dated_costs_removed"]), ("300.00", []))
        self.assertEqual(july["allocated_residual"], "300.00")
        self.assertTrue(cell["available"], cell["base"]["limitations"])
        self.assertIn(("2026-07-05", "1200.00"), base_events(timeline, "recurring:rent:"))
        answer = sufficiency_page(database, today=TODAY)["combined_answer"]
        self.assertEqual([pocket["counted"] for pocket in answer["pockets"]], ["projected"])

    def test_an_ended_commitment_is_not_spent_again_through_the_residual(self) -> None:
        database = rent_ledger()
        decide(database, "RECURRING_COMMITMENT_SET", {"commitment": "rent"}, {
            "name": "Rent", "currency": "SGD", "merchant_key": "Landlord",
            "account_ids": [], "seed_entry_anchors": [],
            "schedule": {"kind": "monthly", "day": 5, "anchor_on": "2026-01-05"},
            "expected_amount": "1200.00", "amount_tolerance": "0.00",
            "status": "ended", "ended_on": "2026-06-30", "reason": "moved out",
        })

        estimate = baseline_estimate(database, today=TODAY)

        self.assertEqual(estimate["by_currency"]["SGD"]["variable_residual_monthly"], "300.00")
        self.assertEqual(base_events(cash_timeline(database, today=TODAY), "recurring:rent:"), [])


if __name__ == "__main__":
    unittest.main()
