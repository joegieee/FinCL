"""Correcting a debt through ``/api/v1/batch``: a changed or removed event,
reopened and closed again around it, all or nothing.

Every child is held to the same checks as a single ``/api/v1/commands`` call,
against the state the earlier children leave behind.
"""

from __future__ import annotations

import sqlite3
import uuid

from tests.test_api import ApiTestBase


def _alloc(anchor: str, principal: str) -> dict:
    return {"entry_anchor": anchor, "principal_amount": principal, "interest_amount": "0.00",
            "fee_amount": "0.00", "separate_fee_amount": "0.00"}


class DebtCorrectionBase(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()
        connection = sqlite3.connect(self.database)
        try:
            anchors = dict(connection.execute(
                "SELECT amount_coefficient, natural_anchor FROM ledger_entries"
            ).fetchall())
        finally:
            connection.close()
        self.cafe, self.bookshop, self.grocer, self.seed = (
            anchors[-400], anchors[-1900], anchors[-5125], anchors[50000]
        )
        self.debt = str(uuid.uuid4())
        self.opening = str(uuid.uuid4())
        self.repaid = str(uuid.uuid4())

    def command(self, kind: str, anchors: dict, payload: dict):
        response = self.post("/api/v1/commands", {
            "request_id": str(uuid.uuid4()), "expected_generation": self.overview()["generation"],
            "kind": kind, "anchors": anchors, "payload": payload,
        }, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)

    def batch(self, items: list[dict], reason: str = "Correct the debt"):
        return self.post("/api/v1/batch", {
            "request_id": str(uuid.uuid4()), "expected_generation": self.overview()["generation"],
            "reason": reason, "items": items,
        }, self.csrf)

    def event(self, event: str, kind: str, amount: str, on: str, sources: list[dict] | None = None,
              *, opening: bool = False) -> dict:
        return {"kind": "PERSONAL_DEBT_EVENT_SET", "anchors": {"debt": self.debt, "event": event},
                "payload": {"event_kind": kind, "direction": "PAYABLE", "principal_amount": amount,
                            "effective_on": on, "entry_allocations": sources or [],
                            "opening_balance": opening, "expense_category": None, "reason": "Loan from Alex"}}

    def lifecycle(self, kind: str, on: str) -> dict:
        return {"kind": kind, "anchors": {"debt": self.debt}, "payload": {"effective_on": on, "reason": "Correction"}}

    def retire(self, event: str) -> dict:
        return {"kind": "PERSONAL_DEBT_EVENT_RETIRE", "anchors": {"debt": self.debt, "event": event},
                "payload": {"reason": "Recorded wrongly"}}

    def settled_and_closed(self) -> None:
        """Borrowed 19 before tracking, repaid it from the bookshop payment, closed."""
        self.command("PERSONAL_DEBT_OPEN", {"debt": self.debt},
                     {"direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Alex",
                      "opened_on": "2026-01-01", "due_on": None, "reason": "Loan from Alex"})
        for item in (self.event(self.opening, "BORROWED", "19.00", "2026-01-01", opening=True),
                     self.event(self.repaid, "PRINCIPAL_REPAID", "19.00", "2026-01-03", [_alloc(self.bookshop, "19.00")]),
                     self.lifecycle("PERSONAL_DEBT_CLOSE", "2026-01-04")):
            self.command(item["kind"], item["anchors"], item["payload"])

    def detail(self) -> dict:
        response = self.client.get(f"/api/v1/debts/{self.debt}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["debt"]


class CoupledCorrectionTests(DebtCorrectionBase):
    def test_a_closed_debt_is_reopened_corrected_and_closed_again_in_one_record(self) -> None:
        self.settled_and_closed()
        journal = self.journal_len()
        # The loan was 23, and the cafe payment repaid part of it.
        response = self.batch([
            self.lifecycle("PERSONAL_DEBT_REOPEN", "2026-01-04"),
            self.retire(self.opening),
            self.event(str(uuid.uuid4()), "BORROWED", "23.00", "2026-01-01", opening=True),
            self.event(str(uuid.uuid4()), "PRINCIPAL_REPAID", "4.00", "2026-01-02", [_alloc(self.cafe, "4.00")]),
            self.lifecycle("PERSONAL_DEBT_CLOSE", "2026-01-04"),
        ])
        self.assertIn(response.status_code, {200, 202}, response.text)
        self.assertEqual(self.journal_len(), journal + 1)
        debt = self.detail()
        self.assertEqual((debt["status"], debt["closed_on"]), ("CLOSED", "2026-01-04"))
        self.assertEqual([(e["event_kind"], e["principal"]["value"]) for e in debt["principal_history"]],
                         [("BORROWED", "23.00"), ("PRINCIPAL_REPAID", "4.00"), ("PRINCIPAL_REPAID", "19.00")])

    def test_a_correction_that_cannot_close_again_records_nothing(self) -> None:
        self.settled_and_closed()
        journal = self.journal_len()
        response = self.batch([
            self.lifecycle("PERSONAL_DEBT_REOPEN", "2026-01-04"),
            self.retire(self.opening),
            self.event(str(uuid.uuid4()), "BORROWED", "23.00", "2026-01-01", opening=True),
            self.lifecycle("PERSONAL_DEBT_CLOSE", "2026-01-04"),
        ])
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("not zero", response.json()["detail"])
        self.assertEqual(self.journal_len(), journal)
        self.assertEqual(self.detail()["status"], "CLOSED")

    def test_a_correction_may_leave_the_debt_open(self) -> None:
        self.settled_and_closed()
        response = self.batch([
            self.lifecycle("PERSONAL_DEBT_REOPEN", "2026-01-04"),
            self.retire(self.repaid),
        ])
        self.assertIn(response.status_code, {200, 202}, response.text)
        debt = self.detail()
        self.assertEqual((debt["status"], debt["remaining_principal"]["value"]), ("OPEN", "19.00"))

    def test_a_retire_must_name_an_event_that_is_there(self) -> None:
        self.settled_and_closed()
        journal = self.journal_len()
        for items in ([self.lifecycle("PERSONAL_DEBT_REOPEN", "2026-01-04"), self.retire(str(uuid.uuid4()))],
                      [self.lifecycle("PERSONAL_DEBT_REOPEN", "2026-01-04"), self.retire(self.opening),
                       self.retire(self.opening)]):
            with self.subTest(items=len(items)):
                response = self.batch(items)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("event does not exist", response.json()["detail"])
        self.assertEqual(self.journal_len(), journal)

    def test_an_owner_share_and_an_incoming_type_are_retired_only_where_recorded(self) -> None:
        journal = self.journal_len()
        for kind, anchor, expected in (("ENTRY_RESIDUAL_RETIRE", self.grocer, "no recorded owner share"),
                                       ("INBOUND_CLASSIFICATION_RETIRE", self.seed, "no recorded incoming type")):
            with self.subTest(kind=kind):
                response = self.batch([{"kind": kind, "anchors": {"entry": anchor}, "payload": {"reason": "Undo"}}])
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(expected, response.json()["detail"])
        self.assertEqual(self.journal_len(), journal)


class InboundClassificationRetireTests(DebtCorrectionBase):
    def classify_seed(self, classification: str) -> None:
        self.command("INBOUND_CLASSIFICATION", {"entry": self.seed},
                     {"classification": classification, "reason": "Classified on arrival"})

    def seed_entry(self) -> dict:
        response = self.client.get(f"/api/v1/transactions/entry?anchor={self.seed}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["entry"]

    def test_retiring_returns_the_inflow_to_its_default(self) -> None:
        self.classify_seed("income")
        self.assertEqual(self.seed_entry()["bucket"], "income")
        self.command("INBOUND_CLASSIFICATION_RETIRE", {"entry": self.seed}, {"reason": "It was not pay"})
        entry = self.seed_entry()
        self.assertIsNone(entry["classification"])
        self.assertNotEqual(entry["bucket"], "income")

    def test_a_loan_recorded_on_an_own_account_transfer_clears_it_in_the_same_record(self) -> None:
        """An own-account transfer outranks a debt, so the loan only counts once it is cleared."""
        self.classify_seed("self_transfer")
        self.command("PERSONAL_DEBT_OPEN", {"debt": self.debt},
                     {"direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Alex",
                      "opened_on": "2026-01-01", "due_on": None, "reason": "Loan from Alex"})
        response = self.batch([
            {"kind": "INBOUND_CLASSIFICATION_RETIRE", "anchors": {"entry": self.seed},
             "payload": {"reason": "Alex lent this, it is not my own money"}},
            self.event(str(uuid.uuid4()), "BORROWED", "500.00", "2026-01-01", [_alloc(self.seed, "500.00")]),
        ])
        self.assertIn(response.status_code, {200, 202}, response.text)
        entry = self.seed_entry()
        self.assertIsNone(entry["classification"])
        self.assertEqual(entry["bucket"], "debt")
