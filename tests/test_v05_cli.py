"""CLI parity for the v0.5 setup / inventory / maintenance verbs."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.service import FinCLService
from fincl.v05 import accounts_page, categories_page
from tests.test_revolut_import import csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
    ["Card Payment", "Current", "2026-01-20 10:00:00", "2026-01-20 10:00:01", "Grocer", "-51.25", "0.00", "SGD", "COMPLETED", "448.75"],
]


class V05CliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "ledger.sqlite3"
        source = root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        result = FinCLService.plaintext_phase1(self.database, root / "evidence").import_revolut(source)
        self.assertEqual(result.status, "ACCEPTED")
        self.runner = CliRunner()
        refreshed = self.runner.invoke(app, ["refresh", "--database", str(self.database)])
        self.assertEqual(refreshed.exit_code, 0, refreshed.output)

    def _cli(self, *args: str) -> str:
        outcome = self.runner.invoke(app, [*args, "--database", str(self.database)])
        self.assertEqual(outcome.exit_code, 0, outcome.output)
        return outcome.output

    def test_setup_and_import_history_read_verbs(self) -> None:
        self.assertIn("ready", self._cli("setup", "--plain"))
        listing = json.loads(self._cli("imports", "list", "--json"))
        self.assertEqual(len(listing["imports"]), 1)
        detail = json.loads(self._cli("imports", "show", listing["imports"][0]["id"], "--json"))
        self.assertIn("issues", detail)

    def test_account_scope_and_details_round_trip(self) -> None:
        account = accounts_page(self.database)["accounts"][0]
        self._cli(
            "accounts", "scope", account["id"], "--pocket", "SGD",
            "--start", "2026-01-01", "--end", "2026-01-20", "--reason", "expected active",
        )
        self._cli("accounts", "details", account["id"], "--name", "Daily", "--reason", "label")
        changed = accounts_page(self.database)["accounts"][0]
        self.assertEqual(changed["display_name"], "Daily")
        self.assertEqual(changed["coverage_status"], "covered")

    def test_category_details_and_archive_round_trip(self) -> None:
        key = "Lifestyle / Board Games"
        self._cli("categories", "create", key, "--reason", "seed custom category")
        self._cli("categories", "details", key, "--group", "Lifestyle", "--name", "Tabletop", "--reason", "label")
        self._cli("categories", "archive", key, "--reason", "unused")
        item = next(row for row in categories_page(self.database)["categories"] if row["key"] == key)
        self.assertEqual(item["display_name"], "Tabletop")
        self.assertTrue(item["archived"])
        self._cli("categories", "archive", key, "--restore", "--reason", "back in use")
        item = next(row for row in categories_page(self.database)["categories"] if row["key"] == key)
        self.assertFalse(item["archived"])

    def test_scope_rejects_missing_pocket_anchor(self) -> None:
        account = accounts_page(self.database)["accounts"][0]
        outcome = self.runner.invoke(app, [
            "accounts", "scope", account["id"], "--pocket", "",
            "--start", "2026-01-01", "--end", "", "--reason", "x",
            "--database", str(self.database),
        ])
        self.assertEqual(outcome.exit_code, 1, outcome.output)
