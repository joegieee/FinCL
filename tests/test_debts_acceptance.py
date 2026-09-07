"""v0.3 acceptance scenarios spanning slices: multi-debt entries, fee pools,
cross-domain conflicts (DESIGN-debts-and-context.md §§2-3, v0.3-debts.md §4)."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from fractions import Fraction
from pathlib import Path

from fincl.analysis.allocations import claimed_amount, open_residual
from fincl.application import record_debt_decision, record_refund_allocation
from fincl.errors import FinCLError
from fincl.refresh import refresh_ledger
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-02-01 09:00:00", "2026-02-01 09:00:01", "Seed", "2000.00", "0.00", "SGD", "COMPLETED", "2000.00"],
    ["Card Payment", "Current", "2026-02-03 10:00:00", "2026-02-03 10:00:01", "Shared Flat", "-120.00", "0.00", "SGD", "COMPLETED", "1880.00"],
    ["Card Payment", "Current", "2026-02-04 10:00:00", "2026-02-04 10:00:01", "Lent With Fee", "-100.00", "2.00", "SGD", "COMPLETED", "1778.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01", "Gadget Store", "-80.00", "0.00", "SGD", "COMPLETED", "1698.00"],
    ["Card Refund", "Current", "2026-02-20 10:00:00", "2026-02-20 10:00:01", "Gadget Store", "80.00", "0.00", "SGD", "COMPLETED", "1778.00"],
]


def _intent(kind: str, anchors: dict, payload: dict) -> str:
    return hashlib.sha256(f"{kind}{anchors}{payload}".encode()).hexdigest()


class AcceptanceBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.database = self.root / "ledger.sqlite3"
        source = self.root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        FinCLService.plaintext_phase1(self.database, self.root / "evidence").import_revolut(source)
        refresh_ledger(self.database)

    def anchor(self, *, coefficient: int, direction: str, kind: str | None = None) -> str:
        clause = " AND event_kind = ?" if kind else ""
        params: tuple = (coefficient, direction) + ((kind,) if kind else ())
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT natural_anchor FROM ledger_entries "
                "WHERE abs(amount_coefficient) = ? AND economic_direction = ?" + clause,
                params,
            ).fetchone()[0]
        finally:
            connection.close()

    def rows(self, table: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            connection.close()

    def record(self, kind: str, anchors: dict, payload: dict):
        return record_debt_decision(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent(kind, anchors, payload),
            decision_kind=kind, anchors=anchors, payload=payload,
        )

    def open_receivable(self, opened_on: str = "2026-02-01") -> str:
        debt_id = str(uuid.uuid4())
        self.record(
            "PERSONAL_DEBT_OPEN", {"debt": debt_id},
            {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "X",
             "opened_on": opened_on, "due_on": None, "reason": "counterparty"},
        )
        return debt_id

    def alloc(self, anchor: str, principal: str, *, interest: str = "0.00",
              fee: str = "0.00", separate_fee: str = "0.00") -> dict:
        return {"entry_anchor": anchor, "principal_amount": principal,
                "interest_amount": interest, "fee_amount": fee,
                "separate_fee_amount": separate_fee}

    def expense_for_other(self, debt: str, anchor: str, principal: str, on: str) -> None:
        self.record(
            "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
             "principal_amount": principal, "effective_on": on,
             "entry_allocations": [self.alloc(anchor, principal)],
             "opening_balance": False, "expense_category": None, "reason": "share"},
        )


class MultiDebtOnOneEntryTests(AcceptanceBase):
    def test_120_bill_split_across_two_debts_and_a_residual_partitions_exactly(self) -> None:
        entry = self.anchor(coefficient=12000, direction="OUT")
        a, b = self.open_receivable(), self.open_receivable()
        self.expense_for_other(a, entry, "50.00", "2026-02-03")
        self.expense_for_other(b, entry, "30.00", "2026-02-03")
        self.record(
            "ENTRY_RESIDUAL_SET", {"entry": entry},
            {"expense_amount": "40.00", "expense_category": "Housing", "reason": "my share"},
        )
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(claimed_amount(connection, entry), Fraction(120))
            self.assertEqual(open_residual(connection, entry), Fraction(0))
        finally:
            connection.close()
        self.assertEqual(len(self.rows("personal_debt_allocations")), 2)

    def test_a_second_debt_that_overshoots_the_shared_entry_is_refused(self) -> None:
        entry = self.anchor(coefficient=12000, direction="OUT")
        a, b = self.open_receivable(), self.open_receivable()
        self.expense_for_other(a, entry, "50.00", "2026-02-03")
        self.record(
            "ENTRY_RESIDUAL_SET", {"entry": entry},
            {"expense_amount": "40.00", "expense_category": "Housing", "reason": "my share"},
        )
        # 50 + 40 already claimed; a 40 second debt would total 130 > 120.
        with self.assertRaisesRegex(FinCLError, "exceeds the component magnitude"):
            self.expense_for_other(b, entry, "40.00", "2026-02-03")


class SeparateFeePoolTests(AcceptanceBase):
    def test_separate_source_fee_is_claimed_from_the_fee_component_only(self) -> None:
        entry = self.anchor(coefficient=10000, direction="OUT")
        debt = self.open_receivable()
        self.record(
            "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "LENT", "direction": "RECEIVABLE", "principal_amount": "100.00",
             "effective_on": "2026-02-04",
             "entry_allocations": [self.alloc(entry, "100.00", separate_fee="2.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Lent 100 plus a 2 transfer fee"},
        )
        allocation = self.rows("personal_debt_allocations")[0]
        self.assertEqual(allocation["principal_coefficient"], 10000)
        self.assertEqual(allocation["separate_fee_coefficient"], 200)
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(claimed_amount(connection, entry, role="amount"), Fraction(100))
            self.assertEqual(claimed_amount(connection, entry, role="fee"), Fraction(2))
            self.assertEqual(open_residual(connection, entry, role="fee"), Fraction(0))
        finally:
            connection.close()

    def test_a_separate_fee_beyond_the_fee_component_is_refused(self) -> None:
        entry = self.anchor(coefficient=10000, direction="OUT")
        debt = self.open_receivable()
        with self.assertRaisesRegex(FinCLError, "exceeds the component magnitude"):
            self.record(
                "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": "LENT", "direction": "RECEIVABLE", "principal_amount": "100.00",
                 "effective_on": "2026-02-04",
                 "entry_allocations": [self.alloc(entry, "100.00", separate_fee="3.00")],
                 "opening_balance": False, "expense_category": None,
                 "reason": "Claims a 3 fee against a 2 fee component"},
            )


class CrossDomainConflictTests(AcceptanceBase):
    def _allocate_refund(self) -> str:
        purchase = self.anchor(coefficient=8000, direction="OUT")
        refund = self.anchor(coefficient=8000, direction="IN", kind="REFUND")
        record_refund_allocation(
            self.database, request_id="rf1", intent_hash="rf1h",
            refund_anchor=refund,
            allocations=[{"purchase_anchor": purchase, "refund_amount": "80.00",
                          "purchase_amount": "80.00"}],
            reason="matched receipt",
        )
        return purchase

    def test_a_debt_allocation_conflicting_with_a_refund_on_one_entry_is_refused(self) -> None:
        purchase = self._allocate_refund()
        debt = self.open_receivable()
        # The 80 purchase is already fully claimed by the refund attribution.
        with self.assertRaisesRegex(FinCLError, "exceeds the component magnitude"):
            self.record(
                "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": "LENT", "direction": "RECEIVABLE", "principal_amount": "80.00",
                 "effective_on": "2026-02-05",
                 "entry_allocations": [self.alloc(purchase, "80.00")],
                 "opening_balance": False, "expense_category": None,
                 "reason": "Also claims the refunded purchase as lent principal"},
            )

    def test_a_debt_allocation_within_the_unclaimed_remainder_is_accepted(self) -> None:
        # Refund only claims 50 of the 80 purchase, leaving 30 for a debt.
        purchase = self.anchor(coefficient=8000, direction="OUT")
        refund = self.anchor(coefficient=8000, direction="IN", kind="REFUND")
        record_refund_allocation(
            self.database, request_id="rf2", intent_hash="rf2h", refund_anchor=refund,
            allocations=[{"purchase_anchor": purchase, "refund_amount": "50.00",
                          "purchase_amount": "50.00"}],
            reason="partial receipt",
        )
        debt = self.open_receivable()
        self.record(
            "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "LENT", "direction": "RECEIVABLE", "principal_amount": "30.00",
             "effective_on": "2026-02-05",
             "entry_allocations": [self.alloc(purchase, "30.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Lent the unrefunded remainder"},
        )
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(claimed_amount(connection, purchase), Fraction(80))
            self.assertEqual(open_residual(connection, purchase), Fraction(0))
        finally:
            connection.close()
