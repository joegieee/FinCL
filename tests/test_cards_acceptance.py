"""v0.4 acceptance scenarios (docs/releases/v0.4-cards.md §5).

Covers the cases not already pinned by the per-slice suites: final-cent schedule
rounding, two similar independent plans, dated limit changes, credit / over-limit
utilization text, a refund that credits an installment purchase without settling
the plan, and a debt-vs-installment cross-domain component conflict.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.application import (
    record_card_decision,
    record_debt_decision,
    record_installment_decision,
    record_refund_allocation,
)
from fincl.cards.queries import _utilization
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.refresh import refresh_ledger

_FIXTURE = Path(__file__).parent / "fixtures" / "mari_card.pdf"


def _intent(kind: str, anchors: dict, payload: dict) -> str:
    return hashlib.sha256(f"{kind}{anchors}{payload}".encode()).hexdigest()


class CardAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        DocumentImportService.plaintext_phase2(
            self.database, self.evidence
        ).import_mari_card(_FIXTURE)
        refresh_ledger(self.database)
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.account_id = connection.execute(
                "SELECT account_id FROM statements LIMIT 1"
            ).fetchone()[0]
            self.anchors = {
                a.rsplit(":", 1)[1]: a
                for (a,) in connection.execute(
                    "SELECT natural_anchor FROM ledger_entries"
                )
            }
        finally:
            connection.close()

    def _card(self, kind, anchors, payload):
        return record_card_decision(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent(kind, anchors, payload),
            decision_kind=kind, anchors=anchors, payload=payload,
        )

    def _inst(self, kind, anchors, payload):
        return record_installment_decision(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent(kind, anchors, payload),
            decision_kind=kind, anchors=anchors, payload=payload,
        )

    def _rows(self, table: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            connection.close()

    def _plan(self, *, occurrence_anchor: str, known_principal=None, schedule=None):
        plan_id = str(uuid.uuid4())
        self._inst(
            "INSTALLMENT_PLAN_SET",
            {"plan": plan_id},
            {
                "card_account_id": self.account_id,
                "currency": "SGD",
                "plan_reference": None,
                "merchant_label": "Plan",
                "term_months": None,
                "known_principal": known_principal,
                "financing_fee": None,
                "opened_on": "2026-07-21",
                "status": "CONFIRMED",
                "occurrences": [
                    {
                        "occurrence_id": str(uuid.uuid4()),
                        "occurrence_index": None,
                        "occurrence_count": None,
                        "entry_anchor": occurrence_anchor,
                        "effective_on": "2026-07-21",
                        "amount": "15.00",
                        "date_precision": "DAY",
                    }
                ],
                "schedule": schedule or [],
                "reason": "acceptance plan",
            },
        )
        return plan_id

    # -- final-cent schedule rounding ----------------------------------------- #

    def test_schedule_discloses_a_final_cent_rounding_remainder(self) -> None:
        self._plan(
            occurrence_anchor=self.anchors["5"],
            known_principal="10.00",
            schedule=[
                {"due_on": "2026-07-21", "principal": "3.33", "fee": "0.00"},
                {"due_on": "2026-08-21", "principal": "3.33", "fee": "0.00"},
                {"due_on": "2026-09-21", "principal": "3.33", "fee": "0.00"},
            ],
        )
        rows = sorted(
            self._rows("installment_schedule"), key=lambda r: r["schedule_index"]
        )
        self.assertEqual(rows[-1]["rounding_remainder_coefficient"], 1)  # 0.01
        self.assertEqual(rows[-1]["rounding_remainder_scale"], 2)

    # -- two similar independent plans -------------------------------------- #

    def test_two_similar_independent_plans_do_not_conflict(self) -> None:
        self._plan(occurrence_anchor=self.anchors["5"])
        self._plan(occurrence_anchor=self.anchors["3"])
        self.assertEqual(len(self._rows("installment_plans")), 2)
        self.assertEqual(len(self._rows("installment_occurrences")), 2)

    # -- dated limit changes --------------------------------------------------- #

    def test_limit_change_is_picked_by_as_of_date(self) -> None:
        from fincl.cards.queries import card_detail

        facility_id = str(uuid.uuid4())
        self._card(
            "CARD_FACILITY_SET",
            {"facility": facility_id},
            {
                "label": "F", "account_ids": [self.account_id], "currency": "SGD",
                "reason": "issuer scope",
            },
        )
        for observed_on, limit in (("2026-01-01", "500.00"), ("2026-08-01", "800.00")):
            self._card(
                "CARD_LIMIT_OBSERVATION_SET",
                {"facility": facility_id, "observation": str(uuid.uuid4())},
                {
                    "observed_on": observed_on, "limit": limit,
                    "source_ref": None, "reason": "observed",
                },
            )
        refresh_ledger(self.database)
        early = card_detail(self.database, self.account_id, as_of="2026-06-01")
        late = card_detail(self.database, self.account_id, as_of="2026-09-01")
        self.assertEqual(early["limit_basis"]["value"], "500.00")
        self.assertEqual(early["limit_basis"]["observed_on"], "2026-01-01")
        self.assertEqual(late["limit_basis"]["value"], "800.00")

    # -- credit / over-limit utilization text ------------------------------- #

    def test_utilization_text_preserves_credit_and_over_limit_amounts(self) -> None:
        limit = {"kind": "money", "value": "500.00", "currency": "SGD"}
        over = _utilization(
            limit=limit, liability={"kind": "money", "value": "560.00", "currency": "SGD"},
            limit_currency="SGD", liability_currency="SGD", basis_label="statement utilization",
        )
        self.assertTrue(over["over_limit"])
        self.assertEqual(over["over_limit_amount"], "60.00")
        credit = _utilization(
            limit=limit, liability={"kind": "money", "value": "-12.00", "currency": "SGD"},
            limit_currency="SGD", liability_currency="SGD", basis_label="statement utilization",
        )
        self.assertTrue(credit["credit_balance"])
        self.assertEqual(credit["credit_balance_amount"], "12.00")
        mismatch = _utilization(
            limit=limit, liability={"kind": "money", "value": "10.00", "currency": "USD"},
            limit_currency="SGD", liability_currency="USD", basis_label="x",
        )
        self.assertFalse(mismatch["available"])

    # -- refund credits a plan purchase without settling it ----------------- #

    def test_refund_on_an_installment_purchase_does_not_settle_the_plan(self) -> None:
        plan_id = self._plan(occurrence_anchor=self.anchors["5"])
        self._inst(
            "INSTALLMENT_ALLOCATION_SET",
            {"plan": plan_id},
            {
                "allocations": [
                    {
                        "entry_anchor": self.anchors["2"],  # -20.00 purchase
                        "component_role": "principal",
                        "amount": "8.00",
                    }
                ],
                "reason": "purchase link",
            },
        )
        # A refund of 1.00 credits the same purchase; 8.00 + 1.00 <= 20.00.
        record_refund_allocation(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash="deadbeef" * 8,
            refund_anchor=self.anchors["4"],  # +1.00 refund
            allocations=[
                {
                    "purchase_anchor": self.anchors["2"],
                    "refund_amount": "1.00",
                    "purchase_amount": "1.00",
                }
            ],
            reason="Partial refund on the plan's original purchase",
        )
        plans = self._rows("installment_plans")
        self.assertEqual(plans[0]["status"], "CONFIRMED")
        self.assertEqual(self._rows("installment_settlements"), [])

    # -- debt vs installment cross-domain conflict ------------------------- #

    def test_debt_and_installment_claims_cannot_over_partition_one_entry(self) -> None:
        purchase = self.anchors["2"]  # -20.00
        debt_id = str(uuid.uuid4())
        record_debt_decision(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent("PERSONAL_DEBT_OPEN", {"debt": debt_id}, {}),
            decision_kind="PERSONAL_DEBT_OPEN", anchors={"debt": debt_id},
            payload={
                "direction": "RECEIVABLE", "currency": "SGD",
                "counterparty_label": "Sam", "opened_on": "2026-07-01",
                "due_on": None, "reason": "lent via the card purchase",
            },
        )
        record_debt_decision(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent("PERSONAL_DEBT_EVENT_SET", {}, {"a": 1}),
            decision_kind="PERSONAL_DEBT_EVENT_SET",
            anchors={"debt": debt_id, "event": str(uuid.uuid4())},
            payload={
                "event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
                "principal_amount": "15.00", "effective_on": "2026-07-21",
                "entry_allocations": [
                    {
                        "entry_anchor": purchase, "principal_amount": "15.00",
                        "interest_amount": "0.00", "fee_amount": "0.00",
                        "separate_fee_amount": "0.00",
                    }
                ],
                "opening_balance": False, "expense_category": None,
                "reason": "Sam's share of the purchase",
            },
        )
        plan_id = self._plan(occurrence_anchor=self.anchors["5"])
        # 15.00 (debt) + 8.00 (installment) = 23.00 > 20.00 purchase magnitude.
        with self.assertRaises(FinCLError):
            self._inst(
                "INSTALLMENT_ALLOCATION_SET",
                {"plan": plan_id},
                {
                    "allocations": [
                        {
                            "entry_anchor": purchase,
                            "component_role": "principal",
                            "amount": "8.00",
                        }
                    ],
                    "reason": "over the shared partition",
                },
            )


if __name__ == "__main__":
    unittest.main()
