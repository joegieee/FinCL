"""Schedule date arithmetic for recurring commitments (DESIGN-personal-finance §7).

A monthly schedule on day N clamps to the month's last day when N overflows,
then returns to N the next month: day 31 gives Jan 31 → Feb 28/29 → Mar 31.
A weekly schedule fires every 7 days from its anchor weekday. This module is
pure date math; matching observed entries to occurrences lives elsewhere.

Two interval kinds carry the cadences a plain weekly/monthly schedule cannot
express. ``interval_weeks`` fires every N weeks from its anchor, which is what a
fortnightly or four-weekly commitment needs: a four-weekly subscription drifts
nearly two weeks away from a monthly one over a year, so modelling it as monthly
loses a payment. ``interval_months`` fires every N months on a clamped day,
which is what a quarterly, semiannual or annual bill needs. Both anchor on a
real observed date, so the phase of the cycle is evidence rather than a default.

The original ``weekly`` and ``monthly`` payloads are unchanged and still parse
exactly as before, so every commitment already recorded keeps its meaning.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

from ..errors import FinCLError

SCHEDULE_KINDS = ("weekly", "monthly", "interval_weeks", "interval_months")

# The cadence names detection emits, mapped to the schedule that reproduces them.
# ``weeks``/``months`` is the interval; ``None`` means the plain kind already
# expresses it.
CADENCE_SCHEDULES: dict[str, tuple[str, int | None]] = {
    "weekly": ("weekly", None),
    "fortnightly": ("interval_weeks", 2),
    "four-weekly": ("interval_weeks", 4),
    "monthly": ("monthly", None),
    "quarterly": ("interval_months", 3),
    "semiannual": ("interval_months", 6),
    "annual": ("interval_months", 12),
}

_MAX_WEEKS = 52
_MAX_MONTHS = 60


def parse_schedule(value: Any) -> dict[str, Any]:
    """Validate a schedule payload and return it normalised."""
    if not isinstance(value, dict) or value.get("kind") not in SCHEDULE_KINDS:
        raise FinCLError("schedule.kind must be one of " + ", ".join(SCHEDULE_KINDS))
    anchor = value.get("anchor_on")
    if not isinstance(anchor, str):
        raise FinCLError("schedule.anchor_on must be a YYYY-MM-DD date")
    try:
        anchor_date = date.fromisoformat(anchor)
    except ValueError as exc:
        raise FinCLError("schedule.anchor_on is not a real date") from exc
    if value["kind"] == "interval_weeks":
        if set(value) != {"kind", "weeks", "anchor_on"}:
            raise FinCLError(
                "interval_weeks schedule fields must be {kind, weeks, anchor_on}"
            )
        weeks = value["weeks"]
        if not isinstance(weeks, int) or isinstance(weeks, bool) or not 1 <= weeks <= _MAX_WEEKS:
            raise FinCLError(f"schedule.weeks must be an integer 1-{_MAX_WEEKS}")
        return {"kind": "interval_weeks", "weeks": weeks, "anchor_on": anchor}
    if value["kind"] == "interval_months":
        if set(value) != {"kind", "months", "day", "anchor_on"}:
            raise FinCLError(
                "interval_months schedule fields must be {kind, months, day, anchor_on}"
            )
        months = value["months"]
        if not isinstance(months, int) or isinstance(months, bool) or not 1 <= months <= _MAX_MONTHS:
            raise FinCLError(f"schedule.months must be an integer 1-{_MAX_MONTHS}")
        day = value["day"]
        if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 31:
            raise FinCLError("schedule.day must be an integer 1-31")
        return {"kind": "interval_months", "months": months, "day": day, "anchor_on": anchor}
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
    if schedule["kind"] == "interval_weeks":
        step = timedelta(days=7 * int(schedule["weeks"]))
        # Walk from the anchor to the first occurrence at or after `start`,
        # keeping the anchor's phase in both directions so a window before the
        # anchor still lands on the same cycle.
        current = anchor
        while current > start:
            current -= step
        while current < start:
            current += step
        while current <= end:
            result.append(current)
            current += step
        return result
    if schedule["kind"] == "interval_months":
        interval = int(schedule["months"])
        day = int(schedule["day"])
        # Count whole intervals from the anchor's month, backing up one interval
        # so a window opening mid-cycle still catches its own occurrence.
        offset = (start.year - anchor.year) * 12 + (start.month - anchor.month)
        step_index = offset // interval - 1
        while True:
            total = anchor.year * 12 + (anchor.month - 1) + step_index * interval
            occurrence = _clamp_monthly(total // 12, total % 12 + 1, day)
            if occurrence > end:
                break
            if occurrence >= start:
                result.append(occurrence)
            step_index += 1
        return result
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
