"""v0.3 slice 3: forgiveness / write-off, close / reopen, coupled corrections."""

from __future__ import annotations

import sqlite3
import uuid

from fincl.application import record_batch
from fincl.decisions import replay_journal
from fincl.errors import FinCLError
from fincl.report import economic_flow_report_data
from tests.test_debts_slice2 import Slice2Base, _intent


class ForgivenessTests(Slice2Base):
    def _flow(self) -> dict:
        return economic_flow_report_data(self.database)["currencies"][0]

    def _open_with_opening_balance(self, direction: str, amount: str) -> str:
        debt = self.open_debt(direction)
        kind = "BORROWED" if direction == "PAYABLE" else "LENT"
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": kind, "direction": direction,
             "principal_amount": amount, "effective_on": "2026-03-02",
             "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": f"Opening balance {amount}"},
        )
        return debt

    def test_payable_forgiven_clears_balance_as_exceptional_inflow_not_income(self) -> None:
        debt = self._open_with_opening_balance("PAYABLE", "100.00")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PAYABLE_FORGIVEN", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-10",
             "entry_allocations": [], "opening_balance": False,
             "expense_category": None, "reason": "Alex forgave the balance"},
        )
        events = self.rows("personal_debt_events")
        running = sum(int(e["signed"]) * int(e["principal_coefficient"]) for e in events)
        self.assertEqual(running, 0)
        flow = self._flow()
        self.assertEqual(flow["exceptional_inflow"], "100.00")
        self.assertEqual(flow["income"], "0")

    def test_receivable_written_off_is_exceptional_loss_not_spending(self) -> None:
        debt = self._open_with_opening_balance("RECEIVABLE", "70.00")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "RECEIVABLE_WRITTEN_OFF", "direction": "RECEIVABLE",
             "principal_amount": "70.00", "effective_on": "2026-03-11",
             "entry_allocations": [], "opening_balance": False,
             "expense_category": None, "reason": "Jo will not repay"},
        )
        flow = self._flow()
        self.assertEqual(flow["exceptional_loss"], "70.00")
        self.assertEqual(flow["spending"], "270.00")  # unchanged seed spending

    def test_forgiving_more_than_owed_is_refused(self) -> None:
        debt = self._open_with_opening_balance("PAYABLE", "100.00")
        with self.assertRaises(FinCLError):
            self.record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": "PAYABLE_FORGIVEN", "direction": "PAYABLE",
                 "principal_amount": "150.00", "effective_on": "2026-03-10",
                 "entry_allocations": [], "opening_balance": False,
                 "expense_category": None, "reason": "Forgives more than is owed"},
            )


class CloseReopenTests(Slice2Base):
    def _settled_payable(self) -> str:
        debt = self.open_debt("PAYABLE")
        for kind, amt, on in (("BORROWED", "100.00", "2026-03-02"),):
            self.record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": kind, "direction": "PAYABLE", "principal_amount": amt,
                 "effective_on": on, "entry_allocations": [], "opening_balance": True,
                 "expense_category": None, "reason": "Opening balance"},
            )
        anchor = self.anchor_for(coefficient=11000, direction="OUT")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-09",
             "entry_allocations": [self._alloc(anchor, "100.00", interest="10.00")],
             "opening_balance": False, "expense_category": None, "reason": "Settled"},
        )
        return debt

    def test_close_a_settled_debt(self) -> None:
        debt = self._settled_payable()
        self.record("PERSONAL_DEBT_CLOSE", {"debt": debt},
                    {"effective_on": "2026-03-10", "reason": "Fully settled"})
        row = self.rows("personal_debts")[0]
        self.assertEqual(row["status"], "CLOSED")
        self.assertEqual(row["closed_on"], "2026-03-10")

    def test_close_with_a_non_zero_balance_is_refused(self) -> None:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "50.00",
             "effective_on": "2026-03-02", "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Still owed"},
        )
        with self.assertRaisesRegex(FinCLError, "not zero"):
            self.record("PERSONAL_DEBT_CLOSE", {"debt": debt},
                        {"effective_on": "2026-03-10", "reason": "Premature close"})

    def test_an_event_after_close_is_refused(self) -> None:
        debt = self._settled_payable()
        self.record("PERSONAL_DEBT_CLOSE", {"debt": debt},
                    {"effective_on": "2026-03-10", "reason": "Settled"})
        anchor = self.anchor_for(coefficient=200000, direction="IN")
        with self.assertRaisesRegex(FinCLError, "later active events"):
            self.record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt, "event": str(uuid.uuid4())},
                {"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "40.00",
                 "effective_on": "2026-03-12", "entry_allocations": [self._alloc(anchor, "40.00")],
                 "opening_balance": False, "expense_category": None, "reason": "After close"},
            )

    def test_reopen_requires_a_prior_close(self) -> None:
        debt = self._settled_payable()
        with self.assertRaisesRegex(FinCLError, "not closed"):
            self.record("PERSONAL_DEBT_REOPEN", {"debt": debt},
                        {"effective_on": "2026-03-11", "reason": "Nothing to reopen"})

    def test_reopen_then_a_later_event_is_accepted(self) -> None:
        debt = self._settled_payable()
        self.record("PERSONAL_DEBT_CLOSE", {"debt": debt},
                    {"effective_on": "2026-03-10", "reason": "Settled"})
        self.record("PERSONAL_DEBT_REOPEN", {"debt": debt},
                    {"effective_on": "2026-03-11", "reason": "Borrowed again"})
        row = self.rows("personal_debts")[0]
        self.assertEqual(row["status"], "OPEN")
        self.assertIsNone(row["closed_on"])
        anchor = self.anchor_for(coefficient=200000, direction="IN")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "40.00",
             "effective_on": "2026-03-12", "entry_allocations": [self._alloc(anchor, "40.00")],
             "opening_balance": False, "expense_category": None, "reason": "Borrowed again"},
        )
        events = self.rows("personal_debt_events")
        running = sum(int(e["signed"]) * int(e["principal_coefficient"]) for e in events)
        self.assertEqual(running, 4000)

    def test_rebuild_reproduces_closed_status(self) -> None:
        debt = self._settled_payable()
        self.record("PERSONAL_DEBT_CLOSE", {"debt": debt},
                    {"effective_on": "2026-03-10", "reason": "Settled"})
        before = [tuple(r) for r in self.rows("personal_debts")]
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DELETE FROM personal_debts")
            connection.commit()
            replay_journal(connection)
            connection.commit()
        finally:
            connection.close()
        self.assertEqual([tuple(r) for r in self.rows("personal_debts")], before)


