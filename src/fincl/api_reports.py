"""Saved report read routes under `/api/v1` (v0.7 slice 2).

Definitions are written through the shared `/preview` and `/commands`
endpoints with `REPORT_DEFINITION_SET/RETIRE`; these routes only read. A
report's result is evaluated on every read against the current generation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from . import clock
from .errors import FinCLError


def _map_error(exc: FinCLError) -> HTTPException:
    message = str(exc)
    if message.startswith("STALE_GENERATION"):
        return HTTPException(409, message)
    if "does not exist" in message:
        return HTTPException(404, message)
    return HTTPException(422, message)


def register_report_routes(app: FastAPI, database: Path) -> None:
    @app.get("/api/v1/reports")
    def reports() -> Any:
        from .reports.queries import reports_page

        return reports_page(database)

    @app.get("/api/v1/reports/{report_id}/result")
    def report_result(report_id: str, generation: str | None = None) -> Any:
        from .reports.queries import report_result as result

        try:
            return result(database, report_id, today=clock.today(), expected=generation)
        except FinCLError as exc:
            raise _map_error(exc) from exc

    @app.post("/api/v1/reports/preview")
    def report_preview(body: dict[str, Any], generation: str | None = None) -> Any:
        from .reports.queries import preview_result

        try:
            return preview_result(database, body, today=clock.today(), expected=generation)
        except FinCLError as exc:
            raise _map_error(exc) from exc
