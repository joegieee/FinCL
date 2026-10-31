"""Debt repayment plans: the waterfall, the forecast following the plan, and the
recurring commitment that becomes one."""

from __future__ import annotations

import sqlite3
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from fincl.db import connect_sqlite
from fincl.debts.schedule import schedule_state
from fincl.decisions import replay_journal
from fincl.errors import FinCLError
from fincl.forecast.engine import cash_timeline
from fincl.planning.sufficiency import sufficiency_page
from tests.test_api import ApiTestBase
from tests.test_debt_corrections import DebtCorrectionBase, _alloc
from tests.test_forecast_debts import _debt
from tests.test_forecast_sources import TODAY, base_events, decide, rent_ledger

CUTOFF = "2026-06-25"


def _rows(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"kind": "rows", "rows": [{"due_on": on, "amount": amount} for on, amount in pairs]}


def _plan(database: Path, debt: str, plan: dict[str, Any], *, effective_from: str = "2026-02-01") -> None:
    decide(database, "PERSONAL_DEBT_SCHEDULE_SET", {"debt": debt},
           {"effective_from": effective_from, "plan": plan, "reason": "agreed plan"})


def _repay(
    database: Path, debt: str, amount: str, on: str, *, kind: str = "PAYABLE_FORGIVEN", anchor: str | None = None
) -> None:
    """Reduce a payable by ``amount``: off the ledger (forgiveness) unless a bank
    payment ``anchor`` backs it."""
    decide(database, "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, {
        "event_kind": "PRINCIPAL_REPAID" if anchor else kind, "direction": "PAYABLE",
        "principal_amount": amount, "effective_on": on,
        "entry_allocations": [] if anchor is None else [{
            "entry_anchor": anchor, "principal_amount": amount, "interest_amount": "0.00",
            "fee_amount": "0.00", "separate_fee_amount": "0.00"}],
        "opening_balance": False, "expense_category": None, "reason": "paid",
    })


def _grocer(database: Path) -> str:
    connection = connect_sqlite(database)
    try:
        return str(connection.execute(
            "SELECT e.natural_anchor FROM ledger_entries e JOIN document_records d ON d.id = e.document_record_id "
            "WHERE d.description_raw = 'Corner Grocer' ORDER BY e.effective_at LIMIT 1").fetchone()[0])
    finally:
        connection.close()


def _state(database: Path, debt: str, as_of: str = CUTOFF) -> dict[str, Any]:
    connection = connect_sqlite(database)
    try:
        state = schedule_state(connection, debt, as_of=as_of)
    finally:
        connection.close()
    assert state is not None
    return state


