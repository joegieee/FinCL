"""v0.3 slice 2: source-linked debt events, shared-expense residuals, registry."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from fractions import Fraction
from pathlib import Path

from fincl.analysis.allocations import claimed_amount, open_residual
from fincl.application import record_batch, record_debt_decision
from fincl.decision_journal import default_journal_path, read_records
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.refresh import refresh_ledger
from fincl.report import economic_flow_report_data
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-03-01 09:00:00", "2026-03-01 09:00:01", "Seed", "2000.00", "0.00", "SGD", "COMPLETED", "2000.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01", "Shared Flat", "-120.00", "0.00", "SGD", "COMPLETED", "1880.00"],
    ["Card Payment", "Current", "2026-03-09 10:00:00", "2026-03-09 10:00:01", "Repay Alex", "-110.00", "0.00", "SGD", "COMPLETED", "1770.00"],
    ["Card Payment", "Current", "2026-03-12 10:00:00", "2026-03-12 10:00:01", "Repay Part", "-40.00", "0.00", "SGD", "COMPLETED", "1730.00"],
]


def _intent(kind: str, anchors: dict, payload: dict) -> str:
    return hashlib.sha256(f"{kind}{anchors}{payload}".encode()).hexdigest()


class Slice2Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.database = self.root / "ledger.sqlite3"
        source = self.root / "revolut.csv"
        source.write_bytes(csv_bytes(_ROWS))
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        refresh_ledger(self.database)

    def anchor_for(self, *, coefficient: int, direction: str) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT natural_anchor FROM ledger_entries "
                "WHERE abs(amount_coefficient) = ? AND economic_direction = ?",
                (coefficient, direction),
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
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=_intent(kind, anchors, payload),
            decision_kind=kind,
            anchors=anchors,
            payload=payload,
        )

    def open_debt(self, direction: str, *, currency: str = "SGD", opened_on: str = "2026-03-01") -> str:
        debt_id = str(uuid.uuid4())
        self.record(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": direction, "currency": currency, "counterparty_label": "Alex",
             "opened_on": opened_on, "due_on": None, "reason": "counterparty Alex"},
        )
        return debt_id

    def _alloc(self, anchor: str, principal: str, *, interest: str = "0.00",
               fee: str = "0.00", separate_fee: str = "0.00") -> dict:
        return {
            "entry_anchor": anchor, "principal_amount": principal,
            "interest_amount": interest, "fee_amount": fee,
            "separate_fee_amount": separate_fee,
        }


class SharedBillTests(Slice2Base):
    def test_shared_bill_splits_receivable_and_residual_and_partitions(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        debt = self.open_debt("RECEIVABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
             "principal_amount": "80.00", "effective_on": "2026-03-05",
             "entry_allocations": [self._alloc(anchor, "80.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Covered Alex's share of the flat"},
        )
        self.record(
            "ENTRY_RESIDUAL_SET",
            {"entry": anchor},
            {"expense_amount": "40.00", "expense_category": "Housing",
             "reason": "My own share of the shared flat"},
        )
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(claimed_amount(connection, anchor), Fraction(120))
            self.assertEqual(open_residual(connection, anchor), Fraction(0))
        finally:
            connection.close()
        alloc = self.rows("personal_debt_allocations")[0]
        self.assertEqual(alloc["bound"], 1)
        self.assertEqual(alloc["principal_coefficient"], 8000)

    def test_over_allocation_is_refused(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        debt = self.open_debt("RECEIVABLE")
        with self.assertRaisesRegex(FinCLError, "exceeds the component magnitude"):
            self.record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
                 "principal_amount": "130.00", "effective_on": "2026-03-05",
                 "entry_allocations": [self._alloc(anchor, "130.00")],
                 "opening_balance": False, "expense_category": None,
                 "reason": "Claims more than the bank movement"},
            )

    def test_wrong_direction_is_refused(self) -> None:
        inflow = self.anchor_for(coefficient=200000, direction="IN")
        debt = self.open_debt("RECEIVABLE")
        with self.assertRaisesRegex(FinCLError, "bank OUT movement"):
            self.record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": "LENT", "direction": "RECEIVABLE",
                 "principal_amount": "50.00", "effective_on": "2026-03-05",
                 "entry_allocations": [self._alloc(inflow, "50.00")],
                 "opening_balance": False, "expense_category": None,
                 "reason": "Lending is funded by an outflow, not an inflow"},
            )

    def test_cross_currency_allocation_is_refused(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        debt = self.open_debt("RECEIVABLE", currency="USD")
        with self.assertRaisesRegex(FinCLError, "cross-currency"):
            self.record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": "LENT", "direction": "RECEIVABLE",
                 "principal_amount": "80.00", "effective_on": "2026-03-05",
                 "entry_allocations": [self._alloc(anchor, "80.00")],
                 "opening_balance": False, "expense_category": None,
                 "reason": "Debt is USD, entry is SGD"},
            )


class RepaymentTests(Slice2Base):
    def _borrow_100(self) -> str:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance owed to Alex"},
        )
        return debt

    def test_repayment_110_is_100_principal_and_10_interest(self) -> None:
        debt = self._borrow_100()
        anchor = self.anchor_for(coefficient=11000, direction="OUT")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-09",
             "entry_allocations": [self._alloc(anchor, "100.00", interest="10.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Settled 100 principal plus 10 agreed interest"},
        )
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(claimed_amount(connection, anchor), Fraction(110))
            self.assertEqual(open_residual(connection, anchor), Fraction(0))
        finally:
            connection.close()
        events = self.rows("personal_debt_events")
        running = sum(int(e["signed"]) * int(e["principal_coefficient"]) for e in events)
        self.assertEqual(running, 0)

    def test_partial_repayment_leaves_balance(self) -> None:
        debt = self._borrow_100()
        anchor = self.anchor_for(coefficient=4000, direction="OUT")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "40.00", "effective_on": "2026-03-12",
             "entry_allocations": [self._alloc(anchor, "40.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Partial repayment of 40"},
        )
        events = self.rows("personal_debt_events")
        running = sum(int(e["signed"]) * int(e["principal_coefficient"]) for e in events)
        self.assertEqual(running, 6000)

    def test_overpaid_principal_is_refused(self) -> None:
        debt = self._borrow_100()
        anchor = self.anchor_for(coefficient=11000, direction="OUT")
        with self.assertRaisesRegex(FinCLError, "zero principal|negative principal"):
            self.record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
                 "principal_amount": "110.00", "effective_on": "2026-03-09",
                 "entry_allocations": [self._alloc(anchor, "110.00")],
                 "opening_balance": False, "expense_category": None,
                 "reason": "Repays more principal than is owed"},
            )


class OffLedgerAndRebuildTests(Slice2Base):
    def test_expense_paid_for_me_creates_payable_principal_off_ledger(self) -> None:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "EXPENSE_PAID_FOR_ME", "direction": "PAYABLE",
             "principal_amount": "25.00", "effective_on": "2026-03-06",
             "entry_allocations": [], "opening_balance": False,
             "expense_category": "Dining", "reason": "Alex covered my dinner"},
        )
        event = self.rows("personal_debt_events")[0]
        self.assertEqual(event["bound"], 1)
        self.assertEqual(event["expense_category"], "Dining")
        self.assertEqual(self.rows("personal_debt_allocations"), [])

    def test_rebuild_from_journal_reproduces_allocations(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        debt = self.open_debt("RECEIVABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
             "principal_amount": "80.00", "effective_on": "2026-03-05",
             "entry_allocations": [self._alloc(anchor, "80.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Covered Alex's share"},
        )
        self.record(
            "ENTRY_RESIDUAL_SET",
            {"entry": anchor},
            {"expense_amount": "40.00", "expense_category": "Housing", "reason": "My share"},
        )
        before = [tuple(r) for r in self.rows("personal_debt_allocations")]
        residual_before = [tuple(r) for r in self.rows("entry_residuals")]
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DELETE FROM personal_debt_allocations")
            connection.execute("DELETE FROM entry_residuals")
            connection.commit()
            replay_journal(connection)
            connection.commit()
        finally:
            connection.close()
        self.assertEqual([tuple(r) for r in self.rows("personal_debt_allocations")], before)
        self.assertEqual([tuple(r) for r in self.rows("entry_residuals")], residual_before)

    def _journal_len(self) -> int:
        return len(read_records(default_journal_path(self.database)))

    def test_coupled_batch_opens_debt_event_and_residual_atomically(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        debt_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        items = [
            {"kind": "PERSONAL_DEBT_OPEN", "anchors": {"debt": debt_id},
             "payload": {"direction": "RECEIVABLE", "currency": "SGD",
                         "counterparty_label": "Alex", "opened_on": "2026-03-01",
                         "due_on": None, "reason": "shared flat"}},
            {"kind": "PERSONAL_DEBT_EVENT_SET",
             "anchors": {"debt": debt_id, "event": event_id},
             "payload": {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
                         "principal_amount": "80.00", "effective_on": "2026-03-05",
                         "entry_allocations": [self._alloc(anchor, "80.00")],
                         "opening_balance": False, "expense_category": None,
                         "reason": "Alex's share"}},
            {"kind": "ENTRY_RESIDUAL_SET", "anchors": {"entry": anchor},
             "payload": {"expense_amount": "40.00", "expense_category": "Housing",
                         "reason": "my share"}},
        ]
        before = self._journal_len()
        record_batch(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent("DECISION_BATCH", {}, {"items": items}),
            items=items, reason="Record the shared flat bill",
        )
        self.assertEqual(self._journal_len(), before + 1)
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(open_residual(connection, anchor), Fraction(0))
        finally:
            connection.close()
        self.assertEqual(len(self.rows("personal_debts")), 1)
        self.assertEqual(len(self.rows("personal_debt_allocations")), 1)
        self.assertEqual(len(self.rows("entry_residuals")), 1)

    def test_over_claiming_batch_is_rejected_whole(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        debt_id = str(uuid.uuid4())
        items = [
            {"kind": "PERSONAL_DEBT_OPEN", "anchors": {"debt": debt_id},
             "payload": {"direction": "RECEIVABLE", "currency": "SGD",
                         "counterparty_label": "Alex", "opened_on": "2026-03-01",
                         "due_on": None, "reason": "shared flat"}},
            {"kind": "PERSONAL_DEBT_EVENT_SET",
             "anchors": {"debt": debt_id, "event": str(uuid.uuid4())},
             "payload": {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
                         "principal_amount": "100.00", "effective_on": "2026-03-05",
                         "entry_allocations": [self._alloc(anchor, "100.00")],
                         "opening_balance": False, "expense_category": None,
                         "reason": "Alex's share"}},
            {"kind": "ENTRY_RESIDUAL_SET", "anchors": {"entry": anchor},
             "payload": {"expense_amount": "40.00", "expense_category": "Housing",
                         "reason": "my share"}},
        ]
        before = self._journal_len()
        with self.assertRaisesRegex(FinCLError, "exceeds the component magnitude"):
            record_batch(
                self.database, request_id=str(uuid.uuid4()),
                intent_hash=_intent("DECISION_BATCH", {}, {"items": items}),
                items=items, reason="100 + 40 > 120",
            )
        self.assertEqual(self._journal_len(), before)
        self.assertEqual(self.rows("personal_debts"), [])

    def test_residual_retire_frees_the_component(self) -> None:
        anchor = self.anchor_for(coefficient=4000, direction="OUT")
        self.record(
            "ENTRY_RESIDUAL_SET",
            {"entry": anchor},
            {"expense_amount": "40.00", "expense_category": "Housing", "reason": "mine"},
        )
        self.record("ENTRY_RESIDUAL_RETIRE", {"entry": anchor}, {"reason": "misclassified"})
        self.assertEqual(self.rows("entry_residuals"), [])


class AnalyticalEffectsTests(Slice2Base):
    """v0.3 §3: principal has zero spending; residuals / EXPENSE_PAID_FOR_ME are spending."""

    def _spending(self) -> str:
        return economic_flow_report_data(self.database)["currencies"][0]["spending"]

    def test_baseline_spending(self) -> None:
        # -120 + -110 + -40 outflows in the seed.
        self.assertEqual(self._spending(), "270.00")

    def test_repayment_principal_leaves_only_interest_as_spending(self) -> None:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance owed to Alex"},
        )
        anchor = self.anchor_for(coefficient=11000, direction="OUT")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-09",
             "entry_allocations": [self._alloc(anchor, "100.00", interest="10.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "100 principal + 10 interest"},
        )
        # 270 - 100 principal excluded; the 10 interest stays inside spending.
        self.assertEqual(self._spending(), "170.00")

    def test_shared_bill_leaves_only_the_owner_share_as_spending(self) -> None:
        anchor = self.anchor_for(coefficient=12000, direction="OUT")
        debt = self.open_debt("RECEIVABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
             "principal_amount": "80.00", "effective_on": "2026-03-05",
             "entry_allocations": [self._alloc(anchor, "80.00")],
             "opening_balance": False, "expense_category": None,
             "reason": "Alex's 80 share"},
        )
        self.record(
            "ENTRY_RESIDUAL_SET", {"entry": anchor},
            {"expense_amount": "40.00", "expense_category": "Housing / Rent",
             "reason": "my 40 share"},
        )
        # 270 - 80 receivable principal; the 40 residual stays as spending.
        self.assertEqual(self._spending(), "190.00")
        from fincl.analysis.components import build_components
        from fincl.db import connect_readonly
        from fincl.intervals import DateInterval
        connection = connect_readonly(self.database)
        try:
            window = [DateInterval("2026-03-01", "2026-03-31")]
            projection = build_components(
                connection, covered_intervals=window, analysis_intervals=window
            )
        finally:
            connection.close()
        housing = [
            c for c in projection.components
            if c.category_key == "Housing / Rent" and c.bucket == "spending"
        ]
        self.assertEqual(len(housing), 1)
        self.assertEqual(housing[0].coefficient, -4000)

    def test_expense_paid_for_me_adds_synthetic_spending(self) -> None:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "EXPENSE_PAID_FOR_ME", "direction": "PAYABLE",
             "principal_amount": "25.00", "effective_on": "2026-03-06",
             "entry_allocations": [], "opening_balance": False,
             "expense_category": "Dining / Restaurants", "reason": "Alex covered dinner"},
        )
        self.assertEqual(self._spending(), "295.00")
