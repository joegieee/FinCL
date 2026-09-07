"""v0.6 — budget actuals reconcile with the independent analysis totals, and a
plan that has adopted dated revisions refuses legacy PLAN_SET updates."""

from __future__ import annotations

import uuid

from fincl.report import economic_flow_report_data
from tests.test_api import ApiTestBase


class BudgetReconciliationTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()

    def command(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> dict:
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "kind": kind, "anchors": anchors, "payload": payload,
        }
        self.assertEqual(self.post("/api/v1/preview", body, self.csrf).status_code, 200)
        response = self.post("/api/v1/commands", body, self.csrf)
        self.assertIn(response.status_code, {200, 202}, response.text)
        return response.json()

    def _budget(self, *, category_key: str | None, month: str = "2026-01", limit: str = "100000.00") -> str:
        plan = str(uuid.uuid4())
        self.command(
            "BUDGET_REVISION_SET", {"plan": plan, "revision": str(uuid.uuid4())},
            {"effective_month": month, "name": "recon", "currency": "SGD",
             "category_key": category_key, "limit": limit, "reason": "recon"},
        )
        return plan

    def test_all_spending_budget_actual_equals_economic_flow_spending_plus_fees(self) -> None:
        plan = self._budget(category_key=None)
        page = self.client.get("/api/v1/budgets?month=2026-01&currency=SGD").json()
        actual = next(b["actual"] for b in page["budgets"] if b["plan"] == plan)

        # The fixture's covered window is 2026-01-01..2026-01-20 and every entry
        # falls inside it, so the whole-month budget actual equals the covered
        # window's economic-flow spending + fees.
        flow = economic_flow_report_data(self.database, start="2026-01-01", end="2026-01-20")
        sgd = next(c for c in flow["currencies"] if c["currency"] == "SGD")
        from decimal import Decimal
        expected = Decimal(sgd["spending"]) + Decimal(sgd["fees"])
        self.assertEqual(Decimal(actual), expected)

    def test_category_budget_actual_equals_that_categorys_dashboard_total(self) -> None:
        totals = self.overview()["category_totals"]
        self.assertTrue(totals, "fixture should have categorised spending")
        target = totals[0]
        plan = self._budget(category_key=target["name"])
        page = self.client.get("/api/v1/budgets?month=2026-01&currency=SGD").json()
        actual = next(b["actual"] for b in page["budgets"] if b["plan"] == plan)
        self.assertEqual(actual, target["value"])

    def test_legacy_plan_set_is_refused_after_a_dated_revision(self) -> None:
        plan = self._budget(category_key=None)
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "kind": "PLAN_SET", "anchors": {"plan": plan},
            "payload": {"type": "budget", "name": "legacy", "currency": "SGD",
                        "target": "50.00", "saved": "0", "category": "", "reason": "legacy"},
        }
        self.assertEqual(self.post("/api/v1/preview", body, self.csrf).status_code, 422)

    def test_a_plan_set_that_predates_any_dated_record_still_works(self) -> None:
        plan = str(uuid.uuid4())
        self.command(
            "PLAN_SET", {"plan": plan},
            {"type": "budget", "name": "old", "currency": "SGD", "target": "80.00",
             "saved": "0", "category": "", "reason": "before adoption"},
        )
        # It is present in the legacy dashboard plans and replay did not refuse it.
        self.assertTrue(any(p["id"] == plan for p in self.overview()["plans"]))
