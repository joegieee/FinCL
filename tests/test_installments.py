"""v0.4 slice 3: installment chain confirmation, known/unknown obligations,
purchase / spread lenses and early settlement."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from fincl.api import create_app
from fincl.application import record_installment_decision
from fincl.cli import app
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.rebuild import rebuild_database
from fincl.refresh import refresh_ledger

_FIXTURE = Path(__file__).parent / "fixtures" / "mari_card.pdf"
_ORIGIN = "http://127.0.0.1:8765"


def _intent(kind: str, anchors: dict, payload: dict) -> str:
    return hashlib.sha256(f"{kind}{anchors}{payload}".encode()).hexdigest()


class InstallmentTests(unittest.TestCase):
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
        self.installment_anchor = self.anchors["5"]  # -15.00 split payment row
        self.purchase_anchor = self.anchors["2"]  # -20.00 card purchase
        self.repayment_anchor = self.anchors["1"]  # +50.00 repayment

    def _record(self, kind: str, anchors: dict, payload: dict):
        return record_installment_decision(
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=_intent(kind, anchors, payload),
            decision_kind=kind,
            anchors=anchors,
            payload=payload,
        )

    def _rows(self, table: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            connection.close()

    def _plan(self, *, known_principal="15.00", schedule=True, status="CONFIRMED"):
        plan_id = str(uuid.uuid4())
        payload = {
            "card_account_id": self.account_id,
            "currency": "SGD",
            "plan_reference": "SPLIT-6M",
            "merchant_label": "Split Payment Merchant",
            "term_months": 6,
            "known_principal": known_principal,
            "financing_fee": "1.20",
            "opened_on": "2026-07-21",
            "status": status,
            "occurrences": [
                {
                    "occurrence_id": str(uuid.uuid4()),
                    "occurrence_index": 3,
                    "occurrence_count": 6,
                    "entry_anchor": self.installment_anchor,
                    "effective_on": "2026-07-21",
                    "amount": "15.00",
                    "date_precision": "DAY",
                }
            ],
            "schedule": (
                [
                    {"due_on": "2026-07-21", "principal": "2.50", "fee": "0.20"},
                    {"due_on": "2026-08-21", "principal": "2.50", "fee": "0.20"},
                    {"due_on": "2026-09-21", "principal": "2.50", "fee": "0.20"},
                    {"due_on": "2026-10-21", "principal": "2.50", "fee": "0.20"},
                    {"due_on": "2026-11-21", "principal": "2.50", "fee": "0.20"},
                    {"due_on": "2026-12-21", "principal": "2.50", "fee": "0.20"},
                ]
                if schedule
                else []
            ),
            "reason": "Issuer plan ref SPLIT-6M, occurrence 3 of 6 on the July statement",
        }
        self._record("INSTALLMENT_PLAN_SET", {"plan": plan_id}, payload)
        return plan_id

    # -- projection ------------------------------------------------------- #

    def test_plan_occurrences_and_schedule_project(self) -> None:
        plan_id = self._plan()
        plans = self._rows("installment_plans")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["principal_coefficient"], 1500)
        self.assertEqual(plans[0]["term_months"], 6)
        self.assertEqual(plans[0]["status"], "CONFIRMED")
        occ = self._rows("installment_occurrences")
        self.assertEqual(len(occ), 1)
        self.assertEqual(occ[0]["bound"], 1)
        self.assertEqual(occ[0]["occurrence_index"], 3)
        schedule = sorted(self._rows("installment_schedule"), key=lambda r: r["schedule_index"])
        self.assertEqual(len(schedule), 6)
        self.assertTrue(all(r["is_projected"] == 1 for r in schedule))
        # 6 * 2.50 = 15.00 exactly -> disclosed rounding remainder is zero.
        self.assertEqual(schedule[-1]["rounding_remainder_coefficient"], 0)

    def test_unknown_principal_forbids_an_invented_schedule(self) -> None:
        with self.assertRaises(FinCLError):
            self._plan(known_principal=None, schedule=True)
        # unknown principal with no schedule is fine and shown as unknown.
        plan_id = self._plan(known_principal=None, schedule=False)
        from fincl.installments.queries import installment_detail

        refresh_ledger(self.database)
        detail = installment_detail(self.database, plan_id)
        self.assertIsNone(detail["plan"]["progress"]["known_principal"])
        self.assertIsNone(detail["plan"]["progress"]["remaining_obligation"])

    def test_one_occurrence_cannot_back_two_active_plans(self) -> None:
        self._plan()
        with self.assertRaises(FinCLError):
            self._plan()

    def test_purchase_allocation_respects_the_shared_partition(self) -> None:
        plan_id = self._plan()
        # The -20.00 purchase can carry a 12.00 principal claim.
        self._record(
            "INSTALLMENT_ALLOCATION_SET",
            {"plan": plan_id},
            {
                "allocations": [
                    {
                        "entry_anchor": self.purchase_anchor,
                        "component_role": "principal",
                        "amount": "12.00",
                    }
                ],
                "reason": "Original purchase for the split-payment plan",
            },
        )
        allocations = self._rows("installment_purchase_allocations")
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]["bound"], 1)
        # Claiming 25.00 against the 20.00 purchase over-partitions it.
        with self.assertRaises(FinCLError):
            self._record(
                "INSTALLMENT_ALLOCATION_SET",
                {"plan": plan_id},
                {
                    "allocations": [
                        {
                            "entry_anchor": self.purchase_anchor,
                            "component_role": "principal",
                            "amount": "25.00",
                        }
                    ],
                    "reason": "over-claim",
                },
            )

    def test_early_settlement_partitions_and_marks_settled(self) -> None:
        plan_id = self._plan()
        self._record(
            "INSTALLMENT_SETTLEMENT_SET",
            {"plan": plan_id, "settlement": str(uuid.uuid4())},
            {
                "settled_on": "2026-08-15",
                "source_entry_anchor": self.repayment_anchor,
                "principal_amount": "10.00",
                "fee_amount": "0.50",
                "waiver_amount": "0.70",
                "reason": "Settled the plan early against the July repayment",
            },
        )
        settlements = self._rows("installment_settlements")
        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0]["principal_coefficient"], 1000)
        self.assertEqual(settlements[0]["waiver_coefficient"], 70)
        from fincl.installments.queries import installment_detail

        refresh_ledger(self.database)
        detail = installment_detail(self.database, plan_id)
        self.assertEqual(detail["plan"]["effective_status"], "SETTLED")

    def test_plan_retire_cascades_to_dependents(self) -> None:
        plan_id = self._plan()
        self._record(
            "INSTALLMENT_ALLOCATION_SET",
            {"plan": plan_id},
            {
                "allocations": [
                    {
                        "entry_anchor": self.purchase_anchor,
                        "component_role": "principal",
                        "amount": "12.00",
                    }
                ],
                "reason": "purchase link",
            },
        )
        self._record(
            "INSTALLMENT_PLAN_RETIRE",
            {"plan": plan_id},
            {"reason": "Misidentified; not an installment plan after all"},
        )
        self.assertEqual(self._rows("installment_plans"), [])
        self.assertEqual(self._rows("installment_occurrences"), [])
        self.assertEqual(self._rows("installment_purchase_allocations"), [])
        self.assertEqual(self._rows("installment_schedule"), [])

    # -- rebuild + API + CLI ------------------------------------------------ #

    def test_rebuild_reproduces_the_installment_projection(self) -> None:
        self._plan()
        connection = sqlite3.connect(self.database)
        try:
            before = connection.execute(
                "SELECT plan_id, principal_coefficient, term_months, status "
                "FROM installment_plans"
            ).fetchall()
        finally:
            connection.close()
        output = self.root / "rebuilt.sqlite3"
        rebuild_database(self.database, self.evidence, output)
        from fincl.db import initialize_schema
        from fincl.decision_journal import default_journal_path
        from fincl.decisions import replay_journal

        rebuilt = sqlite3.connect(output)
        rebuilt.row_factory = sqlite3.Row
        try:
            initialize_schema(rebuilt)
            replay_journal(rebuilt, journal_path=default_journal_path(self.database))
            after = rebuilt.execute(
                "SELECT plan_id, principal_coefficient, term_months, status "
                "FROM installment_plans"
            ).fetchall()
        finally:
            rebuilt.close()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_api_installments_reads(self) -> None:
        plan_id = self._plan()
        refresh_ledger(self.database)
        application = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.evidence,
        )
        client = TestClient(application, base_url=_ORIGIN)
        self.addCleanup(client.close)
        client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        listing = client.get("/api/v1/installments")
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(len(listing.json()["plans"]), 1)
        detail = client.get(f"/api/v1/installments/{plan_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertIn("recognized_purchase_cost", body["views"])
        self.assertIn("spread_allocation", body["views"])
        self.assertEqual(len(body["schedule"]), 6)
        missing = client.get("/api/v1/installments/not-a-plan")
        self.assertEqual(missing.status_code, 404)

    def test_cli_list_and_settle(self) -> None:
        plan_id = self._plan()
        runner = CliRunner()
        listing = runner.invoke(
            app,
            ["installments", "list", "--database", str(self.database), "--json", "--no-refresh"],
        )
        self.assertEqual(listing.exit_code, 0, listing.output)
        self.assertIn(plan_id, listing.output)
        settle = runner.invoke(
            app,
            [
                "installments", "settle", "--plan", plan_id,
                "--settled-on", "2026-08-15", "--source-entry", self.repayment_anchor,
                "--principal", "10.00", "--fee", "0.50", "--waiver", "0.00",
                "--reason", "early settlement",
                "--database", str(self.database), "--json",
            ],
        )
        self.assertEqual(settle.exit_code, 0, settle.output)
        self.assertEqual(len(self._rows("installment_settlements")), 1)


if __name__ == "__main__":
    unittest.main()
