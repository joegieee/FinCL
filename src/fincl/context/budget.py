"""A trip's budget against what was spent: the planned items linked to it.

The budget is whatever planned spending carries the trip's link; the spend is
the trip's own assigned, net cost through today, valued in the budget's
currency. Both come from the server, per currency, so the page only shows them.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

_ZERO = Decimal(0)


def trip_budgets(
    connection: sqlite3.Connection, state: dict[str, Any], *, today: date,
) -> dict[str, list[dict[str, Any]]]:
    """Per trip, one cell per currency of its linked planned spending."""
    if not connection.execute("SELECT 1 FROM planned_flows WHERE link_kind = 'trip' LIMIT 1").fetchone():
        return {}
    from ..planned.models import LINK_TRIP, OUTCOME_CANCELLED, OUTCOME_DONE, STATE_OVERDUE
    from ..planned.queries import assessed, horizon_for, money
    from .queries import _spend_from

    def spend(conn: sqlite3.Connection, trip_id: str, currency: str, as_of: str) -> dict[str, Any]:
        return _spend_from(conn, state, trip_id, currency, as_of)

    flows, _ = assessed(connection, horizon_for(connection, today), trip_spend=spend, today=today)
    grouped: dict[tuple[str, str], list[Any]] = {}
    for flow, rows, _ in flows:
        if flow["link_kind"] == LINK_TRIP and str(flow["link_id"]) in state["trips"]:
            grouped.setdefault((str(flow["link_id"]), str(flow["currency"])), []).append((flow, rows))

    result: dict[str, list[dict[str, Any]]] = {}
    for (trip_id, currency), members in sorted(grouped.items()):
        occurrences = [row for _, rows in members for row in rows]
        counted = [row for row in occurrences if row.occurrence.outcome != OUTCOME_CANCELLED]
        planned = sum((row.occurrence.planned for row in counted), _ZERO)
        spent = spend(connection, trip_id, currency, today.isoformat())
        actual = Decimal(spent["actual"])
        finished = all(row.occurrence.outcome == OUTCOME_DONE for row in counted)
        remaining = _ZERO if finished else max(planned - actual, _ZERO)
        overdue = [row for row in counted if row.assessment.state == STATE_OVERDUE]
        result.setdefault(trip_id, []).append({
            "currency": currency,
            "as_of": today.isoformat(),
            "planned": money(planned),
            "actual": money(actual),
            "remaining": money(remaining),
            "over_plan": money(actual - planned) if actual > planned else None,
            "over_budget": actual > planned,
            "missed": bool(overdue),
            "unvalued_count": len(spent["unvalued"]),
            "flows": [
                {
                    "flow": str(flow["flow"]), "name": str(flow["name"]), "certainty": str(flow["certainty"]),
                    "earliest": min((row.occurrence.earliest for row in rows), default=None),
                    "latest": max((row.occurrence.latest for row in rows), default=None),
                }
                for flow, rows in members
            ],
        })
    return result


def unassigned_todo(
    trip: dict[str, Any], review: list[dict[str, Any]], status: str,
) -> dict[str, Any] | None:
    """The to-do a past or running trip raises while spending in its dates is unplaced.

    Only rows the owner can settle count: no country yet, or several trips
    claiming them. A transaction in another country has already been answered.
    """
    if status == "upcoming":
        return None
    open_rows = [e for e in review if e["country"] is None or trip["trip_id"] in e["candidates"]]
    if not open_rows:
        return None
    count = len(open_rows)
    return {
        "kind": "assign_trip_spend", "trip_id": trip["trip_id"], "name": trip["name"], "count": count,
        "text": f"{count} transaction{' falls' if count == 1 else 's fall'} inside {trip['name']}'s dates "
                "but " + ("is" if count == 1 else "are") + " not assigned to it",
    }


__all__ = ["trip_budgets", "unassigned_todo"]
