"""Cash-flow usability M6 — baseline/forecast refinement.

Per-month matching of confirmed recurring cost, the regime and sensitivity
views (§5.5), and the outlier-derived ``irregular`` proposal carrying the
``suggested`` review state (§3.1, §3.2).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from fincl.application import record_decision
from fincl.baseline import baseline_estimate, baseline_roles
from fincl.baseline.estimators import _regime, _sensitivity
from fincl.db import connect_readonly
from fincl.decisions import entry_anchor
from fincl.document_service import DocumentImportService
from fincl.financial_roles import effective_financial_role
from fincl.generation import current_generation
from fincl.queries import transactions_page
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes


def _revolut_ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


class RegimeUnitTests(unittest.TestCase):
    def test_a_steady_window_raises_no_warning(self) -> None:
        months = ["2026-01", "2026-02", "2026-03", "2026-04"]
        regime = _regime(months, [Decimal("500"), Decimal("510"), Decimal("495"), Decimal("505")])
        self.assertFalse(regime["warning"])
        self.assertEqual(regime["reasons"], [])
        self.assertEqual(regime["leading_empty_months"], 0)

    def test_activity_that_begins_mid_window_is_flagged(self) -> None:
        months = ["2026-01", "2026-02", "2026-03", "2026-04"]
        regime = _regime(months, [Decimal("0"), Decimal("0"), Decimal("400"), Decimal("420")])
        self.assertTrue(regime["warning"])
        self.assertEqual(regime["leading_empty_months"], 2)
        self.assertIn("2026-03", regime["reasons"][0])

    def test_a_material_recent_shift_is_flagged_without_a_probability_claim(self) -> None:
        months = ["2026-01", "2026-02", "2026-03", "2026-04"]
        regime = _regime(months, [Decimal("200"), Decimal("210"), Decimal("600"), Decimal("640")])
        self.assertTrue(regime["warning"])
        joined = " ".join(regime["reasons"])
        self.assertIn("materially higher", joined)
        self.assertNotIn("probability", joined.lower())
        self.assertNotIn("%", joined)


class SensitivityUnitTests(unittest.TestCase):
    def test_the_single_highest_month_is_excluded_and_labelled_not_a_probability(self) -> None:
        months = ["2026-01", "2026-02", "2026-03"]
        result = _sensitivity(months, [Decimal("100"), Decimal("400"), Decimal("130")], Decimal("50"), 2)
        self.assertTrue(result["available"])
        self.assertTrue(result["not_a_probability"])
        self.assertEqual(result["excluded_month"], "2026-02")
        self.assertEqual(result["excluded_routine_residual"], "400.00")
        # mean of the two kept residuals, then the flat fixed cost added back.
        self.assertEqual(result["variable_residual_monthly"], "115.00")
        self.assertEqual(result["routine_lifestyle_baseline"], "165.00")

    def test_a_one_month_window_has_nothing_to_exclude(self) -> None:
        result = _sensitivity(["2026-01"], [Decimal("100")], Decimal("0"), 2)
        self.assertFalse(result["available"])
        self.assertIn("nothing to exclude", result["reason"])


class EffectiveRoleOutlierTests(unittest.TestCase):
    def _entry(self, **over: object) -> dict[str, object]:
        base: dict[str, object] = {
            "bucket": "spending",
            "category": "Food & Dining / Groceries",
            "currency_pocket": "SGD",
            "amount_coefficient": "-90000",
            "amount_scale": "2",
            "account_id": "a1",
            "description_text": "",
        }
        base.update(over)
        return base

    def test_an_outlier_becomes_a_suggested_irregular_proposal(self) -> None:
        role = effective_financial_role(self._entry(outlier_irregular=True), [])
        self.assertEqual(role["cost_behavior"], "irregular")
        self.assertEqual(role["cost_behavior_basis"], "outlier review")
        self.assertEqual(role["role_review_state"], "suggested")

    def test_a_manual_override_is_never_replaced_by_the_suggestion(self) -> None:
        role = effective_financial_role(
            self._entry(outlier_irregular=True, cost_behavior="fixed_recurring"), []
        )
        self.assertEqual(role["cost_behavior"], "fixed_recurring")
        self.assertEqual(role["cost_behavior_basis"], "manual")
        self.assertEqual(role["role_review_state"], "confirmed")

    def test_a_confirmed_recurring_occurrence_outranks_the_suggestion(self) -> None:
        role = effective_financial_role(
            self._entry(outlier_irregular=True, recurring_confirmed=True), []
        )
        self.assertEqual(role["cost_behavior"], "fixed_recurring")
        self.assertEqual(role["cost_behavior_basis"], "recurring evidence")
        self.assertNotEqual(role["role_review_state"], "suggested")


def _rows() -> list[list[str]]:
    rows: list[list[str]] = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "3000.00", "0.00", "SGD", "COMPLETED", "3000.00"],
    ]
    balance = Decimal("3000.00")
    for month in ("01", "02", "03"):
        for day, merchant, amount in (
            ("02", "Landlord", "500.00"),
            ("09", "Corner Shop", "20.00"),
        ):
            balance -= Decimal(amount)
            rows.append([
                "Card Payment", "Current", f"2026-{month}-{day} 10:00:00",
                f"2026-{month}-{day} 10:00:01", merchant, f"-{amount}",
                "0.00", "SGD", "COMPLETED", f"{balance}",
            ])
    return rows


class RecurringMatchingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _revolut_ledger(_rows())
        connection = connect_readonly(self.database)
        try:
            landlord = connection.execute(
                "SELECT id, effective_at FROM ledger_entries "
                "WHERE amount_coefficient = -50000 ORDER BY effective_at"
            ).fetchall()
            self.assertEqual(len(landlord), 3)
            self.anchors = [entry_anchor(connection, row["id"]) for row in landlord]
            self.scheduled = [row["effective_at"][:10] for row in landlord]
        finally:
            connection.close()
        commitment = "rent-commitment"
        record_decision(
            self.database, request_id="rc-set", intent_hash="rc-set",
            decision_kind="RECURRING_COMMITMENT_SET", anchors={"commitment": commitment},
            payload={
                "name": "Rent", "currency": "SGD", "merchant_key": "landlord",
                "account_ids": ["acct-1"], "seed_entry_anchors": self.anchors,
                "schedule": {"kind": "monthly", "day": 2, "anchor_on": "2026-01-02"},
                "expected_amount": "500.00", "amount_tolerance": "0.00",
                "status": "active", "ended_on": None, "reason": "confirmed rent",
            },
            policy_version="planning-v1", snapshot="rent", refresh=False,
        )
        for index, (anchor, scheduled_on) in enumerate(zip(self.anchors, self.scheduled, strict=True)):
            record_decision(
                self.database, request_id=f"rc-occ-{index}", intent_hash=f"rc-occ-{index}",
                decision_kind="RECURRING_OCCURRENCE_SET",
                anchors={"commitment": commitment, "scheduled_on": scheduled_on},
                payload={"entry": anchor, "reason": "bound"},
                policy_version="planning-v1", snapshot="occ",
                refresh=index == len(self.anchors) - 1,
            )

    def test_confirmed_recurring_cost_leaves_the_routine_residual_once(self) -> None:
        estimate = baseline_estimate(self.database, today=date.today())
        sgd = estimate["by_currency"]["SGD"]
        for row in sgd["months"]:
            self.assertEqual(row["identified_recurring"], "500.00", row["month"])
            self.assertEqual(row["routine_variable_residual"], "20.00", row["month"])
            self.assertEqual(row["owner_cost_recorded"], "520.00", row["month"])
            self.assertTrue(row["reconciles"], row["month"])
        # counted once: flat commitment sum + residual mean, not the raw owner cost.
        self.assertEqual(sgd["fixed_recurring_monthly"], "500.00")
        self.assertEqual(sgd["variable_residual_monthly"], "20.00")
        self.assertEqual(sgd["routine_lifestyle_baseline"], "520.00")

    def test_the_estimate_carries_regime_and_sensitivity_blocks(self) -> None:
        sgd = baseline_estimate(self.database, today=date.today())["by_currency"]["SGD"]
        self.assertIn("regime", sgd)
        self.assertIn("sensitivity", sgd)
        self.assertFalse(sgd["regime"]["warning"])
        self.assertTrue(sgd["sensitivity"]["available"])


class SuggestedSurfacingTests(unittest.TestCase):
    def _rows(self) -> list[list[str]]:
        rows: list[list[str]] = [
            ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
             "Seed", "5000.00", "0.00", "SGD", "COMPLETED", "5000.00"],
        ]
        balance = Decimal("5000.00")
        for month in ("01", "02", "03"):
            for day, amount in (("05", "20.00"), ("12", "20.00"), ("19", "20.00")):
                balance -= Decimal(amount)
                rows.append([
                    "Card Payment", "Current", f"2026-{month}-{day} 10:00:00",
                    f"2026-{month}-{day} 10:00:01", "Corner Shop", f"-{amount}",
                    "0.00", "SGD", "COMPLETED", f"{balance}",
                ])
        balance -= Decimal("900.00")
        rows.append([
            "Card Payment", "Current", "2026-03-31 10:00:00", "2026-03-31 10:00:01",
            "Corner Shop", "-900.00", "0.00", "SGD", "COMPLETED", f"{balance}",
        ])
        return rows

    def test_roles_mark_the_outlier_group_suggested(self) -> None:
        database = _revolut_ledger(self._rows())
        roles = baseline_roles(database, today=date.today())
        irregular = [g for g in roles["groups"] if g["cost_pattern"] == "irregular"]
        self.assertTrue(irregular)
        self.assertTrue(all(g["review_status"] == "suggested" for g in irregular))

    def test_the_transaction_read_surfaces_a_suggested_irregular_behavior(self) -> None:
        database = _revolut_ledger(self._rows())
        connection = connect_readonly(database)
        try:
            token = current_generation(connection)["generation_token"]
        finally:
            connection.close()
        page = transactions_page(
            database, generation=token, start="2026-01-01", end="2026-03-31",
            currency="SGD", sort="amount", limit=200,
        )
        outlier = next(e for e in page["entries"] if e["amount"] == "-900.00")
        self.assertEqual(outlier["cost_behavior"], "irregular")
        self.assertEqual(outlier["cost_behavior_basis"], "outlier review")
        self.assertEqual(outlier["role_review_state"], "suggested")
        routine = next(e for e in page["entries"] if e["amount"] == "-20.00")
        self.assertNotEqual(routine["role_review_state"], "suggested")


if __name__ == "__main__":
    unittest.main()
