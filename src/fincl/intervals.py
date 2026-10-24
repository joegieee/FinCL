"""Inclusive calendar-date interval sets for the coverage contract (F23).

Coverage is expressed as a set of inclusive ``[start, end]`` calendar-date
intervals in Asia/Singapore.  A source family contributes one such set; the
covered set is the intersection of every required family's set.

Every function here is pure and operates on ISO ``YYYY-MM-DD`` strings, which
sort lexicographically in date order, so the arithmetic never needs a timezone.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from .errors import FinCLError

COVERAGE_POLICY_VERSION = "coverage-intervals-v1"


@dataclass(frozen=True, slots=True, order=True)
class DateInterval:
    """An inclusive ``[start, end]`` span of calendar dates."""

    start: str
    end: str

    def __post_init__(self) -> None:
        try:
            start = date.fromisoformat(self.start)
            end = date.fromisoformat(self.end)
        except ValueError as exc:
            raise FinCLError(
                f"coverage interval needs real YYYY-MM-DD dates: {self.start}..{self.end}"
            ) from exc
        if start > end:
            raise FinCLError(
                f"coverage interval start is after its end: {self.start}..{self.end}"
            )

    def contains_date(self, day: str) -> bool:
        return self.start <= day <= self.end

    def contains_interval(self, other: DateInterval) -> bool:
        return self.start <= other.start and other.end <= self.end

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start, "end": self.end}


def _next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def normalize(intervals: Iterable[DateInterval]) -> list[DateInterval]:
    """Sort and merge a family's intervals, joining overlaps and adjacent days.

    Two intervals merge when they overlap or when one ends the calendar day
    before the other begins.  The result is disjoint and gap-separated by at
    least one clear day.
    """
    ordered = sorted(intervals)
    merged: list[DateInterval] = []
    for interval in ordered:
        if merged and interval.start <= _next_day(merged[-1].end):
            previous = merged[-1]
            merged[-1] = DateInterval(previous.start, max(previous.end, interval.end))
        else:
            merged.append(interval)
    return merged


def intersect_two(a: list[DateInterval], b: list[DateInterval]) -> list[DateInterval]:
    """Intersect two already-normalized interval sets."""
    result: list[DateInterval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i].start, b[j].start)
        hi = min(a[i].end, b[j].end)
        if lo <= hi:
            result.append(DateInterval(lo, hi))
        if a[i].end <= b[j].end:
            i += 1
        else:
            j += 1
    return result


def intersect_all(sets: list[list[DateInterval]]) -> list[DateInterval]:
    """Intersect every required family's normalized set.

    An empty ``sets`` list means no required family was found and yields an
    empty result; a single empty member also yields an empty result, so a
    required family that produced no eligible interval cannot silently drop out
    of the intersection.
    """
    if not sets:
        return []
    accumulator = normalize(sets[0])
    for member in sets[1:]:
        accumulator = intersect_two(accumulator, normalize(member))
        if not accumulator:
            return []
    return accumulator


def contains_range(intervals: list[DateInterval], start: str, end: str) -> bool:
    """True when ``[start, end]`` lies wholly inside a single member interval."""
    candidate = DateInterval(start, end)
    return any(interval.contains_interval(candidate) for interval in intervals)


def covers_date(intervals: list[DateInterval], day: str) -> bool:
    return any(interval.contains_date(day) for interval in intervals)


def covering_interval(intervals: list[DateInterval], other: DateInterval) -> DateInterval | None:
    for interval in intervals:
        if interval.contains_interval(other):
            return interval
    return None


def bounding(intervals: list[DateInterval]) -> DateInterval | None:
    """The one interval when the set is a single span, else ``None``.

    The legacy ``covered_window`` / ``analysis_window`` bounding fields are only
    meaningful for a contiguous set; for zero or several intervals a caller must
    fall back to the full interval list.
    """
    return intervals[0] if len(intervals) == 1 else None


def uncovered_dates(intervals: list[DateInterval], start: str, end: str) -> list[DateInterval]:
    """The sub-spans of ``[start, end]`` that no member interval covers."""
    requested = DateInterval(start, end)
    gaps: list[DateInterval] = []
    cursor = requested.start
    for interval in normalize(intervals):
        if interval.end < cursor or interval.start > requested.end:
            continue
        if interval.start > cursor:
            gaps.append(DateInterval(cursor, _prev_day(interval.start)))
        cursor = max(cursor, _next_day(interval.end))
        if cursor > requested.end:
            break
    if cursor <= requested.end:
        gaps.append(DateInterval(cursor, requested.end))
    return gaps


def _prev_day(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def month_span(year_month: str) -> DateInterval:
    """The full ``[first, last]`` day interval represented by ``YYYY-MM``."""
    first = date.fromisoformat(f"{year_month}-01")
    if first.month == 12:
        following = date(first.year + 1, 1, 1)
    else:
        following = date(first.year, first.month + 1, 1)
    return DateInterval(first.isoformat(), (following - timedelta(days=1)).isoformat())
