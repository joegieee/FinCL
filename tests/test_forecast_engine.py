"""Stage 3B cash timeline traps, one test per required failure mode."""

from __future__ import annotations

import unittest

from fincl.cards.cash_conversion import (
    partition_card_liability,
    reconcile_settlements,
    settlement_plan,
)
from fincl.forecast.engine import (
    COLUMN_DEBT_CARD,
    COLUMN_INCOME,
    COLUMN_IRREGULAR,
    COLUMN_LIFESTYLE,
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_LIKELY,
    DIRECTION_IN,
    DIRECTION_OUT,
    TIMING_INFERRED,
    ForecastEvent,
    ResidualCategory,
    TimelineEvent,
    _action_items,
    _card_events,
    _manifest_categories,
    build_cash_timeline,
    run_timeline,
)


def _manifest(*, balance: str = "1000.00") -> dict:
    return {
        "generation": "gen-3b",
        "as_of": "2026-01-31",
        "stale": False,
        "cutoff": {"supported_cutoff": "2026-01-31", "reasons": []},
        "cash": {
            "by_currency": {
                "SGD": {
                    "readiness": "established-input estimate",
                    "balance": balance,
                    "reason": None,
                }
            }
        },
        "reserve_and_allowance": {
            "by_currency": {
                "SGD": {
                    "reserve": {"available": True, "amount": "200.00"},
                    "allowance": {"available": True},
                }
            }
        },
        "sources": [],
        "limitations": [],
    }


