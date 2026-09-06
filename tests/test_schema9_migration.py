"""M1b: the real schema 8 -> 9 migration and its backup/restore round trip.

Schema 9 adds two derived tables and nothing else, so a faithful schema-8
fixture is a fresh schema-9 ledger with those two tables dropped and the version
stamp rolled back — the exact pre-upgrade layout, built from the same DDL.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from fincl.backup import backup_ledger, restore_ledger
from fincl.cli import app
from fincl.db import SCHEMA_VERSION
from fincl.errors import SchemaVersionError
from fincl.migrations import MIGRATION_8_TO_9_CHECKSUM, apply_pending_migrations
from fincl.refresh import refresh_ledger
from fincl.report import economic_flow_report_data
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes

_ROWS = [
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
     "Coffee", "-4.00", "0.00", "SGD", "COMPLETED", "96.00"],
]


def _roll_back_to_schema_8(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TABLE projection_generation")
        connection.execute("DROP TABLE command_receipts")
        connection.execute(
            "UPDATE schema_metadata SET value = '8' WHERE key = 'schema_version'"
        )
        connection.commit()
    finally:
        connection.close()


class Schema9MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        self.evidence = self.ledger / "evidence"
        self.database = self.ledger / "fincl.sqlite3"
        source = self.root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        FinCLService.plaintext_phase1(self.database, self.evidence).import_revolut(source)

    def _stored_version(self) -> int:
        connection = sqlite3.connect(self.database)
        try:
            return int(
                connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def _tables(self, database: Path) -> set[str]:
        connection = sqlite3.connect(database)
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()

    def test_a_schema_8_ledger_is_refused_at_normal_startup(self) -> None:
        _roll_back_to_schema_8(self.database)
        with self.assertRaisesRegex(SchemaVersionError, "schema version 8"):
            refresh_ledger(self.database)

    def test_migrate_adds_the_tables_stamps_the_version_and_records_the_step(self) -> None:
        _roll_back_to_schema_8(self.database)
        self.assertNotIn("projection_generation", self._tables(self.database))

        bundle = self.root / "premigration-bundle"
        backup_ledger(self.database, self.evidence, bundle, allow_stale_schema=True)
        result = apply_pending_migrations(self.database, backup_dir=bundle)

        self.assertEqual((result.from_version, result.to_version), (8, SCHEMA_VERSION))
        self.assertEqual(result.steps, [9])
        self.assertEqual(self._stored_version(), SCHEMA_VERSION)
        self.assertIn("projection_generation", self._tables(self.database))
        self.assertIn("command_receipts", self._tables(self.database))

        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT from_version, checksum FROM schema_migrations WHERE to_version = 9"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (8, MIGRATION_8_TO_9_CHECKSUM))

        # The upgraded ledger opens, refreshes and reads normally.
        token = refresh_ledger(self.database).generation_token
        self.assertTrue(token.startswith("sha256:"))
        self.assertIn("currencies", economic_flow_report_data(self.database))

    def test_migrate_is_a_no_op_on_a_current_ledger(self) -> None:
        bundle = self.root / "bundle"
        backup_ledger(self.database, self.evidence, bundle)
        result = apply_pending_migrations(self.database, backup_dir=bundle)
        self.assertEqual(
            (result.from_version, result.to_version, result.steps),
            (SCHEMA_VERSION, SCHEMA_VERSION, []),
        )

    def test_the_premigration_backup_restores_to_a_separate_directory(self) -> None:
        _roll_back_to_schema_8(self.database)
        bundle = self.root / "premigration-bundle"
        backup_ledger(self.database, self.evidence, bundle, allow_stale_schema=True)
        apply_pending_migrations(self.database, backup_dir=bundle)

        restored = self.root / "restored"
        result = restore_ledger(bundle, restored)
        self.assertTrue((restored / "fincl.sqlite3").is_file())
        self.assertEqual(Path(result.location), restored)
        connection = sqlite3.connect(restored / "fincl.sqlite3")
        try:
            version = int(
                connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        # Restores at its captured (pre-upgrade) schema; the operator migrates it.
        self.assertEqual(version, 8)

    def test_cli_migrate_end_to_end(self) -> None:
        _roll_back_to_schema_8(self.database)
        outcome = CliRunner().invoke(
            app,
            [
                "migrate",
                "--database", str(self.database),
                "--evidence-dir", str(self.evidence),
                "--backup-output", str(self.root / "cli-bundle"),
                "--json",
            ],
        )
        self.assertEqual(outcome.exit_code, 0, outcome.output)
        self.assertIn('"status": "MIGRATED"', outcome.output)
        self.assertEqual(self._stored_version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
