"""Superseding a statement never drops a decision made on its rows.

A decision anchored on a row of a superseded statement applies to the matching
row of the statement that carries it. The same holds for overlaps already in a
ledger, which refresh settles, and a superseded statement counts again when
the statement that carried it stops counting.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fincl.decision_journal import append_decision, default_journal_path
from fincl.document_service import DocumentImportService
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes

_CATEGORY = "Food & Dining / Groceries"


def _row(kind: str, day: str, description: str, amount: str, balance: str) -> list[str]:
    return [kind, "Current", f"{day} 10:00:00", f"{day} 10:00:01", description, amount,
            "0.00", "SGD", "COMPLETED", balance]


JANUARY = [
    _row("Topup", "2026-01-01", "Salary", "1000.00", "1000.00"),
    _row("Card Payment", "2026-01-05", "Grocer", "-40.00", "960.00"),
    _row("Refund", "2026-01-08", "Grocer", "15.00", "975.00"),
    _row("Card Payment", "2026-01-20", "Bookshop", "-25.00", "950.00"),
    _row("Card Payment", "2026-01-31", "Fee", "-5.00", "945.00"),
]
MID_MONTH = JANUARY[:3]


class SupersessionKeepsDecisionsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger.sqlite3"
        self.service = DocumentImportService.plaintext_phase2(self.database, self.root / "evidence")

    def upload(self, name: str, rows: list[list[str]]):
        source = self.root / name
        source.write_bytes(revolut_pdf_bytes(rows))
        return self.service.import_revolut_pdf(source)

    def query(self, sql: str, *params: object) -> list[tuple]:
        connection = sqlite3.connect(self.database)
        try:
            return [tuple(row) for row in connection.execute(sql, params)]
        finally:
            connection.close()

    def execute(self, *statements: tuple[str, tuple]) -> None:
        connection = sqlite3.connect(self.database)
        try:
            for sql, params in statements:
                connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def anchor(self, period_end: str, description: str, amount: str) -> str:
        ((anchor,),) = self.query(
            """SELECT e.natural_anchor FROM ledger_entries e
               JOIN statements s ON s.id = e.statement_id
               JOIN document_records d ON d.id = e.document_record_id
               WHERE s.period_end = ? AND d.description_raw = ? AND e.amount_text = ?""",
            period_end, description, amount,
        )
        return str(anchor)

    def decide_on_mid_month_rows(self) -> None:
        journal = default_journal_path(self.database)
        purchase = self.anchor("2026-01-08", "Grocer", "-40.00")
        refund = self.anchor("2026-01-08", "Grocer", "15.00")
        append_decision(
            journal, decision_kind="CATEGORY_OVERRIDE", anchors={"entry": purchase},
            payload={"category": _CATEGORY, "reason": "weekly shop"},
            policy_version="category-engine-2026-v1", snapshot="override",
        )
        append_decision(
            journal, decision_kind="REFUND_ALLOCATION_SET", anchors={"refund": refund},
            payload={"allocations": [{"purchase_anchor": purchase, "refund_amount": "15.00",
                                      "purchase_amount": "15.00"}], "reason": "returned an item"},
            policy_version="refund-v1", snapshot="refund",
        )

    def assert_decisions_follow_the_month_statement(self) -> None:
        purchase = self.anchor("2026-01-31", "Grocer", "-40.00")
        refund = self.anchor("2026-01-31", "Grocer", "15.00")
        self.assertEqual(
            self.query("SELECT category_key FROM analysis_components WHERE origin_anchor = ?", purchase),
            [(_CATEGORY,)],
        )
        self.assertEqual(
            self.query("SELECT refund_anchor, purchase_anchor FROM refund_allocations"),
            [(refund, purchase)],
        )
        self.assertEqual(self.query("SELECT count(*) FROM category_overrides WHERE bound = 0"), [(0,)])
        self.assertEqual(
            self.query("SELECT count(*) FROM relationship_decisions WHERE bound = 0"), [(0,)]
        )
        # Every row counts once.
        self.assertEqual(
            self.query("SELECT count(*) FROM analysis_components WHERE role = 'amount'"), [(5,)]
        )

    def test_a_categorised_mid_month_export_keeps_its_decisions_after_the_month_statement(self) -> None:
        self.upload("mid.pdf", MID_MONTH)
        refresh_ledger(self.database)
        self.decide_on_mid_month_rows()
        refresh_ledger(self.database)

        month = self.upload("month.pdf", JANUARY)
        result = refresh_ledger(self.database)

        self.assertEqual(month.supersedes, ("2026-01-01..2026-01-08",))
        self.assertEqual(result.decisions_dangling, 0)
        self.assert_decisions_follow_the_month_statement()

    def test_an_overlap_already_in_a_ledger_is_settled_on_refresh_keeping_decisions(self) -> None:
        self.upload("mid.pdf", MID_MONTH)
        self.upload("month.pdf", JANUARY)
        # As a ledger imported before supersession existed: both counted.
        self.execute(
            ("UPDATE statements SET selection_state = 'SELECTED'", ()),
            ("UPDATE ledger_entries SET analytics_eligible = 1", ()),
            ("DELETE FROM extraction_issues WHERE code = 'STATEMENT_SUPERSEDED'", ()),
        )
        self.assertEqual(
            self.query("SELECT count(*) FROM statements WHERE selection_state = 'SELECTED'"), [(2,)]
        )
        self.decide_on_mid_month_rows()
        refresh_ledger(self.database)

        self.assertEqual(
            sorted(self.query("SELECT period_end, selection_state FROM statements")),
            [("2026-01-08", "CANDIDATE"), ("2026-01-31", "SELECTED")],
        )
        self.assert_decisions_follow_the_month_statement()
        before = self.query("SELECT id, selection_state FROM statements ORDER BY id")
        refresh_ledger(self.database)
        self.assertEqual(self.query("SELECT id, selection_state FROM statements ORDER BY id"), before)

    def test_an_old_conflict_between_identical_copies_is_settled_on_refresh(self) -> None:
        self.upload("month.pdf", JANUARY)
        source = self.root / "month-again.pdf"
        source.write_bytes(revolut_pdf_bytes(JANUARY) + b"\n% downloaded again\n")
        self.service.import_revolut_pdf(source)
        # As a ledger that quarantined a re-download before supersession existed.
        self.execute(
            ("UPDATE statements SET selection_state = 'CANDIDATE', conflict_state = 'CONFLICT'", ()),
            ("UPDATE ledger_entries SET analytics_eligible = 0", ()),
            ("DELETE FROM extraction_issues WHERE code = 'STATEMENT_SUPERSEDED'", ()),
        )

        refresh_ledger(self.database)

        self.assertEqual(
            sorted(self.query("SELECT selection_state, conflict_state FROM statements")),
            [("CANDIDATE", "NONE"), ("SELECTED", "NONE")],
        )
        self.assertEqual(
            self.query("SELECT count(*) FROM analysis_components WHERE role = 'amount'"), [(5,)]
        )

    def test_a_superseded_statement_counts_again_when_its_carrier_stops_counting(self) -> None:
        self.upload("mid.pdf", MID_MONTH)
        self.upload("month.pdf", JANUARY)
        refresh_ledger(self.database)
        self.execute((
            "UPDATE statements SET selection_state = 'QUARANTINED' WHERE period_end = '2026-01-31'", (),
        ), (
            "UPDATE ledger_entries SET analytics_eligible = 0 WHERE statement_id IN "
            "(SELECT id FROM statements WHERE period_end = '2026-01-31')", (),
        ))

        refresh_ledger(self.database)

        self.assertEqual(
            self.query("SELECT selection_state FROM statements WHERE period_end = '2026-01-08'"),
            [("SELECTED",)],
        )
        self.assertEqual(
            self.query("SELECT count(*) FROM analysis_components WHERE role = 'amount'"), [(3,)]
        )


if __name__ == "__main__":
    unittest.main()