class CashTimelineTrapTests(unittest.TestCase):
    def test_action_items_keep_gap_ownership_and_exact_interval(self) -> None:
        manifest = _manifest()
        manifest["cash"]["by_currency"]["SGD"].update({
            "missing_intervals": [{
                "start": "2026-02-01", "end": "2026-02-14",
                "reason": "statement coverage is missing",
            }],
        })
        manifest["income"] = {"gap": {"reason": "pending receipts are unclassified"}}
        manifest["unresolved_items"] = {"ambiguous_obligations": [{
            "obligation_id": "rent", "label": "Rent", "currency": "SGD",
            "reasons": ["date basis is 'no_stated_date'"],
        }]}

        items = _action_items(manifest, ["card card-1 SGD has no reconciled exactly-once settlement plan"])

        self.assertEqual(items[0]["output"], "cash")
        self.assertIn("2026-02-01 to 2026-02-14", items[0]["detail"])
        self.assertEqual({item["output"] for item in items}, {
            "cash", "income", "obligation_timing", "card_settlement",
        })

    def test_action_items_carry_the_account_and_obligation_they_name(self) -> None:
        """The answer groups and routes fixes by these; a label alone cannot."""
        manifest = _manifest()
        manifest["cash"]["by_currency"]["SGD"]["missing_intervals"] = [{
            "account_id": "everyday", "start": "2026-02-01", "end": "2026-02-14",
            "reason": "statement coverage is missing",
        }]
        manifest["unresolved_items"] = {"ambiguous_obligations": [{
            "obligation_id": "debt:1", "origin": "personal_debt", "label": "Loan",
            "currency": "SGD", "reasons": ["date basis is 'no_stated_date'"],
        }]}

        items = {item["output"]: item for item in _action_items(manifest)}

        self.assertEqual(
            (items["cash"]["account_id"], items["cash"]["start"], items["cash"]["end"]),
            ("everyday", "2026-02-01", "2026-02-14"),
        )
        self.assertEqual(
            (items["obligation_timing"]["origin"], items["obligation_timing"]["label"]),
            ("personal_debt", "Loan"),
        )

    def test_residual_spending_is_not_gated_by_allowance_readiness(self) -> None:
        manifest = _manifest()
        manifest["reserve_and_allowance"]["by_currency"]["SGD"]["allowance"] = {
            "available": False,
            "reason": "essential-role scope is incomplete",
        }
        estimate = {
            "available": True,
            "by_currency": {
                "SGD": {
                    "variable_residual_monthly": "300.00",
                    "variable_residual_max": "450.00",
                    "irregular_allowance_monthly": "50.00",
                }
            },
        }

        categories = _manifest_categories(manifest, estimate)
        result = build_cash_timeline(manifest, estimate=estimate)

        self.assertEqual(len(categories), 2)
        self.assertTrue(result["by_currency"]["SGD"]["available"])
        self.assertEqual(result["by_currency"]["SGD"]["readiness"], "available")

    def test_unsupported_currency_does_not_block_supported_currency(self) -> None:
        manifest = _manifest()
        manifest["cash"]["by_currency"]["THB"] = {
            "readiness": "unavailable",
            "balance": None,
            "reason": "no supported opening balance",
        }
        result = build_cash_timeline(
            manifest,
            residual_categories=[
                ResidualCategory(
                    category_id="routine-sgd", currency="SGD", mean_monthly="100.00",
                    highest_observed_monthly="100.00", label="Routine",
                )
            ],
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["readiness"], "partial")
        self.assertTrue(result["by_currency"]["SGD"]["available"])
        self.assertFalse(result["by_currency"]["THB"]["available"])

    def test_missing_income_labels_the_cash_path_as_a_spending_only_stress_view(self) -> None:
        manifest = _manifest()
        manifest["income"] = {"classified_income_evidenced": False}
        result = build_cash_timeline(
            manifest,
            residual_categories=[
                ResidualCategory(
                    category_id="routine-sgd", currency="SGD", mean_monthly="100.00",
                    highest_observed_monthly="100.00", label="Routine",
                )
            ],
        )

        self.assertTrue(result["by_currency"]["SGD"]["available"])
        self.assertEqual(
            result["by_currency"]["SGD"]["forecast_kind"],
            "spending-only stress view",
        )

    def test_partial_cash_boundary_keeps_its_actual_date_and_gap(self) -> None:
        manifest = _manifest()
        manifest["cash"]["by_currency"]["SGD"].update({
            "readiness": "limited-history estimate",
            "observation_as_of": "2026-01-20",
            "stale_days": 11,
            "missing_intervals": [{"start": "2026-01-21", "end": "2026-01-31", "reason": "coverage missing"}],
        })
        result = build_cash_timeline(
            manifest,
            residual_categories=[
                ResidualCategory(
                    category_id="routine-sgd", currency="SGD", mean_monthly="100.00",
                    highest_observed_monthly="100.00", label="Routine",
                )
            ],
        )

        currency = result["by_currency"]["SGD"]
        self.assertEqual(currency["readiness"], "partial")
        self.assertEqual(currency["cash_cutoff"]["observation_as_of"], "2026-01-20")
        self.assertEqual(currency["cash_cutoff"]["stale_days"], 11)
        self.assertEqual(currency["cash_cutoff"]["missing_intervals"][0]["start"], "2026-01-21")

    def test_positive_month_end_does_not_hide_earlier_intra_month_breach(self) -> None:
        result = run_timeline(
            currency="SGD",
            scale=2,
            opening_minor=10_000,
            start="2026-01-31",
            end="2026-02-28",
            events=[
                TimelineEvent(
                    event_id="rent",
                    source_id="obligation:rent:2026-02",
                    column=COLUMN_LIFESTYLE,
                    direction=DIRECTION_OUT,
                    on="2026-02-05",
                    amount_minor=15_000,
                    label="Rent",
                ),
                TimelineEvent(
                    event_id="salary",
                    source_id="income:salary:2026-02",
                    column=COLUMN_INCOME,
                    direction=DIRECTION_IN,
                    on="2026-02-25",
                    amount_minor=20_000,
                    label="Salary",
                ),
            ],
        )

        self.assertEqual(result["months"][0]["closing"], "150.00")
        self.assertTrue(result["months"][0]["closes_positive_over_an_intra_month_low"])
        self.assertEqual(result["first_zero"]["on"], "2026-02-05")
        self.assertEqual(result["lowest_balance"]["amount"], "-50.00")

    def test_higher_spending_or_lower_income_never_improves_conservative_cash(self) -> None:
        income = ForecastEvent(
            event_id="salary-feb",
            source_id="candidate:salary:feb",
            currency="SGD",
            amount="500.00",
            column=COLUMN_INCOME,
            direction=DIRECTION_IN,
            label="Salary",
            timing_basis=TIMING_INFERRED,
            window_earliest="2026-02-20",
            window_latest="2026-02-25",
            confidence=CONFIDENCE_LIKELY,
            recurring_salary=True,
        )
        residual = ResidualCategory(
            category_id="food",
            currency="SGD",
            mean_monthly="100.00",
            highest_observed_monthly="150.00",
            label="Food",
        )

        result = build_cash_timeline(
            _manifest(), sourced_events=[income], residual_categories=[residual]
        )
        currency = result["by_currency"]["SGD"]

        self.assertTrue(currency["monotonicity"]["holds"])
        for base, conservative in zip(
            currency["base"]["days"], currency["conservative"]["days"], strict=True
        ):
            self.assertLessEqual(
                int(conservative["closing"].replace(".", "")),
                int(base["closing"].replace(".", "")),
            )
        self.assertEqual(
            currency["conservative"]["policy"]["upper_spend_label"],
            "observed stress case",
        )

    def test_existing_card_debt_and_future_card_purchase_settle_once(self) -> None:
        partitions = partition_card_liability(
            billed=[
                {
                    "id": "card_statement:stmt-1:SGD",
                    "statement_id": "stmt-1",
                    "amount": "300.00",
                    "due_on": "2026-02-10",
                }
            ],
            unbilled={"known": True, "amount": "0.00", "items": []},
            prospective=[{"id": "purchase:desk", "on": "2026-02-15", "amount": "200.00"}],
            as_of="2026-01-31",
            latest_period_end="2026-01-28",
        )
        geometry = {
            "available": True,
            "unresolved": [],
            "projected_cycles": [
                {
                    "period_start": "2026-01-29",
                    "period_end": "2026-02-28",
                    "due_on": "2026-03-15",
                }
            ],
        }
        behaviour = {
            "supports_settlement_schedule": True,
            "unresolved": [],
        }
        plan = settlement_plan(
            partitions=partitions,
            geometry=geometry,
            behaviour=behaviour,
            currency="SGD",
            as_of="2026-01-31",
        )
        reconciliation = reconcile_settlements(partitions, plan)
        conversion = {
            "contract_version": "card-cash-conversion-v1",
            "cards": [
                {
                    "account_id": "card-1",
                    "label": "Card",
                    "currency": "SGD",
                    "plan": plan,
                    "reconciliation": reconciliation,
                }
            ],
        }

        events, limitations = _card_events(
            conversion,
            prospective=[
                {
                    "id": "purchase:desk",
                    "account_id": "card-1",
                    "currency": "SGD",
                    "on": "2026-02-15",
                    "amount": "200.00",
                }
            ],
        )
        result = build_cash_timeline(
            _manifest(balance="2000.00"),
            sourced_events=events,
            residual_categories=[
                ResidualCategory(
                    category_id="manifest:routine:SGD",
                    currency="SGD",
                    mean_monthly="200.00",
                    highest_observed_monthly="200.00",
                    label="Routine",
                )
            ],
        )
        base = result["by_currency"]["SGD"]["base"]

        self.assertEqual(limitations, [])
        self.assertTrue(reconciliation["reconciled"])
        self.assertEqual(
            [event.source_id for event in events],
            ["card_statement:stmt-1:SGD", "purchase:desk"],
        )
        self.assertTrue(base["settlement"]["settled_exactly_once"])
        card_rows = [
            event
            for event in base["events"]["items"]
            if event["column"] == COLUMN_DEBT_CARD
        ]
        self.assertEqual(len(card_rows), 2)
        self.assertNotIn("purchase:desk", [event["source_id"] for event in card_rows[:-1]])

    def test_sparse_category_survives_into_forecast(self) -> None:
        result = build_cash_timeline(
            _manifest(balance="5000.00"),
            residual_categories=[
                ResidualCategory(
                    category_id="food",
                    currency="SGD",
                    mean_monthly="300.00",
                    highest_observed_monthly="300.00",
                    label="Food",
                ),
                ResidualCategory(
                    category_id="sparse-medical",
                    currency="SGD",
                    mean_monthly="30.00",
                    highest_observed_monthly="90.00",
                    label="Sparse medical",
                    column=COLUMN_IRREGULAR,
                    provenance={"observed_months": 1, "comparable_months": 3},
                ),
            ],
        )
        base = result["by_currency"]["SGD"]["base"]
        sparse = [
            row for row in base["allocation_metadata"]
            if row["category_id"] == "sparse-medical"
        ]

        self.assertEqual(len(sparse), 6)
        self.assertEqual(sum(int(row["allocated_residual"].replace(".", "")) for row in sparse), 18_000)
        self.assertEqual(base["totals"]["by_column"][COLUMN_IRREGULAR], "180.00")

    def test_conditional_date_remains_labelled_conditional(self) -> None:
        event = ForecastEvent(
            event_id="insurance-renewal",
            source_id="obligation:insurance:renewal-2026",
            currency="SGD",
            amount="250.00",
            column=COLUMN_IRREGULAR,
            direction=DIRECTION_OUT,
            label="Insurance renewal",
            on="2026-04-01",
            confidence=CONFIDENCE_CONFIRMED,
            conditional=True,
            condition="only if the policy renews",
        )

        result = build_cash_timeline(
            _manifest(),
            sourced_events=[event],
            residual_categories=[
                ResidualCategory(
                    category_id="routine",
                    currency="SGD",
                    mean_monthly="0.00",
                    highest_observed_monthly="0.00",
                    label="Routine",
                )
            ],
        )
        applied = result["by_currency"]["SGD"]["base"]["events"]["items"]

        self.assertEqual(len(applied), 1)
        self.assertTrue(applied[0]["conditional"])
        self.assertEqual(applied[0]["condition"], "only if the policy renews")


if __name__ == "__main__":
    unittest.main()
