"""Stage 3C temporal-cutoff and no-fabricated-score traps."""

from __future__ import annotations

import unittest

from fincl.forecast.validation import (
    EVALUATION_RECONSTRUCTION,
    STATUS_NO_BACKTEST,
    CashBridge,
    MonthlyObservation,
    ReviewPolicyVersion,
    temporal_holdout_validation,
)


def _observations(heldout_spending: str = "500") -> list[MonthlyObservation]:
    return [
        MonthlyObservation("jan", "2026-01-10", "SGD", "100", "1000", "2026-01-31"),
        MonthlyObservation("feb", "2026-02-10", "SGD", "200", "1000", "2026-02-28"),
        MonthlyObservation("mar", "2026-03-10", "SGD", "300", "1000", "2026-03-31"),
        MonthlyObservation(
            "apr", "2026-04-10", "SGD", heldout_spending, "900", "2026-04-30"
        ),
    ]


class TemporalHoldoutTests(unittest.TestCase):
    def test_heldout_transactions_do_not_alter_training_inputs(self) -> None:
        first = temporal_holdout_validation(
            _observations("500"), heldout_month="2026-04", review_policy_versions=[]
        )
        changed = temporal_holdout_validation(
            _observations("50000"), heldout_month="2026-04", review_policy_versions=[]
        )

        self.assertEqual(first["training"], changed["training"])
        self.assertEqual(
            first["comparisons"][0]["estimators"]["simple_mean"]["estimate"],
            changed["comparisons"][0]["estimators"]["simple_mean"]["estimate"],
        )
        self.assertNotEqual(first["comparisons"][0]["actual"], changed["comparisons"][0]["actual"])

    def test_later_review_version_does_not_leak_into_cutoff_run(self) -> None:
        observations = _observations()
        observations.append(
            MonthlyObservation(
                "late-review-of-feb",
                "2026-02-11",
                "SGD",
                "9000",
                "0",
                "2026-02-28",
                "policy-after",
            )
        )
        result = temporal_holdout_validation(
            observations,
            heldout_month="2026-04",
            review_policy_versions=[
                ReviewPolicyVersion("policy-before", "2026-03-20T00:00:00+00:00", "review"),
                ReviewPolicyVersion("policy-after", "2026-04-20T00:00:00+00:00", "review"),
            ],
        )

        self.assertEqual(result["evaluation_classification"], EVALUATION_RECONSTRUCTION)
        self.assertEqual(result["training"]["eligible_review_policy_versions"], ["policy-before"])
        self.assertEqual(
            result["training"]["excluded_later_review_policy_versions"], ["policy-after"]
        )
        self.assertNotIn("late-review-of-feb", result["training"]["observation_ids"])
        self.assertEqual(
            result["comparisons"][0]["estimators"]["simple_mean"]["estimate"]["spending"],
            "200.00",
        )

    def test_short_history_returns_no_backtest_not_a_number(self) -> None:
        result = temporal_holdout_validation(_observations()[1:], heldout_month="2026-04")

        self.assertEqual(result["status"], STATUS_NO_BACKTEST)
        self.assertIsNone(result["accuracy_score"])
        self.assertEqual(result["comparisons"], [])

    def test_every_missed_reconciled_cash_shortfall_is_reported(self) -> None:
        result = temporal_holdout_validation(
            _observations(),
            heldout_month="2026-04",
            review_policy_versions=[],
            cash_bridges=[CashBridge("SGD", "250", "-10", True)],
        )

        misses = [
            failure
            for failure in result["observed_failures"]
            if failure["kind"] == "missed_cash_shortfall"
        ]
        self.assertEqual(
            {failure["estimator"] for failure in misses},
            {"simple_mean", "prior_month_carry_forward"},
        )
        self.assertTrue(result["comparisons"][0]["cash_bridge"]["reliable"])


if __name__ == "__main__":
    unittest.main()
