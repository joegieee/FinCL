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
    budget_history,
    budgets_page,
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

    @app.get("/api/v1/goals")
    def goals(as_of: str | None = None) -> Any:
        return goals_page(database, as_of=as_of)

    @app.get("/api/v1/recurring")
    def recurring() -> Any:
        return recurring_page(database)

    @app.get("/api/v1/recurring/{commitment}/occurrences")
    def recurring_occ(commitment: str, start: str, end: str) -> Any:
        if start > end:
            raise FinCLError("start must be on or before end")
        return recurring_occurrences(database, commitment, start=start, end=end)
