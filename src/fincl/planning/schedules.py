"""Schedule date arithmetic for recurring commitments (DESIGN-personal-finance §7).

A monthly schedule on day N clamps to the month's last day when N overflows,
then returns to N the next month: day 31 gives Jan 31 → Feb 28/29 → Mar 31.
A weekly schedule fires every 7 days from its anchor weekday. This module is
pure date math; matching observed entries to occurrences lives elsewhere.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

from ..errors import FinCLError


def parse_schedule(value: Any) -> dict[str, Any]:
    """Validate a schedule payload and return it normalised."""
    if not isinstance(value, dict) or value.get("kind") not in {"weekly", "monthly"}:
        raise FinCLError("schedule.kind must be 'weekly' or 'monthly'")
    anchor = value.get("anchor_on")
    if not isinstance(anchor, str):
        raise FinCLError("schedule.anchor_on must be a YYYY-MM-DD date")
    try:
        anchor_date = date.fromisoformat(anchor)
    except ValueError as exc:
        raise FinCLError("schedule.anchor_on is not a real date") from exc
    if value["kind"] == "weekly":
        if set(value) != {"kind", "weekday", "anchor_on"}:
            raise FinCLError("weekly schedule fields must be {kind, weekday, anchor_on}")
        weekday = value["weekday"]
        if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
            raise FinCLError("schedule.weekday must be an integer 0–6 (Monday=0)")
        if anchor_date.weekday() != weekday:
            raise FinCLError("schedule.anchor_on must fall on schedule.weekday")
        return {"kind": "weekly", "weekday": weekday, "anchor_on": anchor}
    if set(value) != {"kind", "day", "anchor_on"}:
        raise FinCLError("monthly schedule fields must be {kind, day, anchor_on}")
    day = value["day"]
    if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 31:
        raise FinCLError("schedule.day must be an integer 1–31")
    return {"kind": "monthly", "day": day, "anchor_on": anchor}


def _clamp_monthly(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def occurrences(schedule: dict[str, Any], start: date, end: date) -> list[date]:
    """Every scheduled date in the inclusive [start, end] window.

    The configured day/weekday is authoritative: an overflowed monthly day
    clamps for that month only and returns to the configured day next month.
    """
    if start > end:
        return []
    anchor = date.fromisoformat(schedule["anchor_on"])
    result: list[date] = []
    if schedule["kind"] == "weekly":
        step = timedelta(days=7)
        current = anchor
        while current < start:
            current += step
        while current > start:
            current -= step
        while current < start:
            current += step
        while current <= end:
            result.append(current)
            current += step
        return result
    day = schedule["day"]
    year, month = start.year, start.month
    # Back up one month so a start mid-month still catches its own occurrence.
    if month == 1:
        year, month = year - 1, 12
    else:
        month -= 1
    while True:
        occurrence = _clamp_monthly(year, month, day)
        if occurrence > end:
            break
        if occurrence >= start:
            result.append(occurrence)
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return result
