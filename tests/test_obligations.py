"""Stage 2B — derived schedules, candidate inference and unified obligations."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from fincl.db import connect_readonly, connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import entry_anchor
from fincl.obligations import obligations_page
from fincl.obligations.queries import _installment_obligations
from fincl.obligations.rules import (
    baseline_component_link,
    history_confidence,
    payment_state,
    resolve_amount_basis,
)
from fincl.planning.detection import candidates
from fincl.planning.queries import recurring_page
from fincl.refresh import refresh_ledger
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes


class ConfidenceBoundaryTests(unittest.TestCase):
    """History supports Likely; Confirmed needs future evidence or a commitment."""

    def test_history_alone_is_likely_and_never_confirmed(self) -> None:
        derived = history_confidence()
        self.assertEqual(derived["confidence"], "likely")
        self.assertIn("does not evidence a future one", derived["confidence_basis"])

    def test_a_future_receipt_or_a_recorded_commitment_confirms(self) -> None:
        self.assertEqual(
            history_confidence(future_receipt_evidenced=True)["confidence"], "confirmed"
        )
        self.assertEqual(
            history_confidence(commitment_recorded=True)["confidence"], "confirmed"
        )


class ScheduleReplacesLumpSumTests(unittest.TestCase):
    def test_a_remaining_schedule_replaces_the_lump_sum(self) -> None:
        resolved = resolve_amount_basis(
            lump_sum="900.00",
            schedule=[
                {"due_on": "2026-07-15", "amount": "300.00"},
                {"due_on": "2026-08-15", "amount": "300.00"},
                {"due_on": "2026-09-15", "amount": "300.00"},
            ],
            currency="SGD",
        )
        self.assertEqual(resolved["amount_basis"], "remaining_schedule")
        self.assertEqual(resolved["remaining_amount"]["value"], "900.00")
        # The lump sum is still visible, and explicitly not counted: the two
        # must never be summed into a doubled liability.
        self.assertEqual(resolved["superseded_lump_sum"]["value"], "900.00")
        self.assertFalse(resolved["superseded_lump_sum"]["counted"])

    def test_without_a_schedule_the_lump_sum_stands_alone(self) -> None:
        resolved = resolve_amount_basis(lump_sum="900.00", schedule=None, currency="SGD")
        self.assertEqual(resolved["amount_basis"], "lump_sum_due")
        self.assertIsNone(resolved["superseded_lump_sum"])
        self.assertIsNone(resolved["schedule"])

    def test_neither_a_schedule_nor_a_lump_sum_stays_unknown(self) -> None:
        resolved = resolve_amount_basis(lump_sum=None, schedule=None, currency="SGD")
        self.assertEqual(resolved["amount_basis"], "unknown")
        self.assertIsNone(resolved["remaining_amount"])
        self.assertIsNotNone(resolved["remaining_unknown_reason"])


class PassedDueDateTests(unittest.TestCase):
    def test_a_passed_due_date_alone_never_infers_a_missing_payment(self) -> None:
        state = payment_state(
            due_on="2026-05-01", as_of="2026-06-01", payment_count=0, remaining="450.00"
        )
        self.assertEqual(state["status"], "due_date_passed_no_payment_evidence")
        self.assertEqual(state["payment_status"], "unknown")
        self.assertTrue(state["overdue"])
        # It stays on screen until evidence arrives; it is not marked missed.
        self.assertTrue(state["stays_visible"])
        self.assertNotIn("missed", state["status"])

    def test_recorded_evidence_of_payment_settles_and_hides_it(self) -> None:
        state = payment_state(
            due_on="2026-05-01", as_of="2026-06-01", payment_count=1, remaining="0.00"
        )
        self.assertEqual(state["status"], "settled")
        self.assertFalse(state["stays_visible"])

    def test_forgiveness_or_correction_settles_it_too(self) -> None:
        state = payment_state(
            due_on="2026-05-01", as_of="2026-06-01", payment_count=0,
            remaining="450.00", closed=True, closed_reason="written off",
        )
        self.assertEqual(state["status"], "settled")
        self.assertEqual(state["basis"], "written off")


class BaselineComponentLinkTests(unittest.TestCase):
    _PORTIONS = {
        ("SGD", "Housing / Rent"): "1200.00",
        ("SGD", "Food & Dining / Groceries"): "300.00",
    }

    def test_a_rent_event_links_to_rent_and_to_nothing_else(self) -> None:
        link = baseline_component_link(
            categories={"Housing / Rent"}, currency="SGD", portions=self._PORTIONS
        )
        self.assertTrue(link["available"])
        self.assertEqual(link["category_key"], "Housing / Rent")
        self.assertEqual(link["relation"], "replaces_category_portion")
        self.assertEqual(link["monthly_baseline_portion"], "1200.00")

    def test_ambiguous_or_absent_sources_stay_unknown(self) -> None:
        for categories in ({"Housing / Rent", "Food & Dining / Groceries"}, set()):
            link = baseline_component_link(
                categories=categories, currency="SGD", portions=self._PORTIONS
            )
            self.assertFalse(link["available"])
            self.assertIsNone(link["category_key"])
            self.assertEqual(link["relation"], "unknown")


class InstallmentAdapterTests(unittest.TestCase):
    """The installment adapter, on a schema-only ledger.

    A plan carries both a lump-sum remaining principal and a real remaining
    schedule, so it is where the replacement rule has to hold.
    """

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.connection = connect_sqlite(root / "fincl.sqlite3")
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.connection.execute(
            "INSERT INTO installment_plans(plan_id, card_account_id, currency, "
            "plan_reference, merchant_label, term_months, principal_coefficient, "
            "principal_scale, opened_on, status, journal_seq, bound, reason) "
            "VALUES ('plan-1','card-1','SGD',NULL,'Laptop',4,120000,2,"
            "'2026-01-10','CONFIRMED',1,1,'issuer letter')"
        )
        for index, due in enumerate(("2026-02-10", "2026-03-10", "2026-04-10", "2026-05-10"), start=1):
            self.connection.execute(
                "INSERT INTO installment_schedule(plan_id, schedule_index, due_on, "
                "principal_coefficient, principal_scale, is_projected) "
                "VALUES ('plan-1',?,?,30000,2,1)",
                (index, due),
            )
        self.connection.execute(
            "INSERT INTO installment_occurrences(plan_id, occurrence_id, entry_anchor, "
            "effective_on, amount_coefficient, amount_scale, bound) "
            "VALUES ('plan-1','occ-1','anchor-1','2026-02-10',30000,2,1)"
        )
        self.connection.commit()

    def test_the_remaining_schedule_replaces_the_plan_lump_sum(self) -> None:
        obligation = _installment_obligations(self.connection, "2026-06-01")[0]
        self.assertEqual(obligation["obligation_id"], "installment:plan-1")
        self.assertEqual(obligation["amount_basis"], "remaining_schedule")
        self.assertEqual(obligation["remaining_amount"]["value"], "900.00")
        self.assertEqual(len(obligation["schedule"]), 3)
        self.assertEqual(obligation["superseded_lump_sum"]["value"], "900.00")
        self.assertFalse(obligation["superseded_lump_sum"]["counted"])
        self.assertEqual(obligation["actual_payments"]["total"], "300.00")

    def test_a_card_bound_plan_is_not_a_separate_bank_withdrawal(self) -> None:
        obligation = _installment_obligations(self.connection, "2026-06-01")[0]
        self.assertEqual(obligation["cash_effect"], "via_card_statement")

    def test_a_passed_schedule_date_without_evidence_stays_visible(self) -> None:
        obligation = _installment_obligations(self.connection, "2026-06-01")[0]
        # 2026-03-10 is long past and unmatched; that is a date, not a miss.
        self.assertEqual(obligation["date_basis"]["value"], "2026-03-10")
        self.assertTrue(obligation["overdue"])
        self.assertTrue(obligation["stays_visible"])
        self.assertEqual(
            obligation["status"], "due_date_passed_partially_paid"
        )


def _rows() -> list[list[str]]:
    """Six months of rent, groceries and salary in one Revolut export."""
    rows = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "50000.00", "0.00", "SGD", "COMPLETED", "50000.00"],
    ]
    balance = Decimal("50000.00")
    for month in ("01", "02", "03", "04", "05", "06"):
        for day, merchant, amount in (
            ("05", "Landlord", "-1200.00"),
            ("12", "Corner Grocer", "-300.00"),
            ("25", "Acme Payroll", "5000.00"),
        ):
            balance += Decimal(amount)
            rows.append([
                "Card Payment" if amount.startswith("-") else "Topup",
                "Current", f"2026-{month}-{day} 10:00:00",
                f"2026-{month}-{day} 10:00:01", merchant, amount,
                "0.00", "SGD", "COMPLETED", f"{balance}",
            ])
    return rows


class ObligationsLedgerTests(unittest.TestCase):
    """One ledger, exercised through the real read paths."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(tempfile.mkdtemp())
        cls.database = root / "fincl.sqlite3"
        source = root / "revolut.csv"
        source.write_bytes(csv_bytes(_rows()))
        FinCLService.plaintext_phase1(cls.database, root / "evidence").import_revolut(source)
        refresh_ledger(cls.database)

        connection = connect_sqlite(cls.database)
        try:
            anchors = {
                merchant: [
                    entry_anchor(connection, row["id"])
                    for row in connection.execute(
                        "SELECT e.id FROM ledger_entries e "
                        "JOIN raw_records r ON r.id = e.raw_record_id "
                        "WHERE r.description_raw = ? ORDER BY e.effective_at",
                        (merchant,),
                    )
                ]
                for merchant in ("Landlord", "Corner Grocer", "Acme Payroll")
            }
        finally:
            connection.close()
        cls.rent_anchors = anchors["Landlord"]

        journal = default_journal_path(cls.database)
        for anchor in anchors["Acme Payroll"]:
            append_decision(
                journal, decision_kind="INBOUND_CLASSIFICATION", anchors={"entry": anchor},
                payload={"classification": "income", "reason": "employer payslip"},
                policy_version="review-v1", snapshot=f"payroll {anchor}",
            )
        for merchant, category in (
            ("Landlord", "Housing / Rent"),
            ("Corner Grocer", "Food & Dining / Groceries"),
        ):
            for anchor in anchors[merchant]:
                append_decision(
                    journal, decision_kind="CATEGORY_OVERRIDE", anchors={"entry": anchor},
                    payload={"category": category, "reason": "lease and receipts"},
                    policy_version="category-engine-2026-v1", snapshot=f"{merchant} {anchor}",
                )
        cls.commitment = str(uuid.uuid4())
        append_decision(
            journal, decision_kind="RECURRING_COMMITMENT_SET",
            anchors={"commitment": cls.commitment},
            payload={
                "name": "Rent", "currency": "SGD", "merchant_key": "Landlord",
                "account_ids": [], "seed_entry_anchors": cls.rent_anchors[:3],
                "schedule": {"kind": "monthly", "day": 5, "anchor_on": "2026-01-05"},
                "expected_amount": "1200.00", "amount_tolerance": "0.00",
                "status": "active", "ended_on": None, "reason": "signed lease",
            },
            policy_version="planning-v1", snapshot="rent commitment",
        )
        for index, anchor in enumerate(cls.rent_anchors):
            append_decision(
                journal, decision_kind="RECURRING_OCCURRENCE_SET",
                anchors={"commitment": cls.commitment,
                         "scheduled_on": f"2026-0{index + 1}-05"},
                payload={"entry": anchor, "reason": "matched from the statement"},
                policy_version="planning-v1", snapshot=f"rent occurrence {index}",
            )
        refresh_ledger(cls.database)
        cls.page = obligations_page(
            cls.database, as_of="2026-07-15", today=date(2026, 7, 15)
        )

    def _rent(self) -> dict:
        return next(
            item for item in self.page["obligations"]
            if item["obligation_id"] == f"recurring:{self.commitment}"
        )

    def test_the_recurring_event_links_to_its_own_baseline_component(self) -> None:
        link = self._rent()["baseline_link"]
        self.assertTrue(link["available"], link)
        self.assertEqual(link["category_key"], "Housing / Rent")
        self.assertEqual(link["relation"], "replaces_category_portion")
        self.assertEqual(link["monthly_baseline_portion"], "1200.00")

    def test_groceries_and_the_total_are_left_alone(self) -> None:
        effects = {
            row["category_key"]: row
            for row in self.page["baseline_component_effects"]
            if row["currency"] == "SGD"
        }
        rent = effects["Housing / Rent"]
        groceries = effects["Food & Dining / Groceries"]
        self.assertEqual(rent["replaced_by"], [f"recurring:{self.commitment}"])
        self.assertEqual(rent["remaining_historical_portion"], "0.00")
        self.assertEqual(groceries["replaced_by"], [])
        self.assertEqual(groceries["relation"], "unchanged")
        self.assertEqual(groceries["remaining_historical_portion"], "300.00")

    def test_the_obligation_shape_is_the_same_for_every_origin(self) -> None:
        required = {
            "obligation_id", "origin", "amount_basis", "date_basis",
            "remaining_amount", "actual_payments", "confidence", "baseline_link",
        }
        self.assertTrue(self.page["obligations"])
        for obligation in self.page["obligations"]:
            self.assertLessEqual(required, set(obligation), obligation["obligation_id"])

    def test_a_recurring_commitment_is_confirmed_but_its_history_is_not(self) -> None:
        rent = self._rent()
        self.assertEqual(rent["confidence"], "confirmed")
        self.assertEqual(rent["origin"], "recurring_commitment")
        self.assertEqual(rent["actual_payments"]["count"], 6)

    def test_income_candidates_are_populated_and_stay_likely(self) -> None:
        connection = connect_readonly(self.database)
        try:
            found = candidates(connection)
        finally:
            connection.close()
        payroll = next(
            item for item in found["income"] if item["merchant_key"] == "Acme Payroll"
        )
        self.assertEqual(payroll["confidence"], "likely")
        self.assertFalse(payroll["auto_confirmable"])
        self.assertEqual(payroll["source_count"], 6)
        self.assertEqual(payroll["last_receipt_on"], "2026-06-25")
        self.assertEqual(payroll["cadence"], "monthly")
        self.assertEqual(payroll["amount_variation"]["basis"], "stable")
        self.assertEqual(payroll["timing_window"]["day_of_month_min"], 25)

    def test_bill_candidates_carry_variation_and_timing_for_review(self) -> None:
        page = recurring_page(self.database)
        landlord = next(
            item for item in page["bill_candidates"] if item["merchant_key"] == "LANDLORD"
        )
        # Everything needed to review is derived: no amount is typed in.
        self.assertEqual(landlord["suggested_expected_amount"], "1200.00")
        self.assertEqual(landlord["suggested_tolerance"], "0.00")
        self.assertEqual(landlord["source_count"], 6)
        self.assertIn("min_gap_days", landlord["timing_window"])
        self.assertEqual(page["candidates"], page["bill_candidates"])
        self.assertTrue(page["income_candidates"])


if __name__ == "__main__":
    unittest.main()
