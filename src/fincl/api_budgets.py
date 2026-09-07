"""v0.6 planning HTTP surface: budgets, savings goals and recurring commitments.

Read routes only — writes reuse the shared ``/api/v1/preview`` and
``/api/v1/commands`` flow (``api._validate`` gains one planning branch). Kept in
its own module so ``create_app`` grows by a single call.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .errors import FinCLError
from .planning.queries import (
    budget_backdate_preview,
    budget_history,
    budgets_page,
    goal_detail,
    goal_observations,
    goal_scenario,
    goals_page,
    recurring_occurrences,
    recurring_page,
)


def register_planning_routes(app: FastAPI, database: Path) -> None:
    @app.get("/api/v1/budgets")
    def budgets(month: str | None = None, currency: str = "SGD") -> Any:
        return budgets_page(database, month=month or date.today().strftime("%Y-%m"), currency=currency)

    @app.get("/api/v1/budgets/{plan}/history")
    def budgets_history(plan: str) -> Any:
        return budget_history(database, plan)

    @app.get("/api/v1/budgets/{plan}/backdate-preview")
    def budgets_backdate(plan: str, effective_month: str, limit: str,
                         currency: str = "SGD", category_key: str | None = None) -> Any:
        return budget_backdate_preview(
            database, plan, effective_month=effective_month, limit=limit,
            category_key=category_key or None, currency=currency,
        )

    @app.get("/api/v1/goals")
    def goals(as_of: str | None = None) -> Any:
        return goals_page(database, as_of=as_of)

    @app.get("/api/v1/goals/{plan}")
    def goal(plan: str, as_of: str | None = None) -> Any:
        return goal_detail(database, plan, as_of=as_of)

    @app.get("/api/v1/goals/{plan}/observations")
    def observations(plan: str) -> Any:
        return goal_observations(database, plan)

    @app.get("/api/v1/goals/{plan}/scenario")
    def goal_scenario_route(plan: str, dates: str = "", as_of: str | None = None) -> Any:
        parsed = [d.strip() for d in dates.split(",") if d.strip()]
        if len(parsed) > 60:
            raise FinCLError("Choose at most 60 contribution dates")
        return goal_scenario(database, plan, as_of=as_of, contribution_dates=parsed)

    @app.get("/api/v1/recurring")
    def recurring() -> Any:
        return recurring_page(database)

    @app.get("/api/v1/recurring/candidates")
    def recurring_candidates() -> Any:
        page = recurring_page(database)
        return {"generation": page["generation"], "candidates": page["candidates"]}

    @app.get("/api/v1/recurring/commitments")
    def recurring_commitments() -> Any:
        page = recurring_page(database)
        return {"generation": page["generation"], "confirmed": page["confirmed"],
                "paused_or_ended": page["paused_or_ended"]}

    @app.get("/api/v1/recurring/{commitment}/occurrences")
    def recurring_occ(commitment: str, start: str, end: str) -> Any:
        if start > end:
            raise FinCLError("start must be on or before end")
        return recurring_occurrences(database, commitment, start=start, end=end)
