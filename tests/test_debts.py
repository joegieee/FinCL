"""v0.3 slice 1: debt identity, explicit opening balances, list/detail reads."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
import uuid
from pathlib import Path

from typer.testing import CliRunner

from fincl.application import record_debt_decision
from fincl.cli import app
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_api import ApiTestBase


def _intent(kind: str, anchors: dict, payload: dict) -> str:
    return hashlib.sha256(f"{kind}{anchors}{payload}".encode()).hexdigest()


class DebtJournalTests(ApiTestBase):
    def _record(self, kind: str, anchors: dict, payload: dict):
        return record_debt_decision(
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=_intent(kind, anchors, payload),
            decision_kind=kind,
            anchors=anchors,
            payload=payload,
        )

    def _open_payable(self, *, opened_on="2026-01-05", due_on=None):
        debt_id = str(uuid.uuid4())
        self._record(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {
                "direction": "PAYABLE",
                "currency": "SGD",
                "counterparty_label": "Alex",
                "opened_on": opened_on,
                "due_on": due_on,
                "reason": "Split a holiday flat, settling later",
            },
        )
        return debt_id

    def _rows(self, table: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            connection.close()

    def test_open_and_opening_balance_project(self) -> None:
        debt_id = self._open_payable()
        event_id = str(uuid.uuid4())
        self._record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt_id, "event": event_id},
            {
                "event_kind": "BORROWED",
                "direction": "PAYABLE",
                "principal_amount": "400.00",
                "effective_on": "2026-01-05",
                "entry_allocations": [],
                "opening_balance": True,
                "expense_category": None,
                "reason": "Opening balance owed to Alex, funded before tracking",
            },
        )
        debts = self._rows("personal_debts")
        self.assertEqual(len(debts), 1)
        self.assertEqual(debts[0]["direction"], "PAYABLE")
        self.assertEqual(debts[0]["status"], "OPEN")
        events = self._rows("personal_debt_events")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["principal_coefficient"], 40000)
        self.assertEqual(events[0]["signed"], 1)
        self.assertEqual(events[0]["opening_balance"], 1)
        self.assertEqual(events[0]["bound"], 1)

    def test_details_set_updates_label_and_due_date(self) -> None:
        debt_id = self._open_payable()
        self._record(
            "PERSONAL_DEBT_DETAILS_SET",
            {"debt": debt_id},
            {"counterparty_label": "Alex Tan", "due_on": "2026-03-01",
             "reason": "Full name and agreed due date"},
        )
        debt = self._rows("personal_debts")[0]
        self.assertEqual(debt["counterparty_label"], "Alex Tan")
        self.assertEqual(debt["due_on"], "2026-03-01")
        self.assertIsNotNone(debt["details_seq"])

    def test_event_retire_removes_the_row(self) -> None:
        debt_id = self._open_payable()
        event_id = str(uuid.uuid4())
        payload = {
            "event_kind": "BORROWED", "direction": "PAYABLE",
            "principal_amount": "100.00", "effective_on": "2026-01-06",
            "entry_allocations": [], "opening_balance": True,
            "expense_category": None, "reason": "Opening balance, later corrected",
        }
        self._record("PERSONAL_DEBT_EVENT_SET", {"debt": debt_id, "event": event_id}, payload)
        self._record(
            "PERSONAL_DEBT_EVENT_RETIRE",
            {"debt": debt_id, "event": event_id},
            {"reason": "Recorded against the wrong debt"},
        )
        self.assertEqual(self._rows("personal_debt_events"), [])

    def test_direction_mismatch_is_refused(self) -> None:
        debt_id = self._open_payable()
        with self.assertRaises(FinCLError):
            self._record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt_id, "event": str(uuid.uuid4())},
                {
                    "event_kind": "LENT", "direction": "RECEIVABLE",
                    "principal_amount": "10.00", "effective_on": "2026-01-06",
                    "entry_allocations": [], "opening_balance": True,
                    "expense_category": None, "reason": "Wrong direction on purpose",
                },
            )

    def test_due_date_before_open_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._open_payable(opened_on="2026-02-01", due_on="2026-01-01")

    def test_a_later_due_date_before_open_is_refused_too(self) -> None:
        debt_id = self._open_payable(opened_on="2026-02-01")
        with self.assertRaisesRegex(FinCLError, "may not precede"):
            self._record(
                "PERSONAL_DEBT_DETAILS_SET",
                {"debt": debt_id},
                {"counterparty_label": "Alex", "due_on": "2026-01-15",
                 "reason": "A due date before the loan existed"},
            )
        self.assertIsNone(self._rows("personal_debts")[0]["due_on"])

    def test_event_before_open_is_refused(self) -> None:
        debt_id = self._open_payable(opened_on="2026-02-01")
        with self.assertRaises(FinCLError):
            self._record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt_id, "event": str(uuid.uuid4())},
                {
                    "event_kind": "BORROWED", "direction": "PAYABLE",
                    "principal_amount": "10.00", "effective_on": "2026-01-15",
                    "entry_allocations": [], "opening_balance": True,
                    "expense_category": None, "reason": "Dated before the debt opened",
                },
            )

    def test_source_linked_event_with_unresolved_anchor_is_dangling(self) -> None:
        # §3: a dangling anchor keeps the intended debt event with unverified
        # status; it is not refused.
        debt_id = self._open_payable()
        event_id = str(uuid.uuid4())
        self._record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt_id, "event": event_id},
            {
                "event_kind": "BORROWED", "direction": "PAYABLE",
                "principal_amount": "10.00", "effective_on": "2026-01-06",
                "entry_allocations": [
                    {"entry_anchor": "no-such-anchor", "principal_amount": "10.00",
                     "interest_amount": "0.00", "fee_amount": "0.00",
                     "separate_fee_amount": "0.00"}
                ],
                "opening_balance": False,
                "expense_category": None, "reason": "Source-linked, source not yet imported",
            },
        )
        event = self._rows("personal_debt_events")[0]
        self.assertEqual(event["bound"], 0)
        self.assertIsNotNone(event["dangling_reason"])
        allocation = self._rows("personal_debt_allocations")[0]
        self.assertEqual(allocation["bound"], 0)
        self.assertIsNone(allocation["entry_id"])

    def test_allocation_principals_must_sum_to_event_principal(self) -> None:
        debt_id = self._open_payable()
        with self.assertRaisesRegex(FinCLError, "sum to the event principal"):
            self._record(
                "PERSONAL_DEBT_EVENT_SET",
                {"debt": debt_id, "event": str(uuid.uuid4())},
                {
                    "event_kind": "BORROWED", "direction": "PAYABLE",
                    "principal_amount": "10.00", "effective_on": "2026-01-06",
                    "entry_allocations": [
                        {"entry_anchor": "a", "principal_amount": "7.00",
                         "interest_amount": "0.00", "fee_amount": "0.00",
                         "separate_fee_amount": "0.00"}
                    ],
                    "opening_balance": False,
                    "expense_category": None, "reason": "Principals do not add up",
                },
            )

    def test_opening_a_debt_twice_is_refused(self) -> None:
        debt_id = self._open_payable()
        with self.assertRaises(FinCLError):
            self._record(
                "PERSONAL_DEBT_OPEN",
                {"debt": debt_id},
                {
                    "direction": "PAYABLE", "currency": "SGD",
                    "counterparty_label": "Alex", "opened_on": "2026-01-05",
                    "due_on": None, "reason": "Duplicate open",
                },
            )

    def test_projection_is_stable_across_a_no_op_refresh(self) -> None:
        debt_id = self._open_payable()
        self._record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt_id, "event": str(uuid.uuid4())},
            {
                "event_kind": "BORROWED", "direction": "PAYABLE",
                "principal_amount": "250.00", "effective_on": "2026-01-05",
                "entry_allocations": [], "opening_balance": True,
                "expense_category": None, "reason": "Opening balance",
            },
        )
        before = [tuple(r) for r in self._rows("personal_debt_events")]
        refresh_ledger(self.database)
        after = [tuple(r) for r in self._rows("personal_debt_events")]
        self.assertEqual(before, after)

    def test_rebuild_from_the_journal_reproduces_the_projection(self) -> None:
        debt_id = self._open_payable()
        self._record(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt_id, "event": str(uuid.uuid4())},
            {
                "event_kind": "BORROWED", "direction": "PAYABLE",
                "principal_amount": "77.00", "effective_on": "2026-01-05",
                "entry_allocations": [], "opening_balance": True,
                "expense_category": None, "reason": "Opening balance",
            },
        )
        before = [tuple(r) for r in self._rows("personal_debts")]
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DELETE FROM personal_debts")
            connection.commit()
            replay_journal(connection)
            connection.commit()
        finally:
            connection.close()
        self.assertEqual([tuple(r) for r in self._rows("personal_debts")], before)


class DebtApiTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()

    def _command(self, kind: str, anchors: dict, payload: dict) -> dict:
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "kind": kind,
            "anchors": anchors,
            "payload": payload,
        }
        preview = self.post("/api/v1/preview", body, self.csrf)
        self.assertEqual(preview.status_code, 200, preview.text)
        commit = self.post("/api/v1/commands", body, self.csrf)
        self.assertIn(commit.status_code, {200, 202}, commit.text)
        return commit.json()

    def test_open_via_api_then_list_splits_payable_and_receivable(self) -> None:
        payable = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": payable},
            {"direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Sam",
             "opened_on": "2026-01-03", "due_on": None, "reason": "I owe Sam for tickets"},
        )
        self._command(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": payable, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "60.00",
             "effective_on": "2026-01-03", "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance owed to Sam"},
        )
        receivable = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": receivable},
            {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "Jo",
             "opened_on": "2026-01-04", "due_on": "2026-02-04", "reason": "Jo owes me"},
        )

        page = self.client.get("/api/v1/debts").json()
        self.assertEqual(len(page["i_owe"]), 1)
        self.assertEqual(len(page["owed_to_me"]), 1)
        self.assertEqual(page["i_owe"][0]["remaining_principal"]["value"], "60.00")
        self.assertEqual(page["i_owe"][0]["reconciliation_status"], "opening_balance_only")
        self.assertEqual(page["owed_to_me"][0]["remaining_principal"]["value"], "0")

    def test_detail_returns_principal_history(self) -> None:
        debt_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Kim",
             "opened_on": "2026-01-02", "due_on": None, "reason": "Owe Kim"},
        )
        self._command(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt_id, "event": str(uuid.uuid4())},
            {"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "12.50",
             "effective_on": "2026-01-02", "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance"},
        )
        detail = self.client.get(f"/api/v1/debts/{debt_id}").json()
        self.assertEqual(detail["debt"]["remaining_principal"]["value"], "12.50")
        self.assertEqual(len(detail["debt"]["principal_history"]), 1)
        self.assertTrue(detail["debt"]["principal_history"][0]["opening_balance"])

    def test_events_page_lists_a_debts_events(self) -> None:
        debt_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Kim",
             "opened_on": "2026-01-02", "due_on": None, "reason": "Owe Kim"},
        )
        event_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt_id, "event": event_id},
            {"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "12.50",
             "effective_on": "2026-01-02", "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance"},
        )
        response = self.client.get(f"/api/v1/debts/{debt_id}/events")
        self.assertEqual(response.status_code, 200, response.text)
        page = response.json()
        self.assertEqual([event["event_id"] for event in page["events"]], [event_id])
        self.assertEqual(page["events"][0]["principal"], {"value": "12.50", "currency": None})
        self.assertIsNone(page["next_cursor"])

    def test_missing_debt_is_404(self) -> None:
        self.assertEqual(self.client.get(f"/api/v1/debts/{uuid.uuid4()}").status_code, 404)

    def test_stale_generation_is_409(self) -> None:
        self.assertEqual(
            self.client.get("/api/v1/debts?generation=sha256:nope").status_code, 409
        )

    def _anchor(self, coefficient: int, direction: str) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT natural_anchor FROM ledger_entries "
                "WHERE abs(amount_coefficient) = ? AND economic_direction = ?",
                (coefficient, direction),
            ).fetchone()[0]
        finally:
            connection.close()

    def test_allocation_candidates_scoped_to_currency_and_direction(self) -> None:
        debt_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "Ana",
             "opened_on": "2026-01-01", "due_on": None, "reason": "Ana owes me"},
        )
        page = self.client.get(f"/api/v1/debts/allocation-candidates?debt={debt_id}").json()
        self.assertEqual(page["required_direction"], "OUT")
        self.assertEqual(page["event_kind"], "LENT")
        got = {c["entry_anchor"]: c for c in page["candidates"]}
        self.assertEqual(len(got), 3)  # the three SGD OUT entries, no inflow
        for candidate in got.values():
            self.assertEqual(candidate["open_amount"], candidate["amount"])
        # newest transaction first, not an arbitrary anchor-hash order
        dates = [c["effective_at"][:10] for c in page["candidates"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_allocation_candidates_search_narrows_by_description(self) -> None:
        debt_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "Ana",
             "opened_on": "2026-01-01", "due_on": None, "reason": "Ana owes me"},
        )
        page = self.client.get(f"/api/v1/debts/allocation-candidates?debt={debt_id}&search=book").json()
        self.assertEqual([c["description"] for c in page["candidates"]], ["Bookshop"])

    def test_allocation_candidates_cursor_reaches_entries_past_the_first_page(self) -> None:
        debt_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "Ana",
             "opened_on": "2026-01-01", "due_on": None, "reason": "Ana owes me"},
        )
        first = self.client.get(f"/api/v1/debts/allocation-candidates?debt={debt_id}&limit=1").json()
        self.assertEqual(len(first["candidates"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        cursor = urllib.parse.quote(first["next_cursor"], safe="")
        response = self.client.get(
            f"/api/v1/debts/allocation-candidates?debt={debt_id}&limit=1&cursor={cursor}"
        )
        second = response.json()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(second["candidates"]), 1)
        self.assertNotEqual(
            first["candidates"][0]["entry_anchor"], second["candidates"][0]["entry_anchor"],
        )

    def test_source_linked_command_removes_the_fully_consumed_candidate(self) -> None:
        debt_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "Ana",
             "opened_on": "2026-01-01", "due_on": None, "reason": "Ana owes me"},
        )
        anchor = self._anchor(1900, "OUT")
        self._command(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt_id, "event": str(uuid.uuid4())},
            {"event_kind": "LENT", "direction": "RECEIVABLE", "principal_amount": "19.00",
             "effective_on": "2026-01-03", "opening_balance": False, "expense_category": None,
             "entry_allocations": [{"entry_anchor": anchor, "principal_amount": "19.00",
                                    "interest_amount": "0.00", "fee_amount": "0.00",
                                    "separate_fee_amount": "0.00"}],
             "reason": "Lent Ana the price of the book"},
        )
        page = self.client.get(f"/api/v1/debts/allocation-candidates?debt={debt_id}").json()
        # fully allocated elsewhere: no longer offered as a source
        self.assertTrue(all(c["entry_anchor"] != anchor for c in page["candidates"]))
        detail = self.client.get(f"/api/v1/debts/{debt_id}").json()
        self.assertEqual(detail["debt"]["remaining_principal"]["value"], "19.00")

    def test_chronology_violation_is_refused_without_appending(self) -> None:
        debt_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Bo",
             "opened_on": "2026-01-10", "due_on": None, "reason": "Owe Bo"},
        )
        before = self.journal_len()
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "kind": "PERSONAL_DEBT_EVENT_SET",
            "anchors": {"debt": debt_id, "event": str(uuid.uuid4())},
            "payload": {"event_kind": "BORROWED", "direction": "PAYABLE",
                        "principal_amount": "5.00", "effective_on": "2026-01-05",
                        "entry_allocations": [], "opening_balance": True,
                        "expense_category": None, "reason": "Dated before the debt opened"},
        }
        self.assertEqual(self.post("/api/v1/preview", body, self.csrf).status_code, 422)
        self.assertEqual(self.post("/api/v1/commands", body, self.csrf).status_code, 422)
        self.assertEqual(self.journal_len(), before)

    def test_as_of_before_the_opening_event_reads_zero(self) -> None:
        debt_id = str(uuid.uuid4())
        self._command(
            "PERSONAL_DEBT_OPEN",
            {"debt": debt_id},
            {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "Lee",
             "opened_on": "2026-01-10", "due_on": None, "reason": "Lee owes me"},
        )
        self._command(
            "PERSONAL_DEBT_EVENT_SET",
            {"debt": debt_id, "event": str(uuid.uuid4())},
            {"event_kind": "LENT", "direction": "RECEIVABLE", "principal_amount": "200.00",
             "effective_on": "2026-01-10", "entry_allocations": [], "opening_balance": True,
             "expense_category": None, "reason": "Opening balance lent to Lee"},
        )
        early = self.client.get(f"/api/v1/debts/{debt_id}?as_of=2026-01-01").json()
        self.assertEqual(early["debt"]["remaining_principal"]["value"], "0.00")
        now = self.client.get(f"/api/v1/debts/{debt_id}?as_of=2026-01-31").json()
        self.assertEqual(now["debt"]["remaining_principal"]["value"], "200.00")


class DebtCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "ledger" / "fincl.sqlite3"
        self.database.parent.mkdir(parents=True)
        self.evidence = self.database.parent / "evidence"
        source = self.root / "revolut.csv"
        source.write_bytes(csv_bytes([
            ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
             "Coffee", "-4.00", "0.00", "SGD", "COMPLETED", "96.00"],
        ]))
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(source)
        refresh_ledger(self.database)
        self.runner = CliRunner()

    def _run(self, *args: str):
        return self.runner.invoke(app, [*args, "--database", str(self.database)])

    def test_open_add_opening_balance_and_read_back(self) -> None:
        opened = self._run(
            "debt", "open", "--direction", "receivable", "--currency", "SGD",
            "--counterparty", "Priya", "--opened-on", "2026-01-04",
            "--reason", "Lent for a deposit", "--json",
        )
        self.assertEqual(opened.exit_code, 0, opened.output)
        debt_id = json.loads(opened.output)["debt"]

        event = self._run(
            "debt", "add-event", "--debt", debt_id, "--kind", "LENT",
            "--direction", "receivable", "--principal", "500.00",
            "--effective-on", "2026-01-04", "--opening-balance",
            "--reason", "Opening balance lent to Priya", "--json",
        )
        self.assertEqual(event.exit_code, 0, event.output)

        listed = self._run("debt", "list", "--as-of", "2026-01-31", "--json")
        self.assertEqual(listed.exit_code, 0, listed.output)
        page = json.loads(listed.output)
        self.assertEqual(page["i_owe"], [])
        self.assertEqual(len(page["owed_to_me"]), 1)
        self.assertEqual(page["owed_to_me"][0]["remaining_principal"]["value"], "500.00")

        shown = self._run("debt", "show", "--debt", debt_id, "--as-of", "2026-01-31", "--json")
        self.assertEqual(shown.exit_code, 0, shown.output)
        detail = json.loads(shown.output)
        self.assertEqual(len(detail["debt"]["principal_history"]), 1)
        self.assertTrue(detail["debt"]["principal_history"][0]["opening_balance"])

    def test_source_linked_cash_event_needs_an_allocation(self) -> None:
        opened = self._run(
            "debt", "open", "--direction", "payable", "--currency", "SGD",
            "--counterparty", "Dana", "--opened-on", "2026-01-02",
            "--reason", "Owe Dana", "--json",
        )
        debt_id = json.loads(opened.output)["debt"]
        result = self._run(
            "debt", "add-event", "--debt", debt_id, "--kind", "BORROWED",
            "--direction", "payable", "--principal", "20.00",
            "--effective-on", "2026-01-03", "--reason", "Cash borrow, no source given",
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("entry allocation", result.output)

    def _coffee_anchor(self) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT natural_anchor FROM ledger_entries WHERE economic_direction = 'OUT'"
            ).fetchone()[0]
        finally:
            connection.close()

    def test_shared_expense_split_via_allocation_and_residual(self) -> None:
        anchor = self._coffee_anchor()
        opened = self._run(
            "debt", "open", "--direction", "receivable", "--currency", "SGD",
            "--counterparty", "Sam", "--opened-on", "2026-01-01",
            "--reason", "Covered Sam's coffee", "--json",
        )
        debt_id = json.loads(opened.output)["debt"]
        event = self._run(
            "debt", "add-event", "--debt", debt_id, "--kind", "EXPENSE_PAID_FOR_OTHER",
            "--direction", "receivable", "--principal", "3.00",
            "--effective-on", "2026-01-02", "--allocation", f"{anchor}=3.00",
            "--reason", "Sam's share of the coffee", "--json",
        )
        self.assertEqual(event.exit_code, 0, event.output)
        residual = self._run(
            "debt", "residual-set", "--entry", anchor, "--expense", "1.00",
            "--category", "Food & Dining / Cafes & Drinks", "--reason", "My own coffee", "--json",
        )
        self.assertEqual(residual.exit_code, 0, residual.output)
        shown = json.loads(
            self._run("debt", "show", "--debt", debt_id, "--as-of", "2026-01-31", "--json").output
        )
        self.assertEqual(shown["debt"]["remaining_principal"]["value"], "3.00")
