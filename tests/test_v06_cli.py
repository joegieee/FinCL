"""v0.6 CLI verbs for budgets, savings goals and recurring commitments."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.document_service import DocumentImportService
from tests.revolut_pdf import revolut_pdf_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01", "Grocer", "-40.00", "0.00", "SGD", "COMPLETED", "460.00"],
]


def _csv() -> bytes:
    return revolut_pdf_bytes(_ROWS)


class V06CliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "revolut.csv"
        source.write_bytes(_csv())
        self.database = root / "ledger.sqlite3"
        result = DocumentImportService.plaintext_phase2(self.database, root / "evidence").import_revolut_pdf(source)
        self.assertEqual(result.status, "ACCEPTED")
        self.runner = CliRunner()
        self.assertEqual(self._raw("refresh").exit_code, 0)

    def _raw(self, *args: str):  # noqa: ANN202 - test helper
        return self.runner.invoke(app, [*args, "--database", str(self.database)])

    def _cli(self, *args: str) -> str:
        outcome = self._raw(*args)
        self.assertEqual(outcome.exit_code, 0, outcome.output)
        return outcome.output

    def test_budget_revise_history_and_list(self) -> None:
        plan = str(uuid.uuid4())
        self._cli("budgets", "revise", plan, "--month", "2026-01", "--name", "All spending",
                  "--limit", "100.00", "--reason", "set limit")
        listing = json.loads(self._cli("budgets", "list", "--month", "2026-01", "--json"))
        self.assertEqual(len(listing["budgets"]), 1)
        self.assertEqual(listing["budgets"][0]["actual"], "40.00")
        history = json.loads(self._cli("budgets", "history", plan, "--json"))
        self.assertEqual(history["revisions"][0]["limit"], "100.00")

    def test_budget_currency_change_is_refused(self) -> None:
        plan = str(uuid.uuid4())
        self._cli("budgets", "revise", plan, "--month", "2026-01", "--name", "B",
                  "--limit", "50.00", "--reason", "sgd")
        bad = self._raw("budgets", "revise", plan, "--month", "2026-02", "--name", "B",
                        "--currency", "USD", "--limit", "50.00", "--reason", "switch")
        self.assertEqual(bad.exit_code, 1, bad.output)

    def test_goal_set_observe_and_list(self) -> None:
        plan = str(uuid.uuid4())
        self._cli("goals", "set", plan, "--name", "Trip", "--target", "1000.00",
                  "--target-on", "2026-12-01", "--reason", "set")
        self._cli("goals", "observe", plan, "--on", "2026-06-01", "--saved", "250.00", "--reason", "progress")
        goals = json.loads(self._cli("goals", "list", "--as-of", "2026-07-01", "--json"))["goals"]
        goal = next(g for g in goals if g["plan"] == plan)
        self.assertEqual(goal["saved"], "250.00")
        self.assertEqual(goal["remaining"], "750.00")

    def test_recurring_commit_occurrence_and_reads(self) -> None:
        commitment = str(uuid.uuid4())
        self._cli(
            "recurring", "commit", commitment, "--name", "Gym", "--merchant-key", "gymco",
            "--account", "acct-1", "--schedule", "monthly", "--day", "10",
            "--anchor-on", "2026-01-10", "--expected", "60.00", "--tolerance", "5.00",
            "--reason", "confirmed",
        )
        self._cli("recurring", "occurrence-set", commitment, "--on", "2026-02-10", "--reason", "skipped")
        page = json.loads(self._cli("recurring", "list", "--json"))
        self.assertEqual(len(page["confirmed"]), 1)
        occ = json.loads(self._cli("recurring", "occurrences", commitment, "--start", "2026-01-01", "--end", "2026-03-31", "--json"))
        states = {o["scheduled_on"]: o["state"] for o in occ["occurrences"]}
        self.assertEqual(states["2026-02-10"], "explicitly_skipped")
