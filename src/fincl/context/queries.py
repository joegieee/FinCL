"""Trip and country reads (v0.7).

Trip cost reuses the recorded-flow economic components, so a trip total and
the Analysis totals it is part of come from the same rows. This slice offers
the recorded lens only: a refund counts in the context of the refund itself,
not of the purchase it refunds; the lens is named on every read.

Assignment, per transaction:

1. A manual decision wins: a trip, or an explicit exclusion from every trip.
2. Otherwise a transaction is assigned automatically only when its whole
   represented date interval lies inside a trip's inclusive window, its
   country is known and listed by that trip, and exactly one trip matches.
3. Several matches are ambiguous and wait for review. There is no tie-break.
   A countryless trip and an unknown country never auto-assign.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..money import ExactMoney
from .countries import COUNTRY_LIST_VERSION

LENS = "recorded_flow"
LENS_NOTE = ("Recorded lens: each transaction counts on its own date and in its own context. "
             "A refund counts where it was received, not where the purchase was made.")
ASSIGNMENT_POLICY = "trip-assignment-v1"
_COST_BUCKETS = ("spending", "fees")
_REFUND_BUCKETS = ("refunds", "reimbursements")
_OWNER_SHARE = "#owner-residual"


def _text(value: Decimal, scale: int) -> str:
    return f"{value:.{max(scale, 2)}f}"


def _load(connection: sqlite3.Connection, expected: str | None) -> dict[str, Any]:
    if not connection.in_transaction:
        connection.execute("BEGIN")
    row = current_generation(connection)
    if row is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    token = str(row["generation_token"])
    if expected is not None and expected != token:
        raise FinCLError("STALE_GENERATION: reload trips and try again")
    trips = {
        str(r["trip_id"]): {
            "trip_id": str(r["trip_id"]), "name": str(r["name"]), "start_on": str(r["start_on"]),
            "end_on": str(r["end_on"]), "countries": json.loads(r["countries_json"]),
            "reason": str(r["reason"]), "journal_seq": int(r["journal_seq"]),
        }
        for r in connection.execute("SELECT * FROM trips ORDER BY start_on, name, trip_id")
    }
    countries = {str(r[0]): (None if r[1] is None else str(r[1])) for r in connection.execute(
        "SELECT entry_id, country FROM entry_countries")}
    manual = {str(r[0]): (None if r[1] is None else str(r[1])) for r in connection.execute(
        "SELECT entry_id, trip_id FROM entry_trips")}
    entries: dict[str, dict[str, Any]] = {}
    buckets = _COST_BUCKETS + _REFUND_BUCKETS
    for c in connection.execute(
        f"SELECT entry_id, origin_anchor, bucket, signed_coefficient, scale, currency, represented_start, "
        f"represented_end, date_precision, category_key, merchant_display, account FROM analysis_components "
        f"WHERE generation_token=? AND lens=? AND eligible=1 AND origin_kind='ENTRY' "
        f"AND bucket IN ({','.join('?' * len(buckets))}) ORDER BY represented_start, entry_id, component_id",
        (token, LENS, *buckets),
    ):
        entry = entries.setdefault(str(c["entry_id"]), {
            "entry_id": str(c["entry_id"]), "anchor": str(c["origin_anchor"]).removesuffix(_OWNER_SHARE),
            "on": str(c["represented_start"])[:10], "end": str(c["represented_end"])[:10],
            "date_precision": str(c["date_precision"]), "currency": str(c["currency"]),
            "merchant": str(c["merchant_display"]), "account": str(c["account"]),
            "cost": Decimal(0), "refund": Decimal(0), "scale": 2, "categories": {},
        })
        amount = Decimal(int(c["signed_coefficient"])).scaleb(-int(c["scale"]))
        entry["scale"] = max(entry["scale"], int(c["scale"]))
        part = entry["categories"].setdefault(str(c["category_key"]), {"cost": Decimal(0), "refund": Decimal(0)})
        if c["bucket"] in _COST_BUCKETS:
            entry["cost"] -= amount
            part["cost"] -= amount
        else:
            entry["refund"] += amount
            part["refund"] += amount
    for entry_id, entry in entries.items():
        # A split bill carries the owner's share in its own category beside the
        # bill's emptied amount; a slice with nothing in it is not a category of
        # this transaction unless it is the only one.
        slices = [{"category_key": key, **part} for key, part in entry["categories"].items() if part["cost"] or part["refund"]]
        entry["categories"] = slices or [{"category_key": key, **part} for key, part in entry["categories"].items()][:1]
        entry["category_key"] = max(entry["categories"], key=lambda part: part["cost"])["category_key"]
        if entry_id in countries:
            entry["country"] = countries[entry_id]
            entry["country_basis"] = "operator"
        else:
            entry["country"], entry["country_basis"] = None, "unknown"
        windows = [t for t in trips.values() if t["start_on"] <= entry["on"] and entry["end"] <= t["end_on"]]
        entry["in_windows"] = [t["trip_id"] for t in windows]
        candidates = [t["trip_id"] for t in windows if entry["country"] and entry["country"] in t["countries"]]
        entry["candidates"] = candidates
        if entry_id in manual:
            entry["trip"] = manual[entry_id]
            entry["assignment"] = "manual" if manual[entry_id] else "excluded"
        elif len(candidates) == 1:
            entry["trip"], entry["assignment"] = candidates[0], "automatic"
        elif len(candidates) > 1:
            entry["trip"], entry["assignment"] = None, "ambiguous"
        else:
            entry["trip"], entry["assignment"] = None, "none"
    return {"token": token, "trips": trips, "entries": entries}


def _row(entry: dict[str, Any], trip: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "anchor": entry["anchor"], "on": entry["on"], "end": entry["end"],
        "date_precision": entry["date_precision"], "merchant": entry["merchant"],
        "category_key": entry["category_key"], "account": entry["account"], "currency": entry["currency"],
        "cost_amount": _text(entry["cost"], entry["scale"]), "refund_amount": _text(entry["refund"], entry["scale"]),
        "country": entry["country"], "country_basis": entry["country_basis"],
        "assignment": entry["assignment"], "candidates": entry["candidates"],
        "categories": [
            {"category_key": part["category_key"], "cost_amount": _text(part["cost"], entry["scale"]),
             "refund_amount": _text(part["refund"], entry["scale"])}
            for part in entry["categories"]
        ],
    }
    if trip is not None:
        row["outside_window"] = not (trip["start_on"] <= entry["on"] and entry["end"] <= trip["end_on"])
    return row


def _status(trip: dict[str, Any], today: date) -> str:
    if today < date.fromisoformat(trip["start_on"]):
        return "upcoming"
    return "past" if today > date.fromisoformat(trip["end_on"]) else "ongoing"


def _summary(trip: dict[str, Any], members: list[dict[str, Any]], review_count: int, today: date) -> dict[str, Any]:
    totals: dict[str, dict[str, Any]] = {}
    parts: dict[tuple[str, str, str], list[Any]] = {}
    for entry in members:
        cell = totals.setdefault(entry["currency"], {"gross": Decimal(0), "refunds": Decimal(0), "count": 0, "scale": 2})
        cell["gross"] += entry["cost"]
        cell["refunds"] += entry["refund"]
        cell["count"] += 1
        cell["scale"] = max(cell["scale"], entry["scale"])
        for kind, value in (("cost", entry["cost"]), ("refund", entry["refund"])):
            if value:
                part = parts.setdefault((entry["end"], entry["currency"], kind), [Decimal(0), 2])
                part[0] += value
                part[1] = max(part[1], entry["scale"])
    return {
        **{key: trip[key] for key in ("trip_id", "name", "start_on", "end_on", "countries", "reason")},
        "status": _status(trip, today),
        "entry_count": len(members),
        "review_count": review_count,
        "totals_by_currency": {
            currency: {
                "gross_cost": _text(cell["gross"], cell["scale"]),
                "refunds": _text(cell["refunds"], cell["scale"]),
                "net_cost": _text(cell["gross"] - cell["refunds"], cell["scale"]),
                "entry_count": cell["count"],
            }
            for currency, cell in sorted(totals.items())
        },
        # Daily parts behind every total, so each is valued at its own date.
        "cost_parts": [
            {"on": on, "currency": currency, "kind": kind, "amount": _text(value, scale)}
            for (on, currency, kind), (value, scale) in sorted(parts.items())
        ],
    }


def _review(trip_id: str, entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in entries.values()
            if trip_id in e["in_windows"] and e["assignment"] in {"none", "ambiguous"}]


def trips_page_from(connection: sqlite3.Connection, *, today: date, expected: str | None = None) -> dict[str, Any]:
    state = _load(connection, expected)
    entries = state["entries"]
    return {
        "generation": state["token"], "lens": LENS, "lens_note": LENS_NOTE,
        "assignment_policy": ASSIGNMENT_POLICY, "country_list_version": COUNTRY_LIST_VERSION,
        "trips": [
            _summary(trip, [e for e in entries.values() if e["trip"] == trip_id], len(_review(trip_id, entries)), today)
            for trip_id, trip in state["trips"].items()
        ],
    }


def trip_detail_from(
    connection: sqlite3.Connection, trip_id: str, *, today: date, expected: str | None = None,
) -> dict[str, Any]:
    state = _load(connection, expected)
    trip = state["trips"].get(trip_id)
    if trip is None:
        raise FinCLError("that trip does not exist")
    entries = state["entries"]
    members = [e for e in entries.values() if e["trip"] == trip_id]
    review = _review(trip_id, entries)
    by_category: dict[tuple[str, str], list[Any]] = {}
    for entry in members:
        for part in entry["categories"]:
            cell = by_category.setdefault((entry["currency"], part["category_key"]), [Decimal(0), 2])
            cell[0] += part["cost"]
            cell[1] = max(cell[1], entry["scale"])
    in_window = [e for e in entries.values() if trip_id in e["in_windows"]]
    return {
        "generation": state["token"], "lens": LENS, "lens_note": LENS_NOTE,
        "assignment_policy": ASSIGNMENT_POLICY, "country_list_version": COUNTRY_LIST_VERSION,
        "trip": _summary(trip, members, len(review), today),
        "by_category": [
            {"currency": currency, "category_key": category, "cost_amount": _text(value, scale)}
            for (currency, category), (value, scale) in sorted(by_category.items(), key=lambda item: (item[0][0], -item[1][0]))
        ],
        "contributors": [_row(e, trip) for e in members],
        "review": {
            "missing_country": [_row(e) for e in review if e["country"] is None],
            # Ambiguous between other trips is, for this one, just another country.
            "ambiguous": [_row(e) for e in review if trip_id in e["candidates"]],
            "other_country": [_row(e) for e in review if e["country"] is not None and trip_id not in e["candidates"]],
            "excluded": [_row(e) for e in in_window if e["assignment"] == "excluded"],
        },
        "manual_assignments": _manual_assignments(connection, trip_id, entries),
    }


def _manual_assignments(
    connection: sqlite3.Connection, trip_id: str, entries: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Every manual assignment to this trip, including rows no longer counted as trip cost.

    A transaction assigned by hand and later classified as income or a transfer
    leaves the cost view but keeps its decision; retiring the trip has to retire
    it too, so the list comes from the decisions, not from the contributors.
    """
    rows = connection.execute(
        "SELECT t.entry_id, e.natural_anchor, substr(e.effective_at, 1, 10) AS on_day FROM entry_trips t "
        "JOIN ledger_entries e ON e.id = t.entry_id WHERE t.trip_id = ? ORDER BY on_day, e.natural_anchor",
        (trip_id,),
    )
    return [
        {"anchor": str(row["natural_anchor"]), "on": str(row["on_day"]), "counted": str(row["entry_id"]) in entries}
        for row in rows
    ]


