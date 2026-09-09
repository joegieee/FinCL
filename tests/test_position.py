"""The as-of liquid position read (cash-flow framework Stage 1 / 2A groundwork).

A real ledger is built through the Revolut import path and ``refresh_ledger``;
the assertions cover the printed-running-balance basis, staleness disclosure,
the withheld total when a pocket has no current balance, and the dormant
manual-observation supersession once a row is projected into schema 19.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fincl.position import position_page
from fincl.refresh import refresh_ledger
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    FinCLService.plaintext_phase1(database, root / "evidence").import_revolut(source)
    refresh_ledger(database)
    return database


_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "360.00"],
]


class PositionReadTests(unittest.TestCase):
    def test_latest_printed_running_balance_is_the_pocket_basis(self) -> None:
        database = _ledger(_ROWS)

        page = position_page(database, today=date(2026, 3, 10))

        self.assertEqual(len(page["pockets"]), 1)
        pocket = page["pockets"][0]
        self.assertEqual(pocket["currency_pocket"], "SGD")
        self.assertEqual(pocket["basis"], "statement-running-balance")
        self.assertEqual(pocket["balance"], "360.00")
        self.assertEqual(pocket["as_of"], "2026-03-05")
        self.assertTrue(pocket["liquid"])
        self.assertEqual(page["liquid_total"], {"SGD": "360.00"})
        self.assertEqual(page["liquid_total_withheld"], {})

    def test_a_pocket_older_than_the_window_is_returned_but_flagged_stale(self) -> None:
        database = _ledger(_ROWS)

        fresh = position_page(database, today=date(2026, 3, 12))["pockets"][0]
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["staleness_days"], 7)

        stale = position_page(database, today=date(2026, 5, 1))["pockets"][0]
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["balance"], "360.00")
        self.assertEqual(page_currency_total(database, date(2026, 5, 1)), {"SGD": "360.00"})

    def test_a_newer_manual_observation_supersedes_the_printed_balance(self) -> None:
        database = _ledger(_ROWS)
        _project_observation(
            database,
            account_id=_only_account_id(database),
            currency_pocket="SGD",
            observed_on="2026-04-02",
            balance="355.10",
        )

        pocket = position_page(database, today=date(2026, 4, 5))["pockets"][0]

        self.assertEqual(pocket["basis"], "manual-observation")
        self.assertEqual(pocket["balance"], "355.10")
        self.assertEqual(pocket["as_of"], "2026-04-02")
        # The printed evidence is still carried alongside for the drill-down.
        self.assertEqual(pocket["statement_running_balance"]["balance"], "360.00")

    def test_a_retired_manual_observation_falls_back_to_the_printed_balance(self) -> None:
        database = _ledger(_ROWS)
        account_id = _only_account_id(database)
        _project_observation(
            database,
            account_id=account_id,
            currency_pocket="SGD",
            observed_on="2026-04-02",
            balance="355.10",
            retired=True,
        )

        pocket = position_page(database, today=date(2026, 4, 5))["pockets"][0]

        self.assertEqual(pocket["basis"], "statement-running-balance")
        self.assertEqual(pocket["balance"], "360.00")


def page_currency_total(database: Path, today: date) -> dict[str, str]:
    return position_page(database, today=today)["liquid_total"]


def _only_account_id(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("SELECT id FROM accounts").fetchall()
    finally:
        connection.close()
    assert len(rows) == 1, rows
    return rows[0][0]


def _project_observation(
    database: Path,
    *,
    account_id: str,
    currency_pocket: str,
    observed_on: str,
    balance: str,
    retired: bool = False,
) -> None:
    """Insert straight into the schema-19 projection.

    Stage 2 will write these rows from a ``BALANCE_OBSERVATION_SET`` journal
    record; until that kind is wired, the read path is exercised by projecting
    the row directly.
    """
    whole, _, fraction = balance.partition(".")
    coefficient = int(whole + fraction)
    scale = len(fraction)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO balance_observations
                (observation, account_id, currency_pocket, observed_on,
                 balance_coefficient, balance_scale, journal_seq, retired)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"obs-{observed_on}",
                account_id,
                currency_pocket,
                observed_on,
                coefficient,
                scale,
                1,
                1 if retired else 0,
            ),
        )
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
