"""M1b: a failed refresh rolls back whole — projection and generation intact."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fincl import refresh as refresh_module
from fincl.decision_journal import append_decision, default_journal_path
from fincl.document_service import DocumentImportService
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
     "Coffee", "-4.00", "0.00", "SGD", "COMPLETED", "96.00"],
    ["Card Payment", "Current", "2026-01-03 10:00:00", "2026-01-03 10:00:01",
     "Lunch", "-8.00", "0.00", "SGD", "COMPLETED", "88.00"],
]


class AtomicRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger.sqlite3"
        source = self.root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.good = refresh_ledger(self.database)

    def _coffee_anchor(self) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT natural_anchor FROM ledger_entries "
                "WHERE amount_text = '-4.00' LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()

    def _snapshot(self) -> dict[str, object]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return {
                "generation": connection.execute(
                    "SELECT generation_token FROM projection_generation WHERE id = 1"
                ).fetchone()[0],
                "entry_categories": connection.execute(
                    "SELECT count(*) FROM entry_categories"
                ).fetchone()[0],
                "category_rows": [
                    tuple(r)
                    for r in connection.execute(
                        "SELECT entry_id, group_name, category FROM entry_categories "
                        "ORDER BY entry_id"
                    )
                ],
                "runs": connection.execute(
                    "SELECT count(*) FROM categorization_runs"
                ).fetchone()[0],
            }
        finally:
            connection.close()

    def test_a_refresh_leaves_query_planner_statistics(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            tables = {row[0] for row in connection.execute("SELECT tbl FROM sqlite_stat1")}
        finally:
            connection.close()
        self.assertIn("ledger_entries", tables)

    def test_a_failing_refresh_leaves_projection_and_generation_untouched(self) -> None:
        before = self._snapshot()

        # A new rule would recategorize entries, but publish_generation blows up
        # after categorize has written its rows.
        append_decision(
            default_journal_path(self.database),
            decision_kind="CATEGORY_OVERRIDE",
            anchors={"entry": self._coffee_anchor()},
            payload={"category": "Food & Dining / Cafes & Drinks", "reason": "test"},
            policy_version="category-engine-2026-v1",
            snapshot="atomic test override",
        )

        boom = RuntimeError("publish failed")
        with mock.patch.object(
            refresh_module, "publish_generation", side_effect=boom
        ), self.assertRaises(RuntimeError):
            refresh_ledger(self.database)

        after = self._snapshot()
        self.assertEqual(after, before, "a failed refresh must not leave partial state")
        self.assertEqual(after["generation"], self.good.generation_token)

    def test_the_retry_after_a_failure_converges(self) -> None:
        append_decision(
            default_journal_path(self.database),
            decision_kind="CATEGORY_OVERRIDE",
            anchors={"entry": self._coffee_anchor()},
            payload={"category": "Food & Dining / Cafes & Drinks", "reason": "test"},
            policy_version="category-engine-2026-v1",
            snapshot="atomic test override",
        )
        with mock.patch.object(
            refresh_module, "publish_generation", side_effect=RuntimeError("x")
        ), self.assertRaises(RuntimeError):
            refresh_ledger(self.database)

        # Unpatched retry succeeds and now reflects the override.
        result = refresh_ledger(self.database)
        self.assertNotEqual(result.generation_token, self.good.generation_token)
        connection = sqlite3.connect(self.database)
        try:
            row = connection.execute(
                "SELECT ec.precedence_tier, ec.category FROM entry_categories ec "
                "JOIN ledger_entries e ON e.id = ec.entry_id "
                "WHERE e.amount_text = '-4.00'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (1, "Cafes & Drinks"))


if __name__ == "__main__":
    unittest.main()
