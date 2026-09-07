"""v0.6 slice 2 — savings goal details, absolute observations and archive state."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
import uuid
from datetime import date, timedelta
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


class GoalProjectionTests(unittest.TestCase):
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

    def _details(self, target: str = "5000.00", target_on: str | None = "2026-12-01", currency: str = "SGD") -> None:
        self._append(
            "GOAL_DETAILS_SET", {"plan": self.plan},
            {"name": "Emergency fund", "currency": currency, "target": target,
             "target_on": target_on, "reason": "set target"},
        )

    def _observation(self, observed_on: str, saved: str) -> str:
        observation = str(uuid.uuid4())
        self._append(
            "GOAL_OBSERVATION_SET", {"plan": self.plan, "observation": observation},
            {"observed_on": observed_on, "saved": saved, "reason": "progress"},
        )
        return observation

    def test_details_observations_and_archive_project_and_replay(self) -> None:
        self._details()
        first = self._observation("2026-02-01", "1000.00")
        self._observation("2026-03-01", "1200.00")  # absolute, not a delta
        self._append("GOAL_ARCHIVE_SET", {"plan": self.plan}, {"archived": True, "reason": "done"})
        replay_journal(self.connection)

        detail = self.connection.execute(
            "SELECT name, target_coefficient, target_scale, currency, target_on "
            "FROM goal_details WHERE plan = ?", (self.plan,)
        ).fetchone()
        self.assertEqual((detail["target_coefficient"], detail["target_scale"], detail["currency"]), (500000, 2, "SGD"))
        self.assertEqual(detail["target_on"], "2026-12-01")

        observations = self.connection.execute(
            "SELECT observed_on, saved_coefficient FROM goal_observations WHERE plan = ? "
            "ORDER BY observed_on", (self.plan,)
        ).fetchall()
        self.assertEqual([(o["observed_on"], o["saved_coefficient"]) for o in observations],
                         [("2026-02-01", 100000), ("2026-03-01", 120000)])

        archived = self.connection.execute(
            "SELECT archived FROM goal_archive WHERE plan = ?", (self.plan,)
        ).fetchone()["archived"]
        self.assertEqual(archived, 1)

        # Retire the first observation, replay from scratch.
        self._append("GOAL_OBSERVATION_RETIRE", {"plan": self.plan, "observation": first}, {"reason": "wrong"})
        self.connection.execute("DELETE FROM goal_observations")
        self.connection.execute("DELETE FROM goal_details")
        self.connection.execute("DELETE FROM goal_archive")
        self.connection.commit()
        replay_journal(self.connection)
        remaining = self.connection.execute(
            "SELECT observation FROM goal_observations WHERE plan = ?", (self.plan,)
        ).fetchall()
        self.assertEqual(len(remaining), 1)
        self.assertNotEqual(remaining[0]["observation"], first)

    def test_a_future_observed_date_is_rejected(self) -> None:
        self._details()
        future = (date.today() + timedelta(days=3)).isoformat()
        self._observation(future, "100.00")
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_currency_is_immutable(self) -> None:
        self._details(currency="SGD")
        self._details(currency="USD")
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_target_must_be_positive_saved_may_be_zero(self) -> None:
        self._details(target="0.00")
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_a_zero_first_observation_is_accepted(self) -> None:
        self._details()
        self._observation("2026-01-15", "0.00")
        replay_journal(self.connection)
        row = self.connection.execute(
            "SELECT saved_coefficient FROM goal_observations WHERE plan = ?", (self.plan,)
        ).fetchone()
        self.assertEqual(row["saved_coefficient"], 0)
