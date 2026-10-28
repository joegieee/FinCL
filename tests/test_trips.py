"""v0.7 country and trip context."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from fincl.api import create_app
from fincl.document_service import DocumentImportService
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

    def test_a_row_ambiguous_between_other_trips_is_another_country_here(self) -> None:
        self.trip("Trip A", "2026-01-10", "2026-01-15", ["FR"])
        self.trip("Trip B", "2026-01-10", "2026-01-15", ["FR"])
        trip_c = self.trip("Trip C", "2026-01-10", "2026-01-15", ["JP"])
        self.country("Taxi", "FR")
        review = self.detail(trip_c)["review"]
        self.assertEqual(review["ambiguous"], [])
        self.assertIn("taxi", self.merchants(review["other_country"]))

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
