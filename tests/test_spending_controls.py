"""Stage 4B spending-control failure modes."""

from __future__ import annotations

import unittest

from fincl.controls.models import (
    apply_what_if_case,
    budget_pace,
    conservative_capacity,
    group_commitments,
)
from fincl.forecast.engine import (
    COLUMN_INCOME,
    COLUMN_LIFESTYLE,
    CONFIDENCE_CONFIRMED,
    DIRECTION_IN,
    DIRECTION_OUT,
    ForecastEvent,
    ResidualCategory,
    TimelineEvent,
    build_cash_timeline,
    run_timeline,
)


def _manifest(balance: str = "1000.00") -> dict:
    return {
        "generation": "controls-test",
        "as_of": "2026-01-31",
        "stale": False,
        "cutoff": {"supported_cutoff": "2026-01-31", "reasons": []},
        "cash": {"by_currency": {"SGD": {"readiness": "supported", "balance": balance}}},
        "reserve_and_allowance": {"by_currency": {"SGD": {"reserve": {"available": True, "amount": "0.00"}, "allowance": {"available": True}}}},
        "sources": [],
        "limitations": [],
    }


class SpendingControlsTests(unittest.TestCase):
    def test_concise_obligation_source_does_not_break_commitment_grouping(self) -> None:
        grouped = group_commitments(
            events=[{
                "event_id": "annual-invoice", "source_id": "debt:annual",
                "direction": DIRECTION_OUT, "amount": "3000.00",
                "label": "Annual insurance invoice", "on": "2027-02-15",
                "provenance": {"source": "personal debt opening balance", "tier": 2},
            }],
            role_by_category={},
        )

        items = [item for group in grouped["groups"].values() for item in group["items"]]
        self.assertEqual(items[0]["edit_href"], "?view=Baseline")

    def test_known_bill_is_counted_once_across_disjoint_groups(self) -> None:
        category_id = "role:SGD:Housing / Rent"
        bill = ForecastEvent(
            event_id="rent:feb", source_id="rent:feb", currency="SGD",
            amount="100.00", column=COLUMN_LIFESTYLE, direction=DIRECTION_OUT,
            label="Rent", on="2026-02-05", confidence=CONFIDENCE_CONFIRMED,
            residual_category=category_id, residual_on="2026-02-05",
            residual_amount="100.00", removes_from_residual=True,
            provenance={"tier": 1, "category_key": "Housing / Rent"},
        )
        result = build_cash_timeline(
            _manifest(), sourced_events=[bill],
            residual_categories=[ResidualCategory(
                category_id=category_id, currency="SGD", mean_monthly="100.00",
                highest_observed_monthly="100.00", label="Rent",
                provenance={"tier": 1, "category_key": "Housing / Rent"},
            )],
        )
        base = result["by_currency"]["SGD"]["base"]
        february = next(row for row in base["months"] if row["month"] == "2026-02")
        grouped = group_commitments(
            events=base["events"]["items"],
            role_by_category={"Housing / Rent": {"tier": 1}},
        )

        self.assertEqual(february["expense"], "100.00")
        self.assertTrue(grouped["disjoint"])
        self.assertEqual(grouped["duplicated_source_ids"], [])
        self.assertEqual(
            sum(
                item["source_id"] == "rent:feb"
                for group in grouped["groups"].values()
                for item in group["items"]
            ),
            1,
        )

    def test_mid_month_pace_uses_the_selected_month_calendar(self) -> None:
        pace = budget_pace(
            month="2026-02", as_of="2026-02-14", limit="280.00",
            observed_spending="140.00", coverage_supported=True,
        )

        self.assertEqual(pace["elapsed_days"], 14)
        self.assertEqual(pace["days_in_month"], 28)
        self.assertEqual(pace["calendar_elapsed_percent"], "50.0")
        self.assertEqual(pace["budget_consumed_percent"], "50.0")
        self.assertEqual(pace["pace_projected_spend"], "280.00")

    def test_cut_improves_cash_without_changing_the_before_case(self) -> None:
        before = run_timeline(
            currency="SGD", scale=2, opening_minor=20_000,
            start="2026-01-31", end="2026-02-28", reserve_minor=0,
            events=[TimelineEvent(
                event_id="dining:feb", source_id="dining:feb",
                column=COLUMN_LIFESTYLE, direction=DIRECTION_OUT,
                on="2026-02-10", amount_minor=10_000, label="Dining",
                provenance={"category_id": "role:SGD:Food & Dining / Dining Out", "tier": 4},
            )],
        )
        original_closing = before["closing_balance"]
        after, unresolved = apply_what_if_case(
            case=before, currency="SGD",
            category_cuts=[{"category_id": "role:SGD:Food & Dining / Dining Out", "monthly_reduction": "20.00"}],
            date_changes=[], hypothetical_income=[],
        )

        self.assertEqual(unresolved, [])
        self.assertEqual(before["closing_balance"], original_closing)
        self.assertEqual(original_closing, "100.00")
        self.assertEqual(after["closing_balance"], "120.00")
        self.assertTrue(all(
            float(after_row["low_point"]) >= float(before_row["low_point"])
            for before_row, after_row in zip(before["days"], after["days"], strict=True)
        ))

    def test_early_shortfall_is_not_hidden_by_later_income(self) -> None:
        case = run_timeline(
            currency="SGD", scale=2, opening_minor=10_000,
            start="2026-01-31", end="2026-02-28", reserve_minor=0,
            events=[
                TimelineEvent(
                    event_id="bill", source_id="bill", column=COLUMN_LIFESTYLE,
                    direction=DIRECTION_OUT, on="2026-02-01", amount_minor=15_000,
                    label="Bill",
                ),
                TimelineEvent(
                    event_id="later-income", source_id="later-income",
                    column=COLUMN_INCOME, direction=DIRECTION_IN,
                    on="2026-02-20", amount_minor=100_000, label="Later income",
                ),
            ],
        )
        capacity = conservative_capacity(
            case=case, earmarked_minor=0, reserve_minor=0, scale=2, supported=True,
        )

        self.assertEqual(case["closing_balance"], "950.00")
        self.assertEqual(capacity["status"], "funding_need")
        self.assertEqual(capacity["funding_need"], "50.00")
        self.assertIsNone(capacity["additional_spending_capacity"])
        self.assertIsNone(capacity["weekly"])


if __name__ == "__main__":
    unittest.main()
