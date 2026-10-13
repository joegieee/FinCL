"""Saved report read routes under `/api/v1` (v0.7 slice 2).

Definitions are written through the shared `/preview` and `/commands`
endpoints with `REPORT_DEFINITION_SET/RETIRE`; these routes only read. A
report's result is evaluated on every read against the current generation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from . import clock
from .errors import FinCLError


def _map_error(exc: FinCLError) -> HTTPException:
    message = str(exc)
    if message.startswith("STALE_GENERATION"):
        return HTTPException(409, message)
    if "does not exist" in message:
        return HTTPException(404, message)
    return HTTPException(422, message)


class ExportRequest(BaseModel):
    """Exactly one of a saved report or an unsaved definition, and a format."""

    model_config = ConfigDict(extra="forbid")
    format: str
    report_id: str | None = None
    definition: dict[str, Any] | None = None
    generation: str | None = None


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

    @app.post("/api/v1/reports/export")
    def report_export(body: ExportRequest) -> Response:
        """The file itself, downloaded by the browser; never written to a server path."""
        from datetime import datetime

        from .custom_categories import custom_categories
        from .reports.exports import build_export

        try:
            name, content_type, data = build_export(
                database, fmt=body.format, today=clock.today(), now=datetime.now().astimezone(),
                report_id=body.report_id, definition=body.definition, expected=body.generation,
                custom_categories=custom_categories(database),
            )
        except FinCLError as exc:
            raise _map_error(exc) from exc
        return Response(data, media_type=content_type, headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })
