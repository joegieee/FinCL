"""A statement whose rows are already imported is the same evidence again.

A re-download with different bytes, or a mid-month export inside the month's
statement, is kept as evidence but never counted twice. Only rows that disagree
are a conflict.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fincl.document_service import DocumentImportService
from fincl.queries import relationships_review
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes


def _row(day: str, description: str, amount: str, balance: str) -> list[str]:
    kind = "Topup" if not amount.startswith("-") else "Card Payment"
    return [kind, "Current", f"{day} 10:00:00", f"{day} 10:00:01", description, amount,
            "0.00", "SGD", "COMPLETED", balance]


JANUARY = [
    _row("2026-01-01", "Salary", "1000.00", "1000.00"),
    _row("2026-01-10", "Grocer", "-40.00", "960.00"),
    _row("2026-01-20", "Bookshop", "-25.00", "935.00"),
    _row("2026-01-31", "Fee", "-5.00", "930.00"),
]


class StatementSupersessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger.sqlite3"
        self.service = DocumentImportService.plaintext_phase2(self.database, self.root / "evidence")

    def upload(self, name: str, data: bytes):
        source = self.root / name
        source.write_bytes(data)
        return self.service.import_revolut_pdf(source)

    def counted(self) -> list[tuple[str, str]]:
        connection = sqlite3.connect(self.database)
        try:
            return sorted(
                (row[0], row[1]) for row in connection.execute(
                    "SELECT substr(effective_at, 1, 10), amount_text FROM ledger_entries "
                    "WHERE analytics_eligible = 1")
            )
        finally:
            connection.close()

    def states(self) -> dict[str, tuple[str, str]]:
        connection = sqlite3.connect(self.database)
        try:
            return {
                f"{row[0]}..{row[1]}": (row[2], row[3]) for row in connection.execute(
                    "SELECT period_start, period_end, selection_state, conflict_state FROM statements")
            }
        finally:
            connection.close()

    def test_a_redownload_with_different_bytes_adds_nothing(self) -> None:
        original = revolut_pdf_bytes(JANUARY)
        first = self.upload("january.pdf", original)
        second = self.upload("january-again.pdf", original + b"\n% downloaded again\n")

        self.assertEqual((first.status, second.status), ("ACCEPTED", "ACCEPTED"))
        self.assertFalse(second.evidence_reused)
        self.assertEqual(second.superseded_by, "2026-01-01..2026-01-31")
        self.assertEqual(len(self.counted()), 4)
        connection = sqlite3.connect(self.database)
        try:
            states = sorted(connection.execute(
                "SELECT selection_state, conflict_state FROM statements").fetchall())
        finally:
            connection.close()
        self.assertEqual(states, [("CANDIDATE", "NONE"), ("SELECTED", "NONE")])
        refresh_ledger(self.database)
        self.assertEqual(relationships_review(self.database)["total"], 0)

    def test_a_mid_month_export_inside_the_months_statement_is_counted_once(self) -> None:
        mid = self.upload("mid.pdf", revolut_pdf_bytes(JANUARY[:2]))
        month = self.upload("month.pdf", revolut_pdf_bytes(JANUARY))

        self.assertEqual(month.supersedes, ("2026-01-01..2026-01-10",))
        self.assertIsNone(mid.superseded_by)
        self.assertEqual(self.states()["2026-01-01..2026-01-10"], ("CANDIDATE", "NONE"))
        self.assertEqual(len(self.counted()), 4)

        later = self.upload("mid-later.pdf", revolut_pdf_bytes(JANUARY[:3]))
        self.assertEqual(later.superseded_by, "2026-01-01..2026-01-31")
        self.assertEqual(len(self.counted()), 4)

    def test_a_contained_statement_that_disagrees_is_still_counted(self) -> None:
        self.upload("month.pdf", revolut_pdf_bytes(JANUARY))
        different = [JANUARY[0], _row("2026-01-10", "Grocer", "-41.00", "959.00")]

        result = self.upload("mid.pdf", revolut_pdf_bytes(different))

        self.assertIsNone(result.superseded_by)
        self.assertEqual(self.states()["2026-01-01..2026-01-10"], ("SELECTED", "NONE"))

    def test_the_same_period_with_different_rows_is_still_a_conflict(self) -> None:
        self.upload("month.pdf", revolut_pdf_bytes(JANUARY))
        changed = [*JANUARY[:2], _row("2026-01-20", "Bookshop", "-26.00", "934.00"),
                   _row("2026-01-31", "Fee", "-5.00", "929.00")]

        result = self.upload("month-changed.pdf", revolut_pdf_bytes(changed))

        self.assertEqual(result.status, "QUARANTINED")
        self.assertIsNone(result.superseded_by)
        self.assertEqual(self.states(), {"2026-01-01..2026-01-31": ("CANDIDATE", "CONFLICT")})


if __name__ == "__main__":
    unittest.main()
