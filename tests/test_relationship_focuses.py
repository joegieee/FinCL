"""The categorization inbox's relationship-discovery focuses.

"Looks like a debt movement" and "Installment candidates" propose rows a debt
or an installment plan probably owns. Each proposal rests on evidence the
ledger already holds -- a debt you recorded against the same payee before, or
an instalment ordinal printed on the statement -- and cites it on the card.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.categorization_queries import categorization_candidate_detail, categorization_candidates
from fincl.db import connect_readonly
from fincl.document_service import DocumentImportService
from fincl.generation import current_generation
from fincl.installments.candidates import installment_candidates
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes
from tests.test_debts_slice2 import Slice2Base

_ROWS = [
    ["Topup", "Current", "2026-03-01 09:00:00", "2026-03-01 09:00:01", "Seed", "2000.00", "0.00", "SGD", "COMPLETED", "2000.00"],
    ["Transfer", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01", "ALEX TAN", "-50.00", "0.00", "SGD", "COMPLETED", "1950.00"],
    ["Transfer", "Current", "2026-03-10 10:00:00", "2026-03-10 10:00:01", "ALEX TAN", "-30.00", "0.00", "SGD", "COMPLETED", "1920.00"],
    ["Transfer", "Current", "2026-03-15 10:00:00", "2026-03-15 10:00:01", "ALEX TAN", "20.00", "0.00", "SGD", "COMPLETED", "1940.00"],
    ["Card Payment", "Current", "2026-03-16 10:00:00", "2026-03-16 10:00:01", "KOPI CORNER", "-5.00", "0.00", "SGD", "COMPLETED", "1935.00"],
]


def _generation(database: Path) -> str:
    connection = connect_readonly(database)
    try:
        return current_generation(connection)["generation_token"]
    finally:
        connection.close()


class DebtFocusTests(Slice2Base):
    def setUp(self) -> None:  # noqa: D401 - replaces the base fixture's rows
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.database = self.root / "ledger.sqlite3"
        source = self.root / "revolut.csv"
        source.write_bytes(revolut_pdf_bytes(_ROWS))
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        refresh_ledger(self.database)
        self.lent = self.anchor_for(coefficient=5000, direction="OUT")
        self.later_out = self.anchor_for(coefficient=3000, direction="OUT")
        self.later_in = self.anchor_for(coefficient=2000, direction="IN")
        self.unrelated = self.anchor_for(coefficient=500, direction="OUT")

    def lend_to_alex(self) -> str:
        debt = self.open_debt("RECEIVABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "LENT", "direction": "RECEIVABLE", "principal_amount": "50.00",
             "effective_on": "2026-03-05", "entry_allocations": [self._alloc(self.lent, "50.00")],
             "opening_balance": False, "expense_category": None, "reason": "Lent Alex 50"},
        )
        refresh_ledger(self.database)
        return debt

    def focus(self, **filters: str) -> list[dict]:
        return categorization_candidates(self.database, generation=_generation(self.database), focus="debts", **filters)["candidates"]

    def test_nothing_is_proposed_without_an_earlier_debt_decision(self) -> None:
        # Same payee three times, but no debt has ever been recorded against it.
        self.assertEqual(self.focus(), [])

    def test_other_movements_with_a_payee_already_recorded_against_a_debt_are_proposed(self) -> None:
        debt = self.lend_to_alex()
        (card,) = self.focus()
        # A bank transfer to a person is identified by its exact description.
        self.assertEqual(card["kind"], "description")
        self.assertEqual(card["label"], "ALEX TAN")
        # The allocated movement itself is not re-proposed; both directions of
        # the rest are (a loan is lent out and paid back in).
        self.assertEqual(card["transaction_count"], 2)
        self.assertEqual(card["directions"], {"IN": 1, "OUT": 1})
        (suggested,) = card["suggested_debts"]
        self.assertEqual(suggested["debt_id"], debt)
        self.assertEqual(suggested["counterparty_label"], "Alex")
        self.assertEqual(suggested["earlier"], 1)
        self.assertIn("You recorded 1 earlier transaction with this payee against Alex", card["relationship_basis"])

        detail = categorization_candidate_detail(
            self.database, generation=_generation(self.database), candidate_id=card["candidate_id"], focus="debts",
        )
        self.assertEqual({record["anchor"] for record in detail["records"]}, {self.later_out, self.later_in})
        self.assertEqual(detail["candidate"]["suggested_debts"][0]["debt_id"], debt)

    def test_a_movement_once_recorded_leaves_the_focus(self) -> None:
        debt = self.lend_to_alex()
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_RETURNED", "direction": "RECEIVABLE", "principal_amount": "20.00",
             "effective_on": "2026-03-15", "entry_allocations": [self._alloc(self.later_in, "20.00")],
             "opening_balance": False, "expense_category": None, "reason": "Alex paid 20 back"},
        )
        refresh_ledger(self.database)
        (card,) = self.focus()
        self.assertEqual(card["transaction_count"], 1)
        self.assertEqual(card["directions"], {"OUT": 1})

    def test_inbox_filters_apply(self) -> None:
        self.lend_to_alex()
        (card,) = self.focus(direction="IN")
        self.assertEqual(card["transaction_count"], 1)
        self.assertEqual(self.focus(currency="USD"), [])


class InstallmentFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.database = root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, root / "evidence").import_mari_card(
            Path(__file__).parent / "fixtures" / "mari_card.pdf"
        )
        refresh_ledger(self.database)

    def test_each_proposed_chain_is_a_group_citing_the_printed_ordinals(self) -> None:
        proposals = installment_candidates(self.database)["candidates"]
        self.assertTrue(proposals)
        cards = categorization_candidates(self.database, generation=_generation(self.database), focus="installments")["candidates"]
        by_key = {card.get("installment_candidate_key"): card for card in cards}
        for proposal in proposals:
            card = by_key[proposal["candidate_key"]]
            self.assertEqual(card["kind"], "installment_chain")
            self.assertEqual(card["label"], proposal["merchant_label"])
            self.assertEqual(card["transaction_count"], len(proposal["occurrences"]))
            self.assertIn(f"of {proposal['term_months']}", card["relationship_basis"])


if __name__ == "__main__":
    unittest.main()
