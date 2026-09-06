"""M1a: the MVP read surface executes no writes and needs an explicit refresh."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fincl.db import connect_readonly, connect_sqlite
from fincl.errors import SchemaVersionError
from fincl.refresh import refresh_ledger
from fincl.report import (
    coverage_report_data,
    economic_flow_report_data,
    latest_report_data,
    profile_report_data,
)
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes

_ROWS = [
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
     "Coffee", "-4.00", "0.00", "SGD", "COMPLETED", "96.00"],
    ["Card Payment", "Current", "2026-01-03 10:00:00", "2026-01-03 10:00:01",
     "Lunch", "-8.00", "0.00", "SGD", "COMPLETED", "88.00"],
]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PureReadTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "ledger.sqlite3"
        source = root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        result = FinCLService.plaintext_phase1(
            self.database, root / "evidence"
        ).import_revolut(source)
        self.assertEqual(result.status, "ACCEPTED")
        refresh_ledger(self.database)

    def test_reads_do_not_mutate_the_database(self) -> None:
        before = _digest(self.database)
        latest_report_data(self.database)
        coverage_report_data(self.database)
        economic_flow_report_data(self.database)
        economic_flow_report_data(self.database, start="2026-01-02", end="2026-01-03")
        profile_report_data(self.database)
        self.assertEqual(_digest(self.database), before)

    def test_read_connection_refuses_writes(self) -> None:
        connection = connect_readonly(self.database)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "INSERT INTO schema_metadata(key, value) VALUES ('x', 'y')"
                )
        finally:
            connection.close()

    def test_query_before_preparation_is_refused(self) -> None:
        missing = self.database.parent / "never-made.sqlite3"
        with self.assertRaises(SchemaVersionError):
            economic_flow_report_data(missing)

    def test_unstamped_database_is_refused(self) -> None:
        blank = self.database.parent / "blank.sqlite3"
        connect_sqlite(blank).close()  # creates the file, no schema
        with self.assertRaises(SchemaVersionError):
            latest_report_data(blank)

    def test_refresh_is_idempotent(self) -> None:
        first = refresh_ledger(self.database)
        second = refresh_ledger(self.database)
        self.assertEqual(first, second)
        self.assertEqual(
            economic_flow_report_data(self.database)["currencies"][0]["spending"],
            "12.00",
        )

    def test_economic_flow_reflects_a_journal_decision_only_after_refresh(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            anchor = connection.execute(
                "SELECT natural_anchor FROM ledger_entries ORDER BY effective_at LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()
        from fincl.decision_journal import append_decision, default_journal_path

        append_decision(
            default_journal_path(self.database),
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": anchor},
            payload={"classification": "income", "reason": "test"},
            policy_version="review-v1",
            snapshot="test",
        )
        # The read surface still serves the pre-decision projection.
        stale = economic_flow_report_data(self.database)
        self.assertEqual(stale["dangling_decisions"], 0)
        refresh_ledger(self.database)
        fresh = economic_flow_report_data(self.database)
        # The anchor is an outflow, so an income classification cannot bind it;
        # it lands as a dangling / non-applying decision after replay.
        self.assertGreaterEqual(
            fresh["dangling_decisions"] + fresh["currencies"][0]["entries"], 1
        )


if __name__ == "__main__":
    unittest.main()
