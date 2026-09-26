"""A confirmed installment plan owns its instalments and its conversion credit.

The original purchase carries the spending and its category (the at-purchase
view); the conversion credit borrows that principal and the instalments repay
it, so -- like debt principal -- neither is asked for a category, flagged for
review or counted as spending.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from fincl.application import record_installment_decision
from fincl.backup import backup_ledger
from fincl.categorization_queries import categorization_candidates, categorization_summary
from fincl.db import SCHEMA_VERSION
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.generation import current_generation
from fincl.installments.candidates import installment_candidates
from fincl.installments.queries import installment_detail
from fincl.migrations import apply_pending_migrations
from fincl.queries import _ENTRY_VIEW
from fincl.refresh import refresh_ledger
from tests.mari_card_pdf import conversion_statement, mari_card_pdf_bytes
from tests.test_installment_candidates import CandidateToPlanPayloadTests

_FIXTURE = Path(__file__).parent / "fixtures" / "mari_card.pdf"


class InstallmentOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "fincl.sqlite3"
        service = DocumentImportService.plaintext_phase2(self.database, self.root / "evidence")
        service.import_mari_card(_FIXTURE)
        second = self.root / "mari_card_aug.pdf"
        second.write_bytes(mari_card_pdf_bytes(conversion_statement()))
        service.import_mari_card(second)
        refresh_ledger(self.database)
        self.candidate = next(
            c for c in installment_candidates(self.database)["candidates"]
            if c["occurrences"][0]["amount"] == "25.07"
        )
        self.instalment = self.candidate["occurrences"][0]["entry_anchor"]
        self.conversion = self.candidate["conversion"]["entry_anchor"]
        self.purchase = self._anchor_where("r.description_raw LIKE 'MERCHANT GAMMA%'")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def _anchor_where(self, clause: str) -> str:
        connection = self._connect()
        try:
            return str(connection.execute(
                "SELECT e.natural_anchor FROM ledger_entries e "
                "JOIN document_records r ON r.id = e.document_record_id "
                f"JOIN extraction_runs x ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED' WHERE {clause}"
            ).fetchone()[0])
        finally:
            connection.close()

    def _set_plan(self, plan_id: str, *, conversion: str | None, **overrides: Any) -> None:
        payload = CandidateToPlanPayloadTests._plan_payload(
            self.candidate, known_principal="150.40"
        )
        if conversion is not None:
            payload["conversion_anchor"] = conversion
        payload.update(overrides)
        record_installment_decision(
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=hashlib.sha256(plan_id.encode() + str(conversion).encode()).hexdigest(),
            decision_kind="INSTALLMENT_PLAN_SET",
            anchors={"plan": plan_id},
            payload=payload,
        )
        refresh_ledger(self.database)

    def _view(self, anchor: str) -> sqlite3.Row:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT bucket, review FROM ({_ENTRY_VIEW}) "
                "WHERE natural_anchor = ? AND selection_state = 'SELECTED'",
                (anchor,),
            ).fetchone()
            assert row is not None
            return row
        finally:
            connection.close()

    def _generation(self) -> str:
        connection = self._connect()
        try:
            generation = current_generation(connection)
            assert generation is not None
            return str(generation["generation_token"])
        finally:
            connection.close()

    def _amount_components(self, anchor: str) -> list[sqlite3.Row]:
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT bucket, signed_coefficient FROM analysis_components "
                "WHERE origin_anchor = ? AND role = 'amount'",
                (anchor,),
            ).fetchall()
        finally:
            connection.close()

    def test_an_unconfirmed_chain_still_asks_for_a_category(self) -> None:
        self.assertEqual(self._view(self.instalment)["bucket"], "spending")
        self.assertEqual(self._view(self.conversion)["bucket"], "pending_review")
        self.assertTrue(self._view(self.instalment)["review"])

    def test_a_confirmed_plan_owns_its_instalments_and_conversion(self) -> None:
        self._set_plan(str(uuid.uuid4()), conversion=self.conversion)
        for anchor in (self.instalment, self.conversion):
            row = self._view(anchor)
            self.assertEqual(row["bucket"], "installment")
            self.assertFalse(row["review"])
        # The original purchase is still the spending, in its own category.
        self.assertEqual(self._view(self.purchase)["bucket"], "spending")

    def test_the_inbox_and_its_progress_skip_plan_owned_rows(self) -> None:
        before = categorization_summary(self.database, generation=self._generation())
        self._set_plan(str(uuid.uuid4()), conversion=self.conversion)
        after = categorization_summary(self.database, generation=self._generation())
        self.assertEqual(after["counts"].get("installment_plan"), 2)
        self.assertEqual(
            after["progress_numerator"] - before["progress_numerator"],
            before["counts"].get("unclassified_active", 0) - after["counts"].get("unclassified_active", 0),
        )
        page = categorization_candidates(self.database, generation=self._generation(), limit=100)
        members = sum(card["transaction_count"] for card in page["candidates"])
        self.assertEqual(members, after["counts"].get("unclassified_active", 0)
                         + after["counts"].get("unclassified_inactive", 0))
        self.assertEqual(
            before["counts"].get("unclassified_active", 0) - after["counts"].get("unclassified_active", 0), 2
        )

    def _running_chain(self) -> tuple[dict[str, Any], str]:
        """The 15.00 chain first seen at 3/6, whose 3/6 posts inside the covered
        window (the 25.07 chain's 1/6 posts after it)."""
        chain = next(
            c for c in installment_candidates(self.database)["candidates"]
            if c["occurrences"][0]["amount"] == "15.00"
        )
        return chain, chain["occurrences"][0]["entry_anchor"]

    def test_spending_counts_the_purchase_once(self) -> None:
        running, instalment = self._running_chain()
        self.assertEqual(
            [(r["bucket"], r["signed_coefficient"]) for r in self._amount_components(instalment)],
            [("spending", -1500)],
        )
        self._set_plan(str(uuid.uuid4()), conversion=self.conversion)
        self.candidate = running
        self._set_plan(str(uuid.uuid4()), conversion=None, known_principal=None)
        self.assertEqual(self._amount_components(instalment), [])
        self.assertEqual(self._amount_components(self.conversion), [])
        purchase = self._amount_components(self.purchase)
        self.assertEqual([(r["bucket"], r["signed_coefficient"]) for r in purchase], [("spending", -15040)])

    def test_a_scheduled_plan_fee_still_counts_as_a_fee(self) -> None:
        running, instalment = self._running_chain()
        self.candidate = running
        schedule = [
            {"due_on": f"2026-{5 + i:02d}-21", "principal": "15.00", "fee": "1.00" if i == 2 else "0"}
            for i in range(6)
        ]
        self._set_plan(str(uuid.uuid4()), conversion=None, known_principal="90.00", schedule=schedule)
        rows = self._amount_components(instalment)
        self.assertEqual([(r["bucket"], r["signed_coefficient"]) for r in rows], [("fees", -100)])

    def test_a_plan_without_its_credit_is_offered_it_and_can_attach_it(self) -> None:
        plan_id = str(uuid.uuid4())
        self._set_plan(plan_id, conversion=None)
        detail = installment_detail(self.database, plan_id)
        self.assertIsNone(detail["conversion"])
        self.assertEqual(detail["suggested_conversion"]["entry_anchor"], self.conversion)
        self.assertEqual(self._view(self.conversion)["bucket"], "pending_review")

        self._set_plan(plan_id, conversion=self.conversion)
        detail = installment_detail(self.database, plan_id)
        self.assertEqual(detail["conversion"]["entry_anchor"], self.conversion)
        self.assertEqual(detail["conversion"]["amount"]["value"], "150.40")
        self.assertIsNone(detail["suggested_conversion"])
        self.assertEqual(self._view(self.conversion)["bucket"], "installment")

    def test_a_purchase_is_refused_as_a_conversion_credit(self) -> None:
        with self.assertRaisesRegex(FinCLError, "must be a credit"):
            self._set_plan(str(uuid.uuid4()), conversion=self.purchase)

    def test_one_credit_cannot_open_two_plans(self) -> None:
        self._set_plan(str(uuid.uuid4()), conversion=self.conversion)
        other = next(
            c for c in installment_candidates(self.database)["candidates"]
            if c["occurrences"][0]["amount"] == "15.00"
        )
        self.candidate = other
        with self.assertRaisesRegex(FinCLError, "more than one installment plan"):
            self._set_plan(str(uuid.uuid4()), conversion=self.conversion)

    def test_a_retired_plan_releases_its_rows(self) -> None:
        plan_id = str(uuid.uuid4())
        self._set_plan(plan_id, conversion=self.conversion)
        record_installment_decision(
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=hashlib.sha256(b"retire").hexdigest(),
            decision_kind="INSTALLMENT_PLAN_RETIRE",
            anchors={"plan": plan_id},
            payload={"reason": "Confirmed the wrong chain"},
        )
        refresh_ledger(self.database)
        self.assertEqual(self._view(self.instalment)["bucket"], "spending")
        self.assertEqual(self._view(self.conversion)["bucket"], "pending_review")


    def test_a_schema_25_ledger_with_plans_backs_up_and_migrates(self) -> None:
        # The migration backup replays the journal against the pre-upgrade
        # layout, which has no `installment_conversions` table yet.
        self._set_plan(str(uuid.uuid4()), conversion=None)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TABLE installment_conversions")
            for table in ("entry_trips", "trips", "entry_countries", "report_definitions", "card_repayment_plans"):  # schemas 27-29
                connection.execute(f"DROP TABLE {table}")
            connection.execute("UPDATE schema_metadata SET value = '25' WHERE key = 'schema_version'")
            connection.commit()
        finally:
            connection.close()
        bundle = self.root / "premigration-bundle"
        backup_ledger(self.database, self.root / "evidence", bundle, allow_stale_schema=True)
        result = apply_pending_migrations(self.database, backup_dir=bundle)
        self.assertEqual((result.from_version, result.to_version), (25, SCHEMA_VERSION))
        refresh_ledger(self.database)
        self.assertEqual(self._view(self.instalment)["bucket"], "installment")

if __name__ == "__main__":
    unittest.main()
