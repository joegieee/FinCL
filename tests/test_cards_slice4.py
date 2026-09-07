"""v0.4 slice 4: manual bill allocation, refund/settlement interplay, corrections."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.api import create_app
from fincl.application import (
    record_card_decision,
    record_installment_decision,
)
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient

_FIXTURE = Path(__file__).parent / "fixtures" / "mari_card.pdf"
_ORIGIN = "http://127.0.0.1:8765"


def _intent(kind: str, anchors: dict, payload: dict) -> str:
    return hashlib.sha256(f"{kind}{anchors}{payload}".encode()).hexdigest()


class ManualBillAllocationTests(unittest.TestCase):
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
            self.statement_id = connection.execute(
                "SELECT id FROM statements LIMIT 1"
            ).fetchone()[0]
            self.anchors = {
                a.rsplit(":", 1)[1]: a
                for (a,) in connection.execute(
                    "SELECT natural_anchor FROM ledger_entries"
                )
            }
        finally:
            connection.close()
        self.repayment_anchor = self.anchors["1"]  # +50.00 repayment/conversion

    def _card(self, kind, anchors, payload):
        return record_card_decision(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent(kind, anchors, payload),
            decision_kind=kind, anchors=anchors, payload=payload,
        )

    def _installment(self, kind, anchors, payload):
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

    def test_manual_allocation_projects_and_is_marked_manual(self) -> None:
        self._card(
            "CARD_PAYMENT_ALLOCATION_SET",
            {"card_entry": self.repayment_anchor},
            {
                "allocations": [{"statement_id": self.statement_id, "amount": "30.00"}],
                "reason": "I applied this repayment to the July bill",
            },
        )
        rows = self._rows("card_payment_allocations")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount_coefficient"], 3000)
        self.assertEqual(rows[0]["bound"], 1)

        from fincl.cards.queries import card_detail

        refresh_ledger(self.database)
        detail = card_detail(self.database, self.account_id)
        manual = detail["manually_allocated_payments"]
        self.assertEqual(len(manual), 1)
        self.assertTrue(manual[0]["manual"])
        self.assertEqual(manual[0]["amount"]["value"], "30.00")

    def test_allocation_sum_over_the_repayment_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._card(
                "CARD_PAYMENT_ALLOCATION_SET",
                {"card_entry": self.repayment_anchor},
                {
                    "allocations": [
                        {"statement_id": self.statement_id, "amount": "60.00"}
                    ],
                    "reason": "too much",
                },
            )

    def test_unknown_statement_identity_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._card(
                "CARD_PAYMENT_ALLOCATION_SET",
                {"card_entry": self.repayment_anchor},
                {
                    "allocations": [{"statement_id": "stmt_not_real", "amount": "5.00"}],
                    "reason": "bad statement",
                },
            )

    def test_manual_allocation_and_installment_settlement_share_the_partition(self) -> None:
        # A settlement already claims 40.00 of the 50.00 repayment.
        plan_id = str(uuid.uuid4())
        self._installment(
            "INSTALLMENT_PLAN_SET",
            {"plan": plan_id},
            {
                "card_account_id": self.account_id,
                "currency": "SGD",
                "plan_reference": "P1",
                "merchant_label": "M",
                "term_months": 6,
                "known_principal": "15.00",
                "financing_fee": None,
                "opened_on": "2026-07-21",
                "status": "CONFIRMED",
                "occurrences": [
                    {
                        "occurrence_id": str(uuid.uuid4()),
                        "occurrence_index": 3,
                        "occurrence_count": 6,
                        "entry_anchor": self.anchors["5"],
                        "effective_on": "2026-07-21",
                        "amount": "15.00",
                        "date_precision": "DAY",
                    }
                ],
                "schedule": [],
                "reason": "plan for partition test",
            },
        )
        self._installment(
            "INSTALLMENT_SETTLEMENT_SET",
            {"plan": plan_id, "settlement": str(uuid.uuid4())},
            {
                "settled_on": "2026-07-21",
                "source_entry_anchor": self.repayment_anchor,
                "principal_amount": "40.00",
                "fee_amount": "0.00",
                "waiver_amount": "0.00",
                "reason": "settled from the repayment",
            },
        )
        # 15.00 more still fits.
        self._card(
            "CARD_PAYMENT_ALLOCATION_SET",
            {"card_entry": self.repayment_anchor},
            {
                "allocations": [{"statement_id": self.statement_id, "amount": "10.00"}],
                "reason": "rest of the repayment on the July bill",
            },
        )
        # 20.00 more does not (40 + 20 > 50).
        with self.assertRaises(FinCLError):
            self._card(
                "CARD_PAYMENT_ALLOCATION_SET",
                {"card_entry": self.repayment_anchor},
                {
                    "allocations": [
                        {"statement_id": self.statement_id, "amount": "20.00"}
                    ],
                    "reason": "over the shared partition",
                },
            )

    def test_retire_removes_the_manual_allocation(self) -> None:
        self._card(
            "CARD_PAYMENT_ALLOCATION_SET",
            {"card_entry": self.repayment_anchor},
            {
                "allocations": [{"statement_id": self.statement_id, "amount": "10.00"}],
                "reason": "will retract",
            },
        )
        self._card(
            "CARD_PAYMENT_ALLOCATION_RETIRE",
            {"card_entry": self.repayment_anchor},
            {"reason": "changed my mind"},
        )
        self.assertEqual(self._rows("card_payment_allocations"), [])

    def test_api_commands_path_records_a_manual_allocation(self) -> None:
        application = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.evidence,
        )
        client = ASGITestClient(application, base_url=_ORIGIN)
        self.addCleanup(client.close)
        csrf = client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        ).json()["csrf"]
        generation = client.get("/api/v1/overview").json()["generation"]
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": generation,
            "kind": "CARD_PAYMENT_ALLOCATION_SET",
            "anchors": {"card_entry": self.repayment_anchor},
            "payload": {
                "allocations": [{"statement_id": self.statement_id, "amount": "12.00"}],
                "reason": "recorded through the command API",
            },
        }
        response = client.post(
            "/api/v1/commands", json=body,
            headers={
                "origin": _ORIGIN, "content-type": "application/json",
                "x-csrf-token": csrf,
            },
        )
        self.assertIn(response.status_code, (200, 202), response.text)
        self.assertEqual(len(self._rows("card_payment_allocations")), 1)


if __name__ == "__main__":
    unittest.main()
