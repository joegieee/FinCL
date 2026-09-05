"""F24 gate R2: journal association and continuity.

Covers bootstrap on first import, same-directory reopen plus a new decision,
cross-directory rebuild refusal, missing / truncated / swapped / extended
journals, legacy adoption, and an interrupted initialization.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.association import (
    adopt_ledger,
    identity_file_path,
    read_association,
    require_valid_association,
)
from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import JOURNAL_FILENAME, append_decision, default_journal_path
from fincl.decisions import entry_anchor, replay_journal
from fincl.errors import FinCLError
from fincl.rebuild import rebuild_database
from fincl.revolut import EXPECTED_HEADER
from fincl.service import FinCLService

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "200.00", "0.00", "USD", "COMPLETED", "200.00"],
    ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01", "Shop", "-10.00", "0.00", "USD", "COMPLETED", "190.00"],
]


def _csv_bytes() -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(EXPECTED_HEADER)
    writer.writerows(_ROWS)
    return output.getvalue().encode("utf-8")


class AssociationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence"
        self.source = self.root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        FinCLService.plaintext_phase1(self.database, self.evidence).import_revolut(self.source)
        self.journal = default_journal_path(self.database)

    def _connect(self) -> sqlite3.Connection:
        connection = connect_sqlite(self.database)
        initialize_schema(connection)
        self.addCleanup(connection.close)
        return connection

    def _first_anchor(self, connection: sqlite3.Connection) -> str:
        entry_id = connection.execute(
            "SELECT id FROM ledger_entries ORDER BY effective_at LIMIT 1"
        ).fetchone()[0]
        return entry_anchor(connection, entry_id)

    def _append_inbound(self, connection: sqlite3.Connection, classification: str) -> None:
        append_decision(
            self.journal,
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": self._first_anchor(connection)},
            payload={"classification": classification, "reason": "test"},
            policy_version="review-v1",
            snapshot=f"inbound {classification}",
        )

    # --- bootstrap ---------------------------------------------------------- #

    def test_first_import_bootstraps_the_association_and_identity_file(self) -> None:
        connection = self._connect()
        association = read_association(connection)
        self.assertIsNotNone(association)
        assert association is not None
        uuid.UUID(association.ledger_id)
        uuid.UUID(association.journal_id)
        self.assertEqual(association.journal_relative_path, JOURNAL_FILENAME)
        self.assertEqual(association.verified_seq, 0)

        identity = json.loads(identity_file_path(self.database).read_text())
        self.assertEqual(identity["journal_id"], association.journal_id)
        self.assertEqual(identity["ledger_id"], association.ledger_id)

    # --- same-directory reopen + new decision ----------------------------- #

    def test_reopen_with_a_new_decision_advances_the_checkpoint(self) -> None:
        connection = self._connect()
        self._append_inbound(connection, "income")
        replay_journal(connection)
        association = read_association(connection)
        assert association is not None
        self.assertEqual(association.verified_seq, 1)
        self.assertNotEqual(association.verified_hash, "0" * 64)
        # A second replay against the unchanged journal is accepted (equal
        # checkpoint) and idempotent.
        bound, dangling = replay_journal(connection)
        self.assertEqual((bound, dangling), (1, 0))

    # --- cross-directory rebuild ---------------------------------------- #

    def test_cross_directory_rebuild_is_refused(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        with self.assertRaisesRegex(FinCLError, "same-directory rebuild"):
            rebuild_database(self.database, self.evidence, other / "rebuilt.sqlite3")

    def test_same_directory_rebuild_keeps_the_identity(self) -> None:
        connection = self._connect()
        source_association = read_association(connection)
        assert source_association is not None
        output = self.root / "rebuilt.sqlite3"
        rebuild_database(self.database, self.evidence, output)
        rebuilt = sqlite3.connect(output)
        self.addCleanup(rebuilt.close)
        row = rebuilt.execute(
            "SELECT ledger_id, journal_id FROM ledger_association WHERE id = 1"
        ).fetchone()
        self.assertEqual(row[0], source_association.ledger_id)
        self.assertEqual(row[1], source_association.journal_id)

    # --- damaged journals ------------------------------------------------- #

    def test_missing_journal_with_recorded_decisions_is_refused(self) -> None:
        connection = self._connect()
        self._append_inbound(connection, "income")
        replay_journal(connection)
        self.journal.unlink()
        with self.assertRaisesRegex(FinCLError, "tail was removed|restore"):
            replay_journal(connection)

    def test_truncated_journal_is_refused(self) -> None:
        connection = self._connect()
        self._append_inbound(connection, "income")
        self._append_inbound(connection, "gift")
        replay_journal(connection)
        lines = self.journal.read_bytes().splitlines(keepends=True)
        self.journal.write_bytes(lines[0])
        with self.assertRaisesRegex(FinCLError, "tail was removed|checkpoint"):
            replay_journal(connection)

    def test_swapped_journal_from_another_ledger_is_refused(self) -> None:
        connection = self._connect()
        self._append_inbound(connection, "income")
        replay_journal(connection)

        other_dir = self.root / "ledger_b"
        other_dir.mkdir()
        other_db = other_dir / "fincl.sqlite3"
        other_source = other_dir / "revolut.csv"
        other_source.write_bytes(_csv_bytes())
        FinCLService.plaintext_phase1(other_db, other_dir / "evidence").import_revolut(other_source)
        other_conn = connect_sqlite(other_db)
        self.addCleanup(other_conn.close)
        initialize_schema(other_conn)
        other_entry = other_conn.execute(
            "SELECT id FROM ledger_entries ORDER BY effective_at LIMIT 1"
        ).fetchone()[0]
        append_decision(
            default_journal_path(other_db),
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": entry_anchor(other_conn, other_entry)},
            payload={"classification": "gift", "reason": "other"},
            policy_version="review-v1",
            snapshot="other ledger decision",
        )

        self.journal.write_bytes(default_journal_path(other_db).read_bytes())
        with self.assertRaisesRegex(FinCLError, "diverges from its checkpoint"):
            replay_journal(connection)

    def test_extended_journal_is_accepted_as_catch_up(self) -> None:
        connection = self._connect()
        self._append_inbound(connection, "income")
        replay_journal(connection)
        # Two more records appended out of band, past the checkpoint.
        self._append_inbound(connection, "gift")
        append_decision(
            self.journal,
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": "sha-not-real:csv:9"},
            payload={"classification": "income", "reason": "dangler"},
            policy_version="review-v1",
            snapshot="dangling",
        )
        replay_journal(connection)
        association = read_association(connection)
        assert association is not None
        self.assertEqual(association.verified_seq, 3)

    # --- legacy adoption ------------------------------------------------- #

    def _make_legacy(self, connection: sqlite3.Connection) -> None:
        """Strip the association and identity file to mimic a pre-F24 ledger."""
        connection.execute("DELETE FROM ledger_association")
        connection.commit()
        identity_file_path(self.database).unlink(missing_ok=True)

    def test_legacy_ledger_is_refused_until_adopted(self) -> None:
        connection = self._connect()
        self._append_inbound(connection, "income")
        replay_journal(connection)
        self._make_legacy(connection)
        with self.assertRaisesRegex(FinCLError, "no journal association"):
            replay_journal(connection)

        association = adopt_ledger(connection, self.database)
        self.assertEqual(association.verified_seq, 1)
        self.assertTrue(identity_file_path(self.database).exists())
        bound, dangling = replay_journal(connection)
        self.assertEqual((bound, dangling), (1, 0))

    def test_adoption_refuses_a_journal_shorter_than_the_watermark(self) -> None:
        connection = self._connect()
        self._append_inbound(connection, "income")
        self._append_inbound(connection, "gift")
        replay_journal(connection)  # watermark -> 2
        self._make_legacy(connection)
        lines = self.journal.read_bytes().splitlines(keepends=True)
        self.journal.write_bytes(lines[0])  # journal -> 1 record
        with self.assertRaisesRegex(FinCLError, "tail is missing"):
            adopt_ledger(connection, self.database)

    def test_adoption_of_an_empty_legacy_ledger_initializes_identity(self) -> None:
        connection = self._connect()
        # No decisions recorded yet; drop association + journal + identity.
        connection.execute("DELETE FROM ledger_association")
        connection.commit()
        identity_file_path(self.database).unlink(missing_ok=True)
        self.journal.unlink(missing_ok=True)
        association = adopt_ledger(connection, self.database)
        self.assertEqual(association.verified_seq, 0)
        self.assertTrue(self.journal.exists())
        self.assertTrue(identity_file_path(self.database).exists())

    # --- interrupted initialization ------------------------------------- #

    def test_interrupted_initialization_is_refused_not_treated_as_empty(self) -> None:
        connection = self._connect()
        # Association row written but identity file lost (or vice versa): here we
        # simulate the identity file surviving without the row.
        connection.execute("DELETE FROM ledger_association")
        connection.commit()
        self.journal.unlink(missing_ok=True)
        connection.execute("DELETE FROM inbound_classifications")
        connection.execute("UPDATE decision_watermark SET applied_seq = 0 WHERE id = 1")
        connection.commit()
        # identity_file_path still exists from the original bootstrap.
        self.assertTrue(identity_file_path(self.database).exists())
        with self.assertRaisesRegex(FinCLError, "did not finish"):
            require_valid_association(connection, self.database)


if __name__ == "__main__":
    unittest.main()
