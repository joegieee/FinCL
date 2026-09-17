"""The real schema 8 -> 9 -> 10 -> 11 -> 12 migrations and their backup/restore
round trip.

Every step either adds derived tables or relaxes a constraint on one, rewriting
no rows, so a faithful older-schema fixture is a fresh current ledger with the
newer tables dropped and the version stamp rolled back — close enough to the
pre-upgrade layout that the add-only steps replay identically and the 11->12
table rebuild is a verbatim row copy.
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
from fincl.document_service import DocumentImportService
from fincl.errors import SchemaVersionError
from fincl.migrations import (
    MIGRATION_8_TO_9_CHECKSUM,
    MIGRATION_9_TO_10_CHECKSUM,
    MIGRATION_10_TO_11_CHECKSUM,
    MIGRATION_11_TO_12_CHECKSUM,
    MIGRATION_12_TO_13_CHECKSUM,
    MIGRATION_13_TO_14_CHECKSUM,
    MIGRATION_14_TO_15_CHECKSUM,
    MIGRATION_15_TO_16_CHECKSUM,
    MIGRATION_16_TO_17_CHECKSUM,
    MIGRATION_17_TO_18_CHECKSUM,
    MIGRATION_18_TO_19_CHECKSUM,
    MIGRATION_19_TO_20_CHECKSUM,
    MIGRATION_20_TO_21_CHECKSUM,
    MIGRATION_21_TO_22_CHECKSUM,
    MIGRATION_22_TO_23_CHECKSUM,
    MIGRATION_23_TO_24_CHECKSUM,
    MIGRATION_24_TO_25_CHECKSUM,
    apply_pending_migrations,
)
from fincl.refresh import refresh_ledger
from fincl.report import economic_flow_report_data
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
     "Coffee", "-4.00", "0.00", "SGD", "COMPLETED", "96.00"],
]


_SCHEMA_25_TABLES = ("reserve_policy",)
_SCHEMA_24_TABLES = ("movement_coverage_certifications",)
_SCHEMA_23_TABLES = ("reporting_policy",)
_SCHEMA_22_TABLES = (
    "forecast_snapshots",
    "forecast_actual_matches",
)
_SCHEMA_21_TABLES = (
    "sinking_funds",
    "sinking_fund_allocations",
    "sinking_fund_releases",
)
_SCHEMA_20_TABLES = ("component_provenance",)
_SCHEMA_19_TABLES = ("balance_observations",)
_SCHEMA_17_TABLES = ("card_payment_allocations",)
_SCHEMA_18_TABLES = (
    "budget_revisions", "budget_archive_periods", "goal_details",
    "goal_observations", "goal_archive", "recurring_commitments",
    "recurring_candidate_dismissals", "recurring_occurrences",
)
_SCHEMA_16_TABLES = (
    "installment_plans",
    "installment_occurrences",
    "installment_schedule",
    "installment_purchase_allocations",
    "installment_settlements",
)
_SCHEMA_15_TABLES = (
    "card_facilities",
    "card_facility_accounts",
    "card_limit_observations",
)
_SCHEMA_14_TABLES = ("card_statement_facts",)
_SCHEMA_13_TABLES = (
    "personal_debts",
    "personal_debt_events",
    "personal_debt_allocations",
    "entry_residuals",
)
_SCHEMA_11_TABLES = ("relationship_decisions", "duplicate_suppressions", "refund_allocations")
_SCHEMA_10_TABLES = ("analysis_components", "component_relationships", "component_source_refs")
_SCHEMA_9_TABLES = ("projection_generation", "command_receipts")
# Tables added by each step, keyed by the version it removes to reach. Schema 12
# added no tables (it rebuilt six in place), so it has no entry here.
_ADDED_AFTER = {
    8: _SCHEMA_9_TABLES,
    9: _SCHEMA_10_TABLES,
    10: _SCHEMA_11_TABLES,
    12: _SCHEMA_13_TABLES,
    13: _SCHEMA_14_TABLES,
    14: _SCHEMA_15_TABLES,
    15: _SCHEMA_16_TABLES,
    16: _SCHEMA_17_TABLES,
    17: _SCHEMA_18_TABLES,
    18: _SCHEMA_19_TABLES,
    19: _SCHEMA_20_TABLES,
    20: _SCHEMA_21_TABLES,
    21: _SCHEMA_22_TABLES,
    22: _SCHEMA_23_TABLES,
    23: _SCHEMA_24_TABLES,
    24: _SCHEMA_25_TABLES,
}


def _roll_back(database: Path, target: int) -> None:
    """Drop every table added after ``target`` and stamp the version back."""
    connection = sqlite3.connect(database)
    try:
        for version in sorted(_ADDED_AFTER, reverse=True):
            if version >= target:
                for table in _ADDED_AFTER[version]:
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            (str(target),),
        )
        connection.commit()
    finally:
        connection.close()


def _roll_back_to_schema_9(database: Path) -> None:
    _roll_back(database, 9)


def _roll_back_to_schema_8(database: Path) -> None:
    _roll_back(database, 8)


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
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(source)

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
        self.assertEqual(
            result.steps, [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]
        )
        self.assertEqual(self._stored_version(), SCHEMA_VERSION)
        for table in (
            *_SCHEMA_9_TABLES, *_SCHEMA_10_TABLES, *_SCHEMA_11_TABLES, *_SCHEMA_13_TABLES,
            *_SCHEMA_14_TABLES, *_SCHEMA_15_TABLES, *_SCHEMA_16_TABLES, *_SCHEMA_17_TABLES,
            *_SCHEMA_18_TABLES, *_SCHEMA_19_TABLES, *_SCHEMA_20_TABLES,
            *_SCHEMA_21_TABLES, *_SCHEMA_22_TABLES, *_SCHEMA_23_TABLES,
            *_SCHEMA_24_TABLES, *_SCHEMA_25_TABLES,
        ):
            self.assertIn(table, self._tables(self.database))

        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT to_version, from_version, checksum FROM schema_migrations "
                "ORDER BY to_version"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            rows,
            [
                (9, 8, MIGRATION_8_TO_9_CHECKSUM),
                (10, 9, MIGRATION_9_TO_10_CHECKSUM),
                (11, 10, MIGRATION_10_TO_11_CHECKSUM),
                (12, 11, MIGRATION_11_TO_12_CHECKSUM),
                (13, 12, MIGRATION_12_TO_13_CHECKSUM),
                (14, 13, MIGRATION_13_TO_14_CHECKSUM),
                (15, 14, MIGRATION_14_TO_15_CHECKSUM),
                (16, 15, MIGRATION_15_TO_16_CHECKSUM),
                (17, 16, MIGRATION_16_TO_17_CHECKSUM),
                (18, 17, MIGRATION_17_TO_18_CHECKSUM),
                (19, 18, MIGRATION_18_TO_19_CHECKSUM),
                (20, 19, MIGRATION_19_TO_20_CHECKSUM),
                (21, 20, MIGRATION_20_TO_21_CHECKSUM),
                (22, 21, MIGRATION_21_TO_22_CHECKSUM),
                (23, 22, MIGRATION_22_TO_23_CHECKSUM),
                (24, 23, MIGRATION_23_TO_24_CHECKSUM),
                (25, 24, MIGRATION_24_TO_25_CHECKSUM),
            ],
        )

        # The upgraded ledger opens, refreshes and reads normally.
        token = refresh_ledger(self.database).generation_token
        self.assertTrue(token.startswith("sha256:"))
        self.assertIn("currencies", economic_flow_report_data(self.database))

    def test_migrate_from_schema_9_adds_only_the_newer_derived_tables(self) -> None:
        # A faithful schema-9 ledger: refresh once at the current schema, then
        # drop every table added after 9 and roll the stamp back.
        refresh_ledger(self.database)
        _roll_back_to_schema_9(self.database)
        for table in (
            *_SCHEMA_10_TABLES, *_SCHEMA_11_TABLES, *_SCHEMA_13_TABLES, *_SCHEMA_14_TABLES,
            *_SCHEMA_15_TABLES, *_SCHEMA_16_TABLES, *_SCHEMA_17_TABLES, *_SCHEMA_18_TABLES,
            *_SCHEMA_19_TABLES, *_SCHEMA_20_TABLES, *_SCHEMA_21_TABLES,
            *_SCHEMA_22_TABLES, *_SCHEMA_23_TABLES, *_SCHEMA_24_TABLES,
            *_SCHEMA_25_TABLES,
        ):
            self.assertNotIn(table, self._tables(self.database))

        fresh_layout = self._tables(self.database)
        bundle = self.root / "schema9-bundle"
        backup_ledger(self.database, self.evidence, bundle, allow_stale_schema=True)
        result = apply_pending_migrations(self.database, backup_dir=bundle)

        self.assertEqual((result.from_version, result.to_version), (9, SCHEMA_VERSION))
        self.assertEqual(
            result.steps,
            [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25],
        )
        self.assertEqual(
            self._tables(self.database) - fresh_layout,
            set(_SCHEMA_10_TABLES)
            | set(_SCHEMA_11_TABLES)
            | set(_SCHEMA_13_TABLES)
            | set(_SCHEMA_14_TABLES)
            | set(_SCHEMA_15_TABLES)
            | set(_SCHEMA_16_TABLES)
            | set(_SCHEMA_17_TABLES)
            | set(_SCHEMA_18_TABLES)
            | set(_SCHEMA_19_TABLES)
            | set(_SCHEMA_20_TABLES)
            | set(_SCHEMA_21_TABLES)
            | set(_SCHEMA_22_TABLES)
            | set(_SCHEMA_23_TABLES)
            | set(_SCHEMA_24_TABLES)
            | set(_SCHEMA_25_TABLES),
        )

        # It opens, refreshes and now carries a committed component projection.
        refresh_ledger(self.database)
        connection = sqlite3.connect(self.database)
        try:
            components = connection.execute(
                "SELECT count(*) FROM analysis_components"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreater(components, 0)

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