def entry_context_from(connection: sqlite3.Connection, anchor: str, *, expected: str | None = None) -> dict[str, Any]:
    """One transaction's country and trip, with the trips it could be assigned to.

    A transaction outside the cost view (income, a transfer) keeps its country
    and any manual decision, but it never counts as trip cost, so it is not
    offered a new trip.
    """
    from ..decisions import resolve_anchor

    state = _load(connection, expected)
    entry_id = resolve_anchor(connection, anchor)
    if entry_id is None:
        raise FinCLError("that transaction does not exist")
    entry = state["entries"].get(entry_id)
    if entry is not None:
        country, basis, trip, assignment = entry["country"], entry["country_basis"], entry["trip"], entry["assignment"]
        candidates, in_windows, on, end = entry["candidates"], entry["in_windows"], entry["on"], entry["end"]
    else:
        set_country = connection.execute("SELECT country FROM entry_countries WHERE entry_id=?", (entry_id,)).fetchone()
        country, basis = (None, "unknown") if set_country is None else (set_country[0], "operator")
        manual = connection.execute("SELECT trip_id FROM entry_trips WHERE entry_id=?", (entry_id,)).fetchone()
        trip = None if manual is None else manual[0]
        assignment = "none" if manual is None else "manual" if trip else "excluded"
        candidates, in_windows, on, end = [], [], None, None
    return {
        "generation": state["token"], "anchor": anchor, "counted_as_cost": entry is not None,
        "on": on, "end": end, "country": country, "country_basis": basis,
        "trip": trip, "assignment": assignment, "candidates": candidates,
        "trips": [
            {"trip_id": t["trip_id"], "name": t["name"], "start_on": t["start_on"], "end_on": t["end_on"],
             "countries": t["countries"], "in_window": t["trip_id"] in in_windows}
            for t in state["trips"].values()
        ],
    }


