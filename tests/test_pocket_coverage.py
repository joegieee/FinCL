"""Coverage is per pocket: one account's history never erases another's.

The answer's baseline is built from each account's own statement periods. Three
uploads that used to break it are replayed here through the import path:

* a second bank with one month of history used to zero every earlier month of
  the first bank, so spending averaged far too low and a real "No" read "Yes";
* a first bank one statement behind the second used to leave no spending model
  at all, so the answer went blank;
* one unreadable (quarantined) statement from a new bank used to do the same.
"""

from __future__ import annotations

import calendar
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from fincl.baseline import baseline_readiness, cost_income_table
from fincl.decision_journal import append_decision, default_journal_path
from fincl.document_service import DocumentImportService
from fincl.planning.sufficiency import VERDICT_SHORT, VERDICT_UNAVAILABLE, sufficiency_page
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes

_FIXTURES = Path(__file__).parent / "fixtures"
# Trust's synthetic statement covers 2026-08-01..2026-08-31.
_TODAY = date(2026, 9, 20)


def _month(month: int, opening: float) -> tuple[list[list[str]], float]:
    """Salary in, 3,645 out: a pocket that genuinely runs down."""
    balance = opening + 2000
    rows = [["Topup", "Current", f"2026-{month:02d}-01 09:00:00", f"2026-{month:02d}-01 09:00:01",
             "Salary", "2000.00", "0.00", "SGD", "COMPLETED", f"{balance:.2f}"]]
    for index in range(7):
        balance -= 520
        day = 3 + index * 3
        rows.append(["Card Payment", "Current", f"2026-{month:02d}-{day:02d} 10:00:00",
                     f"2026-{month:02d}-{day:02d} 10:00:01", f"Shop {index}", "-520.00", "0.00",
                     "SGD", "COMPLETED", f"{balance:.2f}"])
    last = calendar.monthrange(2026, month)[1]
    balance -= 5
    rows.append(["Card Payment", "Current", f"2026-{month:02d}-{last} 10:00:00",
                 f"2026-{month:02d}-{last} 10:00:01", "Monthly fee", "-5.00", "0.00", "SGD",
                 "COMPLETED", f"{balance:.2f}"])
    return rows, balance


class PocketCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger.sqlite3"
        self.service = DocumentImportService.plaintext_phase2(self.database, self.root / "evidence")

    def import_revolut(self, months: range) -> None:
        balance = 20000.0
        for month in months:
            rows, balance = _month(month, balance)
            source = self.root / f"revolut-{month}.pdf"
            source.write_bytes(revolut_pdf_bytes(rows))
            self.assertEqual(self.service.import_revolut_pdf(source).status, "ACCEPTED")

    def import_trust(self, name: str) -> str:
        return self.service.import_trust(_FIXTURES / name).status

    def answer(self) -> dict:
        refresh_ledger(self.database)
        return sufficiency_page(self.database, today=_TODAY)["combined_answer"]

    def assert_every_window_month_has_spending(self) -> None:
        table = cost_income_table(self.database, today=_TODAY)
        self.assertTrue(table["months"])
        for month in table["months"]:
            self.assertNotEqual(month["by_currency"]["SGD"]["gross_cost"], "0", month["month"])

    def test_a_bank_with_one_month_of_history_keeps_the_other_banks_months(self) -> None:
        self.import_revolut(range(1, 9))
        self.assertEqual(self.import_trust("trust_savings.pdf"), "ACCEPTED")

        answer = self.answer()

        self.assertEqual(answer["verdict"], VERDICT_SHORT)
        self.assert_every_window_month_has_spending()
        # Cross-account views keep the window every bank covers.
        connection = sqlite3.connect(self.database)
        try:
            periods = {row[0] for row in connection.execute(
                "SELECT DISTINCT period FROM analysis_components WHERE eligible = 1")}
        finally:
            connection.close()
        self.assertEqual(periods, {"2026-08"})

    def test_an_account_one_statement_behind_ends_the_window_instead_of_blanking_it(self) -> None:
        self.import_revolut(range(1, 8))
        self.assertEqual(self.import_trust("trust_savings.pdf"), "ACCEPTED")

        answer = self.answer()

        self.assertNotEqual(answer["verdict"], VERDICT_UNAVAILABLE)
        readiness = baseline_readiness(self.database, today=_TODAY)
        self.assertEqual(readiness["baseline_window"]["end_month"], "2026-07")
        self.assertIn(
            {"start": "2026-08-01", "end": "2026-08-31"},
            [task.get("interval") for task in readiness["tasks"]
             if task["kind"] == "import-missing" and task["account_id"] == "acct_revolut_current"],
        )
        self.assert_every_window_month_has_spending()

    def test_a_quarantined_statement_from_a_new_bank_changes_nothing(self) -> None:
        self.import_revolut(range(1, 6))
        before = self.answer()
        self.assertEqual(self.import_trust("trust_savings_unbalanced.pdf"), "QUARANTINED")

        after = self.answer()

        self.assertNotEqual(after["verdict"], VERDICT_UNAVAILABLE)
        self.assertEqual(
            (after["verdict"], after["starts_on"], after["starting_total"], after["base"]["lowest"]),
            (before["verdict"], before["starts_on"], before["starting_total"], before["base"]["lowest"]),
        )

    def test_an_archived_account_does_not_hold_the_window_back(self) -> None:
        self.import_revolut(range(1, 8))
        self.assertEqual(self.import_trust("trust_savings.pdf"), "ACCEPTED")
        append_decision(
            default_journal_path(self.database), decision_kind="ACCOUNT_DETAILS_SET",
            anchors={"account": "acct_revolut_current"},
            payload={"display_name": "Revolut Current", "archived_on": "2026-07-31",
                     "reason": "closed"},
            policy_version="v05", snapshot="pocket coverage test",
        )
        refresh_ledger(self.database)

        readiness = baseline_readiness(self.database, today=_TODAY)

        self.assertEqual(readiness["baseline_window"]["end_month"], "2026-08")


if __name__ == "__main__":
    unittest.main()
