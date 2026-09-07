"""v0.6 planning HTTP surface — budgets / goals / recurring read routes and the
shared command flow."""

from __future__ import annotations

import uuid

from tests.test_api import ApiTestBase


class PlanningApiTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()

    def command(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> dict:
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "kind": kind, "anchors": anchors, "payload": payload,
        }
        preview = self.post("/api/v1/preview", body, self.csrf)
        self.assertEqual(preview.status_code, 200, preview.text)
        response = self.post("/api/v1/commands", body, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)
        return response.json()

    def test_budget_revision_projects_and_the_read_route_computes_the_actual(self) -> None:
        plan = str(uuid.uuid4())
        self.command(
            "BUDGET_REVISION_SET", {"plan": plan, "revision": str(uuid.uuid4())},
            {"effective_month": "2026-01", "name": "All spending", "currency": "SGD",
             "category_key": None, "limit": "100.00", "reason": "set"},
        )
        page = self.client.get("/api/v1/budgets?month=2026-01&currency=SGD").json()
        self.assertEqual(len(page["budgets"]), 1)
        budget = page["budgets"][0]
        self.assertEqual(budget["limit"], "100.00")
        self.assertEqual(budget["actual"], "74.25")  # 4.00 + 19.00 + 51.25 spending in Jan
        self.assertEqual(budget["plan"], plan)

        history = self.client.get(f"/api/v1/budgets/{plan}/history").json()
        self.assertEqual(len(history["revisions"]), 1)
        self.assertEqual(history["revisions"][0]["limit"], "100.00")

    def test_a_competing_same_month_revision_is_refused_at_commit(self) -> None:
        plan = str(uuid.uuid4())
        self.command(
            "BUDGET_REVISION_SET", {"plan": plan, "revision": str(uuid.uuid4())},
            {"effective_month": "2026-02", "name": "B", "currency": "SGD",
             "category_key": None, "limit": "50.00", "reason": "set"},
        )
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "kind": "BUDGET_REVISION_SET",
            "anchors": {"plan": plan, "revision": str(uuid.uuid4())},
            "payload": {"effective_month": "2026-02", "name": "B", "currency": "SGD",
                        "category_key": None, "limit": "60.00", "reason": "clash"},
        }
        self.assertEqual(self.post("/api/v1/preview", body, self.csrf).status_code, 200)
        self.assertEqual(self.post("/api/v1/commands", body, self.csrf).status_code, 422)

    def test_goal_details_and_observation_drive_the_goals_route(self) -> None:
        plan = str(uuid.uuid4())
        self.command(
            "GOAL_DETAILS_SET", {"plan": plan},
            {"name": "Trip", "currency": "SGD", "target": "2000.00",
             "target_on": "2026-12-01", "reason": "set"},
        )
        self.command(
            "GOAL_OBSERVATION_SET", {"plan": plan, "observation": str(uuid.uuid4())},
            {"observed_on": "2026-06-01", "saved": "500.00", "reason": "progress"},
        )
        goals = self.client.get("/api/v1/goals?as_of=2026-07-01").json()["goals"]
        goal = next(g for g in goals if g["plan"] == plan)
        self.assertEqual(goal["saved"], "500.00")
        self.assertEqual(goal["remaining"], "1500.00")
        self.assertTrue(goal["dated_progress_known"])

    def test_recurring_commitment_route_and_occurrence_states(self) -> None:
        commitment = str(uuid.uuid4())
        self.command(
            "RECURRING_COMMITMENT_SET", {"commitment": commitment},
            {"name": "Gym", "currency": "SGD", "merchant_key": "gymco",
             "account_ids": ["a"], "seed_entry_anchors": [],
             "schedule": {"kind": "monthly", "day": 10, "anchor_on": "2026-01-10"},
             "expected_amount": "60.00", "amount_tolerance": "5.00",
             "status": "active", "ended_on": None, "reason": "confirmed"},
        )
        self.command(
            "RECURRING_OCCURRENCE_SET", {"commitment": commitment, "scheduled_on": "2026-02-10"},
            {"entry": None, "reason": "skipped"},
        )
        page = self.client.get("/api/v1/recurring").json()
        self.assertEqual(len(page["confirmed"]), 1)
        self.assertEqual(page["confirmed"][0]["expected_amount"], "60.00")

        occ = self.client.get(
            f"/api/v1/recurring/{commitment}/occurrences?start=2026-01-01&end=2026-03-31"
        ).json()["occurrences"]
        states = {o["scheduled_on"]: o["state"] for o in occ}
        self.assertEqual(states["2026-02-10"], "explicitly_skipped")
        self.assertIn(states["2026-01-10"], {"missing_in_covered_window", "associated"})

    def test_goal_target_date_scenario_divides_the_remainder_and_rounds_up(self) -> None:
        plan = str(uuid.uuid4())
        self.command(
            "GOAL_DETAILS_SET", {"plan": plan},
            {"name": "Fund", "currency": "SGD", "target": "1000.00",
             "target_on": "2026-12-01", "reason": "set"},
        )
        dates = "2026-07-01,2026-08-01,2026-09-01"
        s = self.client.get(f"/api/v1/goals/{plan}/scenario?as_of=2026-06-15&dates={dates}").json()
        self.assertEqual(s["remaining"], "1000.00")
        self.assertEqual(s["contribution_dates_considered"], ["2026-07-01", "2026-08-01", "2026-09-01"])
        self.assertEqual(s["monthly_amount"], "333.34")  # ceil(100000 / 3) cents
        self.assertEqual(s["status"], "scheduled")
        self.assertTrue(s["assumes_no_investment_return"])

    def test_goal_scenario_with_no_future_dates_reports_status_only(self) -> None:
        plan = str(uuid.uuid4())
        self.command(
            "GOAL_DETAILS_SET", {"plan": plan},
            {"name": "Fund", "currency": "SGD", "target": "500.00", "target_on": "2026-03-01", "reason": "s"},
        )
        s = self.client.get(f"/api/v1/goals/{plan}/scenario?as_of=2026-06-01&dates=2026-05-01").json()
        self.assertIsNone(s["monthly_amount"])
        self.assertEqual(s["status"], "overdue")

    def test_backdate_preview_lists_every_affected_month(self) -> None:
        plan = str(uuid.uuid4())
        for month, limit in (("2026-01", "300.00"), ("2026-06", "400.00")):
            self.command(
                "BUDGET_REVISION_SET", {"plan": plan, "revision": str(uuid.uuid4())},
                {"effective_month": month, "name": "B", "currency": "SGD",
                 "category_key": None, "limit": limit, "reason": "set"},
            )
        preview = self.client.get(
            f"/api/v1/budgets/{plan}/backdate-preview?effective_month=2026-01&limit=500.00&currency=SGD"
        ).json()
        months = [m["month"] for m in preview["affected_months"]]
        self.assertEqual(months, ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"])
        first = preview["affected_months"][0]
        self.assertEqual((first["previous_limit"], first["new_limit"]), ("300.00", "500.00"))

    def test_a_malformed_planning_command_is_rejected_at_preview(self) -> None:
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "kind": "BUDGET_REVISION_SET",
            "anchors": {"plan": str(uuid.uuid4()), "revision": str(uuid.uuid4())},
            "payload": {"effective_month": "2026-99", "name": "x", "currency": "SGD",
                        "category_key": None, "limit": "10.00", "reason": "bad"},
        }
        self.assertEqual(self.post("/api/v1/preview", body, self.csrf).status_code, 422)
