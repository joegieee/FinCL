"""Adversarial coverage for the durable decision journal and its replay.

The journal is the single originating store in the system (§14.2): everything
else is rebuilt from evidence, but a lost or silently corrupted journal cannot
be reconstructed. These tests exercise tampering, truncation, version drift,
projection self-heal, rebind across a rebuild, and the review CLI error paths.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from typer.testing import CliRunner

from fincl import decisions
from fincl.cli import app
from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import (
    JOURNAL_VERSION,
    append_decision,
    default_journal_path,
    read_records,
)
from fincl.decisions import entry_anchor, replay_journal, resolve_anchor
from fincl.errors import FinCLError
from fincl.rebuild import rebuild_database
from fincl.revolut import EXPECTED_HEADER
from fincl.service import FinCLService

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "100.00", "0.00", "SGD", "COMPLETED", "100.00"],
    ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01", "Shop", "-10.00", "0.00", "SGD", "COMPLETED", "90.00"],
]

_EXCHANGE_INCOMPLETE_ROWS = [
    ["Exchange", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "SGD leg only", "-100.00", "0.00", "SGD", "COMPLETED", "900.00"],
]


def _csv_bytes(rows: list[list[str]] | None = None) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPECTED_HEADER)
    writer.writerows(rows or _ROWS)
    return buffer.getvalue().encode("utf-8")


class JournalIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.journal = Path(self.temporary.name) / "decision-journal.jsonl"

    def _append(self, kind: str, anchors: dict[str, str], snapshot: str) -> None:
        append_decision(
            self.journal,
            decision_kind=kind,
            anchors=anchors,
            payload={"reason": "t"},
            policy_version="test-v1",
            snapshot=snapshot,
        )

    def test_append_chains_and_reads_back(self) -> None:
        self._append("INBOUND_CLASSIFICATION", {"entry": "a"}, "one")
        self._append("INBOUND_CLASSIFICATION", {"entry": "b"}, "two")
        self._append("INBOUND_CLASSIFICATION", {"entry": "c"}, "three")

        records = read_records(self.journal)

        self.assertEqual([r.seq for r in records], [1, 2, 3])
        self.assertEqual(records[0].prev_hash, "0" * 64)
        self.assertEqual(records[1].prev_hash, records[0].record_hash)
        self.assertEqual(records[2].prev_hash, records[1].record_hash)

    def test_missing_kind_or_anchors_or_snapshot_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            append_decision(
                self.journal, decision_kind="", anchors={"entry": "a"},
                payload={}, policy_version="v", snapshot="s",
            )
        with self.assertRaises(FinCLError):
            append_decision(
                self.journal, decision_kind="X", anchors={},
                payload={}, policy_version="v", snapshot="s",
            )
        with self.assertRaises(FinCLError):
            append_decision(
                self.journal, decision_kind="X", anchors={"entry": "a"},
                payload={}, policy_version="v", snapshot="",
            )
        self.assertEqual(read_records(self.journal), [])

    def test_flipped_payload_byte_fails_integrity(self) -> None:
        self._append("INBOUND_CLASSIFICATION", {"entry": "a"}, "original")
        self._append("INBOUND_CLASSIFICATION", {"entry": "b"}, "second")
        lines = self.journal.read_bytes().splitlines()
        lines[0] = lines[0].replace(b'"original"', b'"tampered!"')
        self.journal.write_bytes(b"\n".join(lines) + b"\n")

        with self.assertRaises(FinCLError) as caught:
            read_records(self.journal)
        self.assertIn("integrity", str(caught.exception))

    def test_truncated_final_record_is_rejected(self) -> None:
        self._append("INBOUND_CLASSIFICATION", {"entry": "a"}, "complete")
        raw = self.journal.read_bytes()
        self.journal.write_bytes(raw[:-5])

        with self.assertRaises(FinCLError) as caught:
            read_records(self.journal)
        self.assertIn("incomplete record", str(caught.exception))

    def test_reordered_records_break_the_chain(self) -> None:
        self._append("INBOUND_CLASSIFICATION", {"entry": "a"}, "first")
        self._append("INBOUND_CLASSIFICATION", {"entry": "b"}, "second")
        first, second = self.journal.read_bytes().splitlines()
        self.journal.write_bytes(second + b"\n" + first + b"\n")

        with self.assertRaises(FinCLError):
            read_records(self.journal)

    def test_unsupported_journal_version_is_rejected(self) -> None:
        self._append("INBOUND_CLASSIFICATION", {"entry": "a"}, "ok")
        line = self.journal.read_bytes()
        self.journal.write_bytes(
            line.replace(
                f'"journal_version":{JOURNAL_VERSION}'.encode(),
                b'"journal_version":999',
            )
        )
        with self.assertRaises(FinCLError) as caught:
            read_records(self.journal)
        self.assertIn("version", str(caught.exception))


class JournalReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        FinCLService.plaintext_phase1(self.database, self.evidence).import_revolut(self.source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def _first_entry_anchor(self) -> str:
        entry_id = self.connection.execute(
            "SELECT id FROM ledger_entries ORDER BY effective_at LIMIT 1"
        ).fetchone()[0]
        return entry_anchor(self.connection, entry_id)

    def test_replay_is_idempotent(self) -> None:
        anchor = self._first_entry_anchor()
        append_decision(
            default_journal_path(self.database),
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": anchor},
            payload={"classification": "income", "reason": "salary"},
            policy_version="review-v1",
            snapshot="inbound income",
        )

        first = replay_journal(self.connection)
        rows_first = self.connection.execute(
            "SELECT * FROM inbound_classifications ORDER BY journal_seq"
        ).fetchall()
        second = replay_journal(self.connection)
        rows_second = self.connection.execute(
            "SELECT * FROM inbound_classifications ORDER BY journal_seq"
        ).fetchall()

        self.assertEqual(first, second)
        self.assertEqual([tuple(r) for r in rows_first], [tuple(r) for r in rows_second])
        self.assertEqual(first, (1, 0))

    def test_projection_self_heals_after_being_dropped(self) -> None:
        anchor = self._first_entry_anchor()
        append_decision(
            default_journal_path(self.database),
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": anchor},
            payload={"classification": "gift", "reason": "birthday"},
            policy_version="review-v1",
            snapshot="inbound gift",
        )
        replay_journal(self.connection)
        self.connection.execute("DELETE FROM inbound_classifications")
        self.connection.commit()

        bound, dangling = replay_journal(self.connection)

        restored = self.connection.execute(
            "SELECT classification, bound FROM inbound_classifications"
        ).fetchone()
        self.assertEqual((bound, dangling), (1, 0))
        self.assertEqual(tuple(restored), ("gift", 1))

    def test_dangling_decision_is_projected_but_not_bound(self) -> None:
        append_decision(
            default_journal_path(self.database),
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": "sha-that-does-not-exist:csv:99"},
            payload={"classification": "income", "reason": "x"},
            policy_version="review-v1",
            snapshot="dangling",
        )

        bound, dangling = replay_journal(self.connection)

        row = self.connection.execute(
            "SELECT bound, dangling_reason FROM inbound_classifications"
        ).fetchone()
        self.assertEqual((bound, dangling), (0, 1))
        self.assertEqual(row["bound"], 0)
        self.assertIsNotNone(row["dangling_reason"])

    def test_decision_rebinds_after_a_full_rebuild(self) -> None:
        anchor = self._first_entry_anchor()
        append_decision(
            default_journal_path(self.database),
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": anchor},
            payload={"classification": "income", "reason": "salary"},
            policy_version="review-v1",
            snapshot="inbound income",
        )
        replay_journal(self.connection)
        self.assertIsNotNone(resolve_anchor(self.connection, anchor))

        output = self.root / "rebuilt.sqlite3"
        rebuild_database(self.database, self.evidence, output)

        rebuilt = connect_sqlite(output)
        self.addCleanup(rebuilt.close)
        initialize_schema(rebuilt)
        replay_journal(rebuilt, journal_path=default_journal_path(self.database))
        row = rebuilt.execute(
            "SELECT bound FROM inbound_classifications WHERE anchor = ?", (anchor,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["bound"], 1)


class JournalKindClosureTests(unittest.TestCase):
    """Confirmed 32 — replay refuses an unknown kind or anchor_version, and a
    known-older anchor_version is projected as dangling rather than refused."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        FinCLService.plaintext_phase1(self.database, self.evidence).import_revolut(self.source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.journal = default_journal_path(self.database)

    def _first_anchor(self) -> str:
        entry_id = self.connection.execute(
            "SELECT id FROM ledger_entries ORDER BY effective_at LIMIT 1"
        ).fetchone()[0]
        return entry_anchor(self.connection, entry_id)

    def test_unknown_decision_kind_refuses_replay_and_names_the_seq(self) -> None:
        append_decision(
            self.journal,
            decision_kind="DUPLICATE_CONFIRM",  # a future kind this build has not learned
            anchors={"primary": "a", "candidate": "b"},
            payload={"reason": "x"},
            policy_version="dedupe-v1",
            snapshot="a kind this build does not know",
        )
        with self.assertRaises(FinCLError) as caught:
            replay_journal(self.connection)
        self.assertIn("DUPLICATE_CONFIRM", str(caught.exception))
        self.assertIn("record 1", str(caught.exception))

    def test_unknown_anchor_version_refuses_replay(self) -> None:
        append_decision(
            self.journal,
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": self._first_anchor()},
            payload={"classification": "income", "reason": "x"},
            policy_version="review-v1",
            snapshot="future anchor recipe",
            anchor_version="natural-anchor-v99",
        )
        with self.assertRaises(FinCLError) as caught:
            replay_journal(self.connection)
        self.assertIn("natural-anchor-v99", str(caught.exception))

    def test_known_older_anchor_version_projects_as_dangling(self) -> None:
        append_decision(
            self.journal,
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": self._first_anchor()},
            payload={"classification": "income", "reason": "x"},
            policy_version="review-v1",
            snapshot="written under a since-superseded recipe",
            anchor_version="natural-anchor-v0",
        )
        with unittest.mock.patch.object(
            decisions, "SUPERSEDED_ANCHOR_VERSIONS", frozenset({"natural-anchor-v0"})
        ):
            bound, dangling = replay_journal(self.connection)
        self.assertEqual((bound, dangling), (0, 1))
        row = self.connection.execute(
            "SELECT bound, dangling_reason FROM inbound_classifications"
        ).fetchone()
        self.assertEqual(row["bound"], 0)
        self.assertIn("superseded", row["dangling_reason"])


class ReviewCliErrorPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        FinCLService.plaintext_phase1(
            self.database, self.root / "evidence"
        ).import_revolut(self.source)
        self.runner = CliRunner()

    def test_confirm_link_rejects_unresolvable_anchors(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "review", "confirm-link", "--left", "nope:csv:1", "--right", "nope:csv:2",
                "--reason", "test", "--database", str(self.database),
            ],
        )
        self.assertNotEqual(result.exit_code, 0)

    def test_classify_inbound_rejects_unknown_classification(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "review", "classify-inbound", "--anchor", "x:csv:1", "--as", "banana",
                "--reason", "test", "--database", str(self.database),
            ],
        )
        self.assertNotEqual(result.exit_code, 0)

    def test_escape_invariant_requires_a_reason(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "review", "escape-invariant", "--code", "EXCHANGE_PAIRING_INCOMPLETE",
                "--anchors", "x:csv:1", "--reason", "   ", "--database", str(self.database),
            ],
        )
        self.assertNotEqual(result.exit_code, 0)

    def _entry_anchors(self) -> list[str]:
        connection = sqlite3.connect(self.database)
        try:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT natural_anchor FROM ledger_entries ORDER BY effective_at"
                )
            ]
        finally:
            connection.close()

    def test_confirm_link_without_a_proposal_requires_link_kind(self) -> None:
        left, right = self._entry_anchors()
        result = self.runner.invoke(
            app,
            [
                "review", "confirm-link", "--left", left, "--right", right,
                "--reason", "manual pairing", "--database", str(self.database),
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(read_records(default_journal_path(self.database)), [])

    def test_confirm_link_manual_records_the_given_link_kind(self) -> None:
        left, right = self._entry_anchors()
        result = self.runner.invoke(
            app,
            [
                "review", "confirm-link", "--left", left, "--right", right,
                "--reason", "manual exchange", "--link-kind", "CURRENCY_EXCHANGE",
                "--database", str(self.database),
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        records = read_records(default_journal_path(self.database))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].decision_kind, "LINK_CONFIRM")
        self.assertEqual(records[0].payload["link_kind"], "CURRENCY_EXCHANGE")

    def test_confirm_link_rejects_an_unknown_link_kind(self) -> None:
        left, right = self._entry_anchors()
        result = self.runner.invoke(
            app,
            [
                "review", "confirm-link", "--left", left, "--right", right,
                "--reason", "x", "--link-kind", "TELEPORT", "--database", str(self.database),
            ],
        )
        self.assertNotEqual(result.exit_code, 0)


class InvariantEscapeLivenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "fincl.sqlite3"
        self.runner = CliRunner()

    def _import(self, rows: list[list[str]] | None = None) -> str:
        source = self.root / "revolut.csv"
        source.write_bytes(_csv_bytes(rows))
        return FinCLService.plaintext_phase1(
            self.database, self.root / "evidence"
        ).import_revolut(source).status

    def _anchor_of(self, event_kind: str) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT natural_anchor FROM ledger_entries WHERE event_kind = ? LIMIT 1",
                (event_kind,),
            ).fetchone()[0]
        finally:
            connection.close()

    def test_escape_refused_when_no_live_violation_exists(self) -> None:
        self.assertEqual(self._import(), "ACCEPTED")
        anchor = self._anchor_of("TOPUP")
        result = self.runner.invoke(
            app,
            [
                "review", "escape-invariant", "--code", "EXCHANGE_PAIRING_INCOMPLETE",
                "--anchors", anchor, "--reason", "there is no violation here",
                "--database", str(self.database),
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(read_records(default_journal_path(self.database)), [])

    def test_escape_accepted_for_a_live_violation(self) -> None:
        self.assertEqual(self._import(_EXCHANGE_INCOMPLETE_ROWS), "QUARANTINED")
        anchor = self._anchor_of("EXCHANGE")
        result = self.runner.invoke(
            app,
            [
                "review", "escape-invariant", "--code", "EXCHANGE_PAIRING_INCOMPLETE",
                "--anchors", anchor, "--reason", "single-leg export, second leg is in the next file",
                "--database", str(self.database),
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        records = read_records(default_journal_path(self.database))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].decision_kind, "INVARIANT_ESCAPE")
        self.assertEqual(records[0].payload["code"], "EXCHANGE_PAIRING_INCOMPLETE")

    def test_escape_refused_for_the_wrong_code_on_a_live_violation(self) -> None:
        self.assertEqual(self._import(_EXCHANGE_INCOMPLETE_ROWS), "QUARANTINED")
        anchor = self._anchor_of("EXCHANGE")
        result = self.runner.invoke(
            app,
            [
                "review", "escape-invariant", "--code", "BALANCE_CONTINUITY_MISMATCH",
                "--anchors", anchor, "--reason", "wrong code for this row",
                "--database", str(self.database),
            ],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(read_records(default_journal_path(self.database)), [])


class DanglingDecisionSurfacingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        FinCLService.plaintext_phase1(self.database, self.evidence).import_revolut(self.source)
        append_decision(
            default_journal_path(self.database),
            decision_kind="INBOUND_CLASSIFICATION",
            anchors={"entry": "missing-evidence:csv:404"},
            payload={"classification": "income", "reason": "x"},
            policy_version="review-v1",
            snapshot="dangling inbound",
        )
        self.runner = CliRunner()

    def test_report_json_surfaces_the_dangling_count(self) -> None:
        result = self.runner.invoke(
            app, ["report", "--database", str(self.database), "--json"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertGreaterEqual(json.loads(result.output)["dangling_decisions"], 1)

    def test_coverage_plain_surfaces_the_dangling_count(self) -> None:
        result = self.runner.invoke(
            app, ["coverage", "--database", str(self.database), "--plain"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("dangling_decisions=", result.output)

    def test_rebuild_exits_nonzero_when_a_decision_dangles(self) -> None:
        result = self.runner.invoke(
            app,
            [
                "rebuild", "--database", str(self.database),
                "--evidence-dir", str(self.evidence),
                "--output", str(self.root / "rebuilt.sqlite3"), "--plain",
            ],
        )
        self.assertEqual(result.exit_code, 3, result.output)


if __name__ == "__main__":
    unittest.main()
