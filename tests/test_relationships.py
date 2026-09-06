"""v0.2 analysis slice 3: manual duplicate review and the atomic batch engine."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fincl.application import record_batch, record_decision
from fincl.db import connect_readonly
from fincl.decision_journal import default_journal_path, read_records
from fincl.errors import FinCLError
from fincl.refresh import refresh_ledger
from fincl.report import economic_flow_report_data
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01", "Alpha", "-10.00", "0.00", "SGD", "COMPLETED", "890.00"],
    ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01", "Alpha", "-10.00", "0.00", "SGD", "COMPLETED", "880.00"],
    ["Card Payment", "Current", "2026-01-20 10:00:00", "2026-01-20 10:00:01", "Beta", "-40.00", "0.00", "SGD", "COMPLETED", "840.00"],
]


def _ledger() -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(_ROWS))
    FinCLService.plaintext_phase1(database, root / "evidence").import_revolut(source)
    refresh_ledger(database)
    return database


def _anchors(database: Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT natural_anchor FROM ledger_entries "
                "WHERE economic_direction = 'OUT' ORDER BY effective_at"
            )
        ]
    finally:
        connection.close()


def _suppressions(database: Path) -> dict[str, str]:
    connection = connect_readonly(database)
    try:
        return {
            row["suppressed_anchor"]: row["primary_anchor"]
            for row in connection.execute(
                "SELECT suppressed_anchor, primary_anchor FROM duplicate_suppressions"
            )
        }
    finally:
        connection.close()


class DuplicateSuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger()
        self.alpha1, self.alpha2, self.beta = _anchors(self.database)

    def test_confirming_a_duplicate_suppresses_it_from_every_total(self) -> None:
        before = economic_flow_report_data(self.database)["currencies"][0]["spending"]
        self.assertEqual(before, "60.00")

        record_decision(
            self.database, request_id="d1", intent_hash="i1",
            decision_kind="DUPLICATE_CONFIRM",
            anchors={"primary": self.alpha1, "candidate": self.alpha2},
            payload={"reason": "same Alpha purchase exported twice"},
            policy_version="dedupe-v1", snapshot="alpha dup",
        )
        self.assertEqual(_suppressions(self.database), {self.alpha2: self.alpha1})
        after = economic_flow_report_data(self.database)["currencies"][0]["spending"]
        self.assertEqual(after, "50.00")

        connection = connect_readonly(self.database)
        try:
            anchors = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT origin_anchor FROM analysis_components"
                )
            }
        finally:
            connection.close()
        self.assertNotIn(self.alpha2, anchors)
        self.assertIn(self.alpha1, anchors)

    def test_retiring_the_decision_restores_the_entry(self) -> None:
        record_decision(
            self.database, request_id="d1", intent_hash="i1",
            decision_kind="DUPLICATE_CONFIRM",
            anchors={"primary": self.alpha1, "candidate": self.alpha2},
            payload={"reason": "dup"}, policy_version="dedupe-v1", snapshot="s",
        )
        record_decision(
            self.database, request_id="d2", intent_hash="i2",
            decision_kind="DUPLICATE_RETIRE",
            anchors={"primary": self.alpha1, "candidate": self.alpha2},
            payload={"reason": "actually two real coffees"},
            policy_version="dedupe-v1", snapshot="s",
        )
        self.assertEqual(_suppressions(self.database), {})
        self.assertEqual(
            economic_flow_report_data(self.database)["currencies"][0]["spending"], "60.00"
        )

    def test_a_suppressed_entry_cannot_become_a_primary(self) -> None:
        # alpha2 suppressed by alpha1; a later confirm naming alpha2 as primary of
        # beta must not suppress beta.
        record_decision(
            self.database, request_id="d1", intent_hash="i1",
            decision_kind="DUPLICATE_CONFIRM",
            anchors={"primary": self.alpha1, "candidate": self.alpha2},
            payload={"reason": "dup"}, policy_version="dedupe-v1", snapshot="s",
        )
        record_decision(
            self.database, request_id="d2", intent_hash="i2",
            decision_kind="DUPLICATE_CONFIRM",
            anchors={"primary": self.alpha2, "candidate": self.beta},
            payload={"reason": "conflicting"}, policy_version="dedupe-v1", snapshot="s",
        )
        self.assertEqual(_suppressions(self.database), {self.alpha2: self.alpha1})

    def test_the_projection_is_stable_across_a_no_op_refresh(self) -> None:
        record_decision(
            self.database, request_id="d1", intent_hash="i1",
            decision_kind="DUPLICATE_CONFIRM",
            anchors={"primary": self.alpha1, "candidate": self.alpha2},
            payload={"reason": "dup"}, policy_version="dedupe-v1", snapshot="s",
        )
        first = refresh_ledger(self.database).generation_token
        second = refresh_ledger(self.database).generation_token
        self.assertEqual(first, second)
        self.assertEqual(_suppressions(self.database), {self.alpha2: self.alpha1})


class DecisionBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger()
        self.alpha1, self.alpha2, self.beta = _anchors(self.database)

    def _journal_len(self) -> int:
        return len(read_records(default_journal_path(self.database)))

    def test_a_batch_retires_and_replaces_an_override_in_one_record(self) -> None:
        record_decision(
            self.database, request_id="o1", intent_hash="oi1",
            decision_kind="CATEGORY_OVERRIDE",
            anchors={"entry": self.beta},
            payload={"category": "Food & Dining / Groceries", "reason": "first guess"},
            policy_version="dashboard-v1", snapshot="s",
        )
        before = self._journal_len()
        receipt = record_batch(
            self.database, request_id="b1", intent_hash="bi1",
            items=[
                {"kind": "CATEGORY_OVERRIDE_RETIRE", "anchors": {"entry": self.beta},
                 "payload": {"reason": "wrong"}},
                {"kind": "CATEGORY_OVERRIDE", "anchors": {"entry": self.beta},
                 "payload": {"category": "Shopping / General Shopping", "reason": "correct"}},
            ],
            reason="fix Beta's category",
        )
        self.assertEqual(self._journal_len(), before + 1)  # exactly one outer record
        self.assertEqual(receipt.decision_kind, "DECISION_BATCH")

        connection = connect_readonly(self.database)
        try:
            row = connection.execute(
                "SELECT group_name, category FROM category_overrides co "
                "JOIN ledger_entries e ON e.natural_anchor = co.entry_anchor "
                "WHERE e.natural_anchor = ?", (self.beta,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((row["group_name"], row["category"]), ("Shopping", "General Shopping"))

    def test_an_invalid_child_aborts_the_whole_batch_before_any_append(self) -> None:
        before = self._journal_len()
        with self.assertRaises(FinCLError):
            record_batch(
                self.database, request_id="b1", intent_hash="bi1",
                items=[
                    {"kind": "CATEGORY_OVERRIDE", "anchors": {"entry": self.alpha1},
                     "payload": {"category": "Shopping / General Shopping", "reason": "ok"}},
                    {"kind": "CATEGORY_OVERRIDE", "anchors": {"entry": self.alpha2},
                     "payload": {"category": "Not / A / Real / Category", "reason": "bad"}},
                ],
                reason="half-valid",
            )
        self.assertEqual(self._journal_len(), before)  # nothing appended

    def test_a_nested_batch_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            record_batch(
                self.database, request_id="b1", intent_hash="bi1",
                items=[{"kind": "DECISION_BATCH", "anchors": {"batch": "x"},
                        "payload": {"items": [], "reason": "no"}}],
                reason="nope",
            )

    def test_a_batch_can_couple_a_duplicate_confirm_with_a_reclassification(self) -> None:
        receipt = record_batch(
            self.database, request_id="b1", intent_hash="bi1",
            items=[
                {"kind": "DUPLICATE_CONFIRM",
                 "anchors": {"primary": self.alpha1, "candidate": self.alpha2},
                 "payload": {"reason": "same purchase"}},
                {"kind": "CATEGORY_OVERRIDE", "anchors": {"entry": self.alpha1},
                 "payload": {"category": "Food & Dining / Cafes & Drinks", "reason": "it is coffee"}},
            ],
            reason="dedupe and label",
        )
        self.assertFalse(receipt.refresh_pending)
        self.assertEqual(_suppressions(self.database), {self.alpha2: self.alpha1})
        self.assertEqual(
            economic_flow_report_data(self.database)["currencies"][0]["spending"], "50.00"
        )


class OverrideRetireTests(unittest.TestCase):
    def test_retiring_an_override_returns_the_entry_to_the_rule_engine(self) -> None:
        database = _ledger()
        _a1, _a2, beta = _anchors(database)
        record_decision(
            database, request_id="o1", intent_hash="oi1",
            decision_kind="CATEGORY_OVERRIDE", anchors={"entry": beta},
            payload={"category": "Food & Dining / Groceries", "reason": "manual"},
            policy_version="dashboard-v1", snapshot="s",
        )
        record_decision(
            database, request_id="r1", intent_hash="ri1",
            decision_kind="CATEGORY_OVERRIDE_RETIRE", anchors={"entry": beta},
            payload={"reason": "let rules decide"},
            policy_version="dashboard-v1", snapshot="s",
        )
        connection = connect_readonly(database)
        try:
            overrides = connection.execute(
                "SELECT count(*) FROM category_overrides"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(overrides, 0)
        # Total is unchanged — a category retire never moves cash-flow buckets.
        self.assertEqual(
            economic_flow_report_data(database)["currencies"][0]["spending"], "60.00"
        )


class CliDuplicatesTests(unittest.TestCase):
    def test_cli_confirm_then_retire_round_trips(self) -> None:
        from typer.testing import CliRunner

        from fincl.cli import app

        database = _ledger()
        a1, a2, _beta = _anchors(database)
        runner = CliRunner()
        confirm = runner.invoke(app, [
            "duplicates", "confirm", "--database", str(database),
            "--primary", a1, "--candidate", a2, "--reason", "same coffee twice",
        ])
        self.assertEqual(confirm.exit_code, 0, confirm.output)
        self.assertEqual(_suppressions(database), {a2: a1})

        retire = runner.invoke(app, [
            "duplicates", "retire", "--database", str(database),
            "--primary", a1, "--candidate", a2, "--reason", "no, two coffees",
        ])
        self.assertEqual(retire.exit_code, 0, retire.output)
        self.assertEqual(_suppressions(database), {})


if __name__ == "__main__":
    unittest.main()
