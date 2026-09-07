"""v0.6 slice 1 — dated budget revisions and month-boundary archive state."""

from __future__ import annotations

import csv
import io
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


class BudgetProjectionTests(unittest.TestCase):
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
        self.plan = str(uuid.uuid4())

    def _append(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> None:
        append_decision(
            default_journal_path(self.database), decision_kind=kind, anchors=anchors,
            payload=payload, policy_version="planning-v1", snapshot=f"{kind} {anchors}",
        )

    def _revision(self, month: str, limit: str, *, category_key: str | None = None, name: str = "Dining") -> str:
        revision = str(uuid.uuid4())
        self._append(
            "BUDGET_REVISION_SET",
            {"plan": self.plan, "revision": revision},
            {"effective_month": month, "name": name, "currency": "SGD",
             "category_key": category_key, "limit": limit, "reason": "set limit"},
        )
        return revision

    def test_dated_revisions_and_archive_project_and_replay_from_scratch(self) -> None:
        self._revision("2026-01", "300.00")
        self._revision("2026-03", "350.00", category_key="Food & Dining / Groceries")
        self._append(
            "BUDGET_ARCHIVE_SET", {"plan": self.plan},
            {"effective_month": "2026-06", "archived": True, "reason": "pausing"},
        )
        replay_journal(self.connection)

        rows = self.connection.execute(
            "SELECT effective_month, name, currency, category_key, limit_coefficient, "
            "limit_scale, bound FROM budget_revisions WHERE plan = ? ORDER BY effective_month",
            (self.plan,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["effective_month"], rows[0]["limit_coefficient"], rows[0]["limit_scale"]), ("2026-01", 30000, 2))
        self.assertIsNone(rows[0]["category_key"])
        self.assertEqual(rows[1]["category_key"], "Food & Dining / Groceries")
        self.assertEqual([r["bound"] for r in rows], [1, 1])

        archive = self.connection.execute(
            "SELECT effective_month, archived FROM budget_archive_periods WHERE plan = ?",
            (self.plan,),
        ).fetchone()
        self.assertEqual((archive["effective_month"], archive["archived"]), ("2026-06", 1))

        # Cold rebuild: wipe the projections, replay again, identical rows.
        self.connection.execute("DELETE FROM budget_revisions")
        self.connection.execute("DELETE FROM budget_archive_periods")
        self.connection.commit()
        replay_journal(self.connection)
        again = self.connection.execute(
            "SELECT count(*) FROM budget_revisions WHERE plan = ?", (self.plan,)
        ).fetchone()[0]
        self.assertEqual(again, 2)

    def test_a_competing_same_month_revision_is_refused_on_replay(self) -> None:
        self._revision("2026-01", "300.00")
        self._revision("2026-01", "400.00")  # different revision id, same month
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_a_currency_change_needs_a_new_budget(self) -> None:
        self._revision("2026-01", "300.00")
        revision = str(uuid.uuid4())
        self._append(
            "BUDGET_REVISION_SET",
            {"plan": self.plan, "revision": revision},
            {"effective_month": "2026-02", "name": "Dining", "currency": "USD",
             "category_key": None, "limit": "300.00", "reason": "switch"},
        )
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_replay_refuses_a_malformed_budget_payload(self) -> None:
        self._append(
            "BUDGET_REVISION_SET",
            {"plan": self.plan, "revision": str(uuid.uuid4())},
            {"effective_month": "2026-13", "name": "Bad", "currency": "SGD",
             "category_key": None, "limit": "10.00", "reason": "x"},
        )
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_a_zero_limit_is_rejected(self) -> None:
        self._append(
            "BUDGET_REVISION_SET",
            {"plan": self.plan, "revision": str(uuid.uuid4())},
            {"effective_month": "2026-01", "name": "N", "currency": "SGD",
             "category_key": None, "limit": "0.00", "reason": "x"},
        )
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)
