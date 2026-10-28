"""Planned-flow HTTP surface: the read routes and the shared command flow."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fincl import clock
from tests.test_api import ApiTestBase


class PlannedApiTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()
        clock.pin(date(2026, 2, 10))
        self.addCleanup(clock.pin, None)
        self.flow = str(uuid.uuid4())

    def body(self, kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": str(uuid.uuid4()), "expected_generation": self.overview()["generation"],
            "kind": kind, "anchors": anchors, "payload": payload,
        }

    def command(self, kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
        body = self.body(kind, anchors, payload)
        self.assertEqual(self.post("/api/v1/preview", body, self.csrf).status_code, 200)
        response = self.post("/api/v1/commands", body, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)

    def set_flow(self, flow: str | None = None, **changes: Any) -> None:
        self.command("PLANNED_FLOW_SET", {"flow": flow or self.flow}, {
            "name": "Course", "direction": "outflow", "currency": "SGD", "account_id": None, "category": None,
            "occurrences": [
                {"key": "1", "earliest": "2026-01-02", "latest": "2026-01-02", "amount": "4.00"},
                {"key": "2", "earliest": "2026-03-01", "latest": "2026-03-31", "amount": "300.00"},
            ],
            "certainty": "sure", "baseline_treatment": "extra", "link": None, "reason": "enrolled",
            **changes,
        })

    def test_a_planned_item_is_entered_matched_and_read_back(self) -> None:
        self.set_flow()
        candidates = self.client.get(f"/api/v1/planned/match-candidates?flow={self.flow}&occurrence=1").json()
        first = candidates["candidates"][0]
        self.assertEqual((first["on"], first["amount"], first["suggested_amount"]), ("2026-01-02", "4.00", "4.00"))
        self.command("PLANNED_MATCH_SET", {"flow": self.flow, "occurrence": "1", "entry": first["entry"]},
                     {"amount": first["suggested_amount"], "reason": "paid at the cafe"})

        page = self.client.get("/api/v1/planned").json()
        flow = page["flows"][0]
        self.assertEqual(page["cutoff"], "2026-01-20")
        self.assertEqual([(cell["key"], cell["state"]) for cell in flow["occurrences"]],
                         [("1", "matched"), ("2", "upcoming")])
        self.assertEqual((flow["planned"], flow["matched"], flow["remaining"]), ("304.00", "4.00", "300.00"))
        reported = self.client.get("/api/v1/reporting/workspaces/planned")
        self.assertEqual(reported.status_code, 200, reported.text)
        self.assertIn(["flows", 0, "remaining"], [amount["path"] for amount in reported.json()["amounts"]])

    def test_a_flow_with_matches_is_retired_in_one_batch(self) -> None:
        self.set_flow()
        entry = self.first_anchor()
        self.command("PLANNED_MATCH_SET", {"flow": self.flow, "occurrence": "1", "entry": entry},
                     {"amount": "4.00", "reason": "paid"})
        retire = self.body("PLANNED_FLOW_RETIRE", {"flow": self.flow}, {"reason": "dropped the course"})
        self.assertEqual(self.post("/api/v1/preview", retire, self.csrf).status_code, 422)
        batch = {
            "request_id": str(uuid.uuid4()), "expected_generation": self.overview()["generation"],
            "reason": "dropped the course",
            "items": [
                {"kind": "PLANNED_MATCH_RETIRE", "anchors": {"flow": self.flow, "occurrence": "1", "entry": entry},
                 "payload": {"reason": "dropped the course"}},
                {"kind": "PLANNED_FLOW_RETIRE", "anchors": {"flow": self.flow}, "payload": {"reason": "dropped the course"}},
            ],
        }
        response = self.post("/api/v1/batch", batch, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)
        self.assertEqual(self.client.get("/api/v1/planned").json()["flows"], [])

    def test_an_overdue_item_is_listed_until_its_outcome_is_recorded(self) -> None:
        self.set_flow()
        page = self.client.get("/api/v1/planned").json()
        self.assertEqual([(item["occurrence"], item["question"]) for item in page["attention"]],
                         [("1", "Did this payment happen?")])
        self.command("PLANNED_OCCURRENCE_RESOLVE_SET", {"flow": self.flow, "occurrence": "1"},
                     {"outcome": "cancelled", "reason": "did not happen"})
        page = self.client.get("/api/v1/planned").json()
        self.assertEqual(page["attention"], [])
        self.assertEqual(page["flows"][0]["occurrences"][0]["outcome"], {"outcome": "cancelled", "reason": "did not happen"})

    def test_a_trip_budget_has_no_match_candidates(self) -> None:
        trip = str(uuid.uuid4())
        self.command("TRIP_SET", {"trip": trip}, {"name": "Spring trip", "start_on": "2026-03-01", "end_on": "2026-03-07",
                                                  "countries": [], "reason": "booked"})
        self.set_flow(link={"kind": "trip", "id": trip})
        response = self.client.get(f"/api/v1/planned/match-candidates?flow={self.flow}&occurrence=2")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/v1/planned").json()["flows"][0]["link"],
                         {"kind": "trip", "id": trip, "name": "Spring trip", "exists": True})

    def one_day(self, on: str, amount: str) -> list[dict[str, str]]:
        return [{"key": "a", "earliest": on, "latest": on, "amount": amount}]

    def answer(self) -> dict[str, Any]:
        return self.client.get("/api/v1/planning/sufficiency").json()["combined_answer"]

    def forecast_event_ids(self, case: str) -> list[str]:
        days = self.client.get("/api/v1/forecast").json()["by_currency"]["SGD"][case]["days"]
        return [event for day in days for event in day["event_ids"] if event.startswith("planned:")]

    def test_a_sure_inflow_fixes_a_shortfall_in_both_cases(self) -> None:
        self.assertEqual(self.answer()["answer"], "No")
        self.set_flow(direction="inflow", occurrences=self.one_day("2026-03-01", "100.00"))
        answer = self.answer()
        self.assertEqual((answer["answer"], answer["base"]["verdict"], answer["cautious"]["verdict"]),
                         ("Yes", "enough", "enough"))
        self.assertEqual([(row["label"], row["next_on"], row["confidence"]) for row in answer["income"]],
                         [("Course", "2026-03-01", "confirmed")])
        for case in ("base", "conservative"):
            self.assertEqual(self.forecast_event_ids(case), [f"planned:{self.flow}:a"])

    def test_an_expected_inflow_helps_the_base_case_only(self) -> None:
        self.set_flow(direction="inflow", certainty="expected", occurrences=self.one_day("2026-03-01", "100.00"))
        answer = self.answer()
        self.assertEqual((answer["base"]["verdict"], answer["cautious"]["verdict"]), ("enough", "short"))
        self.assertTrue(answer["only_safe_in_the_base_case"])
        self.assertEqual(self.forecast_event_ids("base"), [f"planned:{self.flow}:a"])
        self.assertEqual(self.forecast_event_ids("conservative"), [])

    def test_a_planned_outflow_moves_the_forecast_and_flips_the_answer(self) -> None:
        self.set_flow(direction="inflow", occurrences=self.one_day("2026-03-01", "200.00"))
        before = self.answer()
        self.assertEqual(before["answer"], "Yes")
        outflow = str(uuid.uuid4())
        self.set_flow(outflow, name="Laptop", occurrences=self.one_day("2026-04-15", "300.00"))
        after = self.answer()
        self.assertEqual(after["answer"], "No")
        self.assertEqual(after["base"]["verdict"], "short")
        self.assertLess(float(after["base"]["lowest"]), float(before["base"]["lowest"]) - 299)
        self.assertIn(f"planned:{outflow}:a", self.forecast_event_ids("base"))
        self.assertIn("Planned spending", [row["label"] for row in after["where_it_goes"]])
