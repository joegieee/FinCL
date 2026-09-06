"""M1b §3: the projection generation token published by every refresh."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.db import SCHEMA_VERSION, connect_readonly
from fincl.generation import current_generation
from fincl.refresh import refresh_ledger
from fincl.service import FinCLService
from tests.test_fx_rates import _MAS_CSV
from tests.test_revolut_import import csv_bytes

_ROWS = [
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
     "Coffee", "-4.00", "0.00", "USD", "COMPLETED", "96.00"],
    ["Card Payment", "Current", "2026-01-03 10:00:00", "2026-01-03 10:00:01",
     "Lunch", "-8.00", "0.00", "USD", "COMPLETED", "88.00"],
]

class GenerationTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger.sqlite3"
        source = self.root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        result = FinCLService.plaintext_phase1(
            self.database, self.root / "evidence"
        ).import_revolut(source)
        self.assertEqual(result.status, "ACCEPTED")

    def _cli(self, *args: str) -> None:
        outcome = CliRunner().invoke(app, [*args, "--database", str(self.database)])
        self.assertEqual(outcome.exit_code, 0, outcome.output)

    def test_schema_is_nine_with_the_new_tables(self) -> None:
        connection = connect_readonly(self.database)
        try:
            version = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual(int(version), SCHEMA_VERSION)
        self.assertIn("projection_generation", tables)
        self.assertIn("command_receipts", tables)

    def test_refresh_publishes_a_token_and_a_no_op_refresh_keeps_it(self) -> None:
        first = refresh_ledger(self.database)
        self.assertTrue(first.generation_token.startswith("sha256:"))
        connection = connect_readonly(self.database)
        try:
            row = current_generation(connection)
        finally:
            connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["generation_token"], first.generation_token)

        second = refresh_ledger(self.database)
        self.assertEqual(second.generation_token, first.generation_token)

    def test_loading_fx_rates_changes_the_token(self) -> None:
        before = refresh_ledger(self.database).generation_token
        mas = self.root / "mas.csv"
        mas.write_text(_MAS_CSV)
        self._cli(
            "import-fx-rates", "--file", str(mas), "--mas-weekly-csv",
            "--source-version", "mas-weekly-2026-09",
        )
        after = refresh_ledger(self.database).generation_token
        self.assertNotEqual(after, before)
        # ...and is then stable again.
        self.assertEqual(refresh_ledger(self.database).generation_token, after)

    def test_a_journal_decision_changes_the_token(self) -> None:
        before = refresh_ledger(self.database).generation_token
        connection = connect_readonly(self.database)
        try:
            anchor = connection.execute(
                "SELECT natural_anchor FROM ledger_entries ORDER BY effective_at LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        self._cli(
            "review", "categorize", anchor,
            "--as", "Food & Dining / Cafes & Drinks", "--reason", "manual",
        )
        after = refresh_ledger(self.database).generation_token
        self.assertNotEqual(after, before)


if __name__ == "__main__":
    unittest.main()
