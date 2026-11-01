"""The synthetic development ledger stays as awkward as it is meant to be.

``scripts/create_demo.py`` exists so the app can be developed without real data, which
only works while the ledger keeps the behaviour that makes real ledgers hard: statements
that do not line up, transfers between accounts, and work left half done.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from fincl import clock
from fincl.api import create_app
from fincl.rebuild import rebuild_database
from fincl.reconciliation import (
    AUTOMATIC_SAME_CURRENCY_LINK_METHODS,
    DBS_REVOLUT_LEGACY_LINK_VERSION,
    REVOLUT_EXCHANGE_LINK_VERSION,
)
from tests.asgi_client import ASGITestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from create_demo import SYNTHETIC_MARKER, create_demo  # noqa: E402

_ORIGIN = "http://127.0.0.1:8765"
_THROUGH = date(2026, 8, 31)


def _rows(database: Path, query: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(database)
    try:
        return [tuple(row) for row in connection.execute(query)]
    finally:
        connection.close()


class DemoLedgerTests(unittest.TestCase):
    root: Path
    database: Path

    @classmethod
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        clock.pin(date(2026, 10, 15))
        cls.addClassCleanup(clock.pin, None)
        cls.database = create_demo(cls.root / "ledger", through=_THROUGH)

    def answer(self) -> dict[str, Any]:
        app = create_app(self.database, port=8765, bootstrap_secret="s", require_access_key=True,
                         evidence_dir=self.database.parent / "evidence")
        client = ASGITestClient(app, base_url=_ORIGIN)
        self.addCleanup(client.close)
        client.post("/session", json={"secret": "s"}, headers={"origin": _ORIGIN, "content-type": "application/json"})
        response = client.get("/api/v1/planning/sufficiency")
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json()["combined_answer"])

    def test_every_statement_imports_and_reconciles(self) -> None:
        self.assertEqual(_rows(self.database, "SELECT DISTINCT status FROM extraction_runs"), [("ACCEPTED",)])
        self.assertEqual(_rows(self.database, "SELECT DISTINCT selection_state FROM statements"), [("SELECTED",)])
        self.assertEqual(_rows(self.database, "SELECT code FROM extraction_issues"), [])
        self.assertEqual(_rows(self.database, "SELECT check_type FROM document_reconciliation_checks "
                                              "WHERE blocking = 1 AND passed = 0"), [])
        self.assertTrue((self.database.parent / SYNTHETIC_MARKER).exists())

    def test_accounts_run_on_their_own_cycles_and_start_at_different_times(self) -> None:
        statements = _rows(self.database, "SELECT source_type, period_start, period_end FROM statements ORDER BY period_start")
        closing_days = {source: {end[8:] for kind, _, end in statements if kind == source} for source, _, _ in statements}
        self.assertEqual(closing_days["DBS_PAYLAH"], {"08"})
        self.assertEqual(closing_days["MARI_CARD"], {"20"})

        first = {}
        for source, start, _ in statements:
            first.setdefault(source, start)
        self.assertEqual(len({first[source] for source in ("REVOLUT_PDF", "MARI_SAVINGS", "MARI_CARD", "TRUST_SAVINGS")}), 4)
        for source in ("REVOLUT_PDF", "MARI_SAVINGS", "MARI_CARD"):
            self.assertNotEqual(first[source][8:], "01", source)

        pockets = _rows(self.database, "SELECT DISTINCT account_id, currency_pocket FROM ledger_entries")
        self.assertEqual(len(pockets), 7)  # and a USD pocket at DBS that never moves
        self.assertEqual(_rows(self.database, "SELECT DISTINCT currency FROM statement_sections "
                                              "WHERE section_kind = 'CURRENCY_LEDGER' AND currency = 'USD'"), [("USD",)])

    def test_statements_are_as_untidy_as_real_ones(self) -> None:
        outside = dict(_rows(self.database, """
            SELECT s.source_type, count(*) FROM ledger_entries e JOIN statements s ON s.id = e.statement_id
            WHERE substr(e.effective_at, 1, 10) NOT BETWEEN s.period_start AND s.period_end GROUP BY 1"""))
        self.assertLessEqual({"DBS_CONSOLIDATED", "DBS_PAYLAH", "MARI_CARD"}, set(outside))

        self.assertEqual(_rows(self.database, "SELECT DISTINCT s.source_type FROM ledger_entries e JOIN statements s "
                                              "ON s.id = e.statement_id WHERE e.effective_precision = 'MONTH'"),
                         [("MARI_SAVINGS",)])
        quiet = _rows(self.database, "SELECT DISTINCT s.source_type FROM statements s WHERE NOT EXISTS "
                                     "(SELECT 1 FROM ledger_entries e WHERE e.statement_id = s.id) ORDER BY 1")
        self.assertEqual(quiet, [("MARI_SAVINGS",), ("TRUST_SAVINGS",)])
        self.assertTrue(_rows(self.database, "SELECT 1 FROM statement_sections WHERE section_kind = 'REVERTED'"))
        self.assertTrue(_rows(self.database, "SELECT 1 FROM statement_sections WHERE section_kind = 'PENDING'"))
        self.assertEqual({currency for (currency,) in _rows(
            self.database, "SELECT DISTINCT source_currency FROM ledger_entries WHERE source_currency IS NOT NULL "
                           "AND source_currency != currency_pocket")}, {"EUR", "GBP", "JPY"})

    def test_every_automatic_transfer_linker_has_work(self) -> None:
        linked = {method for (method,) in _rows(self.database, "SELECT DISTINCT method_version FROM transfer_links")}
        expected = {*AUTOMATIC_SAME_CURRENCY_LINK_METHODS, REVOLUT_EXCHANGE_LINK_VERSION} - {DBS_REVOLUT_LEGACY_LINK_VERSION}
        self.assertEqual(linked, expected)

    def test_decisions_all_land_on_their_entries(self) -> None:
        for table in ("inbound_classifications", "personal_debt_events", "personal_debt_allocations",
                      "installment_plans", "installment_occurrences"):
            self.assertEqual(_rows(self.database, f"SELECT 1 FROM {table} WHERE bound = 0"), [], table)
        self.assertTrue(_rows(self.database, "SELECT 1 FROM entry_categories WHERE category = 'Unclassified'"))

    def test_the_answer_is_left_with_work_to_do(self) -> None:
        answer = self.answer()
        self.assertFalse(answer["complete"])
        todos = {todo["key"]: todo for todo in answer["todos"]}
        self.assertLessEqual({"debt-dates", "card-statement", "recurring-history"}, set(todos))
        self.assertTrue(all(todo["page"] for todo in todos.values()))

    def test_savings_rests_on_its_observation(self) -> None:
        accounts = [account for pocket in self.answer()["pockets"] for account in pocket["accounts"]]
        self.assertEqual(sorted({account["basis"] for account in accounts if account["account"] == "maribank-savings"}),
                         ["observation"])

    def test_the_same_month_builds_the_same_ledger(self) -> None:
        again = create_demo(self.root / "again", through=_THROUGH)
        query = "SELECT natural_anchor, amount_text FROM ledger_entries ORDER BY 1"
        self.assertEqual(_rows(again, query), _rows(self.database, query))

    def test_it_rebuilds_from_its_evidence(self) -> None:
        output = self.database.parent / "rebuilt.sqlite3"
        rebuild_database(self.database, self.database.parent / "evidence", output)
        query = "SELECT natural_anchor, amount_text FROM ledger_entries ORDER BY 1"
        self.assertEqual(_rows(output, query), _rows(self.database, query))


if __name__ == "__main__":
    unittest.main()
