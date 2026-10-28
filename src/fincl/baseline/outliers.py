"""Shared outlier-v1 rule (cash-flow usability §3.1, §3.2).

A spending component is held out as irregular risk when its own amount is at
least :data:`OUTLIER_MULTIPLE`x its category's median month and at least
:data:`OUTLIER_FLOOR`.  It stays in the historical total; it only leaves the
routine residual.

The Stage-1C estimator and the baseline role read each apply this rule against
the recommended baseline window.  A per-transaction consumer needs only the set
of flagged natural anchors so the effective-field read can surface an
``irregular`` proposal at the ``suggested`` review state.  It never overrides a
stronger basis: an anchor bound to an active confirmed recurring occurrence is
excluded here, and the caller keeps manual / rule / recurring decisions ahead of
the suggestion.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from ..context.queries import trip_assigned_entries

OUTLIER_MULTIPLE = Decimal("3")
OUTLIER_FLOOR = Decimal("150")
OUTLIER_METHOD = "outlier-v1: entry ≥ 3× category median-month and ≥ 150"


def _median(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def outlier_anchor_set(
    connection: sqlite3.Connection, token: str, *, months: list[str] | None = None
) -> set[str]:
    """Natural anchors of spending components flagged by ``outlier-v1``.

    ``months`` restricts both the median basis and the flag to those periods;
    without it every recorded-flow period contributes.  Anchors bound to an
    active confirmed recurring occurrence are never flagged.  Spending on a
    trip is neither flagged nor part of the median basis, as in the estimator.
    """
    if not token:
        return set()
    on_trips = trip_assigned_entries(connection)
    rows = connection.execute(
        """SELECT period, currency, category_key, origin_anchor, entry_id,
                  signed_coefficient, scale
             FROM analysis_components
            WHERE generation_token = ? AND lens = 'recorded_flow'
              AND bucket IN ('spending', 'fees')""",
        (token,),
    ).fetchall()
    recurring = {
        row["entry_anchor"]
        for row in connection.execute(
            """SELECT o.entry_anchor FROM recurring_occurrences o
               JOIN recurring_commitments c ON c.commitment = o.commitment
               WHERE o.bound = 1 AND o.entry_anchor IS NOT NULL AND c.status = 'active'"""
        )
    }
    scoped = [
        row for row in rows
        if (months is None or row["period"] in months) and row["entry_id"] not in on_trips
    ]
    category_month: dict[tuple[str, str, str], Decimal] = {}
    for row in scoped:
        key = (row["currency"], row["category_key"], row["period"])
        amount = abs(Decimal(row["signed_coefficient"]).scaleb(-int(row["scale"])))
        category_month[key] = category_month.get(key, Decimal(0)) + amount

    periods = months or sorted({row["period"] for row in scoped})
    medians: dict[tuple[str, str], Decimal] = {}
    for currency, category, _ in category_month:
        if (currency, category) in medians:
            continue
        medians[(currency, category)] = _median(
            [category_month.get((currency, category, month), Decimal(0)) for month in periods]
        )

    flagged: set[str] = set()
    for row in scoped:
        if row["origin_anchor"] in recurring:
            continue
        amount = abs(Decimal(row["signed_coefficient"]).scaleb(-int(row["scale"])))
        threshold = max(OUTLIER_FLOOR, medians[(row["currency"], row["category_key"])] * OUTLIER_MULTIPLE)
        if amount >= threshold and amount > 0:
            flagged.add(row["origin_anchor"])
    return flagged


__all__ = ["OUTLIER_MULTIPLE", "OUTLIER_FLOOR", "OUTLIER_METHOD", "outlier_anchor_set"]
