"""M1b §5: idempotent command decisions and the command_receipts projection."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fincl import refresh as refresh_module
from fincl.application import RequestIdConflict, record_decision
from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path, read_records
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
     "Kopi", "-3.00", "0.00", "SGD", "COMPLETED", "97.00"],
]


class CommandReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "ledger.sqlite3"
        source = root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        DocumentImportService.plaintext_phase2(self.database, root / "evidence").import_revolut_pdf(source)
        self.anchor = self._first_anchor()

    def _first_anchor(self) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT natural_anchor FROM ledger_entries ORDER BY effective_at LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()

    def _override(self, request_id: str, *, category: str, intent: str):
        return record_decision(
            self.database,
            request_id=request_id,
            intent_hash=intent,
            decision_kind="CATEGORY_OVERRIDE",
            anchors={"entry": self.anchor},
            payload={"category": category, "reason": "test"},
            policy_version="category-engine-2026-v1",
            snapshot=f"override {self.anchor} -> {category}",
        )

    def _journal_len(self) -> int:
        return len(read_records(default_journal_path(self.database)))

    def test_first_call_appends_and_returns_a_generation_token(self) -> None:
        receipt = self._override("req-1", category="Food & Dining / Cafes & Drinks", intent="i1")
        self.assertTrue(receipt.appended)
        self.assertTrue((receipt.generation_token or "").startswith("sha256:"))
        self.assertEqual(self._journal_len(), 1)

    def test_same_request_id_and_intent_is_idempotent(self) -> None:
        first = self._override("req-1", category="Food & Dining / Cafes & Drinks", intent="i1")
        again = self._override("req-1", category="Food & Dining / Cafes & Drinks", intent="i1")
        self.assertTrue(first.appended)
        self.assertFalse(again.appended)
        self.assertEqual(again.journal_seq, first.journal_seq)
        self.assertEqual(again.record_hash, first.record_hash)
        self.assertEqual(self._journal_len(), 1)

    def test_same_request_id_different_intent_is_refused(self) -> None:
        self._override("req-1", category="Food & Dining / Cafes & Drinks", intent="i1")
        with self.assertRaises(RequestIdConflict):
            self._override("req-1", category="Shopping / General Shopping", intent="i2")
        self.assertEqual(self._journal_len(), 1)

    def test_the_journal_record_carries_the_command_envelope_and_verifies(self) -> None:
        self._override("req-7", category="Food & Dining / Cafes & Drinks", intent="i7")
        # read_records fully re-verifies the hash chain, command field included.
        records = read_records(default_journal_path(self.database))
        self.assertEqual(records[0].command, {"request_id": "req-7", "intent_hash": "i7"})

    def test_receipts_are_rebuilt_from_the_journal_on_replay(self) -> None:
        self._override("req-9", category="Food & Dining / Cafes & Drinks", intent="i9")
        connection = connect_sqlite(self.database)
        try:
            initialize_schema(connection)
            connection.execute("DELETE FROM command_receipts")
            connection.commit()
            replay_journal(connection)
            row = connection.execute(
                "SELECT intent_hash, journal_seq FROM command_receipts WHERE request_id = 'req-9'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["intent_hash"], "i9")
        self.assertEqual(row["journal_seq"], 1)

    def test_a_failed_refresh_still_records_the_decision_as_pending(self) -> None:
        with mock.patch.object(
            refresh_module, "publish_generation", side_effect=RuntimeError("x")
        ):
            receipt = self._override(
                "req-pending", category="Food & Dining / Cafes & Drinks", intent="ip"
            )
        self.assertTrue(receipt.appended)
        self.assertTrue(receipt.refresh_pending)
        self.assertIsNone(receipt.generation_token)
        self.assertEqual(self._journal_len(), 1)

        # A retry with the same id is idempotent even though the refresh failed.
        again = self._override(
            "req-pending", category="Food & Dining / Cafes & Drinks", intent="ip"
        )
        self.assertFalse(again.appended)
        self.assertEqual(again.journal_seq, receipt.journal_seq)
        self.assertEqual(self._journal_len(), 1)

    def test_recovers_a_durable_append_without_a_duplicate(self) -> None:
        # Simulate a crash after the journal append but before the projection
        # committed: the record is on disk, command_receipts is not.
        record = append_decision(
            default_journal_path(self.database),
            decision_kind="CATEGORY_OVERRIDE",
            anchors={"entry": self.anchor},
            payload={"category": "Food & Dining / Cafes & Drinks", "reason": "crash"},
            policy_version="category-engine-2026-v1",
            snapshot="crash override",
            command={"request_id": "req-crash", "intent_hash": "ic"},
        )
        receipt = self._override(
            "req-crash", category="Food & Dining / Cafes & Drinks", intent="ic"
        )
        self.assertFalse(receipt.appended)
        self.assertEqual(receipt.journal_seq, record.seq)
        self.assertEqual(self._journal_len(), 1)


if __name__ == "__main__":
    unittest.main()
