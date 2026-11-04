"""F27 gate R4: verified backup / restore bundle and explicit migrations.

Covers a WAL-safe snapshot, writer-lock exclusion, missing / corrupt bundle
components, path-traversal rejection, staging cleanup on failure, a restored
ledger accepting a new decision, a newer-schema refusal, and the migration
framework's refusals, rollback, and post-migration reads.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from fincl import migrations
from fincl.backup import (
    backup_ledger,
    restore_ledger,
    verify_backup_bundle,
)
from fincl.db import SCHEMA_VERSION, connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import entry_anchor, replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError, SchemaVersionError
from fincl.locking import WRITER_LOCK_NAME
from fincl.migrations import Migration, apply_pending_migrations
from fincl.report import economic_flow_report_data
from tests.revolut_pdf import revolut_pdf_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "200.00", "0.00", "USD", "COMPLETED", "200.00"],
    ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01", "Shop", "-10.00", "0.00", "USD", "COMPLETED", "190.00"],
]


def _csv_bytes() -> bytes:
    return revolut_pdf_bytes(_ROWS)


class BackupRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        self.evidence = self.ledger / "evidence"
        source = self.root / "revolut.csv"
        source.write_bytes(_csv_bytes())
        self.database = self.ledger / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(source)
        self.journal = default_journal_path(self.database)
        connection = connect_sqlite(self.database)
        self.addCleanup(connection.close)
        initialize_schema(connection)
        anchor = entry_anchor(
            connection,
            connection.execute(
                "SELECT id FROM ledger_entries ORDER BY effective_at LIMIT 1"
            ).fetchone()[0],
        )
        append_decision(
            self.journal,
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": anchor},
            payload={"classification": "income", "reason": "seed"},
            policy_version="review-v1",
            snapshot="inbound income",
        )
        replay_journal(connection)

    def _backup(self, name: str = "bundle") -> Path:
        out = self.root / name
        backup_ledger(self.database, self.evidence, out)
        return out

    # --- round trip ---------------------------------------------------- #

    def test_backup_bundle_is_a_consistent_wal_safe_snapshot(self) -> None:
        bundle = self._backup()
        db = sqlite3.connect(f"file:{bundle / 'fincl.sqlite3'}?mode=ro", uri=True)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertGreater(
            db.execute("SELECT count(*) FROM ledger_entries").fetchone()[0], 0
        )
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["manifest_version"], "backup-v1")
        self.assertEqual(manifest["journal"]["seq"], 1)
        self.assertEqual(len(manifest["evidence"]), 1)

    def test_restore_reproduces_the_ledger_and_reopens(self) -> None:
        bundle = self._backup()
        restored = self.root / "restored"
        result = restore_ledger(bundle, restored)
        self.assertEqual(result.evidence_objects, 1)
        data = economic_flow_report_data(restored / "fincl.sqlite3")
        self.assertEqual(data["coverage_status"], "COVERED")

    def test_restored_ledger_accepts_a_new_decision_that_survives_reopen(self) -> None:
        bundle = self._backup()
        restored = self.root / "restored"
        restore_ledger(bundle, restored)
        restored_db = restored / "fincl.sqlite3"
        connection = connect_sqlite(restored_db)
        self.addCleanup(connection.close)
        initialize_schema(connection)
        anchor = entry_anchor(
            connection,
            connection.execute(
                "SELECT id FROM ledger_entries ORDER BY effective_at DESC LIMIT 1"
            ).fetchone()[0],
        )
        append_decision(
            default_journal_path(restored_db),
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": anchor},
            payload={"classification": "gift", "reason": "post-restore"},
            policy_version="review-v1",
            snapshot="post restore decision",
        )
        replay_journal(connection)
        connection.close()

        reopened = connect_sqlite(restored_db)
        self.addCleanup(reopened.close)
        initialize_schema(reopened)
        bound, dangling = replay_journal(reopened)
        self.assertEqual((bound, dangling), (2, 0))

    # --- refusals ---------------------------------------------------- #

    def test_backup_refuses_an_existing_output(self) -> None:
        (self.root / "bundle").mkdir()
        with self.assertRaisesRegex(FinCLError, "already exists"):
            backup_ledger(self.database, self.evidence, self.root / "bundle")

    def test_restore_refuses_an_existing_output(self) -> None:
        bundle = self._backup()
        (self.root / "restored").mkdir()
        with self.assertRaisesRegex(FinCLError, "already exists"):
            restore_ledger(bundle, self.root / "restored")

    def test_missing_bundle_component_is_refused(self) -> None:
        bundle = self._backup()
        (bundle / "fincl.sqlite3").unlink()
        with self.assertRaisesRegex(FinCLError, "database checksum|missing"):
            restore_ledger(bundle, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_corrupt_bundle_database_is_refused(self) -> None:
        bundle = self._backup()
        (bundle / "fincl.sqlite3").write_bytes(b"corrupted")
        with self.assertRaisesRegex(FinCLError, "database checksum"):
            restore_ledger(bundle, self.root / "restored")

    def test_corrupt_evidence_object_in_bundle_is_refused(self) -> None:
        bundle = self._backup()
        for path in (bundle / "evidence").rglob("*"):
            if path.is_file():
                path.write_bytes(b"tampered")
        with self.assertRaisesRegex(FinCLError, "missing or corrupt"):
            verify_backup_bundle(bundle)

    def test_symlink_in_bundle_is_rejected(self) -> None:
        bundle = self._backup()
        (bundle / "sneaky").symlink_to(self.root)
        with self.assertRaisesRegex(FinCLError, "symlink"):
            verify_backup_bundle(bundle)

    def test_an_evidence_key_outside_the_bundle_is_rejected_before_restore_copies_it(self) -> None:
        bundle = self._backup()
        planted = b"planted\n"
        (bundle.parent / "planted").write_bytes(planted)
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["evidence"].append({"key": "../../planted", "sha256": hashlib.sha256(planted).hexdigest(),
                                     "byte_size": len(planted)})
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        output = self.root / "restores" / "sub" / "restored"
        output.parent.mkdir(parents=True)
        with self.assertRaisesRegex(FinCLError, "unsafe evidence path"):
            restore_ledger(bundle, output)
        self.assertEqual(list(output.parent.parent.iterdir()), [output.parent])
        self.assertEqual(list(output.parent.iterdir()), [])

    def test_newer_schema_bundle_is_refused_with_the_app_version(self) -> None:
        bundle = self._backup()
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["schema_version"] = SCHEMA_VERSION + 1
        (bundle / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(SchemaVersionError, "newer than this build"):
            verify_backup_bundle(bundle)

    def test_failed_restore_leaves_no_staging_directory(self) -> None:
        bundle = self._backup()
        (bundle / "fincl.sqlite3").write_bytes(b"corrupt")
        with self.assertRaises(FinCLError):
            restore_ledger(bundle, self.root / "restored")
        self.assertEqual(list(self.root.glob(".restore-staging-*")), [])

    # --- writer lock ------------------------------------------------- #

    def test_backup_waits_for_the_writer_lock(self) -> None:
        lock_fd = os.open(self.ledger / WRITER_LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        done: list[bool] = []

        def _run() -> None:
            backup_ledger(self.database, self.evidence, self.root / "locked-bundle")
            done.append(True)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=1.0)
        self.assertTrue(worker.is_alive())
        self.assertEqual(done, [])

        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        worker.join(timeout=5.0)
        self.assertEqual(done, [True])


class MigrationFrameworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        self.evidence = self.ledger / "evidence"
        source = self.root / "revolut.csv"
        source.write_bytes(_csv_bytes())
        self.database = self.ledger / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(source)

    def _backup(self) -> Path:
        out = self.root / "bundle"
        backup_ledger(self.database, self.evidence, out)
        return out

    def _stored_version(self) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            connection.close()

    def test_current_version_database_is_a_no_op(self) -> None:
        result = apply_pending_migrations(self.database, backup_dir=self._backup())
        self.assertEqual(
            (result.from_version, result.to_version, result.steps),
            (SCHEMA_VERSION, SCHEMA_VERSION, []),
        )

    def test_missing_migration_is_refused(self) -> None:
        bundle = self._backup()
        with (
            unittest.mock.patch.object(migrations, "SCHEMA_VERSION", SCHEMA_VERSION + 1),
            self.assertRaisesRegex(SchemaVersionError, "no migration is registered"),
        ):
            apply_pending_migrations(self.database, backup_dir=bundle)

    def test_backup_for_a_different_ledger_is_refused(self) -> None:
        other_root = self.root / "other"
        other_root.mkdir()
        other_source = other_root / "r.csv"
        other_source.write_bytes(_csv_bytes())
        other_db = other_root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(other_db, other_root / "evidence").import_revolut_pdf(other_source)
        other_bundle = self.root / "other-bundle"
        backup_ledger(other_db, other_root / "evidence", other_bundle)

        target = SCHEMA_VERSION + 1
        probe = Migration(SCHEMA_VERSION, target, "chk", lambda c: None)
        with (
            unittest.mock.patch.object(migrations, "SCHEMA_VERSION", target),
            unittest.mock.patch.dict(migrations.MIGRATIONS, {SCHEMA_VERSION: probe}, clear=True),
            self.assertRaisesRegex(FinCLError, "different ledger"),
        ):
            apply_pending_migrations(self.database, backup_dir=other_bundle)

    def test_migration_applies_stamps_and_is_then_a_no_op(self) -> None:
        bundle = self._backup()
        target = SCHEMA_VERSION + 1

        def _apply(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE IF NOT EXISTS _mig_probe (x INTEGER)")

        probe = Migration(SCHEMA_VERSION, target, "chk-1", _apply)
        with (
            unittest.mock.patch.object(migrations, "SCHEMA_VERSION", target),
            unittest.mock.patch.dict(migrations.MIGRATIONS, {SCHEMA_VERSION: probe}, clear=True),
        ):
            result = apply_pending_migrations(self.database, backup_dir=bundle)
            self.assertEqual(result.steps, [target])
            self.assertEqual(self._stored_version(), str(target))
            # Re-running is now a no-op.
            again = apply_pending_migrations(self.database, backup_dir=bundle)
            self.assertEqual(again.steps, [])

        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        row = connection.execute(
            "SELECT from_version, checksum FROM schema_migrations WHERE to_version = ?",
            (target,),
        ).fetchone()
        self.assertEqual(row, (SCHEMA_VERSION, "chk-1"))

    def test_a_failing_migration_rolls_back_and_leaves_the_database_usable(self) -> None:
        bundle = self._backup()
        target = SCHEMA_VERSION + 1

        def _apply(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE _doomed (x INTEGER)")
            raise RuntimeError("migration blew up")

        probe = Migration(SCHEMA_VERSION, target, "chk-x", _apply)
        with (
            unittest.mock.patch.object(migrations, "SCHEMA_VERSION", target),
            unittest.mock.patch.dict(migrations.MIGRATIONS, {SCHEMA_VERSION: probe}, clear=True),
            self.assertRaises(RuntimeError),
        ):
            apply_pending_migrations(self.database, backup_dir=bundle)

        self.assertEqual(self._stored_version(), str(SCHEMA_VERSION))
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_doomed'"
            ).fetchone()
        )
        # The database still opens normally.
        economic_flow_report_data(self.database)


if __name__ == "__main__":
    unittest.main()