def _states(state: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [(row["due_on"], row["remaining"], row["state"]) for row in state["rows"]]


class WaterfallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = rent_ledger()

    def _owing(self, amount: str = "1200.00", **kwargs: Any) -> str:
        return _debt(self.database, direction="PAYABLE", due_on=None, amount=amount, **kwargs)

    def test_repayments_pay_the_oldest_rows_first(self) -> None:
        debt = self._owing(label="Pat")
        _plan(self.database, debt, _rows(("2026-05-01", "400.00"), ("2026-06-01", "400.00"), ("2026-08-01", "400.00")))
        _repay(self.database, debt, "500.00", "2026-05-02")

        state = _state(self.database, debt)

        self.assertEqual(_states(state), [
            ("2026-05-01", "0.00", "paid"), ("2026-06-01", "300.00", "overdue"),
            ("2026-08-01", "400.00", "upcoming"),
        ])
        self.assertEqual(state["rows"][1]["paid"], "100.00")
        self.assertEqual((state["next_due"], state["overdue_count"]), ("2026-06-01", 1))

    def test_a_part_paid_row_that_is_not_due_yet_says_so(self) -> None:
        debt = self._owing(label="Quinn")
        _plan(self.database, debt, _rows(("2026-08-01", "600.00"), ("2026-09-01", "600.00")))
        _repay(self.database, debt, "100.00", "2026-03-01")

        self.assertEqual(_states(_state(self.database, debt))[0], ("2026-08-01", "500.00", "part_paid"))
        self.assertEqual(_states(_state(self.database, debt))[1], ("2026-09-01", "600.00", "upcoming"))

    def test_repayments_before_the_plan_started_do_not_pay_it(self) -> None:
        debt = self._owing(label="Robin")
        _repay(self.database, debt, "200.00", "2026-03-01")
        _plan(self.database, debt, _rows(("2026-08-01", "500.00"), ("2026-09-01", "500.00")), effective_from="2026-04-01")

        state = _state(self.database, debt)

        self.assertEqual([row["paid"] for row in state["rows"]], ["0.00", "0.00"])
        # What the plan asks is 1000.00 and 1000.00 is owed, so nothing is trimmed.
        self.assertEqual(state["unscheduled_remainder"], None)

    def test_rows_the_principal_no_longer_needs_are_trimmed_from_the_latest_backwards(self) -> None:
        debt = self._owing(label="Sasha")
        _repay(self.database, debt, "700.00", "2026-03-01")
        _plan(self.database, debt, _rows(("2026-08-01", "400.00"), ("2026-09-01", "400.00"), ("2026-10-01", "400.00")),
              effective_from="2026-04-01")

        state = _state(self.database, debt)

        self.assertEqual(_states(state), [
            ("2026-08-01", "400.00", "upcoming"), ("2026-09-01", "100.00", "upcoming"),
            ("2026-10-01", "0.00", "trimmed"),
        ])
        self.assertEqual(state["scheduled_remaining"], "500.00")

    def test_forgiveness_pays_rows_like_a_repayment(self) -> None:
        debt = self._owing(label="Tamar")
        _plan(self.database, debt, _rows(("2026-08-01", "600.00"), ("2026-09-01", "600.00")))
        _repay(self.database, debt, "600.00", "2026-05-01")

        self.assertEqual(_states(_state(self.database, debt)), [
            ("2026-08-01", "0.00", "paid"), ("2026-09-01", "600.00", "upcoming"),
        ])

    def test_new_borrowing_leaves_an_undated_remainder(self) -> None:
        debt = self._owing(label="Uma")
        _plan(self.database, debt, _rows(("2026-08-01", "600.00"), ("2026-09-01", "600.00")))
        decide(self.database, "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, {
            "event_kind": "EXPENSE_PAID_FOR_ME", "direction": "PAYABLE", "principal_amount": "150.00",
            "effective_on": "2026-05-01", "entry_allocations": [], "opening_balance": False,
            "expense_category": "Food & Dining / Groceries", "reason": "paid for me",
        })

        state = _state(self.database, debt)

        self.assertEqual(state["unscheduled_remainder"], {"amount": "150.00", "due_on": None, "overdue": False})
        self.assertEqual(state["scheduled_remaining"], "1200.00")

    def test_a_dated_remainder_falls_on_the_debts_own_due_date(self) -> None:
        debt = str(uuid.uuid4())
        decide(self.database, "PERSONAL_DEBT_OPEN", {"debt": debt}, {
            "direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Vic",
            "opened_on": "2026-02-01", "due_on": "2026-12-01", "reason": "loan",
        })
        decide(self.database, "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, {
            "event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "1000.00",
            "effective_on": "2026-02-01", "entry_allocations": [], "opening_balance": True,
            "expense_category": None, "reason": "opening",
        })
        _plan(self.database, debt, _rows(("2026-08-01", "400.00")))

        remainder = _state(self.database, debt)["unscheduled_remainder"]

        self.assertEqual(remainder, {"amount": "600.00", "due_on": "2026-12-01", "overdue": False})

    def test_a_series_expands_to_stored_rows_clamping_the_month_end(self) -> None:
        debt = self._owing(label="Wren")
        _plan(self.database, debt, {
            "kind": "series", "schedule": {"kind": "monthly", "day": 31, "anchor_on": "2027-01-31"},
            "amount": "300.00", "count": 3,
        }, effective_from="2026-02-01")

        self.assertEqual([row["due_on"] for row in _state(self.database, debt)["rows"]],
                         ["2027-01-31", "2027-02-28", "2027-03-31"])


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = rent_ledger()
        cls.debt = _debt(cls.database, direction="PAYABLE", due_on=None, amount="1000.00", label="Xan")

    def _refused(self, plan: dict[str, Any], *, effective_from: str = "2026-03-01", debt: str | None = None) -> str:
        from fincl.debts.preview import preview_debt
        from fincl.decisions import resolve_anchor
        connection = connect_sqlite(self.database)
        try:
            with self.assertRaises(FinCLError) as raised:
                preview_debt(connection, "PERSONAL_DEBT_SCHEDULE_SET", {"debt": debt or self.debt},
                             {"effective_from": effective_from, "plan": plan, "reason": "plan"},
                             resolve_anchor=lambda a: resolve_anchor(connection, a))
        finally:
            connection.close()
        return str(raised.exception)

    def test_a_plan_may_not_ask_for_more_than_is_owed(self) -> None:
        message = self._refused(_rows(("2026-08-01", "600.00"), ("2026-09-01", "500.00")))
        self.assertIn("asks for 100.00 more than is owed", message)

    def test_rows_must_be_in_date_order_and_positive(self) -> None:
        self.assertIn("date order", self._refused(_rows(("2026-09-01", "100.00"), ("2026-08-01", "100.00"))))
        self.assertIn("strictly positive", self._refused(_rows(("2026-09-01", "0.00"))))

    def test_a_plan_cannot_start_after_its_first_payment_or_before_the_debt(self) -> None:
        self.assertIn("after the first payment", self._refused(_rows(("2026-08-01", "100.00")), effective_from="2026-09-01"))
        self.assertIn("precede opened_on", self._refused(_rows(("2026-08-01", "100.00")), effective_from="2026-01-01"))

    def test_only_an_open_debt_takes_a_plan_and_a_retire_needs_one(self) -> None:
        from fincl.debts.preview import preview_debt
        from fincl.decisions import resolve_anchor
        connection = connect_sqlite(self.database)
        try:
            with self.assertRaisesRegex(FinCLError, "has no repayment plan"):
                preview_debt(connection, "PERSONAL_DEBT_SCHEDULE_RETIRE", {"debt": self.debt}, {"reason": "drop"},
                             resolve_anchor=lambda a: resolve_anchor(connection, a))
        finally:
            connection.close()
        closed = _debt(self.database, direction="PAYABLE", due_on=None, amount="10.00", label="Yu")
        _repay(self.database, closed, "10.00", "2026-03-01")
        decide(self.database, "PERSONAL_DEBT_CLOSE", {"debt": closed}, {"effective_on": "2026-03-01", "reason": "paid"})
        self.assertIn("only an open debt", self._refused(_rows(("2026-08-01", "1.00")), debt=closed))


