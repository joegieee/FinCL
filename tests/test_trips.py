"""v0.7 country and trip context."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from fincl import clock
from fincl.api import create_app
from fincl.context.queries import trip_spend_to_date
from fincl.db import connect_readonly, connect_sqlite
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient
from tests.revolut_pdf import revolut_pdf_bytes

_ORIGIN = "http://127.0.0.1:8765"


def _row(kind: str, day: str, description: str, amount: str, currency: str, balance: str) -> list[str]:
    stamp = f"{day} 10:00:00"
    return [kind, "Current", stamp, stamp, description, amount, "0.00", currency, "COMPLETED", balance]


_SGD = [
    _row("Topup", "2025-12-01", "Seed", "2000.00", "SGD", "2000.00"),
    _row("Card Payment", "2025-12-20", "Advance booking", "-400.00", "SGD", "1600.00"),
    _row("Card Payment", "2026-01-10", "Meal", "-30.00", "SGD", "1570.00"),
    _row("Card Payment", "2026-01-11", "Lodging", "-200.00", "SGD", "1370.00"),
    _row("Refund", "2026-01-12", "Lodging", "50.00", "SGD", "1420.00"),
    _row("Card Payment", "2026-01-15", "Taxi", "-15.00", "SGD", "1405.00"),
    _row("Card Payment", "2026-01-16", "Snack", "-5.00", "SGD", "1400.00"),
]
_USD = [
    _row("Topup", "2025-12-01", "Seed USD", "100.00", "USD", "100.00"),
    _row("Card Payment", "2026-01-11", "Diner", "-20.00", "USD", "80.00"),
]


class TripTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "ledger.sqlite3"
        service = DocumentImportService.plaintext_phase2(self.database, root / "evidence")
        for name, rows in (("sgd.pdf", _SGD), ("usd.pdf", _USD)):
            source = root / name
            source.write_bytes(revolut_pdf_bytes(rows))
            service.import_revolut_pdf(source)
        refresh_ledger(self.database)
        app = create_app(self.database, port=8765, bootstrap_secret="test-secret", require_access_key=True,
                         evidence_dir=root / "evidence")
        self.client = ASGITestClient(app, base_url=_ORIGIN)
        self.addCleanup(self.client.close)
        response = self.client.post("/session", json={"secret": "test-secret"},
                                    headers={"origin": _ORIGIN, "content-type": "application/json"})
        self.csrf = response.json()["csrf"]

    # -- helpers -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"origin": _ORIGIN, "content-type": "application/json", "x-csrf-token": self.csrf}

    def generation(self) -> str:
        return str(self.client.get("/api/v1/trips").json()["generation"])

    def anchor(self, description: str, *, inflow: bool = False) -> str:
        connection = sqlite3.connect(self.database)
        try:
            return str(connection.execute(
                "SELECT e.natural_anchor FROM ledger_entries e JOIN document_records d ON d.id=e.document_record_id "
                "WHERE d.description_raw=? AND e.economic_direction=?",
                (description, "IN" if inflow else "OUT"),
            ).fetchone()[0])
        finally:
            connection.close()

    def command(self, kind: str, anchors: dict[str, str], payload: dict[str, Any], *, status: int = 200) -> Any:
        response = self.client.post("/api/v1/commands", json={
            "request_id": str(uuid.uuid4()), "expected_generation": self.generation(),
            "kind": kind, "anchors": anchors, "payload": payload,
        }, headers=self._headers())
        self.assertEqual(response.status_code, status, response.text)
        return response

    def batch(self, items: list[dict[str, Any]], *, status: int = 200) -> Any:
        response = self.client.post("/api/v1/batch", json={
            "request_id": str(uuid.uuid4()), "expected_generation": self.generation(),
            "reason": "Synthetic trip batch", "items": items,
        }, headers=self._headers())
        self.assertEqual(response.status_code, status, response.text)
        return response

    def trip(self, name: str, start: str, end: str, countries: list[str]) -> str:
        trip_id = str(uuid.uuid4())
        self.command("TRIP_SET", {"trip": trip_id}, {"name": name, "start_on": start, "end_on": end,
                                                     "countries": countries, "reason": "Synthetic trip"})
        return trip_id

    def country(self, description: str, code: str | None, *, inflow: bool = False) -> None:
        self.command("ENTRY_COUNTRY_SET", {"entry": self.anchor(description, inflow=inflow)},
                     {"country": code, "reason": "Synthetic country"})

    def detail(self, trip_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/v1/trips/{trip_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json())

    @staticmethod
    def merchants(rows: list[dict[str, Any]]) -> set[str]:
        return {str(row["merchant"]).lower() for row in rows}

    # -- tests -------------------------------------------------------------
    def test_unknown_country_never_assigns_and_is_listed_for_review(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        detail = self.detail(trip_a)
        self.assertEqual(detail["contributors"], [])
        self.assertEqual(detail["trip"]["totals_by_currency"], {})
        self.assertEqual(self.merchants(detail["review"]["missing_country"]), {"meal", "lodging", "taxi", "diner"})
        self.assertEqual(detail["lens"], "recorded_flow")

    def test_known_country_inside_the_window_assigns_and_totals_by_currency(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        for description in ("Meal", "Lodging", "Taxi"):
            self.country(description, "FR")
        self.country("Lodging", "FR", inflow=True)
        self.country("Diner", "FR")
        self.country("Snack", "FR")  # the day after the window closes
        detail = self.detail(trip_a)
        self.assertEqual(self.merchants(detail["contributors"]), {"meal", "lodging", "taxi", "diner"})
        self.assertTrue(all(row["assignment"] == "automatic" for row in detail["contributors"]))
        sgd = detail["trip"]["totals_by_currency"]["SGD"]
        self.assertEqual((sgd["gross_cost"], sgd["refunds"], sgd["net_cost"]), ("245.00", "50.00", "195.00"))
        # Currency never implies country, and unlike currencies are never added.
        self.assertEqual(detail["trip"]["totals_by_currency"]["USD"]["gross_cost"], "20.00")
        # Category rows reconcile exactly to the trip's gross cost per currency.
        for currency, cell in detail["trip"]["totals_by_currency"].items():
            parts = sum(float(r["cost_amount"]) for r in detail["by_category"] if r["currency"] == currency)
            self.assertAlmostEqual(parts, float(cell["gross_cost"]))

    def test_country_outside_the_trip_list_and_explicit_unknown_stay_unassigned(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.country("Diner", "US")
        self.country("Meal", None)
        detail = self.detail(trip_a)
        self.assertEqual(detail["contributors"], [])
        self.assertIn("diner", self.merchants(detail["review"]["other_country"]))
        unknown = next(r for r in detail["review"]["missing_country"] if r["merchant"].lower() == "meal")
        self.assertEqual(unknown["country_basis"], "operator")

    def test_overlapping_trips_queue_review_until_a_manual_choice(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        trip_b = self.trip("Trip B", "2026-01-11", "2026-01-20", ["FR"])
        self.country("Meal", "FR")
        self.country("Lodging", "FR")
        trip_a_detail = self.detail(trip_a)
        self.assertEqual(self.merchants(trip_a_detail["contributors"]), {"meal"})
        ambiguous = trip_a_detail["review"]["ambiguous"]
        self.assertEqual(self.merchants(ambiguous), {"lodging"})
        self.assertEqual(set(ambiguous[0]["candidates"]), {trip_a, trip_b})
        self.command("ENTRY_TRIP_SET", {"entry": self.anchor("Lodging")}, {"trip": trip_b, "reason": "Stayed at the second location"})
        self.assertEqual(self.merchants(self.detail(trip_b)["contributors"]), {"lodging"})
        self.assertEqual(self.detail(trip_a)["review"]["ambiguous"], [])

    def test_manual_booking_outside_the_window_and_manual_exclusion(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.country("Meal", "FR")
        self.command("ENTRY_TRIP_SET", {"entry": self.anchor("Advance booking")}, {"trip": trip_a, "reason": "Flight booked ahead"})
        self.command("ENTRY_TRIP_SET", {"entry": self.anchor("Meal")}, {"trip": None, "reason": "Not part of the trip"})
        detail = self.detail(trip_a)
        booking = next(r for r in detail["contributors"] if r["merchant"].lower() == "advance booking")
        self.assertEqual((booking["assignment"], booking["outside_window"]), ("manual", True))
        self.assertNotIn("meal", self.merchants(detail["contributors"]))
        self.assertEqual(self.merchants(detail["review"]["excluded"]), {"meal"})
        # Retiring the exclusion returns the row to automatic assignment.
        self.command("ENTRY_TRIP_RETIRE", {"entry": self.anchor("Meal")}, {"reason": "Undo"})
        self.assertIn("meal", self.merchants(self.detail(trip_a)["contributors"]))

    def test_a_trip_retires_only_together_with_its_manual_assignments(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        booking = self.anchor("Advance booking")
        self.command("ENTRY_TRIP_SET", {"entry": booking}, {"trip": trip_a, "reason": "Flight booked ahead"})
        self.command("TRIP_RETIRE", {"trip": trip_a}, {"reason": "Cancelled"}, status=422)
        self.batch([
            {"kind": "ENTRY_TRIP_RETIRE", "anchors": {"entry": booking}, "payload": {"reason": "Cancelled"}},
            {"kind": "TRIP_RETIRE", "anchors": {"trip": trip_a}, "payload": {"reason": "Cancelled"}},
        ])
        self.assertEqual(self.client.get("/api/v1/trips").json()["trips"], [])
        self.assertEqual(self.client.get(f"/api/v1/trips/{trip_a}").status_code, 404)

    def test_a_trip_retires_with_manual_assignments_that_left_the_cost_view(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        refund = self.anchor("Lodging", inflow=True)
        self.command("ENTRY_TRIP_SET", {"entry": refund}, {"trip": trip_a, "reason": "Refund of the stay"})
        self.command("INBOUND_CLASSIFICATION", {"entry": refund}, {"classification": "income", "reason": "Not a refund"})
        detail = self.detail(trip_a)
        self.assertEqual(detail["contributors"], [])
        self.assertEqual(detail["manual_assignments"], [{"anchor": refund, "on": "2026-01-12", "counted": False}])
        self.batch([
            *({"kind": "ENTRY_TRIP_RETIRE", "anchors": {"entry": row["anchor"]}, "payload": {"reason": "Cancelled"}}
              for row in detail["manual_assignments"]),
            {"kind": "TRIP_RETIRE", "anchors": {"trip": trip_a}, "payload": {"reason": "Cancelled"}},
        ])
        self.assertEqual(self.client.get("/api/v1/trips").json()["trips"], [])

    def test_a_split_bills_own_share_counts_under_its_own_category(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.country("Meal", "FR")
        meal, sam = self.anchor("Meal"), str(uuid.uuid4())
        self.batch([
            {"kind": "PERSONAL_DEBT_OPEN", "anchors": {"debt": sam},
             "payload": {"direction": "RECEIVABLE", "currency": "SGD", "counterparty_label": "Sam",
                         "opened_on": "2026-01-10", "due_on": None, "reason": "Split the meal"}},
            {"kind": "PERSONAL_DEBT_EVENT_SET", "anchors": {"debt": sam, "event": str(uuid.uuid4())},
             "payload": {"event_kind": "EXPENSE_PAID_FOR_OTHER", "direction": "RECEIVABLE",
                         "principal_amount": "20.00", "effective_on": "2026-01-10",
                         "entry_allocations": [{"entry_anchor": meal, "principal_amount": "20.00", "interest_amount": "0.00",
                                                "fee_amount": "0.00", "separate_fee_amount": "0.00"}],
                         "opening_balance": False, "expense_category": None, "reason": "Split the meal"}},
            {"kind": "ENTRY_RESIDUAL_SET", "anchors": {"entry": meal},
             "payload": {"expense_amount": "10.00", "expense_category": "Food & Dining / Dining Out",
                         "reason": "Split the meal"}},
        ])
        detail = self.detail(trip_a)
        self.assertEqual(detail["by_category"],
                         [{"currency": "SGD", "category_key": "Food & Dining / Dining Out", "cost_amount": "10.00"}])
        row = detail["contributors"][0]
        self.assertEqual((row["anchor"], row["category_key"], row["cost_amount"]),
                         (meal, "Food & Dining / Dining Out", "10.00"))
        reported = self.client.get(f"/api/v1/reporting/workspaces/trips/{trip_a}").json()
        values = {tuple(a["path"]): a["value"] for a in reported["amounts"]}
        self.assertEqual(values[("by_category", 0, "cost_amount")]["reported"]["value"], "10.00")
        report = self.client.post("/api/v1/reports/preview", headers=self._headers(), json={
            "name": "By category", "lens": "recorded_flow", "group_by": ["category"], "columns": ["gross_cost"],
            "scope": {"window": {"kind": "all"}, "currencies": [], "accounts": [], "trips": [trip_a], "countries": [],
                      "categories": ["Food & Dining / Dining Out"]},
        })
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual([(r["group"][0]["value"], r["gross_cost"], r["entry_count"]) for r in report.json()["rows"]],
                         [("Food & Dining / Dining Out", "10.00", 1)])

    def test_a_row_ambiguous_between_other_trips_is_another_country_here(self) -> None:
        self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.trip("Trip B", "2026-01-10", "2026-01-15", ["FR"])
        trip_c = self.trip("Trip C", "2026-01-10", "2026-01-15", ["JP"])
        self.country("Taxi", "FR")
        review = self.detail(trip_c)["review"]
        self.assertEqual(review["ambiguous"], [])
        self.assertIn("taxi", self.merchants(review["other_country"]))

    def spend(self, trip_id: str, currency: str, as_of: str) -> dict[str, Any]:
        connection = connect_readonly(self.database)
        try:
            return trip_spend_to_date(connection, trip_id, currency, as_of)
        finally:
            connection.close()

    def rates(self, quote: str, rates: list[dict[str, str]]) -> None:
        connection = connect_sqlite(self.database)
        load_fx_rates(connection, {"source": "Synthetic", "source_version": "trip-tests-v1", "quote_currency": quote,
                                   "rates": rates})
        connection.commit()
        connection.close()
        refresh_ledger(self.database)

    def spending_trip(self) -> str:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        for description in ("Meal", "Lodging", "Taxi", "Diner"):
            self.country(description, "FR")
        self.country("Lodging", "FR", inflow=True)
        self.command("ENTRY_TRIP_SET", {"entry": self.anchor("Advance booking")}, {"trip": trip_a, "reason": "Booked ahead"})
        return trip_a

    def budget(self, trip_id: str, amount: str, *, name: str = "Trip A budget", start: str = "2026-01-10",
               end: str = "2026-01-15") -> str:
        flow = str(uuid.uuid4())
        self.command("PLANNED_FLOW_SET", {"flow": flow}, {
            "name": name, "direction": "outflow", "currency": "SGD", "account_id": None, "category": None,
            "occurrences": [{"key": "1", "earliest": start, "latest": end, "amount": amount}],
            "certainty": "sure", "baseline_treatment": "extra", "link": {"kind": "trip", "id": trip_id},
            "reason": "Synthetic budget",
        })
        return flow

    def test_a_trips_budget_reads_against_its_spend(self) -> None:
        self.rates("SGD", [{"base_currency": "USD", "period": "2026-01", "rate": "1.20"}])
        trip_a = self.spending_trip()
        self.assertEqual(self.detail(trip_a)["trip"]["budgets"], [])
        # The cash cutoff is 2026-01-11, so a budget ending then has passed.
        flow = self.budget(trip_a, "700.00", end="2026-01-11")
        (cell,) = self.detail(trip_a)["trip"]["budgets"]
        self.assertEqual((cell["currency"], cell["planned"], cell["actual"], cell["remaining"], cell["over_plan"]),
                         ("SGD", "700.00", "619.00", "81.00", None))
        self.assertEqual((cell["over_budget"], cell["missed"], cell["unvalued_count"]), (False, True, 0))
        self.assertEqual([(f["flow"], f["name"]) for f in cell["flows"]], [(flow, "Trip A budget")])
        self.assertEqual(self.client.get("/api/v1/trips").json()["trips"][0]["budgets"], [cell])
        reported = self.client.get(f"/api/v1/reporting/workspaces/trips/{trip_a}")
        values = {tuple(a["path"]): a["value"] for a in reported.json()["amounts"]}
        self.assertEqual(values[("trip", "budgets", 0, "planned")]["purpose"], "forecast")
        self.assertEqual(values[("trip", "budgets", 0, "actual")]["purpose"], "flow")

        self.command("PLANNED_FLOW_SET", {"flow": flow}, {
            "name": "Trip A budget", "direction": "outflow", "currency": "SGD", "account_id": None, "category": None,
            "occurrences": [{"key": "1", "earliest": "2026-01-10", "latest": "2026-01-15", "amount": "500.00"}],
            "certainty": "sure", "baseline_treatment": "extra", "link": {"kind": "trip", "id": trip_a},
            "reason": "Smaller budget",
        })
        (cell,) = self.detail(trip_a)["trip"]["budgets"]
        self.assertEqual((cell["remaining"], cell["over_plan"], cell["over_budget"]), ("0.00", "119.00", True))

    def test_a_budget_whose_dates_are_ahead_is_not_missed_and_a_done_one_has_nothing_left(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        flow = self.budget(trip_a, "300.00", start="2027-06-01", end="2027-06-10")
        (cell,) = self.detail(trip_a)["trip"]["budgets"]
        self.assertEqual((cell["planned"], cell["actual"], cell["remaining"], cell["missed"]),
                         ("300.00", "0.00", "300.00", False))
        self.command("PLANNED_OCCURRENCE_RESOLVE_SET", {"flow": flow, "occurrence": "1"},
                     {"outcome": "done", "reason": "Trip shortened"})
        (cell,) = self.detail(trip_a)["trip"]["budgets"]
        self.assertEqual((cell["remaining"], cell["missed"]), ("0.00", False))

    def test_a_trip_with_a_budget_is_not_retired_until_the_budget_is(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        flow = self.budget(trip_a, "300.00")
        refused = self.command("TRIP_RETIRE", {"trip": trip_a}, {"reason": "Cancelled"}, status=422)
        self.assertIn("remove the budget first", refused.text)
        self.batch([
            {"kind": "TRIP_RETIRE", "anchors": {"trip": trip_a}, "payload": {"reason": "Cancelled"}},
        ], status=422)
        self.batch([
            {"kind": "PLANNED_FLOW_RETIRE", "anchors": {"flow": flow}, "payload": {"reason": "Cancelled"}},
            {"kind": "TRIP_RETIRE", "anchors": {"trip": trip_a}, "payload": {"reason": "Cancelled"}},
        ])
        self.assertEqual(self.client.get("/api/v1/trips").json()["trips"], [])

    def test_a_past_trip_with_spending_in_its_dates_but_unplaced_has_a_todo(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        unplaced = len(self.detail(trip_a)["review"]["missing_country"])
        todo = self.detail(trip_a)["trip"]["todo"]
        self.assertEqual((todo["kind"], todo["trip_id"], todo["count"]), ("assign_trip_spend", trip_a, unplaced))
        self.assertEqual(self.client.get("/api/v1/trips").json()["todos"], [todo])
        for description in ("Meal", "Lodging", "Taxi", "Diner"):
            self.country(description, "FR")
        self.country("Lodging", "FR", inflow=True)
        self.assertIsNone(self.detail(trip_a)["trip"]["todo"])
        self.assertEqual(self.client.get("/api/v1/trips").json()["todos"], [])

    def test_an_upcoming_trip_has_no_todo(self) -> None:
        self.addCleanup(clock.pin, None)
        clock.pin(date.fromisoformat("2026-01-09"))
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.assertIsNone(self.detail(trip_a)["trip"]["todo"])

    def test_trip_spend_nets_refunds_and_converts_other_pockets_through_as_of(self) -> None:
        self.rates("SGD", [{"base_currency": "USD", "period": "2026-01", "rate": "1.20"}])
        trip_a = self.spending_trip()
        # 400 booking + 30 + 200 - 50 refund + 15 in SGD, and 20 USD at 1.20.
        self.assertEqual(self.spend(trip_a, "SGD", "2026-01-15")["actual"], "619.00")
        early = self.spend(trip_a, "SGD", "2026-01-11")
        self.assertEqual(early["actual"], "654.00")
        self.assertEqual([(p["on"], p["native_currency"], p["amount"]) for p in early["parts"]], [
            ("2025-12-20", "SGD", "400.00"), ("2026-01-10", "SGD", "30.00"),
            ("2026-01-11", "SGD", "200.00"), ("2026-01-11", "USD", "24.00")])
        self.assertEqual(early["unvalued"], [])

    def test_trip_spend_values_each_part_at_its_own_date(self) -> None:
        self.rates("USD", [{"base_currency": "SGD", "period": "2025-12", "rate": "0.70"},
                           {"base_currency": "SGD", "period": "2026-01", "rate": "0.75"}])
        trip_a = self.spending_trip()
        # 400 x 0.70 + (30 + 200) x 0.75 + 20 USD as itself.
        self.assertEqual(self.spend(trip_a, "USD", "2026-01-11")["actual"], "472.50")

    def test_trip_spend_lists_a_part_it_cannot_value_and_leaves_it_out(self) -> None:
        trip_a = self.spending_trip()
        spend = self.spend(trip_a, "SGD", "2026-01-15")
        self.assertEqual(spend["actual"], "595.00")
        self.assertEqual([(u["on"], u["native_currency"], u["native_amount"]) for u in spend["unvalued"]],
                         [("2026-01-11", "USD", "20.00")])
        with self.assertRaises(FinCLError):
            self.spend(str(uuid.uuid4()), "SGD", "2026-01-15")

    def test_one_transactions_context_names_its_trip_and_the_trips_it_could_join(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        later = self.trip("Trip B", "2026-02-01", "2026-02-05", ["JP"])
        meal = self.anchor("Meal")
        context = self.client.get("/api/v1/entry-context", params={"anchor": meal}).json()
        self.assertEqual((context["country"], context["assignment"], context["counted_as_cost"]), (None, "none", True))
        self.assertEqual({t["trip_id"]: t["in_window"] for t in context["trips"]}, {trip_a: True, later: False})
        self.country("Meal", "FR")
        context = self.client.get("/api/v1/entry-context", params={"anchor": meal}).json()
        self.assertEqual((context["country"], context["trip"], context["assignment"]), ("FR", trip_a, "automatic"))
        seed = self.anchor("Seed", inflow=True)
        self.command("ENTRY_TRIP_SET", {"entry": seed}, {"trip": None, "reason": "Not travel"})
        context = self.client.get("/api/v1/entry-context", params={"anchor": seed}).json()
        self.assertEqual((context["counted_as_cost"], context["assignment"]), (False, "excluded"))
        self.assertEqual(self.client.get("/api/v1/entry-context", params={"anchor": "nope"}).status_code, 404)

    def test_a_trip_is_upcoming_ongoing_or_past_against_the_app_clock(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.addCleanup(clock.pin, None)
        for today, status in (("2026-01-09", "upcoming"), ("2026-01-10", "ongoing"), ("2026-01-15", "ongoing"),
                              ("2026-01-16", "past")):
            clock.pin(date.fromisoformat(today))
            with self.subTest(today=today):
                self.assertEqual(self.client.get("/api/v1/trips").json()["trips"][0]["status"], status)
                self.assertEqual(self.detail(trip_a)["trip"]["status"], status)

    def test_invalid_context_decisions_are_refused(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        meal = {"entry": self.anchor("Meal")}
        self.command("ENTRY_COUNTRY_SET", meal, {"country": "XX", "reason": "Bad code"}, status=422)
        self.command("ENTRY_TRIP_SET", meal, {"trip": str(uuid.uuid4()), "reason": "No such trip"}, status=422)
        self.command("TRIP_SET", {"trip": trip_a}, {"name": "Trip A", "start_on": "2026-01-15", "end_on": "2026-01-10",
                                                   "countries": [], "reason": "Backwards"}, status=422)
        self.command("TRIP_SET", {"trip": "not-a-uuid"}, {"name": "Trip A", "start_on": "2026-01-10", "end_on": "2026-01-15",
                                                         "countries": [], "reason": "Bad id"}, status=422)

    def test_a_countryless_trip_never_auto_assigns(self) -> None:
        anywhere = self.trip("Somewhere", "2026-01-10", "2026-01-15", [])
        self.country("Meal", "FR")
        self.assertEqual(self.detail(anywhere)["contributors"], [])

    def test_replay_reproduces_every_assignment(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.country("Meal", "FR")
        self.command("ENTRY_TRIP_SET", {"entry": self.anchor("Advance booking")}, {"trip": trip_a, "reason": "Booked ahead"})
        before = self.detail(trip_a)
        refresh_ledger(self.database)
        after = self.detail(trip_a)
        before.pop("generation")
        after.pop("generation")
        self.assertEqual(before, after)

    def test_reported_trip_totals_compose_from_dated_parts(self) -> None:
        trip_a = self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.country("Meal", "FR")
        reported = self.client.get(f"/api/v1/reporting/workspaces/trips/{trip_a}")
        self.assertEqual(reported.status_code, 200, reported.text)
        values = {tuple(a["path"]): a["value"] for a in reported.json()["amounts"]}
        gross = values[("trip", "totals_by_currency", "SGD", "gross_cost")]
        self.assertEqual((gross["purpose"], gross["reported"]["value"]), ("flow", "30.00"))


if __name__ == "__main__":
    unittest.main()
