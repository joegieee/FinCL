"""MVP M4: a backup taken after a UI-style write restores with its decisions
and report values intact.

The write path here is ``application.record_decision`` — the exact call the HTTP
``/commands`` endpoint makes — so this proves the M1b application writer and the
F27 backup bundle are compatible, the open item PLAN-mvp.md §2 flagged for the
release gate.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fincl.application import record_decision
from fincl.backup import backup_ledger, restore_ledger
from fincl.decision_journal import default_journal_path, read_records
from fincl.queries import dashboard
from fincl.refresh import refresh_ledger
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "800.00", "0.00", "SGD", "COMPLETED", "800.00"],
    ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01",
     "Grocer", "-45.00", "0.00", "SGD", "COMPLETED", "755.00"],
    ["Card Payment", "Current", "2026-01-14 10:00:00", "2026-01-14 10:00:01",
     "Cafe", "-6.50", "0.00", "SGD", "COMPLETED", "748.50"],
]


class BackupAfterUiWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger" / "fincl.sqlite3"
        self.database.parent.mkdir()
        self.evidence = self.database.parent / "evidence"
        source = self.root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        FinCLService.plaintext_phase1(self.database, self.evidence).import_revolut(source)
        refresh_ledger(self.database)

    def test_decision_and_report_values_survive_backup_restore_reopen(self) -> None:
        entry_anchor = dashboard(self.database)["recent"][0]["anchor"]
        record_decision(
            self.database, request_id="ui-1", intent_hash="h1",
            decision_kind="CATEGORY_OVERRIDE", anchors={"entry": entry_anchor},
            payload={"category": "Food & Dining / Groceries", "reason": "corrected from the app"},
            policy_version="dashboard-v1", snapshot="ui override",
        )
        before = dashboard(self.database)

        bundle = self.root / "bundle"
        backup_ledger(self.database, self.evidence, bundle)

        restored = self.root / "restored"
        restore_ledger(bundle, restored)
        restored_db = restored / "fincl.sqlite3"

        # The projection is inside the copied SQLite file: the generation and
        # summary match before any re-preparation.
        reopened = dashboard(restored_db)
        self.assertEqual(reopened["generation"], before["generation"])
        self.assertEqual(reopened["summary"], before["summary"])

        # And a fresh preparation on the restored copy is a deterministic no-op.
        refresh_ledger(restored_db)
        self.assertEqual(dashboard(restored_db)["generation"], before["generation"])

        records = read_records(default_journal_path(restored_db))
        overrides = [r for r in records if r.decision_kind == "CATEGORY_OVERRIDE"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].command, {"request_id": "ui-1", "intent_hash": "h1"})
        self.assertEqual(overrides[0].payload["reason"], "corrected from the app")


if __name__ == "__main__":
    unittest.main()