class CoupledCorrectionTests(Slice2Base):
    def test_retiring_a_repayment_that_backs_a_closure_is_refused_alone(self) -> None:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "100.00",
             "effective_on": "2026-03-02", "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance"},
        )
        anchor = self.anchor_for(coefficient=11000, direction="OUT")
        repay = str(uuid.uuid4())
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": repay},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-09",
             "entry_allocations": [self._alloc(anchor, "100.00")],
             "opening_balance": False, "expense_category": None, "reason": "Settled"},
        )
        self.record("PERSONAL_DEBT_CLOSE", {"debt": debt},
                    {"effective_on": "2026-03-10", "reason": "Settled"})
        with self.assertRaises(FinCLError):
            self.record("PERSONAL_DEBT_EVENT_RETIRE", {"debt": debt, "event": repay},
                        {"reason": "Recorded against the wrong debt"})

    def test_a_coupled_batch_reopens_corrects_and_recloses(self) -> None:
        debt = self.open_debt("PAYABLE")
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "100.00",
             "effective_on": "2026-03-02", "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance"},
        )
        anchor = self.anchor_for(coefficient=11000, direction="OUT")
        repay = str(uuid.uuid4())
        self.record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt, "event": repay},
            {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
             "principal_amount": "100.00", "effective_on": "2026-03-09",
             "entry_allocations": [self._alloc(anchor, "100.00")],
             "opening_balance": False, "expense_category": None, "reason": "Settled"},
        )
        self.record("PERSONAL_DEBT_CLOSE", {"debt": debt},
                    {"effective_on": "2026-03-10", "reason": "Settled"})

        partial = self.anchor_for(coefficient=4000, direction="OUT")
        items = [
            {"kind": "PERSONAL_DEBT_REOPEN", "anchors": {"debt": debt},
             "payload": {"effective_on": "2026-03-09", "reason": "Repayment was wrong"}},
            {"kind": "PERSONAL_DEBT_EVENT_RETIRE", "anchors": {"debt": debt, "event": repay},
             "payload": {"reason": "Wrong amount"}},
            {"kind": "PERSONAL_DEBT_EVENT_SET", "anchors": {"debt": debt, "event": str(uuid.uuid4())},
             "payload": {"event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE",
                         "principal_amount": "40.00", "effective_on": "2026-03-09",
                         "entry_allocations": [self._alloc(partial, "40.00")],
                         "opening_balance": False, "expense_category": None,
                         "reason": "Corrected: only 40 was repaid"}},
        ]
        with self.assertRaises(FinCLError):
            # 100 borrowed - 40 repaid = 60 outstanding, so the debt cannot stay
            # closed; the batch leaves it reopened.
            record_batch(
                self.database, request_id=str(uuid.uuid4()),
                intent_hash=_intent("DECISION_BATCH", {}, {"items": items}),
                items=[*items, {"kind": "PERSONAL_DEBT_CLOSE", "anchors": {"debt": debt},
                                "payload": {"effective_on": "2026-03-10", "reason": "reclose"}}],
                reason="Reclose after a 40 correction",
            )
        record_batch(
            self.database, request_id=str(uuid.uuid4()),
            intent_hash=_intent("DECISION_BATCH", {}, {"items": items}),
            items=items, reason="Reopen and correct the repayment down to 40",
        )
        row = self.rows("personal_debts")[0]
        self.assertEqual(row["status"], "OPEN")
        events = self.rows("personal_debt_events")
        running = sum(int(e["signed"]) * int(e["principal_coefficient"]) for e in events)
        self.assertEqual(running, 6000)
