"""The one seam through which a trip-linked planned item learns its actual spend.

A trip budget is never matched by hand: its actual is the trip's own assigned,
net spend, valued in the item's currency at each part's date, as the trips
context publishes it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

TripSpend = Callable[[sqlite3.Connection, str, str, str], dict[str, Any]]


def trip_spend_to_date(
    connection: sqlite3.Connection, trip_id: str, currency: str, as_of: str
) -> dict[str, Any]:
    from ..context.queries import trip_spend_to_date as spend

    return spend(connection, trip_id, currency, as_of)


__all__ = ["TripSpend", "trip_spend_to_date"]
