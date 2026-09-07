"""v0.4 slice 1: selected card statement facts, coverage and reconciliation.

Built on the synthetic ``tests/fixtures/mari_card.pdf`` — always present, so
these run everywhere. The real statement corpus stays ``@skipUnless`` in
``test_document_import``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from fincl.api import create_app
from fincl.cli import app
from fincl.document_service import DocumentImportService
from fincl.rebuild import rebuild_database
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient

_FIXTURE = Path(__file__).parent / "fixtures" / "mari_card.pdf"
_ORIGIN = "http://127.0.0.1:8765"


class CardsSliceOneTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        result = DocumentImportService.plaintext_phase2(
            self.database, self.evidence
        ).import_mari_card(_FIXTURE)
        self.assertEqual(result.status, "ACCEPTED")
        refresh_ledger(self.database)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        return connection

    def _facts(self, database: Path) -> dict[tuple[str, str], tuple]:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            return {
                (row["currency_pocket"], row["field_role"]): (
                    row["value_kind"],
                    row["value_text"],
                    row["value_coefficient"],
                    row["value_scale"],
                    row["source_locator"],
                    row["parser_version"],
                    row["role_version"],
                )
                for row in connection.execute("SELECT * FROM card_statement_facts")
            }
        finally:
            connection.close()

    # -- projection ------------------------------------------------------- #

    def test_statement_facts_are_projected_with_exact_values_and_provenance(self) -> None:
        facts = self._facts(self.database)
        self.assertEqual(facts[("SGD", "closing_liability")][:4], ("money", "94.00", 9400, 2))
        self.assertEqual(facts[("SGD", "credit_limit")][:4], ("money", "500.00", 50000, 2))
        self.assertEqual(facts[("SGD", "minimum_payment")][:4], ("money", "30.00", 3000, 2))
        self.assertEqual(
            facts[("SGD", "payment_due_date")][:2], ("date", "2026-08-10")
        )
        self.assertEqual(
            facts[("SGD", "statement_period_start")][:2], ("date", "2026-06-21")
        )
        # Every fact carries a source locator and the parser / role versions.
        for value in facts.values():
            self.assertTrue(value[4])
            self.assertTrue(value[5])
            self.assertEqual(value[6], "mari-card-summary-roles-v1")
        # All nine printed summary buckets are retained.
        self.assertEqual(
            {role for pocket, role in facts if role.startswith("summary_bucket_")},
            {f"summary_bucket_{letter}" for letter in "ABCDEFGHI"},
        )

    def test_cards_page_shows_latest_statement_without_a_timeless_balance(self) -> None:
        from fincl.cards.queries import cards_page

        data = cards_page(self.database)
        self.assertEqual(len(data["cards"]), 1)
        card = data["cards"][0]
        self.assertEqual(card["account"]["source_type"], "MARI_CARD")
        latest = card["latest_statement"]
        self.assertEqual(latest["closing_liability"]["value"], "94.00")
        self.assertEqual(latest["closing_liability"]["currency"], "SGD")
        self.assertEqual(latest["statement_date"], "2026-07-21")
        self.assertEqual(latest["reconciliation_status"], "reconciled")
        self.assertEqual(card["coverage_gaps"], [])
        self.assertEqual(card["installment_obligations"], [])

    def test_statement_view_folds_the_printed_equation_and_bucket_signs(self) -> None:
        from fincl.cards.queries import card_statements

        connection = self._connect()
        account_id = connection.execute(
            "SELECT account_id FROM statements LIMIT 1"
        ).fetchone()[0]
        data = card_statements(self.database, account_id)
        self.assertEqual(len(data["statements"]), 1)
        pocket = data["statements"][0]["pockets"][0]
        self.assertEqual(pocket["currency"], "SGD")
        self.assertEqual(pocket["opening_liability"]["value"], "100.00")
        self.assertEqual(pocket["closing_liability"]["value"], "94.00")
        self.assertEqual(pocket["reconciliation"]["printed_equation"], "A-B-C+D+E-F+G+H-I")
        self.assertEqual(pocket["reconciliation"]["status"], "reconciled")
        self.assertGreater(pocket["reconciliation"]["blocking_total"], 0)
        self.assertEqual(
            pocket["reconciliation"]["blocking_total"],
            pocket["reconciliation"]["blocking_passed"],
        )
        self.assertEqual(set(pocket["summary_buckets"]), set("ABCDEFGHI"))

    def test_missing_statement_period_is_reported_as_a_gap(self) -> None:
        from fincl.cards.projection import coverage_gaps

        class _Row(dict):
            def __getitem__(self, key):
                return dict.get(self, key)

        statements = [
            _Row(period_start="2026-01-01", period_end="2026-01-31"),
            _Row(period_start="2026-04-01", period_end="2026-04-30"),
        ]
        gaps = coverage_gaps(statements)
        self.assertEqual(gaps, [{"from": "2026-02-01", "to": "2026-03-31"}])

    # -- rebuild equality ---------------------------------------------------- #

    def test_rebuild_reproduces_card_statement_facts_exactly(self) -> None:
        output = self.root / "rebuilt.sqlite3"
        rebuild_database(self.database, self.evidence, output)
        self.assertEqual(self._facts(output), self._facts(self.database))

    # -- API --------------------------------------------------------------- #

    def test_api_cards_and_statements_reads(self) -> None:
        application = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.evidence,
        )
        client = ASGITestClient(application, base_url=_ORIGIN)
        self.addCleanup(client.close)
        csrf = client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        self.assertEqual(csrf.status_code, 200, csrf.text)

        cards = client.get("/api/v1/cards")
        self.assertEqual(cards.status_code, 200, cards.text)
        body = cards.json()
        self.assertTrue(body["generation"].startswith("sha256:"))
        self.assertEqual(len(body["cards"]), 1)
        account_id = body["cards"][0]["account"]["account_id"]

        statements = client.get(f"/api/v1/cards/{account_id}/statements")
        self.assertEqual(statements.status_code, 200, statements.text)
        pocket = statements.json()["statements"][0]["pockets"][0]
        self.assertEqual(pocket["reconciliation"]["printed_equation"], "A-B-C+D+E-F+G+H-I")

        stale = client.get("/api/v1/cards?generation=sha256:0000")
        self.assertEqual(stale.status_code, 409)

        missing = client.get("/api/v1/cards/not-a-card/statements")
        self.assertEqual(missing.status_code, 404)

    # -- CLI ------------------------------------------------------------- #

    def test_cli_cards_list_and_show(self) -> None:
        runner = CliRunner()
        listing = runner.invoke(
            app,
            ["cards", "list", "--database", str(self.database), "--json", "--no-refresh"],
        )
        self.assertEqual(listing.exit_code, 0, listing.output)
        self.assertIn('"reconciliation_status": "reconciled"', listing.output)

        connection = self._connect()
        account_id = connection.execute(
            "SELECT account_id FROM statements LIMIT 1"
        ).fetchone()[0]
        show = runner.invoke(
            app,
            [
                "cards", "show", "--account", account_id,
                "--database", str(self.database), "--json", "--no-refresh",
            ],
        )
        self.assertEqual(show.exit_code, 0, show.output)
        self.assertIn("A-B-C+D+E-F+G+H-I", show.output)


if __name__ == "__main__":
    unittest.main()