def trips_page(database: Path, *, today: date, expected: str | None = None) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return trips_page_from(connection, today=today, expected=expected)
    finally:
        connection.close()


def trip_detail(database: Path, trip_id: str, *, today: date, expected: str | None = None) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return trip_detail_from(connection, trip_id, today=today, expected=expected)
    finally:
        connection.close()


def trip_spend_to_date(connection: sqlite3.Connection, trip_id: str, currency: str, as_of: str) -> dict[str, Any]:
    """Net trip-assigned cost through ``as_of``, in ``currency``, for matching a trip budget.

    Every transaction assigned to the trip counts, a booking made before the
    trip included; refunds reduce it. Each day's net in each pocket is valued
    at that day as a flow, through the shared valuation seam, and the parts are
    summed exactly and rounded once. A part with no rate is listed under
    ``unvalued`` and left out of ``actual``, never guessed.
    """
    state = _load(connection, None)
    if trip_id not in state["trips"]:
        raise FinCLError("that trip does not exist")
    return _spend_from(connection, state, trip_id, currency, as_of)


def _spend_from(
    connection: sqlite3.Connection, state: dict[str, Any], trip_id: str, currency: str, as_of: str,
) -> dict[str, Any]:
    from ..reporting_currency.models import currency_definition
    from ..valuation import (
        FLOW_RATE_V1,
        EntryPrecision,
        ValuationContext,
        ValuationItem,
        ValuationPurpose,
        quantize,
        value_one,
    )

    daily: dict[tuple[str, str], list[Any]] = {}
    for entry in state["entries"].values():
        if entry["trip"] != trip_id or entry["end"] > as_of:
            continue
        cell = daily.setdefault((entry["end"], entry["currency"]), [Decimal(0), 2])
        cell[0] += entry["cost"] - entry["refund"]
        cell[1] = max(cell[1], entry["scale"])
    definition = currency_definition(currency)
    scale = definition.minor_unit_scale if definition else 2
    context = ValuationContext(reporting_currency=currency, purpose=ValuationPurpose.FLOW,
                               valuation_as_of=date.fromisoformat(as_of), rate_selection_policy=FLOW_RATE_V1)
    total = Fraction(0)
    parts: list[dict[str, Any]] = []
    unvalued: list[dict[str, Any]] = []
    for (on, pocket), (value, native_scale) in sorted(daily.items()):
        native = _text(value, native_scale)
        valued = value_one(connection, ValuationItem(
            native=ExactMoney.parse(native), native_currency=pocket, purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.DAY, effective_on=date.fromisoformat(on),
        ), context=context)
        if valued.unavailable is not None or valued.reported_exact is None:
            reason = valued.unavailable
            unvalued.append({"on": on, "native_currency": pocket, "native_amount": native,
                             "reason": reason.message if reason else "not valued",
                             "remedy": reason.remedy if reason else ""})
            continue
        total += valued.reported_exact
        parts.append({"on": on, "amount": quantize(valued.reported_exact, scale=scale).text,
                      "native_currency": pocket, "native_amount": native})
    return {
        "trip_id": trip_id, "currency": currency, "as_of": as_of,
        "actual": quantize(total, scale=scale).text, "parts": parts, "unvalued": unvalued,
    }


def trip_assigned_entries(connection: sqlite3.Connection) -> dict[str, str]:
    """Every transaction counted toward a trip, by entry id, with its trip.

    Assignment is derived on read from windows, countries and manual choices;
    this is the one place outside this module that may ask for it, so the
    baseline never re-derives it.
    """
    state = _load(connection, None)
    return {entry_id: str(entry["trip"]) for entry_id, entry in state["entries"].items() if entry["trip"]}


def entry_context(database: Path, anchor: str, *, expected: str | None = None) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return entry_context_from(connection, anchor, expected=expected)
    finally:
        connection.close()


def country_list() -> dict[str, Any]:
    from .countries import COUNTRY_CODES
    return {"version": COUNTRY_LIST_VERSION, "codes": sorted(COUNTRY_CODES)}
