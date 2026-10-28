"""Planned-flow read routes under `/api/v1`.

Writes use the shared `/preview`, `/commands` and `/batch` endpoints with the
`PLANNED_*` kinds; these routes only read.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from . import clock
from .errors import FinCLError


def register_planned_routes(app: FastAPI, database: Path) -> None:
    @app.get("/api/v1/planned")
    def planned(generation: str | None = None) -> Any:
        from .planned.queries import planned_page

        page = planned_page(database, today=clock.today())
        if generation and generation != page["generation"]:
            raise HTTPException(409, "STALE_GENERATION: reload this page")
        return page

    @app.get("/api/v1/planned/match-candidates")
    def planned_match_candidates(flow: str, occurrence: str) -> Any:
        from .planned.queries import match_candidates

        try:
            return match_candidates(database, flow=flow, occurrence=occurrence)
        except FinCLError as exc:
            raise HTTPException(404 if "does not exist" in str(exc) else 422, str(exc)) from exc
