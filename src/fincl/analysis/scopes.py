"""Analysis scope normalization and comparison-window resolution (v0.2 slice 2).

The scope is what makes an analysis answer reproducible: window, currency, lens
and the account / category / merchant narrowing. The comparison window is
resolved from it under the design rules — a full calendar month compares against
the previous complete calendar month; any other range compares against the
equal-duration period immediately before it, never 31 days against 28.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from ..errors import FinCLError
from ..intervals import DateInterval, contains_range

LENS_RECORDED_FLOW = "recorded_flow"

_COMPARISON_KINDS = ("auto", "none", "previous_month", "preceding_months", "preceding_equal_duration")


@dataclass(frozen=True, slots=True)
class AnalysisScope:
    start: str
    end: str
    currency: str
    lens: str
    accounts: tuple[str, ...]
    category_keys: tuple[str, ...]
    merchant: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "currency": self.currency,
            "lens": self.lens,
            "accounts": list(self.accounts),
            "category_keys": list(self.category_keys),
            "merchant": self.merchant,
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    kind: str
    start: str | None
    end: str | None
    available: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "available": self.available,
            "reasons": list(self.reasons),
        }


def _parse(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError(f"{label} must use YYYY-MM-DD") from exc


def normalize_scope(
    *,
    start: str,
    end: str,
    currency: str = "SGD",
    accounts: list[str] | tuple[str, ...] = (),
    category_keys: list[str] | tuple[str, ...] = (),
    merchant: str | None = None,
    lens: str = LENS_RECORDED_FLOW,
) -> AnalysisScope:
    first, last = _parse(start, "Analysis start"), _parse(end, "Analysis end")
    if first > last:
        raise FinCLError("Analysis start must not be after its end")
    if lens != LENS_RECORDED_FLOW:
        raise FinCLError(f"unknown analysis lens {lens!r}")
    clean_merchant = merchant.strip() if merchant and merchant.strip() else None
    return AnalysisScope(
        start=start,
        end=end,
        currency=currency,
        lens=lens,
        accounts=tuple(dict.fromkeys(a for a in accounts if a)),
        category_keys=tuple(dict.fromkeys(c for c in category_keys if c)),
        merchant=clean_merchant,
    )


def is_full_calendar_month(start: str, end: str) -> bool:
    first, last = _parse(start, "start"), _parse(end, "end")
    return (
        first.day == 1
        and first.year == last.year
        and first.month == last.month
        and last.day == calendar.monthrange(last.year, last.month)[1]
    )


def is_whole_months(start: str, end: str) -> bool:
    first, last = _parse(start, "start"), _parse(end, "end")
    return first.day == 1 and last.day == calendar.monthrange(last.year, last.month)[1]


def _preceding_months(start: str, end: str) -> tuple[str, str]:
    first, last = _parse(start, "start"), _parse(end, "end")
    months = (last.year - first.year) * 12 + last.month - first.month + 1
    year, month = divmod(first.year * 12 + first.month - 1 - months, 12)
    return date(year, month + 1, 1).isoformat(), (first - timedelta(days=1)).isoformat()


def _previous_month(start: str) -> tuple[str, str]:
    first = _parse(start, "start")
    year, month = (first.year - 1, 12) if first.month == 1 else (first.year, first.month - 1)
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1).isoformat(), date(year, month, last_day).isoformat()


def _preceding_equal_duration(start: str, end: str) -> tuple[str, str]:
    first, last = _parse(start, "start"), _parse(end, "end")
    span = (last - first).days
    comp_end = first - timedelta(days=1)
    comp_start = comp_end - timedelta(days=span)
    return comp_start.isoformat(), comp_end.isoformat()


def resolve_comparison(
    scope: AnalysisScope,
    covered_intervals: list[DateInterval],
    *,
    requested: str | None,
    main_covered: bool,
) -> Comparison:
    kind = requested or "auto"
    if kind not in _COMPARISON_KINDS:
        raise FinCLError(f"unknown comparison {kind!r}")
    if kind == "none":
        return Comparison("none", None, None, False, ())
    if kind == "auto":
        kind = (
            "previous_month"
            if is_full_calendar_month(scope.start, scope.end)
            else "preceding_months"
            if is_whole_months(scope.start, scope.end)
            else "preceding_equal_duration"
        )
    elif kind == "previous_month" and not is_full_calendar_month(scope.start, scope.end):
        raise FinCLError(
            "a previous-month comparison needs a single complete calendar month"
        )
    elif kind == "preceding_months" and not is_whole_months(scope.start, scope.end):
        raise FinCLError("a preceding-months comparison needs whole calendar months")

    if kind == "previous_month":
        comp_start, comp_end = _previous_month(scope.start)
    elif kind == "preceding_months":
        comp_start, comp_end = _preceding_months(scope.start, scope.end)
    else:
        comp_start, comp_end = _preceding_equal_duration(scope.start, scope.end)

    reasons: list[str] = []
    if not main_covered:
        reasons.append("the selected period is not fully covered")
    if not contains_range(covered_intervals, comp_start, comp_end):
        reasons.append(
            f"the comparison period {comp_start}..{comp_end} is not fully covered"
        )
    return Comparison(kind, comp_start, comp_end, not reasons, tuple(reasons))
