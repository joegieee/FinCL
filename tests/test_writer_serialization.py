"""M1b §2: every writer contends on the one ledger lock; reads do not."""
from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import unittest
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.document_service import DocumentImportService
from fincl.locking import WRITER_LOCK_NAME
from fincl.refresh import refresh_ledger
from fincl.report import economic_flow_report_data
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
     "Coffee", "-4.00", "0.00", "SGD", "COMPLETED", "96.00"],
]


class WriterSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        self.evidence = self.ledger / "evidence"
        self.database = self.ledger / "fincl.sqlite3"
        src = self.root / "revolut.csv"
        src.write_bytes(csv_bytes(_ROWS))
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(src)
        refresh_ledger(self.database)

    def _hold_lock(self) -> int:
        fd = os.open(self.ledger / WRITER_LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _assert_blocks_then_completes(self, target) -> None:
        held = self._hold_lock()
        done: list[bool] = []

        def _run() -> None:
            target()
            done.append(True)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=0.5)
        self.assertTrue(worker.is_alive(), "writer did not wait for the lock")
        self.assertEqual(done, [])

        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        worker.join(timeout=4.0)
        self.assertEqual(done, [True], "writer did not proceed after the lock cleared")

    def test_refresh_waits_for_the_writer_lock(self) -> None:
        self._assert_blocks_then_completes(lambda: refresh_ledger(self.database))

    def test_a_second_import_waits_for_the_writer_lock(self) -> None:
        other = self.root / "second.csv"
        other.write_bytes(csv_bytes([
            ["Card Payment", "Current", "2026-02-02 10:00:00", "2026-02-02 10:00:01",
             "Tea", "-2.00", "0.00", "SGD", "COMPLETED", "94.00"],
        ]))
        self._assert_blocks_then_completes(
            lambda: DocumentImportService.plaintext_phase2(
                self.database, self.evidence
            ).import_revolut_pdf(other)
        )

    def test_a_decision_command_waits_for_the_writer_lock(self) -> None:
        def _categorize() -> None:
            outcome = CliRunner().invoke(app, [
                "categorize", "--database", str(self.database),
            ])
            assert outcome.exit_code == 0, outcome.output

        self._assert_blocks_then_completes(_categorize)

    def test_a_read_does_not_wait_for_the_writer_lock(self) -> None:
        held = self._hold_lock()
        try:
            # No refresh (would take the lock); the read path is lock-free.
            data = economic_flow_report_data(self.database)
            self.assertIn("currencies", data)
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)


if __name__ == "__main__":
    unittest.main()
