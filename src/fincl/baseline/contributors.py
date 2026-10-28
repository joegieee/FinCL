"""The components behind one month of the baseline decomposition.

Every owner-cost component of the month is assigned to exactly one partition
term, using the estimator's own classification: a component bound to an active
recurring commitment is ``identified_recurring``, spending assigned to a trip
is ``trip`` (with the trip it belongs to), a component the estimator held out
under ``outlier-v1`` is ``unresolved_irregular``, refunds and
reimbursements are ``adjustments`` (signed negative), and everything else is
``routine_variable_residual``. Each term's contributors sum to the figure the
estimate published for that month, and the read says whether they do.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..context.queries import trip_assigned_entries
from ..db import connect_readonly
from ..errors import FinCLError
from ..intervals import month_span
from ..money import format_decimal
from .estimators import _recurring_matched_from, baseline_estimate_from

PARTS = ("identified_recurring", "trip", "unresolved_irregular", "routine_variable_residual", "adjustments")


def baseline_contributors(database: Path, *, today: date, currency: str, month: str) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return baseline_contributors_from(connection, today=today, currency=currency, month=month)
    finally:
        connection.close()


def baseline_contributors_from(
    connection: sqlite3.Connection, *, today: date, currency: str, month: str
) -> dict[str, Any]:
    estimate = baseline_estimate_from(connection, today=today)
    if not estimate["available"]:
        return {"available": False, "reason": estimate["reason"], "baseline_window": estimate["baseline_window"]}
    months = estimate["sample"]["months"]
    cell = estimate["by_currency"].get(currency)
    if cell is None:
        raise FinCLError(f"{currency} is not in the baseline estimate")
    if month not in months:
        raise FinCLError(f"{month} is outside the baseline window {months[0]} to {months[-1]}")
    published = next(row for row in cell["months"] if row["month"] == month)

    token = estimate["generation"]
    span_start, span_end = month_span(months[0]).start, month_span(months[-1]).end
    _, recurring_anchors = _recurring_matched_from(connection, token, span_start, span_end)
    irregular_ids = {entry["component_id"] for entry in cell["irregular_risk_entries"]}
    on_trips = trip_assigned_entries(connection)
    rows = connection.execute(
        """
        SELECT component_id, entry_id, origin_anchor, bucket, category_key, merchant_display,
               account, represented_start, date_precision, signed_coefficient, scale
        FROM analysis_components
        WHERE generation_token = ? AND lens = 'recorded_flow' AND currency = ? AND period = ?
          AND bucket IN ('spending', 'fees', 'refunds', 'reimbursements')
          AND represented_start >= ? AND represented_end <= ?
        ORDER BY represented_start, origin_anchor, component_id
        """,
        (token, currency, month, span_start, span_end),
    ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {part: [] for part in PARTS}
    for row in rows:
        magnitude = abs(int(row["signed_coefficient"]))
        if row["bucket"] in ("refunds", "reimbursements"):
            part, coefficient = "adjustments", -magnitude
        elif row["origin_anchor"] in recurring_anchors:
            part, coefficient = "identified_recurring", magnitude
        elif row["entry_id"] in on_trips:
            part, coefficient = "trip", magnitude
        elif row["component_id"] in irregular_ids:
            part, coefficient = "unresolved_irregular", magnitude
        else:
            part, coefficient = "routine_variable_residual", magnitude
        grouped[part].append(
            {
                "component_id": row["component_id"],
                "entry_anchor": row["origin_anchor"],
                "date": row["represented_start"],
                "precision": row["date_precision"],
                "merchant": row["merchant_display"],
                "category_key": row["category_key"],
                "bucket": row["bucket"],
                "account": row["account"],
                "amount": format_decimal(coefficient, int(row["scale"])),
                "trip_id": on_trips.get(row["entry_id"]) if part == "trip" else None,
            }
        )

    parts = []
    for part, contributors in grouped.items():
        total = sum((Decimal(item["amount"]) for item in contributors), Decimal(0)).quantize(Decimal("0.01"))
        parts.append(
            {
                "part": part,
                "total": str(total),
                "reconciles": total == Decimal(published[part]),
                "contributors": contributors,
            }
        )
    return {
        "available": True,
        "generation": token,
        "currency": currency,
        "month": month,
        "owner_cost_recorded": published["owner_cost_recorded"],
        "reconciles": all(part["reconciles"] for part in parts),
        "parts": parts,
    }


__all__ = ["baseline_contributors"]
