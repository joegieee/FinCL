"""Cash-flow usability M5 — income candidate workflow.

Covers the three pieces M5 adds on top of the backend-only candidate read:
grouped one-time confirmation over selected history, the reusable
``INBOUND_RULE_SET`` / ``INBOUND_RULE_RETIRE`` journal kinds, and the
per-occurrence detail the browser surface confirms against.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from fincl.application import record_batch, record_decision
from fincl.categorization import canonical_match, rule_id_for
from fincl.db import connect_readonly, connect_sqlite, initialize_schema
from fincl.decision_journal import default_journal_path, read_records
from fincl.decisions import entry_anchor, replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.inbound_rules import (
    INBOUND_RULE_RETIRE,
    INBOUND_RULE_SET,
    effective_inbound_classification,
    validate_inbound_rule,
)
from fincl.planning.detection import candidates
from fincl.queries import all_entries
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes


def _rows() -> list[list[str]]:
    rows = [["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
             "Seed", "50000.00", "0.00", "SGD", "COMPLETED", "50000.00"]]
    balance = Decimal("50000.00")
    for month in ("01", "02", "03", "04", "05", "06"):
        balance += Decimal("5000.00")
        rows.append(["Topup", "Current", f"2026-{month}-25 10:00:00",
                     f"2026-{month}-25 10:00:01", "Acme Payroll", "5000.00",
                     "0.00", "SGD", "COMPLETED", f"{balance}"])
    balance += Decimal("200.00")
    rows.append(["Topup", "Current", "2026-02-11 10:00:00", "2026-02-11 10:00:01",
                 "Lucky Bonus", "200.00", "0.00", "SGD", "COMPLETED", f"{balance}"])
    return rows


class IncomeCandidateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.database = root / "fincl.sqlite3"
        source = root / "revolut.csv"
        source.write_bytes(csv_bytes(_rows()))
        DocumentImportService.plaintext_phase2(self.database, root / "evidence").import_revolut_pdf(source)
        refresh_ledger(self.database)
        connection = connect_sqlite(self.database)
        try:
            self.payroll = [
                entry_anchor(connection, row["id"])
                for row in connection.execute(
                    "SELECT e.id FROM ledger_entries e JOIN document_records d ON d.id = e.document_record_id "
                    "WHERE d.description_raw = 'Acme Payroll' ORDER BY e.effective_at"
                )
            ]
            self.bonus = next(
                entry_anchor(connection, row["id"])
                for row in connection.execute(
                    "SELECT e.id FROM ledger_entries e JOIN document_records d ON d.id = e.document_record_id "
                    "WHERE d.description_raw = 'Lucky Bonus'"
                )
            )
        finally:
            connection.close()

    def _bucket(self, anchor: str) -> str:
        connection = connect_readonly(self.database)
        try:
            return next(e["bucket"] for e in all_entries(connection) if e["anchor"] == anchor)
        finally:
            connection.close()

    # -- detail the surface confirms against -------------------------------

    def test_income_candidate_carries_every_dated_occurrence(self) -> None:
        connection = connect_readonly(self.database)
        try:
            found = candidates(connection)
        finally:
            connection.close()
        payroll = next(c for c in found["income"] if c["merchant_key"] == "Acme Payroll")
        self.assertEqual(len(payroll["occurrences"]), 6)
        self.assertEqual(
            {key for occ in payroll["occurrences"] for key in occ},
            {"anchor", "date", "amount"},
        )
        self.assertEqual(payroll["occurrences"][0]["amount"], "5000.00")
        self.assertEqual({o["anchor"] for o in payroll["occurrences"]}, set(self.payroll))

    # -- grouped one-time confirmation over selected history --------------

    def test_confirming_the_selected_receipts_once_yields_one_income_stream(self) -> None:
        request_id = str(uuid.uuid4())
        items = [
            {"kind": "INBOUND_CLASSIFICATION", "anchors": {"entry": anchor},
             "payload": {"classification": "income", "reason": "Acme monthly payslip"}}
            for anchor in self.payroll[:4]
        ]
        intent = uuid.uuid5(uuid.NAMESPACE_OID, request_id).hex
        receipt = record_batch(
            self.database, request_id=request_id, intent_hash=intent, items=items,
            reason="Confirm the Acme payroll history",
        )
        refresh_ledger(self.database)

        self.assertEqual(receipt.decision_kind, "DECISION_BATCH")
        batch_records = [r for r in read_records(default_journal_path(self.database))
                         if r.decision_kind == "DECISION_BATCH"]
        self.assertEqual(len(batch_records), 1)
        for anchor in self.payroll[:4]:
            self.assertEqual(self._bucket(anchor), "income")
        # An unrelated credit outside the selection is untouched.
        self.assertEqual(self._bucket(self.bonus), "pending_review")

    # -- reusable inbound rule journal kinds -----------------------------

    def test_validate_rejects_a_mismatched_rule_id_and_bad_classification(self) -> None:
        match = {"description_contains": "Acme Payroll"}
        with self.assertRaises(FinCLError):
            validate_inbound_rule(INBOUND_RULE_SET, {"rule_id": "deadbeef"},
                                  {"match": match, "classification": "income", "reason": "x"})
        with self.assertRaises(FinCLError):
            validate_inbound_rule(INBOUND_RULE_SET, {"rule_id": rule_id_for(canonical_match(match))},
                                  {"match": match, "classification": "salary", "reason": "x"})

    def test_an_inbound_rule_projects_replays_and_retires(self) -> None:
        match = canonical_match({"description_contains": "Acme Payroll"})
        rule_id = rule_id_for(match)
        record_decision(
            self.database, request_id=str(uuid.uuid4()), intent_hash=uuid.uuid4().hex,
            decision_kind=INBOUND_RULE_SET, anchors={"rule_id": rule_id},
            payload={"match": match, "classification": "income", "reason": "employer of record"},
            policy_version="dashboard-v1", snapshot="acme inbound rule",
        )

        def stored() -> list[tuple[str, str]]:
            connection = connect_readonly(self.database)
            try:
                return [(r["rule_id"], r["classification"])
                        for r in connection.execute("SELECT rule_id, classification FROM inbound_rules")]
            finally:
                connection.close()

        self.assertEqual(stored(), [(rule_id, "income")])

        fresh = connect_sqlite(self.database)
        try:
            initialize_schema(fresh)
            replay_journal(fresh)
            self.assertEqual(
                [(r["rule_id"], r["classification"])
                 for r in fresh.execute("SELECT rule_id, classification FROM inbound_rules")],
                [(rule_id, "income")],
            )
        finally:
            fresh.close()

        record_decision(
            self.database, request_id=str(uuid.uuid4()), intent_hash=uuid.uuid4().hex,
            decision_kind=INBOUND_RULE_RETIRE, anchors={"rule_id": rule_id},
            payload={"reason": "no longer needed"},
            policy_version="dashboard-v1", snapshot="retire acme rule",
        )
        self.assertEqual(stored(), [])

    # -- read-model suggestion for future matches ------------------------

    def test_rule_suggests_a_classification_only_for_an_unresolved_inflow(self) -> None:
        base = {
            "economic_direction": "IN", "classification": None, "bucket": "pending_review",
            "currency_pocket": "SGD", "amount_coefficient": "20000", "amount_scale": "2",
            "description_text": "Acme Payroll", "merchant_match": None, "paynow_to": None,
            "account_id": "acc-1",
        }
        rules = [{"journal_seq": 7,
                  "match_json": json.dumps({"description_contains": "Acme Payroll"}),
                  "classification": "income"}]
        self.assertEqual(effective_inbound_classification(base, rules), "income")
        self.assertEqual(effective_inbound_classification({**base, "classification": "gift"}, rules), "")
        self.assertEqual(effective_inbound_classification({**base, "economic_direction": "OUT"}, rules), "")
        self.assertEqual(effective_inbound_classification({**base, "bucket": "income"}, rules), "")
        self.assertEqual(effective_inbound_classification(base, []), "")


if __name__ == "__main__":
    unittest.main()
