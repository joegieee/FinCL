"""Country and trip decisions (v0.7, DESIGN-debts-and-context.md §§4-5).

Six journal kinds, each a SET/RETIRE pair:

- ``ENTRY_COUNTRY_SET {entry} -> {country: code|null, reason}``: where the
  economic activity happened. ``null`` is an explicit "unknown", not an absence.
- ``TRIP_SET {trip} -> {name, start_on, end_on, countries, reason}``: replaces a
  trip; dates inclusive and ordered; countries may be empty.
- ``ENTRY_TRIP_SET {entry} -> {trip: id|null, reason}``: a manual assignment,
  or (``null``) an explicit exclusion from every trip.

Retiring an entry decision returns that entry to source evidence / automatic
assignment. Retiring a trip requires its manual assignments to be retired in the
same batch, so no manual decision is left pointing at nothing.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from datetime import date
from typing import Any

from ..errors import FinCLError
from .countries import COUNTRY_CODES

ENTRY_COUNTRY_SET = "ENTRY_COUNTRY_SET"
ENTRY_COUNTRY_RETIRE = "ENTRY_COUNTRY_RETIRE"
TRIP_SET = "TRIP_SET"
TRIP_RETIRE = "TRIP_RETIRE"
ENTRY_TRIP_SET = "ENTRY_TRIP_SET"
ENTRY_TRIP_RETIRE = "ENTRY_TRIP_RETIRE"
CONTEXT_DECISION_KINDS = frozenset({
    ENTRY_COUNTRY_SET, ENTRY_COUNTRY_RETIRE, TRIP_SET, TRIP_RETIRE, ENTRY_TRIP_SET, ENTRY_TRIP_RETIRE,
})

MAX_TRIP_NAME = 80
MAX_TRIP_COUNTRIES = 30


def _reason(payload: dict[str, Any]) -> None:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise FinCLError("A reason of up to 2,000 characters is required")


def _country(value: Any) -> str:
    if not isinstance(value, str) or value not in COUNTRY_CODES:
        raise FinCLError(f"{value!r} is not a supported two-letter country code")
    return value


def _day(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise FinCLError(f"{label} must be a calendar date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError(f"{label} must be a calendar date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != value:
        raise FinCLError(f"{label} must be a calendar date (YYYY-MM-DD)")
    return parsed


def _trip_id(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise FinCLError("A trip is identified by a lowercase UUID") from exc
    return value


def validate_context(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    """Shape checks shared by preview, single commands, batches and replay."""
    entry_kinds = {ENTRY_COUNTRY_SET, ENTRY_COUNTRY_RETIRE, ENTRY_TRIP_SET, ENTRY_TRIP_RETIRE}
    if kind in entry_kinds and (set(anchors) != {"entry"} or not anchors["entry"]):
        raise FinCLError("This decision is anchored to one transaction")
    if kind == ENTRY_COUNTRY_SET:
        if set(payload) != {"country", "reason"}:
            raise FinCLError("A country decision is {country, reason}")
        if payload["country"] is not None:
            _country(payload["country"])
    elif kind == ENTRY_TRIP_SET:
        if set(payload) != {"trip", "reason"}:
            raise FinCLError("A trip assignment is {trip, reason}")
        if payload["trip"] is not None:
            _trip_id(payload["trip"])
    elif kind in {ENTRY_COUNTRY_RETIRE, ENTRY_TRIP_RETIRE, TRIP_RETIRE}:
        if set(payload) != {"reason"}:
            raise FinCLError("Retiring a decision needs only a reason")
    elif kind == TRIP_SET:
        if set(payload) != {"name", "start_on", "end_on", "countries", "reason"}:
            raise FinCLError("A trip is {name, start_on, end_on, countries, reason}")
        name = payload["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_TRIP_NAME:
            raise FinCLError(f"A trip name is 1-{MAX_TRIP_NAME} characters")
        if _day(payload["start_on"], "start_on") > _day(payload["end_on"], "end_on"):
            raise FinCLError("A trip cannot end before it starts")
        countries = payload["countries"]
        if not isinstance(countries, list) or len(countries) > MAX_TRIP_COUNTRIES:
            raise FinCLError(f"A trip lists at most {MAX_TRIP_COUNTRIES} countries")
        codes = [_country(code) for code in countries]
        if len(set(codes)) != len(codes):
            raise FinCLError("Each country is listed once")
    else:
        raise FinCLError(f"{kind} is not a context decision")
    if kind in {TRIP_SET, TRIP_RETIRE}:
        if set(anchors) != {"trip"}:
            raise FinCLError("This decision is anchored to one trip")
        _trip_id(anchors["trip"])
    _reason(payload)


def check_context_sequence(
    connection: sqlite3.Connection,
    items: Iterable[tuple[str, dict[str, str], dict[str, Any]]],
    resolve_anchor: Callable[[str], str | None],
) -> None:
    """State checks for context decisions recorded together, in order.

    Every anchored transaction must resolve; an assignment names a trip that
    exists at that point; a trip being retired has no manual assignment left
    once the whole sequence is applied.
    """
    trips = {str(row[0]) for row in connection.execute("SELECT trip_id FROM trips")}
    manual = {str(row[0]): str(row[1]) for row in connection.execute(
        "SELECT entry_id, trip_id FROM entry_trips WHERE trip_id IS NOT NULL")}
    retired: set[str] = set()
    for kind, anchors, payload in items:
        if kind not in CONTEXT_DECISION_KINDS:
            continue
        validate_context(kind, anchors, payload)
        if "entry" in anchors:
            entry_id = resolve_anchor(anchors["entry"])
            if entry_id is None:
                raise FinCLError(f"transaction {anchors['entry']} no longer resolves")
            if kind == ENTRY_TRIP_SET:
                trip = payload["trip"]
                if trip is not None and trip not in trips:
                    raise FinCLError("That trip does not exist")
                if trip is None:
                    manual.pop(entry_id, None)
                else:
                    manual[entry_id] = trip
            elif kind == ENTRY_TRIP_RETIRE:
                manual.pop(entry_id, None)
        elif kind == TRIP_SET:
            trips.add(anchors["trip"])
            retired.discard(anchors["trip"])
        elif kind == TRIP_RETIRE:
            if anchors["trip"] not in trips:
                raise FinCLError("That trip does not exist")
            trips.discard(anchors["trip"])
            retired.add(anchors["trip"])
    left = sorted(entry for entry, trip in manual.items() if trip in retired)
    if left:
        raise FinCLError(
            f"Retiring this trip leaves {len(left)} manual assignment(s) pointing at it; "
            "retire them in the same batch"
        )


def apply_context(
    connection: sqlite3.Connection, *, kind: str, anchors: dict[str, str], payload: dict[str, Any],
    journal_seq: int, resolve_anchor: Callable[[str], str | None],
) -> None:
    """Project one context decision during replay.

    An entry anchor that no longer resolves projects nothing (it stays in the
    journal for rebinding), the same as every other entry-anchored decision.
    """
    validate_context(kind, anchors, payload)
    if kind == TRIP_SET:
        connection.execute(
            "INSERT OR REPLACE INTO trips(trip_id,name,start_on,end_on,countries_json,reason,journal_seq) VALUES(?,?,?,?,?,?,?)",
            (anchors["trip"], payload["name"].strip(), payload["start_on"], payload["end_on"],
             json.dumps(sorted(payload["countries"])), payload["reason"], journal_seq),
        )
        return
    if kind == TRIP_RETIRE:
        connection.execute("DELETE FROM entry_trips WHERE trip_id=?", (anchors["trip"],))
        connection.execute("DELETE FROM trips WHERE trip_id=?", (anchors["trip"],))
        return
    entry_id = resolve_anchor(anchors["entry"])
    if entry_id is None:
        return
    if kind == ENTRY_COUNTRY_SET:
        connection.execute(
            "INSERT OR REPLACE INTO entry_countries(entry_id,country,reason,journal_seq) VALUES(?,?,?,?)",
            (entry_id, payload["country"], payload["reason"], journal_seq),
        )
    elif kind == ENTRY_COUNTRY_RETIRE:
        connection.execute("DELETE FROM entry_countries WHERE entry_id=?", (entry_id,))
    elif kind == ENTRY_TRIP_SET:
        trip = payload["trip"]
        if trip is not None and not connection.execute("SELECT 1 FROM trips WHERE trip_id=?", (trip,)).fetchone():
            return
        connection.execute(
            "INSERT OR REPLACE INTO entry_trips(entry_id,trip_id,reason,journal_seq) VALUES(?,?,?,?)",
            (entry_id, trip, payload["reason"], journal_seq),
        )
    elif kind == ENTRY_TRIP_RETIRE:
        connection.execute("DELETE FROM entry_trips WHERE entry_id=?", (entry_id,))