class ForecastFollowsThePlanTests(unittest.TestCase):
    def test_the_plan_replaces_the_lump_sum_in_the_forecast_and_the_answer(self) -> None:
        database = rent_ledger()
        debt = _debt(database, direction="PAYABLE", due_on=None, amount="1200.00", label="Zed")
        before = sufficiency_page(database, today=TODAY)["combined_answer"]
        self.assertEqual(base_events(cash_timeline(database, today=TODAY), f"debt:{debt}"), [])
        self.assertEqual([t["detail"] for t in before["todos"] if t["key"] == "debt-dates"],
                         ["Without a date they are not in the answer: Zed."])

        _plan(database, debt, _rows(("2026-08-10", "400.00"), ("2026-09-10", "400.00"), ("2026-10-10", "400.00")))

        timeline = cash_timeline(database, today=TODAY)
        answer = sufficiency_page(database, today=TODAY)["combined_answer"]
        events = [e for e in timeline["by_currency"]["SGD"]["base"]["events"]["items"]
                  if e["source_id"].startswith(f"debt:{debt}")]
        self.assertEqual([(e["source_id"], e["on"], e["amount"]) for e in events], [
            (f"debt:{debt}:schedule:0", "2026-08-10", "400.00"),
            (f"debt:{debt}:schedule:1", "2026-09-10", "400.00"),
            (f"debt:{debt}:schedule:2", "2026-10-10", "400.00"),
        ])
        self.assertNotIn("debt-dates", [t["key"] for t in answer["todos"]])
        # The money that was left out of the answer is now on its dates.
        self.assertEqual(Decimal(before["base"]["closing"]) - Decimal(answer["base"]["closing"]), Decimal("1200.00"))

    def test_a_payment_moves_the_remaining_rows_not_the_whole_plan(self) -> None:
        database = rent_ledger()
        debt = _debt(database, direction="PAYABLE", due_on=None, amount="1200.00", label="Ada")
        _plan(database, debt, _rows(("2026-08-10", "400.00"), ("2026-09-10", "400.00"), ("2026-10-10", "400.00")))
        _repay(database, debt, "300.00", "2026-06-20", anchor=_grocer(database))
        _repay(database, debt, "100.00", "2026-06-20")

        events = base_events(cash_timeline(database, today=TODAY), f"debt:{debt}")

        self.assertEqual(events, [("2026-09-10", "400.00"), ("2026-10-10", "400.00")])

    def test_an_unpaid_payable_row_past_the_cutoff_is_kept_on_the_first_day_and_asked_about(self) -> None:
        database = rent_ledger()
        debt = _debt(database, direction="PAYABLE", due_on=None, amount="800.00", label="Bea")
        _plan(database, debt, _rows(("2026-06-01", "400.00"), ("2026-08-10", "400.00")))

        timeline = cash_timeline(database, today=TODAY)
        todos = sufficiency_page(database, today=TODAY)["combined_answer"]["todos"]

        cell = timeline["by_currency"]["SGD"]["base"]["events"]
        overdue = [e for e in cell["repositioned"] if e["source_id"] == f"debt:{debt}:schedule:0"]
        self.assertEqual([(e["on"], e["original_date"], e["amount"]) for e in overdue],
                         [("2026-06-26", "2026-06-01", "400.00")])
        self.assertIn((f"debt:{debt}:schedule:1", "2026-08-10"),
                      [(e["source_id"], e["on"]) for e in cell["items"]])
        [todo] = [t for t in todos if t["key"] == "debt-overdue"]
        self.assertEqual(todo["page"], "Debts")
        self.assertIn("Bea was due on 2026-06-01", todo["detail"])

    def test_an_unpaid_receivable_row_past_the_cutoff_is_upside_and_asked_about(self) -> None:
        database = rent_ledger()
        debt = _debt(database, direction="RECEIVABLE", due_on=None, amount="800.00", label="Cal")
        decide(database, "PERSONAL_DEBT_SCHEDULE_SET", {"debt": debt}, {
            "effective_from": "2026-02-01", "reason": "agreed",
            "plan": _rows(("2026-06-01", "400.00"), ("2026-08-10", "400.00")),
        })

        timeline = cash_timeline(database, today=TODAY)
        todos = sufficiency_page(database, today=TODAY)["combined_answer"]["todos"]

        cell = timeline["by_currency"]["SGD"]
        for case in ("base", "conservative"):
            self.assertEqual([e for e in cell[case]["events"]["items"] if e["source_id"].startswith(f"debt:{debt}")], [])
            self.assertEqual(cell[case]["events"]["repositioned"], [])
        self.assertEqual(sorted((e["source_id"], e["amount"]) for e in cell["possible_income_upside"]["events"]),
                         [(f"debt:{debt}:schedule:0", "400.00"), (f"debt:{debt}:schedule:1", "400.00")])
        [todo] = [t for t in todos if t["key"] == "receivable-overdue"]
        self.assertEqual(todo["page"], "Debts")

    def test_only_what_the_plan_leaves_undated_asks_for_a_date(self) -> None:
        database = rent_ledger()
        debt = _debt(database, direction="PAYABLE", due_on=None, amount="1000.00", label="Dov")
        _plan(database, debt, _rows(("2026-08-10", "700.00")))

        timeline = cash_timeline(database, today=TODAY)
        todos = sufficiency_page(database, today=TODAY)["combined_answer"]["todos"]

        self.assertEqual(base_events(timeline, f"debt:{debt}"), [("2026-08-10", "700.00")])
        self.assertEqual([t["detail"] for t in todos if t["key"] == "debt-dates"],
                         ["Without a date they are not in the answer: Dov."])
        obligation = next(o for o in _obligations(database) if o["obligation_id"] == f"debt:{debt}")
        self.assertEqual(obligation["unscheduled_remainder"]["amount"], "300.00")
        self.assertEqual(obligation["remaining_amount"]["value"], "1000.00")
        self.assertEqual(obligation["superseded_lump_sum"]["counted"], False)

    def test_a_retired_plan_returns_the_debt_to_a_lump_sum(self) -> None:
        database = rent_ledger()
        debt = _debt(database, direction="PAYABLE", due_on="2026-09-01", amount="500.00", label="Eli")
        _plan(database, debt, _rows(("2026-08-10", "250.00"), ("2026-10-10", "250.00")))
        self.assertEqual(len(base_events(cash_timeline(database, today=TODAY), f"debt:{debt}")), 2)

        decide(database, "PERSONAL_DEBT_SCHEDULE_RETIRE", {"debt": debt}, {"reason": "back to one payment"})

        self.assertEqual(base_events(cash_timeline(database, today=TODAY), f"debt:{debt}"), [("2026-09-01", "500.00")])


def _obligations(database: Path) -> list[dict[str, Any]]:
    from fincl.obligations.queries import obligations_page
    return obligations_page(database, as_of=CUTOFF, today=TODAY)["obligations"]


class RebuildTests(unittest.TestCase):
    def test_replaying_the_journal_reproduces_the_plan(self) -> None:
        database = rent_ledger()
        debt = _debt(database, direction="PAYABLE", due_on=None, amount="900.00", label="Fay")
        _plan(database, debt, _rows(("2026-08-10", "300.00"), ("2026-09-10", "600.00")))
        _repay(database, debt, "300.00", "2026-06-01")
        before = _state(database, debt)

        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("DELETE FROM personal_debt_schedule_rows")
            connection.execute("DELETE FROM personal_debt_schedules")
            connection.commit()
            replay_journal(connection)
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(_state(database, debt), before)


class DebtScheduleApiTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()
        self.debt = str(uuid.uuid4())
        self._command("PERSONAL_DEBT_OPEN", {"debt": self.debt}, {
            "direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Gil",
            "opened_on": "2026-01-02", "due_on": None, "reason": "loan"})
        self._command("PERSONAL_DEBT_EVENT_SET", {"debt": self.debt, "event": str(uuid.uuid4())}, {
            "event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "1000.00",
            "effective_on": "2026-01-02", "entry_allocations": [], "opening_balance": True,
            "expense_category": None, "reason": "opening"})

    def _command(self, kind: str, anchors: dict, payload: dict) -> None:
        response = self.post("/api/v1/commands", {
            "request_id": str(uuid.uuid4()), "expected_generation": self.overview()["generation"],
            "kind": kind, "anchors": anchors, "payload": payload}, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)

    def _draft(self, items: list[dict]) -> dict:
        return {"expected_generation": self.overview()["generation"], "items": items}

    def _series(self, count: int | None) -> dict:
        return {"kind": "PERSONAL_DEBT_SCHEDULE_SET", "anchors": {"debt": self.debt}, "payload": {
            "effective_from": "2026-02-01", "reason": "Pay Gil back monthly",
            "plan": {"kind": "series", "schedule": {"kind": "monthly", "day": 15, "anchor_on": "2030-08-15"},
                     "amount": "300.00", "count": count}}}

    def test_until_paid_completes_the_rows_and_the_last_one_carries_the_remainder(self) -> None:
        response = self.post("/api/v1/debts/preview", self._draft([self._series(None)]), self.csrf)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        [item] = body["items"]
        self.assertEqual(item["payload"]["plan"], _rows(
            ("2030-08-15", "300.00"), ("2030-09-15", "300.00"), ("2030-10-15", "300.00"), ("2030-11-15", "100.00")))
        [debt] = body["debts"]
        self.assertIsNone(debt["schedule"]["before"])
        after = debt["schedule"]["after"]
        self.assertEqual((after["scheduled_remaining"], after["remaining_principal"], after["unscheduled_remainder"]),
                         ("1000.00", "1000.00", None))

    def test_a_saved_plan_shows_on_the_debt_and_the_list(self) -> None:
        saved = self.post("/api/v1/debts/preview", self._draft([self._series(None)]), self.csrf).json()["items"]
        response = self.post("/api/v1/batch", {
            "request_id": str(uuid.uuid4()), "expected_generation": self.overview()["generation"],
            "reason": "Pay Gil back monthly", "items": saved}, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)

        detail = self.client.get(f"/api/v1/debts/{self.debt}").json()["debt"]
        listed = self.client.get("/api/v1/debts").json()["i_owe"][0]

        self.assertEqual([(r["due_on"], r["amount"], r["state"]) for r in detail["schedule"]["rows"]], [
            ("2030-08-15", "300.00", "upcoming"), ("2030-09-15", "300.00", "upcoming"),
            ("2030-10-15", "300.00", "upcoming"), ("2030-11-15", "100.00", "upcoming")])
        self.assertEqual(listed["plan"], {"next_due": "2030-08-15", "overdue_count": 0, "unscheduled_remainder": False})

        gone = self.post("/api/v1/debts/preview", self._draft([
            {"kind": "PERSONAL_DEBT_SCHEDULE_RETIRE", "anchors": {"debt": self.debt}, "payload": {"reason": "Not needed"}},
        ]), self.csrf).json()
        self.assertIsNone(gone["debts"][0]["schedule"]["after"])

    def test_a_plan_for_more_than_is_owed_is_refused_with_the_excess(self) -> None:
        response = self.post("/api/v1/debts/preview", self._draft([self._series(5)]), self.csrf)

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("asks for 500.00 more than is owed", response.json()["detail"])


