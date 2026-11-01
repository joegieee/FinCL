"""Warnings that only appear when two statements are read against each other."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from fincl.document_service import DocumentImportService
from fincl.refresh import refresh_ledger
from fincl.statement_warnings import STATEMENT_PERIOD_OVERLAP
from tests.statement_pdfs import TrustRow, trust_pdf_bytes


class StatementWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.database = self.root / "ledger.sqlite3"
        self.service = DocumentImportService.plaintext_phase2(self.database, self.root / "evidence")

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def _warnings(self, code: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(
                "SELECT * FROM extraction_issues WHERE code = ? ORDER BY id", (code,)
            ).fetchall()
        finally:
            connection.close()

    def _trust(self, name: str, start: date, end: date, opening: str, rows: list[TrustRow]) -> None:
        self.service.import_trust(
            self._write(name, trust_pdf_bytes(start, end, end, Decimal(opening), rows))
        )

    def test_a_partial_overlap_with_shared_rows_is_flagged_once(self) -> None:
        shared = TrustRow(date(2026, 2, 10), "Shop", Decimal("-20.00"))
        self._trust("a.pdf", date(2026, 1, 1), date(2026, 2, 15), "100.00",
                    [TrustRow(date(2026, 1, 10), "Pay", Decimal("50.00")), shared])
        self._trust("b.pdf", date(2026, 2, 1), date(2026, 3, 15), "130.00",
                    [shared, TrustRow(date(2026, 3, 10), "Pay", Decimal("5.00"))])

        refresh_ledger(self.database)
        first = self._warnings(STATEMENT_PERIOD_OVERLAP)
        refresh_ledger(self.database)
        second = self._warnings(STATEMENT_PERIOD_OVERLAP)

        self.assertEqual(json.loads(first[0]["context_json"])["shared_rows"], 1)
        self.assertEqual([row["id"] for row in first], [row["id"] for row in second])
        self.assertEqual(len(second), 1)

    def test_periods_that_touch_without_shared_rows_are_not_flagged(self) -> None:
        self._trust("a.pdf", date(2026, 1, 1), date(2026, 1, 31), "100.00",
                    [TrustRow(date(2026, 1, 10), "Pay", Decimal("50.00"))])
        self._trust("b.pdf", date(2026, 1, 31), date(2026, 2, 28), "150.00",
                    [TrustRow(date(2026, 2, 10), "Shop", Decimal("-20.00"))])

        refresh_ledger(self.database)

        self.assertEqual(self._warnings(STATEMENT_PERIOD_OVERLAP), [])
