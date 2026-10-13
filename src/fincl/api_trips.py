"""Trip and country read routes under `/api/v1` (v0.7).

Writes use the shared `/preview`, `/commands` and `/batch` endpoints with the
`ENTRY_COUNTRY_*`, `TRIP_*` and `ENTRY_TRIP_*` kinds; these routes only read.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from .errors import FinCLError


def _map_error(exc: FinCLError) -> HTTPException:
    message = str(exc)
    if message.startswith("STALE_GENERATION"):
        return HTTPException(409, message)
    if "does not exist" in message:
        return HTTPException(404, message)
    return HTTPException(422, message)


def register_trip_routes(app: FastAPI, database: Path) -> None:
    @app.get("/api/v1/trips")
    def trips(generation: str | None = None) -> Any:
        from .context.queries import trips_page

        try:
            return trips_page(database, expected=generation)
        except FinCLError as exc:
            raise _map_error(exc) from exc

    @app.get("/api/v1/trips/{trip_id}")
    def trip(trip_id: str, generation: str | None = None) -> Any:
        from .context.queries import trip_detail

        try:
            return trip_detail(database, trip_id, expected=generation)
        except FinCLError as exc:
            raise _map_error(exc) from exc

    @app.get("/api/v1/countries")
    def countries() -> Any:
        from .context.queries import country_list

        return country_list()