class CommitmentBecomesPlanTests(unittest.TestCase):
    """A loan repaid through a confirmed monthly bill is counted twice until the
    bill's remaining dates become the debt's plan."""

    def setUp(self) -> None:
        self.database = rent_ledger()
        connection = connect_sqlite(self.database)
        try:
            self.landlord = [
                row["natural_anchor"] for row in connection.execute(
                    "SELECT e.natural_anchor FROM ledger_entries e "
                    "JOIN document_records d ON d.id = e.document_record_id "
                    "WHERE d.description_raw = 'Landlord' ORDER BY e.effective_at")]
        finally:
            connection.close()

    def _loan(self) -> str:
        debt = str(uuid.uuid4())
        decide(self.database, "PERSONAL_DEBT_OPEN", {"debt": debt}, {
            "direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Landlord loan",
            "opened_on": "2026-01-01", "due_on": None, "reason": "deposit loan"})
        decide(self.database, "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, {
            "event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "10800.00",
            "effective_on": "2026-01-01", "entry_allocations": [], "opening_balance": True,
            "expense_category": None, "reason": "opening"})
        for index, anchor in enumerate(self.landlord):
            decide(self.database, "PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, {
                "event_kind": "PRINCIPAL_REPAID", "direction": "PAYABLE", "principal_amount": "1200.00",
                "effective_on": f"2026-0{index + 1}-05",
                "entry_allocations": [{"entry_anchor": anchor, "principal_amount": "1200.00",
                                       "interest_amount": "0.00", "fee_amount": "0.00",
                                       "separate_fee_amount": "0.00"}],
                "opening_balance": False, "expense_category": None, "reason": "rent"})
        return debt

    def _draft(self, debt: str) -> list[dict[str, Any]]:
        from fincl.debts.schedule import schedule_from_commitment
        connection = connect_sqlite(self.database)
        try:
            return schedule_from_commitment(connection, debt, "rent", reason="Use rent as the plan")
        finally:
            connection.close()

    def _apply(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            decide(self.database, item["kind"], item["anchors"], item["payload"])

    def test_the_draft_is_the_remaining_dates_at_their_amounts_and_ends_the_commitment(self) -> None:
        debt = self._loan()
        decide(self.database, "RECURRING_OCCURRENCE_AMOUNT_SET", {"commitment": "rent", "scheduled_on": "2026-08-05"},
               {"amount": "900.00", "reason": "reduced month"})

        plan, ending = self._draft(debt)

        self.assertEqual(plan["payload"]["effective_from"], "2026-06-06")
        self.assertEqual([(r["due_on"], r["amount"]) for r in plan["payload"]["plan"]["rows"]], [
            ("2026-07-05", "1200.00"), ("2026-08-05", "900.00"), ("2026-09-05", "1200.00"), ("2026-10-05", "300.00")])
        self.assertEqual((ending["payload"]["status"], ending["payload"]["ended_on"]), ("ended", "2026-06-05"))

    def test_after_the_swap_the_money_is_counted_once_and_the_bound_history_stays_lifted(self) -> None:
        debt = self._loan()
        before = cash_timeline(self.database, today=TODAY)
        rent_before = [(e["on"], e["amount"]) for e in before["by_currency"]["SGD"]["base"]["events"]["items"]
                       if e["source_id"].startswith("recurring:rent")]
        residual_before = _residual(before)
        self.assertTrue(rent_before)

        self._apply(self._draft(debt))

        after = cash_timeline(self.database, today=TODAY)
        self.assertEqual([e for e in after["by_currency"]["SGD"]["base"]["events"]["items"]
                          if e["source_id"].startswith("recurring:rent")], [])
        self.assertEqual(base_events(after, f"debt:{debt}"), [
            ("2026-07-05", "1200.00"), ("2026-08-05", "1200.00"), ("2026-09-05", "1200.00")])
        # The six paid months are still bound to the (ended) commitment, so they
        # stay out of the routine residual exactly as before.
        self.assertEqual(_residual(after), residual_before)
        # Rent at 1200 a month is not in the routine allowance, before or after.
        self.assertLess(max(Decimal(amount) for _, amount in residual_before), Decimal("11"))
        todos = sufficiency_page(self.database, today=TODAY)["combined_answer"]["todos"]
        self.assertNotIn("debt-dates", [t["key"] for t in todos])


def _residual(timeline: dict[str, Any]) -> list[tuple[str, str]]:
    cell = timeline["by_currency"]["SGD"]["base"]
    return sorted(
        (str(item.get("label") or item.get("source_id")), str(item["amount"]))
        for item in cell["events"]["items"] if str(item["source_id"]).startswith("residual:")
    )


class CommitmentSwapApiTests(DebtCorrectionBase):
    def _owing_with_a_matching_bill(self) -> str:
        self.command("PERSONAL_DEBT_OPEN", {"debt": self.debt},
                     {"direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Alex",
                      "opened_on": "2026-01-01", "due_on": None, "reason": "Loan from Alex"})
        for item in (self.event(self.opening, "BORROWED", "100.00", "2026-01-01", opening=True),
                     self.event(self.repaid, "PRINCIPAL_REPAID", "19.00", "2026-01-03", [_alloc(self.bookshop, "19.00")])):
            self.command(item["kind"], item["anchors"], item["payload"])
        commitment = str(uuid.uuid4())
        self.command("RECURRING_COMMITMENT_SET", {"commitment": commitment}, {
            "name": "Alex repayment", "currency": "SGD", "merchant_key": "Bookshop", "account_ids": [],
            "seed_entry_anchors": [self.bookshop],
            "schedule": {"kind": "monthly", "day": 3, "anchor_on": "2026-01-03"},
            "expected_amount": "19.00", "amount_tolerance": "0.00", "status": "active", "ended_on": None,
            "reason": "Confirmed as a monthly bill"})
        return commitment

    def test_the_swap_is_one_previewed_batch_that_ends_the_bill(self) -> None:
        commitment = self._owing_with_a_matching_bill()
        self.assertEqual([c["commitment"] for c in self.detail()["overlapping_commitments"]], [commitment])
        generation = self.overview()["generation"]

        draft = self.client.get(f"/api/v1/debts/{self.debt}/commitment-schedule?commitment={commitment}&generation={generation}")
        self.assertEqual(draft.status_code, 200, draft.text)
        items = draft.json()["items"]
        self.assertEqual([item["kind"] for item in items], ["PERSONAL_DEBT_SCHEDULE_SET", "RECURRING_COMMITMENT_SET"])
        self.assertEqual([(r["due_on"], r["amount"]) for r in items[0]["payload"]["plan"]["rows"]], [
            ("2026-02-03", "19.00"), ("2026-03-03", "19.00"), ("2026-04-03", "19.00"),
            ("2026-05-03", "19.00"), ("2026-06-03", "5.00")])

        preview = self.post("/api/v1/debts/preview", {"expected_generation": generation, "items": items}, self.csrf)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["items"], items)
        self.assertEqual(preview.json()["debts"][0]["schedule"]["after"]["unscheduled_remainder"], None)

        saved = self.batch(items, reason="Use the bill as the repayment plan")
        self.assertIn(saved.status_code, {200, 202}, saved.text)
        detail = self.detail()
        self.assertEqual(detail["schedule"]["scheduled_remaining"], "81.00")
        self.assertEqual(detail["overlapping_commitments"], [])
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute(
                "SELECT status, ended_on FROM recurring_commitments WHERE commitment = ?", (commitment,)
            ).fetchone(), ("ended", "2026-01-03"))
        finally:
            connection.close()

    def test_a_bill_that_is_not_active_or_not_there_is_refused(self) -> None:
        commitment = self._owing_with_a_matching_bill()
        missing = self.client.get(f"/api/v1/debts/{self.debt}/commitment-schedule?commitment={uuid.uuid4()}")
        self.assertEqual(missing.status_code, 404, missing.text)
        unknown_debt = self.client.get(f"/api/v1/debts/{uuid.uuid4()}/commitment-schedule?commitment={commitment}")
        self.assertEqual(unknown_debt.status_code, 404, unknown_debt.text)

    def test_a_draft_may_only_end_a_commitment(self) -> None:
        commitment = self._owing_with_a_matching_bill()
        still_running = {"kind": "RECURRING_COMMITMENT_SET", "anchors": {"commitment": commitment}, "payload": {
            "name": "Alex repayment", "currency": "SGD", "merchant_key": "Bookshop", "account_ids": [],
            "seed_entry_anchors": [self.bookshop], "schedule": {"kind": "monthly", "day": 3, "anchor_on": "2026-01-03"},
            "expected_amount": "19.00", "amount_tolerance": "0.00", "status": "paused", "ended_on": None,
            "reason": "Pause it"}}
        response = self.post("/api/v1/debts/preview",
                             {"expected_generation": self.overview()["generation"], "items": [still_running]}, self.csrf)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("only end", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
