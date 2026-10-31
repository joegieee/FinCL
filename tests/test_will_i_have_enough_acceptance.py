"""One ledger, driven through the HTTP API, held to the "will I have enough" criteria together.

Each lane proves its own piece. This story checks that the pieces agree: a planned
bill, a debt plan, a trip budget and a recurring commitment all reach one forecast,
none of them counted twice, and the answer's to-dos send the owner somewhere that
can fix them.

The ledger is six months of rent, groceries and salary in one account, a one-off
purchase, a past trip, and a hotel paid for a trip that is still under way. The supported cash cutoff
is 2026-06-25 and the clock is pinned a month later, so the account is stale.
"""

from __future__ import annotations

import io
import re
import sqlite3
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fincl import clock
from fincl.api import create_app
from fincl.document_service import DocumentImportService
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient
from tests.revolut_pdf import revolut_pdf_bytes

_ORIGIN = "http://127.0.0.1:8765"
_ROOT = Path(__file__).resolve().parent.parent
_CUTOFF = "2026-06-25"
_HORIZON_END = "2026-12-25"
_TABS = set(re.search(r"const tabs=\[(.*?)\] as const", (_ROOT / "frontend/src/App.tsx").read_text()).group(1)
            .replace("'", "").split(","))
_READ_ONLY_PAGES = {
    "Overview", "Forecast", "Will I have enough", "Obligations", "Net worth", "Variance", "Baseline",
    "Analysis", "Cash Flow", "Money Owed", "Transactions", "Reports",
}


def _row(day: str, kind: str, merchant: str, amount: str, balance: Decimal) -> list[str]:
    stamp = f"{day} 10:00:00"
    return [kind, "Current", stamp, stamp, merchant, amount, "0.00", "SGD", "COMPLETED", f"{balance}"]


def _statement(
    purchases: list[tuple[str, str, str]], balance: Decimal
) -> tuple[list[list[str]], Decimal]:
    rows = []
    for day, merchant, amount in sorted(purchases):
        balance += Decimal(amount)
        rows.append(_row(day, "Card Payment" if amount.startswith("-") else "Topup", merchant, amount, balance))
    return rows, balance


def _history() -> tuple[list[list[str]], Decimal]:
    purchases = []
    for month in range(1, 7):
        purchases += [
            (f"2026-0{month}-05", "Landlord", "-1200.00"),
            (f"2026-0{month}-12", "Corner Grocer", "-300.00"),
            (f"2026-0{month}-25", "Acme Payroll", "5000.00"),
        ]
    purchases += [
        ("2026-03-18", "Appliance Mart", "-600.00"),
        ("2026-04-10", "Island Resort", "-900.00"),
        ("2026-06-22", "Harbour Hotel", "-400.00"),
    ]
    seed = Decimal("50000.00")
    rows, balance = _statement(purchases, seed)
    return [_row("2026-01-01", "Topup", "Seed", "50000.00", seed), *rows], balance


class WillIHaveEnoughAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.database = root / "ledger.sqlite3"
        rows, self.balance = _history()
        source = root / "history.pdf"
        source.write_bytes(revolut_pdf_bytes(rows))
        DocumentImportService.plaintext_phase2(self.database, root / "evidence").import_revolut_pdf(source)
        refresh_ledger(self.database)
        app = create_app(self.database, port=8765, bootstrap_secret="test-secret", require_access_key=True,
                         evidence_dir=root / "evidence")
        self.client = ASGITestClient(app, base_url=_ORIGIN)
        self.addCleanup(self.client.close)
        response = self.client.post("/session", json={"secret": "test-secret"},
                                    headers={"origin": _ORIGIN, "content-type": "application/json"})
        self.csrf = response.json()["csrf"]
        clock.pin(date(2026, 7, 25))
        self.addCleanup(clock.pin, None)
        self.classify_the_history()

    # -- helpers -----------------------------------------------------------
    def headers(self) -> dict[str, str]:
        return {"origin": _ORIGIN, "content-type": "application/json", "x-csrf-token": self.csrf}

    def generation(self) -> str:
        return str(self.client.get("/api/v1/overview").json()["generation"])

    def command(self, kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
        response = self.client.post("/api/v1/commands", json={
            "request_id": str(uuid.uuid4()), "expected_generation": self.generation(),
            "kind": kind, "anchors": anchors, "payload": payload,
        }, headers=self.headers())
        self.assertIn(response.status_code, {200, 202}, response.text)

    def anchors(self, merchant: str) -> list[str]:
        connection = sqlite3.connect(self.database)
        try:
            return [str(row[0]) for row in connection.execute(
                "SELECT e.natural_anchor FROM ledger_entries e JOIN document_records d "
                "ON d.id = e.document_record_id WHERE d.description_raw = ? ORDER BY e.effective_at",
                (merchant,))]
        finally:
            connection.close()

    def classify_the_history(self) -> None:
        for anchor in self.anchors("Acme Payroll"):
            self.command("INBOUND_CLASSIFICATION", {"entry": anchor},
                         {"classification": "income", "reason": "payslip"})
        for merchant, category in (
            ("Landlord", "Housing / Rent"), ("Corner Grocer", "Food & Dining / Groceries"),
            ("Appliance Mart", "Shopping / Electronics"), ("Island Resort", "Travel / Accommodation"),
            ("Harbour Hotel", "Travel / Accommodation"),
        ):
            for anchor in self.anchors(merchant):
                self.command("CATEGORY_OVERRIDE", {"entry": anchor}, {"category": category, "reason": "receipt"})
        island = self.trip("Island week", "2026-04-08", "2026-04-14")
        self.command("ENTRY_TRIP_SET", {"entry": self.anchors("Island Resort")[0]},
                     {"trip": island, "reason": "stay"})

    def answer(self) -> dict[str, Any]:
        response = self.client.get("/api/v1/planning/sufficiency")
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json()["combined_answer"])

    def case(self, name: str = "base") -> dict[str, Any]:
        """One case of the Forecast tab. The answer calls the cautious one "cautious"; the tab "conservative"."""
        response = self.client.get("/api/v1/forecast")
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json()["by_currency"]["SGD"][name])

    def closing(self) -> tuple[Decimal, Decimal]:
        answer = self.answer()
        return Decimal(answer["base"]["closing"]), Decimal(answer["cautious"]["closing"])

    def events(self, prefix: str, name: str = "base") -> list[dict[str, Any]]:
        return [e for e in self.case(name)["events"]["items"] if e["source_id"].startswith(prefix)]

    def on_the_forecast_tab(self, source_id: str, on: str, name: str = "base") -> bool:
        days = {day["date"]: day["event_ids"] for day in self.case(name)["days"]}
        return source_id in days.get(on, [])

    def expense(self, month: str, name: str = "base") -> Decimal:
        return Decimal(next(m for m in self.case(name)["months"] if m["month"] == month)["expense"])

    def todos(self) -> dict[str, dict[str, Any]]:
        """The answer's to-dos by key. Wherever they are read, each must open a page that can fix it."""
        todos = {todo["key"]: todo for todo in self.answer()["todos"]}
        for todo in todos.values():
            self.assertIn(todo["page"], _TABS, todo)
            self.assertNotIn(todo["page"], _READ_ONLY_PAGES, todo)
            if todo.get("anchor"):
                stem = re.sub(r"-[0-9a-f]{8}-[0-9a-f-]{27}$", "", todo["anchor"])
                feature = _ROOT / "frontend/src/features" / todo["page"].lower()
                self.assertTrue(any(stem in path.read_text() for path in feature.glob("*.tsx")), todo)
        return todos

    def plan(self, name: str, direction: str, occurrences: list[tuple[str, str, str, str]], **changes: Any) -> str:
        flow = str(uuid.uuid4())
        self.command("PLANNED_FLOW_SET", {"flow": flow}, {
            "name": name, "direction": direction, "currency": "SGD", "account_id": None, "category": None,
            "occurrences": [{"key": key, "earliest": first, "latest": last, "amount": amount}
                            for key, first, last, amount in occurrences],
            "certainty": "sure", "baseline_treatment": "extra", "link": None, "reason": "known",
            **changes,
        })
        return flow

    def once(self, on: str, amount: str) -> list[tuple[str, str, str, str]]:
        return [("a", on, on, amount)]

    def open_debt(self, label: str, amount: str) -> str:
        debt = str(uuid.uuid4())
        self.command("PERSONAL_DEBT_OPEN", {"debt": debt}, {
            "direction": "PAYABLE", "currency": "SGD", "counterparty_label": label,
            "opened_on": "2026-02-01", "due_on": None, "reason": "borrowed"})
        self.command("PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": str(uuid.uuid4())}, {
            "event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": amount,
            "effective_on": "2026-02-01", "entry_allocations": [], "opening_balance": True,
            "expense_category": None, "reason": "opening balance"})
        return debt

    def trip(self, name: str, start: str, end: str) -> str:
        trip = str(uuid.uuid4())
        self.command("TRIP_SET", {"trip": trip}, {
            "name": name, "start_on": start, "end_on": end, "countries": [], "reason": "booked"})
        return trip

    # -- the story ---------------------------------------------------------
    def test_one_ledger_meets_the_criteria_together(self) -> None:
        self.a_commitment_with_linked_history_is_counted_once()
        self.future_money_moves_the_forecast_and_the_answer()
        self.planned_spending_is_declared_extra_or_taken_from_the_allowance()
        self.a_debt_is_forecast_by_its_repayment_schedule()
        self.a_trip_budget_gives_way_to_what_the_trip_spent()
        self.a_date_that_passes_is_matched_or_asked_about()
        self.every_todo_opens_a_page_that_can_fix_it()

    def a_commitment_with_linked_history_is_counted_once(self) -> None:
        rent = self.anchors("Landlord")
        self.command("RECURRING_COMMITMENT_SET", {"commitment": "rent"}, {
            "name": "Rent", "currency": "SGD", "merchant_key": "Landlord", "account_ids": [],
            "seed_entry_anchors": rent[:3], "schedule": {"kind": "monthly", "day": 5, "anchor_on": "2026-01-05"},
            "expected_amount": "1200.00", "amount_tolerance": "0.00", "status": "active", "ended_on": None,
            "reason": "lease"})
        unlinked = self.expense("2026-07")
        self.assertIn("recurring-history", self.todos())
        self.assertEqual(self.todos()["recurring-history"]["page"], "Planning")

        for index, anchor in enumerate(rent, start=1):
            self.command("RECURRING_OCCURRENCE_SET", {"commitment": "rent", "scheduled_on": f"2026-0{index}-05"},
                         {"entry": anchor, "reason": "statement"})

        self.assertNotIn("recurring-history", self.todos())
        # Rent 1200 once, groceries 300, and the allowances for one-off purchases (600 over
        # five months) and trips (900 over five months) that the history supports.
        self.assertEqual(self.expense("2026-07"), Decimal("1800.00"))
        self.assertEqual(unlinked - self.expense("2026-07"), Decimal("1200.00"))
        self.assertEqual([(e["source_id"], e["amount"]) for e in self.events("recurring:rent")][:2],
                         [("recurring:rent:2026-07-05", "1200.00"), ("recurring:rent:2026-08-05", "1200.00")])

        # The account's last statement is a month old: the answer says so and where to fix it.
        answer = self.answer()
        self.assertEqual((answer["answer"], answer["starts_on"], answer["ends_on"]), ("Yes", _CUTOFF, _HORIZON_END))
        [pocket] = answer["pockets"]
        self.assertEqual([(a["display_name"], a["evidence_through"], a["stale"]) for a in pocket["accounts"]],
                         [("Revolut Current", _CUTOFF, True)])
        stale = self.todos()["recent-statements"]
        self.assertEqual((stale["page"], stale["anchor"]), ("Setup", "setup-coverage"))
        self.assertIn("Revolut Current, proven through 2026-06-25", stale["detail"])

    def future_money_moves_the_forecast_and_the_answer(self) -> None:
        base, cautious = self.closing()

        dental = self.plan("Dental work", "outflow", self.once("2026-09-20", "2000.00"))
        self.assertEqual(self.closing(), (base - 2000, cautious - 2000))
        for case in ("base", "conservative"):
            self.assertTrue(self.on_the_forecast_tab(f"planned:{dental}:a", "2026-09-20", case))
        base, cautious = base - 2000, cautious - 2000

        course = self.plan("Course fees", "outflow", [
            ("a", "2026-08-10", "2026-08-10", "500.00"), ("b", "2026-09-10", "2026-09-10", "500.00"),
            ("c", "2026-10-10", "2026-10-10", "500.00")])
        self.assertEqual(self.closing(), (base - 1500, cautious - 1500))
        for key, on in (("a", "2026-08-10"), ("b", "2026-09-10"), ("c", "2026-10-10")):
            self.assertTrue(self.on_the_forecast_tab(f"planned:{course}:{key}", on))
        base, cautious = base - 1500, cautious - 1500

        before = self.answer()
        renovation = self.plan("Renovation", "outflow", self.once("2026-12-01", "80000.00"))
        short = self.answer()
        self.assertEqual((before["answer"], short["answer"]), ("Yes", "No"))
        # The cautious case counts no salary, so the purchase alone empties it; the base case
        # carries on a little longer on the salary it expects.
        self.assertEqual(short["cautious"]["first_short_on"], "2026-12-01")
        self.assertGreaterEqual(short["base"]["first_short_on"], "2026-12-01")
        self.assertIn("Planned spending", [row["label"] for row in short["where_it_goes"]])
        self.assertTrue(self.on_the_forecast_tab(f"planned:{renovation}:a", "2026-12-01"))

        payout = self.plan("Insurance payout", "inflow", self.once("2026-11-20", "20000.00"))
        rescued = self.answer()
        # The cautious case still counts no salary, so a sure payout rescues the base case only.
        self.assertEqual((rescued["answer"], rescued["base"]["verdict"], rescued["cautious"]["verdict"]),
                         ("Yes", "enough", "short"))
        self.assertTrue(rescued["only_safe_in_the_base_case"])
        self.assertEqual(Decimal(rescued["base"]["closing"]), base - 80000 + 20000)
        self.assertEqual(Decimal(rescued["cautious"]["closing"]), cautious - 80000 + 20000)
        self.assertEqual([(row["label"], row["next_on"], row["confidence"]) for row in rescued["income"]
                          if row["label"] == "Insurance payout"], [("Insurance payout", "2026-11-20", "confirmed")])
        for case in ("base", "conservative"):
            self.assertTrue(self.on_the_forecast_tab(f"planned:{payout}:a", "2026-11-20", case))

    def planned_spending_is_declared_extra_or_taken_from_the_allowance(self) -> None:
        # November already carries a 120 allowance for one-off purchases. A purchase declared
        # as coming out of it adds only what exceeds the allowance; an extra one adds all of it.
        november, december = self.expense("2026-11"), self.expense("2026-12")

        self.plan("Washing machine", "outflow", self.once("2026-11-12", "300.00"),
                  baseline_treatment="irregular_allowance")
        self.assertEqual(self.expense("2026-11") - november, Decimal("180.00"))

        self.plan("Wedding gift", "outflow", self.once("2026-12-12", "300.00"), baseline_treatment="extra")
        self.assertEqual(self.expense("2026-12") - december, Decimal("300.00"))

    def a_debt_is_forecast_by_its_repayment_schedule(self) -> None:
        pat = self.open_debt("Pat", "1200.00")
        quinn = self.open_debt("Quinn", "500.00")
        undated = self.todos()["debt-dates"]
        self.assertEqual((undated["page"], undated["detail"]),
                         ("Debts", "Without a date they are not in the answer: Pat, Quinn."))
        self.assertEqual(self.events(f"debt:{pat}"), [])
        base, cautious = self.closing()

        self.command("PERSONAL_DEBT_SCHEDULE_SET", {"debt": pat}, {
            "effective_from": "2026-02-01", "reason": "agreed monthly",
            "plan": {"kind": "rows", "rows": [{"due_on": "2026-08-10", "amount": "400.00"},
                                              {"due_on": "2026-09-10", "amount": "400.00"},
                                              {"due_on": "2026-10-10", "amount": "400.00"}]}})

        self.assertEqual([(e["source_id"], e["on"], e["amount"]) for e in self.events(f"debt:{pat}")], [
            (f"debt:{pat}:schedule:0", "2026-08-10", "400.00"),
            (f"debt:{pat}:schedule:1", "2026-09-10", "400.00"),
            (f"debt:{pat}:schedule:2", "2026-10-10", "400.00"),
        ])
        # The three rows are the 1200 owed, once, in both cases.
        self.assertEqual(self.closing(), (base - 1200, cautious - 1200))
        # Quinn still has neither a date nor a plan, and says so.
        self.assertEqual(self.todos()["debt-dates"]["detail"], "Without a date they are not in the answer: Quinn.")
        self.assertEqual(self.events(f"debt:{quinn}"), [])

    def a_trip_budget_gives_way_to_what_the_trip_spent(self) -> None:
        # The past trip gives every month a trip allowance of 180 (900 over five months).
        july = self.expense("2026-07")

        harbour = self.trip("Harbour stay", "2026-06-20", "2026-07-20")
        budget = self.plan("Harbour budget", "outflow", [("a", "2026-07-01", "2026-07-20", "1000.00")],
                           link={"kind": "trip", "id": harbour})
        # The budget replaces July's trip allowance rather than sitting on top of it.
        self.assertEqual(self.expense("2026-07") - july, Decimal("1000.00") - Decimal("180.00"))
        [planned] = self.events(f"planned:{budget}:a")
        self.assertEqual(planned["amount"], "1000.00")
        closing, _ = self.closing()

        # 400 of the stay was already paid before the statements end; once it is on the trip,
        # the budget has 600 left and the forecast carries only that.
        self.command("ENTRY_TRIP_SET", {"entry": self.anchors("Harbour Hotel")[0]},
                     {"trip": harbour, "reason": "stay"})

        [planned] = self.events(f"planned:{budget}:a")
        self.assertEqual(planned["amount"], "600.00")
        self.assertEqual(self.expense("2026-07") - july, Decimal("600.00") - Decimal("180.00"))
        self.assertEqual(self.closing()[0] - closing, Decimal("400.00"))
        [flow] = [f for f in self.client.get("/api/v1/planned").json()["flows"] if f["flow"] == budget]
        self.assertEqual((flow["planned"], flow["matched"], flow["remaining"]), ("1000.00", "400.00", "600.00"))

    def a_date_that_passes_is_matched_or_asked_about(self) -> None:
        parking = self.plan("Parking fine", "outflow", self.once("2026-07-02", "150.00"))
        owed = self.plan("Cousin repays", "inflow", self.once("2026-07-02", "800.00"))
        for flow in (parking, owed):
            self.assertTrue(self.on_the_forecast_tab(f"planned:{flow}:a", "2026-07-02"))
        self.assertNotIn("planned-overdue", self.todos())

        # A statement through 2026-07-04 moves the cutoff past both dates.
        rows, self.balance = _statement(
            [("2026-06-26", "Corner Grocer", "-30.00"), ("2026-07-04", "Corner Grocer", "-25.00")], self.balance)
        response = self.client.post(
            "/api/v1/imports",
            files=[("files", ("july.pdf", io.BytesIO(revolut_pdf_bytes(rows)), "application/pdf"))],
            headers={"origin": _ORIGIN, "x-csrf-token": self.csrf})
        self.assertEqual((response.status_code, response.json()["accepted"]), (200, 1), response.text)
        self.assertEqual(self.client.post("/api/v1/refresh", headers=self.headers()).status_code, 200)
        self.assertEqual(self.answer()["starts_on"], "2026-07-04")

        # The unpaid bill stays in the forecast, on the first day, in both cases.
        for case in ("base", "conservative"):
            [kept] = self.events(f"planned:{parking}", case)
            self.assertEqual((kept["on"], kept["amount"], kept["provenance"]["overdue"]), ("2026-07-05", "150.00", True))
            [moved] = [e for e in self.case(case)["events"]["repositioned"] if e["source_id"] == kept["source_id"]]
            self.assertEqual(moved["original_date"], "2026-07-02")
        # The unreceived money is not counted in either, and is listed as possible.
        for case in ("base", "conservative"):
            self.assertEqual(self.events(f"planned:{owed}", case), [])
        answer = self.answer()
        self.assertIn(("Cousin repays", "800.00"), [(item["label"], item["amount"]) for item in answer["upside"]])
        overdue = self.todos()["planned-overdue"]
        self.assertEqual(overdue["page"], "Planned")
        self.assertIn("Parking fine", overdue["detail"])
        self.assertIn("Cousin repays", overdue["detail"])

        # Saying what happened ends the question, and the bill with it.
        self.command("PLANNED_OCCURRENCE_RESOLVE_SET", {"flow": parking, "occurrence": "a"},
                     {"outcome": "cancelled", "reason": "the fine was waived"})
        self.assertNotIn("Parking fine", self.todos()["planned-overdue"]["detail"])
        self.assertEqual(
            [e for e in self.case()["events"]["repositioned"] if e["source_id"].startswith(f"planned:{parking}")], [])
        self.command("PLANNED_OCCURRENCE_RESOLVE_SET", {"flow": owed, "occurrence": "a"},
                     {"outcome": "cancelled", "reason": "never paid"})
        self.assertNotIn("planned-overdue", self.todos())

    def every_todo_opens_a_page_that_can_fix_it(self) -> None:
        todos = self.todos()
        self.assertEqual(sorted(todos), ["debt-dates", "essential", "export-period", "recent-statements"])
        self.assertEqual({key: todo["page"] for key, todo in todos.items()}, {
            "debt-dates": "Debts", "essential": "Categorization", "export-period": "Setup",
            "recent-statements": "Setup"})


if __name__ == "__main__":
    unittest.main()
