"""The one seam through which a trip-linked planned item learns its actual spend.

A trip budget is never matched by hand: its actual is the trip's own assigned,
net spend, valued in the item's currency at each part's date. That figure is
owned by the trips context. Until it publishes ``trip_spend_to_date``, this
reports no spend, so a trip-linked item reads as not yet started rather than as
settled by a guess.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

TripSpend = Callable[[sqlite3.Connection, str, str, str], dict[str, Any]]


def trip_spend_to_date(
    connection: sqlite3.Connection, trip_id: str, currency: str, as_of: str
) -> dict[str, Any]:
    return {"trip_id": trip_id, "currency": currency, "actual": "0.00", "parts": [], "unvalued": []}


__all__ = ["TripSpend", "trip_spend_to_date"]
