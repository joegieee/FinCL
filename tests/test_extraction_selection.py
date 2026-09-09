"""Phase 4 milestone 4.1 — one selected extraction run per evidence blob.

``natural_anchor`` is unique only within an extraction run and nothing demoted a
superseded run, so a parser-version bump could double-count the analytical set
and split a ``CATEGORY_OVERRIDE``'s binding (Confirmed 30). These tests cover the
``selection_state`` machinery, the partial unique index, ``resolve_anchor``
ambiguity, and the ``needs_reextraction`` surfacing.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.db import connect_sqlite, initialize_schema
from fincl.decisions import _resolve, resolve_anchor
from fincl.document_service import DocumentImportService
from fincl.selection import (
    CURRENT_EXTRACTION_TRIPLES,
    needs_reextraction,
    recompute_selection,
)
from tests.revolut_pdf import revolut_pdf_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "100.00", "0.00", "SGD", "COMPLETED", "100.00"],
    ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01", "Shop", "-10.00", "0.00", "SGD", "COMPLETED", "90.00"],
]


def _csv_bytes() -> bytes:
    return revolut_pdf_bytes(_ROWS)


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(self.source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def _runs(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT id, evidence_sha256, status, selection_state, source_profile_version, "
            "parser_version, extraction_policy_version FROM extraction_runs ORDER BY id"
        ).fetchall()

    def _clone_row(self, table: str, where: str, overrides: dict[str, object]) -> None:
        source = self.connection.execute(f"SELECT * FROM {table} WHERE {where}").fetchone()
        values = dict(source)
        values.update(overrides)
        cols = list(values)
        self.connection.execute(
            f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(values[c] for c in cols),
        )

    def _clone_stale_run(self) -> str:
        """Insert a second ACCEPTED run for the same blob at a superseded parser
        version, plus a twin ledger entry sharing the natural_anchor."""
        original = self._runs()[0]
        stale_id = "ext_stale_parser_v0"
        self.connection.execute(
            """
            INSERT INTO extraction_runs(
                id, evidence_sha256, source_profile_version, parser_version,
                extraction_policy_version, started_at, status, selection_state
            ) VALUES (?, ?, ?, 'revolut-parser-v0', ?, ?, 'ACCEPTED', 'SUPERSEDED')
            """,
            (
                stale_id,
                original["evidence_sha256"],
                original["source_profile_version"],
                original["extraction_policy_version"],
                "2020-01-01T00:00:00Z",
            ),
        )
        anchor = self.connection.execute(
            "SELECT natural_anchor FROM ledger_entries ORDER BY id LIMIT 1"
        ).fetchone()[0]
        self._clone_row(
            "raw_records",
            "id = (SELECT raw_record_id FROM ledger_entries ORDER BY id LIMIT 1)",
            {"id": "raw_twin_stale", "extraction_run_id": stale_id,
             "source_anchor": "twin", "extraction_ordinal": 999},
        )
        self._clone_row(
            "ledger_entries",
            "id = (SELECT id FROM ledger_entries ORDER BY id LIMIT 1)",
            {"id": "entry_twin_stale", "extraction_run_id": stale_id,
             "raw_record_id": "raw_twin_stale", "natural_anchor": anchor},
        )
        self.connection.commit()
        return stale_id

    def test_fresh_import_selects_exactly_one_run_per_blob(self) -> None:
        runs = self._runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "ACCEPTED")
        self.assertEqual(runs[0]["selection_state"], "SELECTED")
        triple = (
            runs[0]["source_profile_version"],
            runs[0]["parser_version"],
            runs[0]["extraction_policy_version"],
        )
        self.assertIn(triple, CURRENT_EXTRACTION_TRIPLES)

    def test_recompute_is_idempotent(self) -> None:
        recompute_selection(self.connection)
        recompute_selection(self.connection)
        states = [r["selection_state"] for r in self._runs()]
        self.assertEqual(states, ["SELECTED"])

    def test_parser_bump_demotes_the_stale_run(self) -> None:
        self._clone_stale_run()
        recompute_selection(self.connection)
        by_id = {r["id"]: r["selection_state"] for r in self._runs()}
        self.assertEqual(by_id["ext_stale_parser_v0"], "SUPERSEDED")
        selected = [rid for rid, state in by_id.items() if state == "SELECTED"]
        self.assertEqual(len(selected), 1)
        self.assertNotEqual(selected[0], "ext_stale_parser_v0")

    def test_partial_unique_index_forbids_two_selected_runs_per_blob(self) -> None:
        stale_id = self._clone_stale_run()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE extraction_runs SET selection_state = 'SELECTED' WHERE id = ?",
                (stale_id,),
            )

    def test_resolve_anchor_picks_the_selected_twin(self) -> None:
        self._clone_stale_run()
        recompute_selection(self.connection)
        anchor = self.connection.execute(
            "SELECT natural_anchor FROM ledger_entries WHERE id = 'entry_twin_stale'"
        ).fetchone()[0]
        matches = self.connection.execute(
            "SELECT count(*) FROM ledger_entries WHERE natural_anchor = ?", (anchor,)
        ).fetchone()[0]
        self.assertEqual(matches, 2)
        resolved = resolve_anchor(self.connection, anchor)
        selected_entry = self.connection.execute(
            "SELECT e.id FROM ledger_entries e JOIN extraction_runs x ON x.id = e.extraction_run_id "
            "WHERE e.natural_anchor = ? AND x.selection_state = 'SELECTED'",
            (anchor,),
        ).fetchone()[0]
        self.assertEqual(resolved, selected_entry)
        self.assertIsNone(_resolve(self.connection, anchor).dangling_reason)

    def test_resolve_anchor_dangles_on_genuine_ambiguity(self) -> None:
        """Two non-selected entries sharing an anchor -> distinct dangling reason."""
        anchor = self.connection.execute(
            "SELECT natural_anchor FROM ledger_entries ORDER BY id LIMIT 1"
        ).fetchone()[0]
        self._clone_row(
            "raw_records",
            "id = (SELECT raw_record_id FROM ledger_entries ORDER BY id LIMIT 1)",
            {"id": "raw_ambig_twin", "source_anchor": "ambig", "extraction_ordinal": 998},
        )
        self._clone_row(
            "ledger_entries",
            "id = (SELECT id FROM ledger_entries ORDER BY id LIMIT 1)",
            {"id": "entry_ambig_twin", "raw_record_id": "raw_ambig_twin"},
        )
        self.connection.execute("UPDATE extraction_runs SET selection_state = 'QUARANTINED'")
        self.connection.commit()
        result = _resolve(self.connection, anchor)
        self.assertIsNone(result.entry_id)
        self.assertEqual(result.dangling_reason, "anchor resolves to multiple entries")

    def test_needs_reextraction_surfaces_a_stale_only_blob(self) -> None:
        # Demote the only run to a stale version with no current run behind it.
        self.connection.execute(
            "UPDATE extraction_runs SET parser_version = 'revolut-parser-v0', "
            "selection_state = 'SUPERSEDED'"
        )
        self.connection.commit()
        recompute_selection(self.connection)
        stale = needs_reextraction(self.connection)
        self.assertEqual(len(stale), 1)
        self.assertTrue(stale[0]["original_path"].endswith("revolut.csv"))

    def test_coverage_reports_needs_reextraction_count(self) -> None:
        self.connection.execute(
            "UPDATE extraction_runs SET parser_version = 'revolut-parser-v0'"
        )
        self.connection.commit()
        recompute_selection(self.connection)
        result = CliRunner().invoke(
            app, ["coverage", "--database", str(self.database), "--plain"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("needs_reextraction=1", result.output)


if __name__ == "__main__":
    unittest.main()
