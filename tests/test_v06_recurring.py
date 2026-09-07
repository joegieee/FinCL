"""v0.6 slice 3 — recurring commitments, dismissals and occurrence associations."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.errors import FinCLError
from fincl.revolut import EXPECTED_HEADER
from fincl.service import FinCLService

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
]


def _csv() -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPECTED_HEADER)
    writer.writerows(_ROWS)
    return buffer.getvalue().encode()


class RecurringProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv())
        self.database = self.root / "fincl.sqlite3"
        FinCLService.plaintext_phase1(self.database, self.root / "evidence").import_revolut(source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.commitment = str(uuid.uuid4())

    def _append(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> None:
        append_decision(
            default_journal_path(self.database), decision_kind=kind, anchors=anchors,
            payload=payload, policy_version="planning-v1", snapshot=f"{kind} {anchors}",
        )

    def _commitment(self, *, currency: str = "SGD", status: str = "active", ended_on: str | None = None) -> None:
        self._append(
            "RECURRING_COMMITMENT_SET", {"commitment": self.commitment},
            {
                "name": "Streaming", "currency": currency, "merchant_key": "streamco",
                "account_ids": ["acct-1"], "seed_entry_anchors": ["a1", "a2", "a3"],
                "schedule": {"kind": "monthly", "day": 15, "anchor_on": "2026-01-15"},
                "expected_amount": "14.99", "amount_tolerance": "1.00",
                "status": status, "ended_on": ended_on, "reason": "confirmed",
            },
        )

    def test_commitment_dismissal_and_occurrences_project_and_replay(self) -> None:
        self._commitment()
        self._append("RECURRING_CANDIDATE_DISMISS", {"candidate": "detv1:streamco:SGD:acct-1:monthly"}, {"reason": "not a subscription"})
        self._append(
            "RECURRING_OCCURRENCE_SET", {"commitment": self.commitment, "scheduled_on": "2026-02-15"},
            {"entry": "entry-anchor-x", "reason": "manual match"},
        )
        self._append(
            "RECURRING_OCCURRENCE_SET", {"commitment": self.commitment, "scheduled_on": "2026-03-15"},
            {"entry": None, "reason": "skipped this month"},
        )
        replay_journal(self.connection)

        row = self.connection.execute(
            "SELECT name, currency, expected_coefficient, expected_scale, tolerance_coefficient, "
            "status, schedule_json, account_ids_json FROM recurring_commitments WHERE commitment = ?",
            (self.commitment,),
        ).fetchone()
        self.assertEqual((row["currency"], row["expected_coefficient"], row["expected_scale"], row["tolerance_coefficient"]),
                         ("SGD", 1499, 2, 100))
        self.assertEqual(json.loads(row["schedule_json"])["day"], 15)
        self.assertEqual(json.loads(row["account_ids_json"]), ["acct-1"])
        self.assertEqual(row["status"], "active")

        dismissal = self.connection.execute("SELECT reason FROM recurring_candidate_dismissals").fetchone()
        self.assertEqual(dismissal["reason"], "not a subscription")

        occ = self.connection.execute(
            "SELECT scheduled_on, entry_anchor FROM recurring_occurrences WHERE commitment = ? ORDER BY scheduled_on",
            (self.commitment,),
        ).fetchall()
        self.assertEqual([(o["scheduled_on"], o["entry_anchor"]) for o in occ],
                         [("2026-02-15", "entry-anchor-x"), ("2026-03-15", None)])

        # Retire one occurrence, cold replay.
        self._append("RECURRING_OCCURRENCE_RETIRE", {"commitment": self.commitment, "scheduled_on": "2026-03-15"}, {"reason": "back to detection"})
        for table in ("recurring_commitments", "recurring_candidate_dismissals", "recurring_occurrences"):
            self.connection.execute(f"DELETE FROM {table}")
        self.connection.commit()
        replay_journal(self.connection)
        remaining = self.connection.execute(
            "SELECT scheduled_on FROM recurring_occurrences WHERE commitment = ?", (self.commitment,)
        ).fetchall()
        self.assertEqual([r["scheduled_on"] for r in remaining], ["2026-02-15"])

    def test_currency_is_immutable_for_a_commitment(self) -> None:
        self._commitment(currency="SGD")
        self._commitment(currency="USD")
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_ended_status_requires_a_date(self) -> None:
        self._commitment(status="ended", ended_on=None)
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_occurrence_for_an_unknown_commitment_is_refused(self) -> None:
        self._append(
            "RECURRING_OCCURRENCE_SET", {"commitment": str(uuid.uuid4()), "scheduled_on": "2026-02-15"},
            {"entry": None, "reason": "orphan"},
        )
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)
