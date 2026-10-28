"""Stage 3B — the pure cash-timeline arithmetic.

The arithmetic core takes a starting balance, sourced cash events and an
optional reserve target, then returns the day-by-day and month-by-month
consequence without mutating the ledger.  :func:`cash_timeline` is the thin
read-only composition layer over the Stage 3A manifest and the established
source adapters.  Keeping scenario construction pure is what makes the two
failures that matter testable in isolation:

* a month that closes positive while an earlier day inside it went to zero —
  month-end arithmetic alone cannot see that, a daily walk can; and
* the monotonicity property — raising an outflow or lowering an inflow must
  never *improve* any balance on any day.  That holds by construction here
  (the balance is a running sum of the event amounts), and the tests exercise
  this function directly rather than a whole ledger read so a regression names
  the arithmetic rather than a fixture.

Money is integer minor units at one declared scale throughout.  The caller
chooses the scale (never below two) and formats on the way out; no float and
no rounding ever happens inside the walk.

Declared conventions
--------------------
Three modelling assumptions are unavoidable and all three are stated in the
output rather than buried here: :data:`SAME_DAY_CONVENTION` (what to do when
the evidence does not order two events on one day), :data:`DAILY_ALLOCATION_RULE`
(how an unscheduled monthly cost becomes daily amounts) and
:data:`PARTIAL_MONTH_RULE` (what a partial month costs).
"""

from __future__ import annotations

import calendar
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..baseline.estimators import baseline_estimate_from
from ..cards.cash_conversion import card_cash_conversion_from
from ..db import connect_readonly
from ..money import ExactMoney, format_decimal
from ..obligations.queries import obligations_page_from
from ..planning.queries import recurring_page_from
from ..planning.schedules import occurrences as schedule_occurrences
from ..read_cache import request_cache
from .manifest import READINESS_UNAVAILABLE, forecast_input_manifest_from

ENGINE_VERSION = "cash-timeline-engine-v1"
HORIZON_MONTHS = 6

# --- the output columns -----------------------------------------------------
# Lifestyle cost, liability settlement, irregular cost and forward allocation
# answer different questions and must never be netted into one "expenses" line:
# a month whose lifestyle is flat and whose card settlement doubled is not the
# same month as its mirror image.
COLUMN_INCOME = "income"
COLUMN_LIFESTYLE = "lifestyle_costs"
COLUMN_DEBT_CARD = "debt_and_card_payments"
COLUMN_IRREGULAR = "known_irregular_costs"
COLUMN_ALLOCATION = "future_allocations"
EXPENSE_COLUMNS = (
    COLUMN_LIFESTYLE,
    COLUMN_DEBT_CARD,
    COLUMN_IRREGULAR,
    COLUMN_ALLOCATION,
)
COLUMNS = (COLUMN_INCOME, *EXPENSE_COLUMNS)

DIRECTION_IN = "inflow"
DIRECTION_OUT = "outflow"

# --- timing vocabulary ------------------------------------------------------
TIMING_KNOWN = "known"
TIMING_INFERRED = "inferred"
TIMING_UNRESOLVED = "unresolved"

PRECISION_DAY = "day"
PRECISION_WINDOW = "window"
PRECISION_UNRESOLVED = "unresolved"

SAME_DAY_CONVENTION = (
    "same-day-outflows-first-v1: when several events land on one day and no "
    "evidence orders them, every outflow is applied before every inflow. The "
    "day's low point is therefore the worst ordering that day admits, so an "
    "intra-day shortfall is never hidden by a receipt that may in fact arrive "
    "later in the day. This is a stress convention, not a claim about the "
    "actual order."
)

DAILY_ALLOCATION_RULE = (
    "daily-cent-allocation-v1: an unscheduled monthly cost is divided into "
    "equal whole minor units across the days it covers; the remainder of N "
    "units is added one unit per day to the first N days. The period total is "
    "exact; no single day is. This is a declared modelling assumption about "
    "when unscheduled money leaves, never evidence that it leaves then."
)

PARTIAL_MONTH_RULE = (
    "remaining-day-rate-v1: a partial month carries the monthly rate for the "
    "days that remain in it (monthly amount x remaining days / days in month), "
    "never the monthly amount less what was already spent. Spending recorded "
    "earlier in the month is history, not credit against the groceries still "
    "to come."
)

CONVENTIONS = {
    "engine_version": ENGINE_VERSION,
    "same_day_ordering": SAME_DAY_CONVENTION,
    "daily_allocation": DAILY_ALLOCATION_RULE,
    "partial_month": PARTIAL_MONTH_RULE,
}

CONFIDENCE_CONFIRMED = "confirmed"
CONFIDENCE_LIKELY = "likely"
CONFIDENCE_POSSIBLE = "possible"
CONFIDENCE_LEVELS = (
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_LIKELY,
    CONFIDENCE_POSSIBLE,
)

LIKELY_INCLUDE = "include"
LIKELY_EXCLUDE = "exclude"
LIKELY_POLICIES = (LIKELY_INCLUDE, LIKELY_EXCLUDE)

SCENARIO_BASE = "base"
SCENARIO_CONSERVATIVE = "conservative"


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One dated cash movement, already resolved onto a scenario's calendar.

    ``source_id`` is the canonical identifier of the thing being settled (an
    obligation id, a card settlement source id, an allocation slice key).  It
    is what :func:`settlement_reconciliation` checks for exactly-once, so two
    events that settle the same underlying money must carry the same
    ``source_id`` and a scenario must never emit it twice.
    """

    event_id: str
    source_id: str
    column: str
    direction: str
    on: str
    amount_minor: int
    label: str
    timing_basis: str = TIMING_KNOWN
    window_earliest: str | None = None
    window_latest: str | None = None
    conditional: bool = False
    condition: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if self.column not in COLUMNS:
            raise ValueError(f"unknown timeline column: {self.column!r}")
        if self.direction not in (DIRECTION_IN, DIRECTION_OUT):
            raise ValueError(f"unknown direction: {self.direction!r}")
        if self.amount_minor < 0:
            raise ValueError("a timeline event carries a magnitude, never a sign")
        if self.timing_basis not in (TIMING_KNOWN, TIMING_INFERRED, TIMING_UNRESOLVED):
            raise ValueError(f"unknown timing basis: {self.timing_basis!r}")
        event_day = date.fromisoformat(self.on)
        earliest = date.fromisoformat(self.window_earliest or self.on)
        latest = date.fromisoformat(self.window_latest or self.on)
        if earliest > event_day or event_day > latest:
            raise ValueError("the event date must fall inside its timing window")
        if self.conditional and not self.condition:
            raise ValueError("a conditional event must state its condition")

    def as_dict(self, scale: int) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source_id": self.source_id,
            "column": self.column,
            "direction": self.direction,
            "on": self.on,
            "amount": format_decimal(self.amount_minor, scale),
            "label": self.label,
            "timing": {
                "basis": self.timing_basis,
                "earliest": self.window_earliest or self.on,
                "latest": self.window_latest or self.on,
                "precision": (
                    PRECISION_DAY if self.timing_basis == TIMING_KNOWN
                    else PRECISION_UNRESOLVED if self.timing_basis == TIMING_UNRESOLVED
                    else PRECISION_WINDOW
                ),
            },
            "conditional": self.conditional,
            "condition": self.condition,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class ForecastEvent:
    """A sourced external movement before scenario timing is resolved.

    Amounts remain source-text decimals until a currency-wide exact scale is
    chosen.  ``source_id`` is the canonical source/event identifier; when it is
    absent the engine preserves the item as a limitation instead of attempting
    fuzzy de-duplication.
    """

    event_id: str
    source_id: str
    currency: str
    amount: str
    column: str
    direction: str
    label: str
    on: str | None = None
    timing_basis: str = TIMING_KNOWN
    window_earliest: str | None = None
    window_latest: str | None = None
    confidence: str = CONFIDENCE_CONFIRMED
    fixed: bool = True
    recurring_salary: bool = False
    conditional: bool = False
    condition: str | None = None
    residual_category: str | None = None
    residual_on: str | None = None
    residual_amount: str | None = None
    removes_from_residual: bool = False
    provenance: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.currency:
            raise ValueError("a sourced forecast event needs a currency")
        money = ExactMoney.parse(self.amount)
        if money.coefficient < 0:
            raise ValueError("a forecast event carries a magnitude, never a sign")
        if self.column not in COLUMNS:
            raise ValueError(f"unknown timeline column: {self.column!r}")
        if self.direction not in (DIRECTION_IN, DIRECTION_OUT):
            raise ValueError(f"unknown direction: {self.direction!r}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"unknown confidence: {self.confidence!r}")
        if self.timing_basis not in (TIMING_KNOWN, TIMING_INFERRED, TIMING_UNRESOLVED):
            raise ValueError(f"unknown timing basis: {self.timing_basis!r}")
        if self.on is not None:
            date.fromisoformat(self.on)
        if self.window_earliest is not None:
            date.fromisoformat(self.window_earliest)
        if self.window_latest is not None:
            date.fromisoformat(self.window_latest)
        if self.residual_on is not None:
            date.fromisoformat(self.residual_on)
        if self.residual_amount is not None and ExactMoney.parse(
            self.residual_amount
        ).coefficient < 0:
            raise ValueError("a residual replacement amount must not be negative")
        if self.conditional and not self.condition:
            raise ValueError("a conditional event must state its condition")
        if self.removes_from_residual and not self.residual_category:
            raise ValueError("a residual replacement must identify its matching category")


@dataclass(frozen=True, slots=True)
class ResidualCategory:
    """One unscheduled category retained independently in the daily model."""

    category_id: str
    currency: str
    mean_monthly: str
    highest_observed_monthly: str
    label: str
    column: str = COLUMN_LIFESTYLE
    # The lowest comparable month actually observed for this category. It drives
    # the *downward* half of the reported dispersion band and nothing else: no
    # scenario spends at this level, because a forecast built on the cheapest
    # month anyone ever had would be a wish. ``None`` means the category has no
    # observed low (a single-figure allowance), and its band is one-sided.
    lowest_observed_monthly: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.category_id:
            raise ValueError("a residual category needs a stable category_id")
        mean = ExactMoney.parse(self.mean_monthly)
        high = ExactMoney.parse(self.highest_observed_monthly)
        if mean.coefficient < 0 or high.coefficient < 0:
            raise ValueError("residual spending assumptions must not be negative")
        if self.lowest_observed_monthly is not None:
            low = ExactMoney.parse(self.lowest_observed_monthly)
            if low.coefficient < 0:
                raise ValueError("residual spending assumptions must not be negative")
        if self.column not in EXPENSE_COLUMNS:
            raise ValueError("a residual category must use an expense column")


# --- calendar helpers -------------------------------------------------------


def add_months(anchor: date, months: int, day: int | None = None) -> date:
    """``anchor`` rolled forward, clamped to the target month's length."""
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(day or anchor.day, calendar.monthrange(year, month)[1]))


def day_range(start: str, end: str) -> list[str]:
    """Every ISO day in the inclusive ``[start, end]`` span."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        return []
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def month_of(day: str) -> str:
    return day[:7]


def days_in_month(month: str) -> int:
    year, index = (int(part) for part in month.split("-"))
    return calendar.monthrange(year, index)[1]


def allocate_daily_cents(total_minor: int, days: int) -> list[int]:
    """Split ``total_minor`` across ``days`` exactly, per :data:`DAILY_ALLOCATION_RULE`.

    Negative totals are not accepted: a negative unscheduled cost would be an
    inflow wearing an expense's name, and the caller has to say which it means.
    """
    if days <= 0:
        return []
    if total_minor < 0:
        raise ValueError("an unscheduled cost allocation must not be negative")
    base, remainder = divmod(total_minor, days)
    return [base + (1 if index < remainder else 0) for index in range(days)]


def remaining_day_amount(monthly_minor: int, covered_days: int, month: str) -> int:
    """A partial month's share of a monthly rate, per :data:`PARTIAL_MONTH_RULE`."""
    if monthly_minor < 0:
        raise ValueError("a monthly rate must not be negative")
    length = days_in_month(month)
    if covered_days >= length:
        return monthly_minor
    return monthly_minor * covered_days // length


# --- the walk ---------------------------------------------------------------


def _day_timing(
    events: Sequence[TimelineEvent], day: str, prior: dict[str, Any] | None = None
) -> dict[str, Any]:
    inferred = [
        event
        for event in events
        if event.timing_basis in (TIMING_INFERRED, TIMING_UNRESOLVED)
    ]
    prior_ids = list((prior or {}).get("inferred_event_ids", []))
    if not inferred and not prior_ids:
        return {
            "precision": PRECISION_DAY,
            "earliest": day,
            "latest": day,
            "inferred_event_ids": [],
        }
    unresolved = (
        (prior or {}).get("precision") == PRECISION_UNRESOLVED
        or any(event.timing_basis == TIMING_UNRESOLVED for event in inferred)
    )
    earliest = min(
        [day]
        + ([str(prior["earliest"])] if prior_ids and prior is not None else [])
        + [event.window_earliest or event.on for event in inferred]
    )
    latest = max(
        [day]
        + ([str(prior["latest"])] if prior_ids and prior is not None else [])
        + [event.window_latest or event.on for event in inferred]
    )
    return {
        "precision": PRECISION_UNRESOLVED if unresolved else PRECISION_WINDOW,
        "earliest": earliest,
        "latest": latest,
        "inferred_event_ids": list(
            dict.fromkeys(prior_ids + [event.event_id for event in inferred])
        ),
    }


def _milestone(
    *,
    occurs: bool,
    day: str | None,
    amount_minor: int | None,
    scale: int,
    timing: dict[str, Any] | None,
    preceded_by_inferred: bool,
    note: str,
) -> dict[str, Any]:
    if not occurs:
        return {"occurs": False, "on": None, "amount": None, "note": note}
    assert day is not None and timing is not None
    return {
        "occurs": True,
        "on": day if timing["precision"] == PRECISION_DAY else None,
        "modelled_on": day,
        "amount": None if amount_minor is None else format_decimal(amount_minor, scale),
        "precision": timing["precision"],
        "window": {"earliest": timing["earliest"], "latest": timing["latest"]},
        "preceded_by_inferred_timing": preceded_by_inferred,
        "note": note,
    }


def run_timeline(
    *,
    currency: str,
    scale: int,
    opening_minor: int,
    start: str,
    end: str,
    events: Iterable[TimelineEvent],
    reserve_minor: int | None = None,
    dispersion: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Walk the days from ``start`` (exclusive) to ``end`` (inclusive).

    ``opening_minor`` is the balance standing at the close of ``start`` — the
    supported cash cutoff — so the first modelled day is the day after it.

    An event dated on or before the cutoff is *repositioned* onto the first
    modelled day rather than dropped: an overdue obligation with no payment
    evidence is still money that has to leave.  It is reported separately, and
    its timing precision is unresolved because the ledger does not say when.
    An event dated after ``end`` leaves no cash inside the horizon and is
    reported as settling beyond it.
    """
    if scale < 2:
        raise ValueError("the timeline works at a minor-unit scale of at least two")
    days = day_range(
        (date.fromisoformat(start) + timedelta(days=1)).isoformat(), end
    )
    if not days:
        raise ValueError("the timeline horizon contains no modelled day")
    first_day, last_day = days[0], days[-1]

    ordered = sorted(events, key=lambda item: (item.on, item.source_id, item.event_id))
    applied: list[TimelineEvent] = []
    repositioned: list[dict[str, Any]] = []
    beyond: list[dict[str, Any]] = []
    for event in ordered:
        if event.on > last_day:
            beyond.append(event.as_dict(scale))
            continue
        if event.on < first_day:
            moved = TimelineEvent(
                event_id=event.event_id,
                source_id=event.source_id,
                column=event.column,
                direction=event.direction,
                on=first_day,
                amount_minor=event.amount_minor,
                label=event.label,
                timing_basis=TIMING_UNRESOLVED,
                window_earliest=first_day,
                window_latest=last_day,
                conditional=event.conditional,
                condition=event.condition,
                provenance=dict(event.provenance),
            )
            repositioned.append(
                {
                    **moved.as_dict(scale),
                    "original_date": event.on,
                    "reason": (
                        "the stated date is on or before the supported cash cutoff "
                        "and no payment is recorded; the money still has to move, so "
                        "it is modelled on the first day of the horizon and its "
                        "timing is reported unresolved rather than exact"
                    ),
                }
            )
            applied.append(moved)
            continue
        applied.append(event)

    by_day: dict[str, list[TimelineEvent]] = {}
    for event in applied:
        by_day.setdefault(event.on, []).append(event)

    balance = opening_minor
    day_rows: list[dict[str, Any]] = []
    lowest_minor = opening_minor
    lowest_day: str | None = None
    lowest_timing: dict[str, Any] | None = None
    lowest_preceded = False
    highest_minor = opening_minor
    highest_day: str | None = None
    highest_timing: dict[str, Any] | None = None
    highest_preceded = False
    cutoff_timing = {
        "precision": PRECISION_DAY,
        "earliest": start,
        "latest": start,
        "inferred_event_ids": [],
    }
    zero_day: str | None = start if opening_minor <= 0 else None
    zero_timing: dict[str, Any] | None = cutoff_timing if zero_day else None
    zero_preceded = False
    breach_day: str | None = (
        start
        if reserve_minor is not None and opening_minor < reserve_minor
        else None
    )
    breach_timing: dict[str, Any] | None = cutoff_timing if breach_day else None
    breach_preceded = False
    seen_inferred = False
    cumulative_timing: dict[str, Any] | None = None
    column_totals = dict.fromkeys(COLUMNS, 0)

    for day in days:
        day_events = by_day.get(day, [])
        outflow = sum(
            event.amount_minor for event in day_events if event.direction == DIRECTION_OUT
        )
        inflow = sum(
            event.amount_minor for event in day_events if event.direction == DIRECTION_IN
        )
        for event in day_events:
            column_totals[event.column] += event.amount_minor
        opening_of_day = balance
        # The declared same-day convention: outflows first, so `low` is the
        # worst point the day can reach under any ordering the evidence allows.
        low = opening_of_day - outflow
        close = low + inflow
        timing = _day_timing(day_events, day, cumulative_timing)
        cumulative_timing = timing
        day_rows.append(
            {
                "date": day,
                "opening": format_decimal(opening_of_day, scale),
                "inflow": format_decimal(inflow, scale),
                "outflow": format_decimal(outflow, scale),
                "low_point": format_decimal(low, scale),
                "closing": format_decimal(close, scale),
                "event_ids": [event.event_id for event in day_events],
                "timing": timing,
            }
        )
        if low < lowest_minor:
            lowest_minor, lowest_day, lowest_timing = low, day, timing
            lowest_preceded = seen_inferred
        if close > highest_minor:
            highest_minor, highest_day, highest_timing = close, day, timing
            highest_preceded = seen_inferred
        if zero_day is None and low <= 0:
            zero_day, zero_timing, zero_preceded = day, timing, seen_inferred
        if (
            reserve_minor is not None
            and breach_day is None
            and low < reserve_minor
        ):
            breach_day, breach_timing, breach_preceded = day, timing, seen_inferred
        if timing["precision"] != PRECISION_DAY:
            seen_inferred = True
        balance = close

    months = _month_rows(
        day_rows, days, opening_minor, scale, reserve_minor, by_day, dispersion or {},
    )

    if lowest_day is None:
        lowest = {
            "occurs": True,
            "on": start,
            "amount": format_decimal(opening_minor, scale),
            "precision": PRECISION_DAY,
            "window": {"earliest": start, "latest": start},
            "preceded_by_inferred_timing": False,
            "note": "no modelled day falls below the starting balance",
        }
    else:
        lowest = _milestone(
            occurs=True, day=lowest_day, amount_minor=lowest_minor, scale=scale,
            timing=lowest_timing, preceded_by_inferred=lowest_preceded,
            note=(
                "the lowest point any modelled day reaches under the same-day "
                "stress convention (outflows before inflows)"
            ),
        )

    maximum = (
        _milestone(
            occurs=True, day=highest_day, amount_minor=highest_minor, scale=scale,
            timing=highest_timing, preceded_by_inferred=highest_preceded,
            note="the highest closing balance any modelled day reaches",
        )
        if highest_day is not None
        else {
            "occurs": True, "on": start,
            "amount": format_decimal(opening_minor, scale),
            "precision": PRECISION_DAY,
            "window": {"earliest": start, "latest": start},
            "preceded_by_inferred_timing": False,
            "note": "no modelled day closes above the starting balance",
        }
    )

    first_zero = _milestone(
        occurs=zero_day is not None, day=zero_day,
        amount_minor=None, scale=scale, timing=zero_timing,
        preceded_by_inferred=zero_preceded,
        note=(
            "the first day whose low point reaches zero or below; a month may "
            "still close positive around it"
        ),
    )

    if reserve_minor is None:
        reserve_breach: dict[str, Any] = {
            "available": False,
            "reason": (
                "no reserve target is available, so no reserve breach is stated; "
                "the cash balances above are unaffected"
            ),
        }
        reserve_gaps: dict[str, Any] = {
            "available": False,
            "reason": (
                "no reserve target is available, so no funding gap is stated; "
                "the cash balances above are unaffected"
            ),
        }
    else:
        reserve_breach = {
            "available": True,
            **_milestone(
                occurs=breach_day is not None, day=breach_day,
                amount_minor=reserve_minor, scale=scale, timing=breach_timing,
                preceded_by_inferred=breach_preceded,
                note=(
                    "the first day whose low point falls below the reserve target"
                ),
            ),
            "reserve_target": format_decimal(reserve_minor, scale),
        }
        gap_months = [
            {
                "month": row["month"],
                "closing": row["closing"],
                "lowest_balance": row["lowest_balance"],
                "gap": row["reserve_gap"],
            }
            for row in months
            if row["reserve_gap"] is not None and row["reserve_gap"] != format_decimal(0, scale)
        ]
        largest = max(
            (int(_unformat(row["gap"], scale)) for row in gap_months), default=0
        )
        reserve_gaps = {
            "available": True,
            "reserve_target": format_decimal(reserve_minor, scale),
            "months_below_target": gap_months,
            "largest_gap": format_decimal(largest, scale),
            "ever_fully_funded": len(gap_months) < len(months),
            "basis": (
                "per month row, the reserve target less that month's lowest "
                "intra-month balance under the same-day stress convention"
            ),
        }

    continuity_holds = (
        bool(months)
        and months[0]["opening"] == format_decimal(opening_minor, scale)
        and months[-1]["closing"] == format_decimal(balance, scale)
        and all(
            months[index]["closing"] == months[index + 1]["opening"]
            for index in range(len(months) - 1)
        )
    )

    return {
        "currency": currency,
        "scale": scale,
        "starting_balance": format_decimal(opening_minor, scale),
        "starting_balance_as_of": start,
        "horizon": {
            "start": start,
            "first_modelled_day": first_day,
            "end": last_day,
            "modelled_days": len(days),
        },
        "days": day_rows,
        "months": months,
        "closing_balance": format_decimal(balance, scale),
        "totals": {
            "income": format_decimal(column_totals[COLUMN_INCOME], scale),
            "expense": format_decimal(
                sum(column_totals[column] for column in EXPENSE_COLUMNS), scale
            ),
            "net": format_decimal(
                column_totals[COLUMN_INCOME]
                - sum(column_totals[column] for column in EXPENSE_COLUMNS),
                scale,
            ),
            "by_column": {
                column: format_decimal(column_totals[column], scale)
                for column in COLUMNS
            },
        },
        "lowest_balance": lowest,
        "maximum_cash": maximum,
        "first_zero": first_zero,
        "first_reserve_breach": reserve_breach,
        "reserve_funding_gaps": reserve_gaps,
        "continuity": {
            "holds": continuity_holds,
            "basis": (
                "every month row opens on the previous row's closing balance, the "
                "first opens on the starting balance and the last closes on the "
                "horizon's closing balance"
            ),
        },
        "events": {
            "applied": len(applied),
            "items": [event.as_dict(scale) for event in applied],
            "repositioned": repositioned,
            "settling_beyond_horizon": beyond,
            "same_day_convention": SAME_DAY_CONVENTION,
        },
        "settlement": settlement_reconciliation(applied, beyond),
    }


def _unformat(value: str, scale: int) -> int:
    sign = -1 if value.startswith("-") else 1
    digits = value.lstrip("+-").replace(".", "")
    return sign * int(digits or "0")


def _month_rows(
    day_rows: list[dict[str, Any]],
    days: list[str],
    opening_minor: int,
    scale: int,
    reserve_minor: int | None,
    by_day: dict[str, list[TimelineEvent]],
    dispersion: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """Group the daily walk into month rows with opening/closing continuity.

    Each row also carries the observed-dispersion band around its expense and
    closing figures. The closing band accumulates: a month that could run light
    leaves that difference in the balance for every later month, so the band
    widens across the horizon exactly as the uncertainty does. Spending low
    raises the closing balance and spending high lowers it, which is why the
    swings cross over between the two bands.
    """
    rows: list[dict[str, Any]] = []
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in day_rows:
        month = month_of(row["date"])
        if month not in grouped:
            grouped[month] = []
            order.append(month)
        grouped[month].append(row)

    running = opening_minor
    swing_down_to_date = 0
    swing_up_to_date = 0
    for month in order:
        member_rows = grouped[month]
        covered = len(member_rows)
        column_totals = dict.fromkeys(COLUMNS, 0)
        for row in member_rows:
            for event in by_day.get(row["date"], []):
                column_totals[event.column] += event.amount_minor
        opening = running
        closing = _unformat(member_rows[-1]["closing"], scale)
        low_row = min(member_rows, key=lambda item: _unformat(item["low_point"], scale))
        low_minor = _unformat(low_row["low_point"], scale)
        gap = (
            None
            if reserve_minor is None
            else format_decimal(max(reserve_minor - low_minor, 0), scale)
        )
        swing = dispersion.get(month, {})
        swing_down_to_date += swing.get("down", 0)
        swing_up_to_date += swing.get("up", 0)
        expense_minor = sum(column_totals[column] for column in EXPENSE_COLUMNS)
        band = (
            None
            if not swing.get("down") and not swing.get("up")
            else {
                "expense": {
                    "low": format_decimal(max(expense_minor - swing.get("down", 0), 0), scale),
                    "high": format_decimal(expense_minor + swing.get("up", 0), scale),
                },
                "closing": {
                    "low": format_decimal(closing - swing_up_to_date, scale),
                    "high": format_decimal(closing + swing_down_to_date, scale),
                },
                "basis": (
                    "every unscheduled category repeating its own cheapest or "
                    "dearest observed month across the baseline window"
                ),
                "cumulative": True,
                "not_a_confidence_interval": True,
                "note": (
                    "observed dispersion, not a calibrated interval: a month "
                    "outside this band is possible, it simply has no precedent "
                    "in the observed window"
                ),
            }
        )
        rows.append(
            {
                "month": month,
                "partial": covered < days_in_month(month),
                "days_covered": covered,
                "days_in_month": days_in_month(month),
                "first_day": member_rows[0]["date"],
                "last_day": member_rows[-1]["date"],
                "opening": format_decimal(opening, scale),
                "closing": format_decimal(closing, scale),
                "income": format_decimal(column_totals[COLUMN_INCOME], scale),
                "expense": format_decimal(
                    sum(column_totals[column] for column in EXPENSE_COLUMNS), scale
                ),
                "by_column": {
                    column: format_decimal(column_totals[column], scale)
                    for column in COLUMNS
                },
                "lowest_balance": {
                    "amount": format_decimal(low_minor, scale),
                    "on": (
                        low_row["date"]
                        if low_row["timing"]["precision"] == PRECISION_DAY
                        else None
                    ),
                    "modelled_on": low_row["date"],
                    "precision": low_row["timing"]["precision"],
                    "window": {
                        "earliest": low_row["timing"]["earliest"],
                        "latest": low_row["timing"]["latest"],
                    },
                    "note": (
                        "the lowest point inside the month; a month can close "
                        "positive and still reach this"
                    ),
                },
                "closes_positive_over_an_intra_month_low": (
                    closing > 0 and low_minor <= 0
                ),
                "reserve_gap": gap,
                "observed_range": band,
            }
        )
        running = closing
    return rows


def settlement_reconciliation(
    applied: Sequence[TimelineEvent], beyond: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Every sourced external event settles exactly once, checked by identity.

    Slices of one deterministic allocation (a monthly cost spread over its
    days) share a source but are not one settlement each; they carry distinct
    source ids per slice for exactly that reason, so a duplicate here is always
    a real double-settlement rather than an artefact of the spreading rule.
    """
    counts: dict[str, int] = {}
    for event in applied:
        counts[event.source_id] = counts.get(event.source_id, 0) + 1
    for row in beyond:
        counts[str(row["source_id"])] = counts.get(str(row["source_id"]), 0) + 1
    duplicated = sorted(key for key, count in counts.items() if count > 1)
    return {
        "settled_exactly_once": not duplicated,
        "distinct_source_ids": len(counts),
        "duplicated_source_ids": duplicated,
        "contract": (
            "each sourced external event converts to cash exactly once: its "
            "canonical source identifier appears on exactly one settlement, "
            "inside the horizon or beyond it"
        ),
    }


# --- scenario construction -------------------------------------------------


def _minor(value: str, scale: int) -> int:
    return ExactMoney.parse(value).rescaled_coefficient(scale)


def _middle_day(earliest: str, latest: str) -> str:
    first = date.fromisoformat(earliest)
    last = date.fromisoformat(latest)
    return (first + timedelta(days=(last - first).days // 2)).isoformat()


def _resolve_event(
    event: ForecastEvent, *, scenario: str, scale: int
) -> TimelineEvent | None:
    """Place an event without erasing the date evidence behind that placement."""
    if event.timing_basis == TIMING_UNRESOLVED and not (
        event.on or event.window_earliest or event.window_latest
    ):
        return None
    earliest = event.window_earliest or event.on
    latest = event.window_latest or event.on
    if earliest is None or latest is None:
        return None
    if earliest > latest:
        raise ValueError(f"event {event.event_id!r} has an inverted timing window")
    if event.timing_basis == TIMING_KNOWN:
        if event.on is None:
            return None
        resolved = event.on
    elif scenario == SCENARIO_CONSERVATIVE:
        resolved = latest if event.direction == DIRECTION_IN else earliest
    else:
        resolved = event.on or _middle_day(earliest, latest)
    if not earliest <= resolved <= latest:
        raise ValueError(f"event {event.event_id!r} falls outside its timing window")
    return TimelineEvent(
        event_id=event.event_id,
        source_id=event.source_id,
        column=event.column,
        direction=event.direction,
        on=resolved,
        amount_minor=_minor(event.amount, scale),
        label=event.label,
        timing_basis=event.timing_basis,
        window_earliest=earliest,
        window_latest=latest,
        conditional=event.conditional,
        condition=event.condition,
        provenance={
            **event.provenance,
            "confidence": event.confidence,
            "fixed": event.fixed,
            "recurring_salary": event.recurring_salary,
            "scenario_date_rule": (
                "known date governs"
                if event.timing_basis == TIMING_KNOWN
                else "later receipt / earlier payment"
                if scenario == SCENARIO_CONSERVATIVE
                else "source date or midpoint of the observed window"
            ),
        },
    )


def _included(
    event: ForecastEvent,
    *,
    scenario: str,
    likely_income_policy: str,
    strong_income_interruption: bool,
) -> bool:
    if event.direction == DIRECTION_OUT:
        # A possible cost is a risk, so only the cautious case pays it.
        return event.confidence != CONFIDENCE_POSSIBLE or scenario == SCENARIO_CONSERVATIVE
    if event.confidence == CONFIDENCE_POSSIBLE:
        return False
    if scenario == SCENARIO_BASE:
        return event.confidence in (CONFIDENCE_CONFIRMED, CONFIDENCE_LIKELY)
    if strong_income_interruption and event.recurring_salary:
        return False
    return event.confidence == CONFIDENCE_CONFIRMED or (
        event.confidence == CONFIDENCE_LIKELY
        and likely_income_policy == LIKELY_INCLUDE
    )


def _calendar_months(start: str, end: str) -> list[tuple[str, list[str]]]:
    modelled = day_range(
        (date.fromisoformat(start) + timedelta(days=1)).isoformat(), end
    )
    grouped: dict[str, list[str]] = {}
    for day in modelled:
        grouped.setdefault(month_of(day), []).append(day)
    return list(grouped.items())


def _residual_events(
    *,
    categories: Sequence[ResidualCategory],
    sourced: Sequence[ForecastEvent],
    resolved: Sequence[TimelineEvent],
    scenario: str,
    currency: str,
    scale: int,
    start: str,
    end: str,
) -> tuple[list[TimelineEvent], list[dict[str, Any]], list[str]]:
    """Allocate every category independently and remove only matching costs.

    The third value is notes, not limitations: a dated cost larger than its
    matching residual empties that residual for the month and says so, but the
    dated cost itself is fully counted, so the case stays available.
    """
    result: list[TimelineEvent] = []
    allocations: list[dict[str, Any]] = []
    notes: list[str] = []
    resolved_by_event = {item.event_id: item for item in resolved}
    for category in categories:
        if category.currency != currency:
            continue
        mean = _minor(category.mean_monthly, scale)
        highest = _minor(category.highest_observed_monthly, scale)
        monthly = mean if scenario == SCENARIO_BASE else max(mean, highest)
        for month, covered_days in _calendar_months(start, end):
            prorated = remaining_day_amount(monthly, len(covered_days), month)
            removals: list[dict[str, Any]] = []
            removed = 0
            for source in sourced:
                if (
                    source.currency != currency
                    or not source.removes_from_residual
                    or source.residual_category != category.category_id
                    or source.event_id not in resolved_by_event
                ):
                    continue
                placed = resolved_by_event[source.event_id]
                residual_day = source.residual_on or placed.on
                if month_of(residual_day) != month or not (start < residual_day <= end):
                    continue
                amount = _minor(source.residual_amount or source.amount, scale)
                removed += amount
                removals.append(
                    {
                        "event_id": source.event_id,
                        "source_id": source.source_id,
                        "residual_on": residual_day,
                        "amount": format_decimal(amount, scale),
                    }
                )
            if removed > prorated:
                notes.append(
                    f"{format_decimal(removed - prorated, scale)} {currency} of dated "
                    f"cost in {category.category_id} during {month} exceeds that "
                    "category's matching residual; the matching residual is reduced "
                    "to zero and no other category is touched"
                )
            remaining = max(prorated - removed, 0)
            slices = allocate_daily_cents(remaining, len(covered_days))
            for day, amount in zip(covered_days, slices, strict=True):
                if amount == 0:
                    continue
                source_id = (
                    f"residual:{scenario}:{currency}:{category.category_id}:{month}:{day}"
                )
                result.append(
                    TimelineEvent(
                        event_id=source_id,
                        source_id=source_id,
                        column=category.column,
                        direction=DIRECTION_OUT,
                        on=day,
                        amount_minor=amount,
                        label=category.label,
                        timing_basis=TIMING_INFERRED,
                        window_earliest=covered_days[0],
                        window_latest=covered_days[-1],
                        provenance={
                            **category.provenance,
                            "kind": "unscheduled_residual_allocation",
                            "category_id": category.category_id,
                            "scenario": scenario,
                            "allocation_rule": DAILY_ALLOCATION_RULE,
                        },
                    )
                )
            allocations.append(
                {
                    "category_id": category.category_id,
                    "label": category.label,
                    "currency": currency,
                    "column": category.column,
                    "month": month,
                    "case": scenario,
                    "monthly_assumption": format_decimal(monthly, scale),
                    "covered_days": len(covered_days),
                    "prorated_before_dated_costs": format_decimal(prorated, scale),
                    "dated_costs_removed": removals,
                    "allocated_residual": format_decimal(remaining, scale),
                    "assumption": DAILY_ALLOCATION_RULE,
                }
            )
    return result, allocations, notes


def _residual_dispersion(
    *,
    categories: Sequence[ResidualCategory],
    allocations: Sequence[dict[str, Any]],
    currency: str,
    scale: int,
) -> dict[str, dict[str, int]]:
    """Per month, how far residual spending could swing on observed evidence.

    The forecast's month figure uses each category's *mean* observed month. This
    says how much lighter or heavier that month would be if every category had
    instead repeated its own cheapest or dearest observed month — which is the
    honest width of the estimate, and much more useful than a single number that
    hides it.

    It is observed dispersion, not a confidence interval. Nothing here models a
    distribution or attaches a probability; a month outside the band is entirely
    possible, it just has no precedent in the baseline window. Dated obligations
    are deliberately excluded: a rent payment already sourced from evidence does
    not become uncertain because routine spending is, so the swing is computed on
    the same prorated residual the allocation used, after the same removals.
    """
    by_id = {category.category_id: category for category in categories}
    swings: dict[str, dict[str, int]] = {}
    for allocation in allocations:
        if allocation["currency"] != currency:
            continue
        category = by_id.get(str(allocation["category_id"]))
        if category is None:
            continue
        month = str(allocation["month"])
        covered = int(allocation["covered_days"])
        base = _minor(str(allocation["monthly_assumption"]), scale)
        removed = sum(
            _minor(str(item["amount"]), scale)
            for item in allocation["dated_costs_removed"]
        )
        allocated = _minor(str(allocation["allocated_residual"]), scale)

        def leg(
            monthly: str | None,
            *,
            month: str = month,
            covered: int = covered,
            removed: int = removed,
            allocated: int = allocated,
        ) -> int:
            if monthly is None:
                return allocated
            prorated = remaining_day_amount(_minor(monthly, scale), covered, month)
            return max(prorated - removed, 0)

        low = leg(category.lowest_observed_monthly)
        high = leg(category.highest_observed_monthly)
        # A category whose assumption already *is* the high (the conservative
        # case) has no upward room left; clamping at zero keeps the band from
        # inverting rather than reporting a negative width.
        cell = swings.setdefault(month, {"down": 0, "up": 0, "base": 0})
        cell["down"] += max(allocated - low, 0)
        cell["up"] += max(high - allocated, 0)
        cell["base"] += base
    return swings


def _scenario(
    *,
    name: str,
    currency: str,
    scale: int,
    opening_minor: int,
    reserve_minor: int | None,
    start: str,
    end: str,
    sourced_events: Sequence[ForecastEvent],
    residual_categories: Sequence[ResidualCategory],
    likely_income_policy: str,
    strong_income_interruption: bool,
) -> dict[str, Any]:
    selected_sources = [
        event
        for event in sourced_events
        if event.currency == currency
        and _included(
            event,
            scenario=name,
            likely_income_policy=likely_income_policy,
            strong_income_interruption=strong_income_interruption,
        )
    ]
    limitations: list[str] = []
    placed: list[TimelineEvent] = []
    identity_counts: dict[str, int] = {}
    for event in selected_sources:
        if event.source_id:
            identity_counts[event.source_id] = identity_counts.get(event.source_id, 0) + 1
    duplicated = {source_id for source_id, count in identity_counts.items() if count > 1}
    if duplicated:
        limitations.append(
            "canonical source identifiers occur more than once and are withheld "
            "rather than double-settled: " + ", ".join(sorted(duplicated))
        )
    for event in selected_sources:
        if not event.source_id:
            limitations.append(
                f"{event.event_id or '<unidentified event>'} lacks sufficient "
                "canonical identity for safe deduplication; it remains an explicit "
                "limitation and is not settled under a guessed identifier"
            )
            continue
        if event.source_id in duplicated:
            continue
        resolved = _resolve_event(event, scenario=name, scale=scale)
        if resolved is None:
            limitations.append(
                f"{event.source_id} has no safe date or timing window; it remains "
                "an explicit unsettled limitation and is not assigned a guessed date"
            )
        else:
            placed.append(resolved)
    residual, allocations, allocation_notes = _residual_events(
        categories=residual_categories,
        sourced=selected_sources,
        resolved=placed,
        scenario=name,
        currency=currency,
        scale=scale,
        start=start,
        end=end,
    )
    result = run_timeline(
        currency=currency,
        scale=scale,
        opening_minor=opening_minor,
        start=start,
        end=end,
        events=[*placed, *residual],
        reserve_minor=reserve_minor,
        dispersion=_residual_dispersion(
            categories=residual_categories, allocations=allocations,
            currency=currency, scale=scale,
        ),
    )
    result["case"] = name
    result["available"] = not limitations and result["settlement"]["settled_exactly_once"]
    result["limitations"] = limitations
    result["notes"] = allocation_notes
    result["allocation_metadata"] = allocations
    result["policy"] = {
        "income": (
            "Confirmed and evidence-derived Likely income"
            if name == SCENARIO_BASE
            else (
                "Confirmed income; all recurring salary is excluded as an explicit "
                "strong income-interruption scenario, not presented as the likely outcome"
                if strong_income_interruption
                else f"Confirmed income; Likely income policy: {likely_income_policy}"
            )
        ),
        "spending": (
            "observed routine baseline plus known obligations and unresolved irregular allowance"
            if name == SCENARIO_BASE
            else (
                "no automatic cuts; fixed obligations stay fixed and variable residual "
                "uses the greater of its mean and highest observed comparable month"
            )
        ),
        "upper_spend_label": (
            None
            if name == SCENARIO_BASE
            else "observed stress case"
        ),
    }
    result["actual_and_projected"] = {
        "actual": {
            "through": start,
            "starting_balance": format_decimal(opening_minor, scale),
            "forecast_values_in_actual_totals": False,
        },
        "projected": {
            "start": result["horizon"]["first_modelled_day"],
            "end": end,
            "totals": result["totals"],
        },
        "rule": (
            "recent actual evidence ends at the supported cash cutoff; every later "
            "number is projected and no forecast number enters an actual report or total"
        ),
    }
    return result


def _manifest_categories(
    manifest: dict[str, Any], estimate: dict[str, Any] | None
) -> list[ResidualCategory]:
    """Make the aggregate fallback the manifest can support without inventing detail.

    Spending evidence is enough to project spending.  An incomplete essential
    role scope only withholds the *protective* reserve and allowance claims; it
    must not make recorded residual spending disappear from cash arithmetic.
    """
    categories: list[ResidualCategory] = []
    if not estimate or not estimate.get("available"):
        return categories
    for currency, cell in estimate.get("by_currency", {}).items():
        categories.extend(
            [
                ResidualCategory(
                    category_id=f"manifest:routine:{currency}",
                    currency=currency,
                    mean_monthly=str(cell["variable_residual_monthly"]),
                    highest_observed_monthly=str(cell["variable_residual_max"]),
                    # Absent from an estimate produced before the observed low
                    # was published: the band is then one-sided rather than
                    # fabricated from the mean.
                    lowest_observed_monthly=(
                        str(cell["variable_residual_min"])
                        if "variable_residual_min" in cell else None
                    ),
                    label="Observed routine residual spending",
                    provenance={
                        "source": "fincl.baseline.estimators.baseline_estimate",
                        "method": manifest.get("methods", {}).get("residual_method"),
                        "sparse_categories_retained": True,
                    },
                ),
                ResidualCategory(
                    category_id=f"manifest:irregular:{currency}",
                    currency=currency,
                    mean_monthly=str(cell["irregular_allowance_monthly"]),
                    highest_observed_monthly=str(cell["irregular_allowance_monthly"]),
                    label="Unresolved irregular allowance",
                    column=COLUMN_IRREGULAR,
                    provenance={
                        "source": "Stage 3A manifest reserve_and_allowance",
                        "treatment": "retained; never treated as zero",
                    },
                ),
            ]
        )
    return categories


def _action_items(manifest: dict[str, Any], card_limits: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Turn unresolved forecast inputs into output-owned, actionable gaps.

    These are deliberately not a generic health score: one missing card
    settlement must not masquerade as an income or cash-boundary problem.
    The rank is a stable, coarse ordering for the UI; it is not a probability
    or a claim about the user's financial priority.
    """
    items: list[dict[str, Any]] = []

    for currency, cash in manifest.get("cash", {}).get("by_currency", {}).items():
        for interval in cash.get("missing_intervals", []):
            start, end = interval.get("start"), interval.get("end")
            if not start or not end:
                continue
            items.append({
                "kind": "missing_evidence", "output": "cash", "currency": currency,
                "rank": 100, "action": "Import the missing cash-movement interval",
                "detail": f"{start} to {end}: {interval.get('reason', 'movement coverage is missing')}",
                "account_id": interval.get("account_id"), "start": start, "end": end,
            })

    income_gap = manifest.get("income", {}).get("gap")
    if income_gap:
        items.append({
            "kind": "needs_review", "output": "income", "currency": None, "rank": 80,
            "action": "Classify the pending inbound groups",
            "detail": str(income_gap.get("reason", "Income evidence needs review")),
        })

    for obligation in manifest.get("unresolved_items", {}).get("ambiguous_obligations", []):
        items.append({
            "kind": "needs_review", "output": "obligation_timing",
            "currency": obligation.get("currency"), "rank": 70,
            "action": "Resolve the obligation amount or settlement date",
            "detail": f"{obligation.get('label', obligation.get('obligation_id'))}: "
                      + "; ".join(obligation.get("reasons", [])),
            "obligation_id": obligation.get("obligation_id"), "origin": obligation.get("origin"),
            "label": obligation.get("label"),
        })

    for task in manifest.get("unresolved_items", {}).get("evidence_gaps", []):
        # Readiness already scopes these to the affected account/month. Keep
        # that exact scope instead of turning a whole account into a gap.
        items.append({
            "kind": "missing_evidence", "output": "baseline",
            "currency": task.get("currency_pocket"), "rank": 60,
            "action": "Import or confirm the named evidence interval",
            "detail": str(task.get("note") or task.get("detail") or task),
            "task": task.get("kind"), "account_id": task.get("account_id"),
            "account_name": task.get("display_name"),
            "start": (task.get("interval") or {}).get("start"),
            "end": (task.get("interval") or {}).get("end"),
        })

    unlinked = manifest.get("unresolved_items", {}).get("unlinked_recurring", [])
    for commitment in unlinked:
        count = len(commitment["scheduled_on"])
        items.append({
            "kind": "needs_review", "output": "recurring_history",
            "currency": commitment["currency"], "rank": 65,
            "action": "Link or skip the past payments of this recurring bill",
            "detail": f"{commitment['name']}: {count} past date(s) have no linked payment",
            "commitment": commitment["commitment"], "scheduled_on": commitment["scheduled_on"],
            "todo": {
                "key": "recurring-history", "page": "Planning",
                "title": "Link past payments to your recurring bills",
                "detail": (
                    f"{commitment['name']} has {count} past date{'s' if count != 1 else ''} with "
                    "no payment linked or marked skipped. Until you do, those payments are "
                    "counted in your usual spending as well as on their own dates."
                ),
            },
        })

    for limit in card_limits:
        items.append({
            "kind": "needs_review", "output": "card_settlement", "currency": None,
            "rank": 90, "action": "Resolve the card settlement plan", "detail": limit,
        })

    for currency, cell in manifest.get("reserve_and_allowance", {}).get("by_currency", {}).items():
        reserve = cell.get("reserve", {})
        if not reserve.get("available"):
            items.append({
                "kind": "needs_review", "output": "reserve", "currency": currency,
                "rank": 40, "action": "Assign essential-status roles",
                "detail": str(reserve.get("reason", "Reserve eligibility is incomplete")),
            })
    return sorted(items, key=lambda item: (-item["rank"], item["output"], item["detail"]))


def _gap_attribution(base: dict[str, Any], conservative: dict[str, Any]) -> dict[str, Any]:
    scale = int(base["scale"])
    income = _unformat(base["totals"]["income"], scale) - _unformat(
        conservative["totals"]["income"], scale
    )
    expense_and_timing = _unformat(conservative["totals"]["expense"], scale) - _unformat(
        base["totals"]["expense"], scale
    )
    closing_gap = _unformat(base["closing_balance"], scale) - _unformat(
        conservative["closing_balance"], scale
    )
    lowest_gap = _unformat(base["lowest_balance"]["amount"], scale) - _unformat(
        conservative["lowest_balance"]["amount"], scale
    )
    return {
        "base_minus_conservative_closing": format_decimal(closing_gap, scale),
        "income_contribution": format_decimal(income, scale),
        "expense_and_timing_contribution": format_decimal(expense_and_timing, scale),
        "closing_reconciles": closing_gap == income + expense_and_timing,
        "base_minus_conservative_lowest_balance": format_decimal(lowest_gap, scale),
        "timing_note": (
            "earlier payments and later receipts can deepen or move the lowest-balance "
            "window without changing the horizon total; that effect is kept with the "
            "expense/timing contribution, separate from omitted income"
        ),
    }


def _monotonicity(base: dict[str, Any], conservative: dict[str, Any]) -> dict[str, Any]:
    scale = int(base["scale"])
    failures = [
        left["date"]
        for left, right in zip(base["days"], conservative["days"], strict=True)
        if _unformat(right["closing"], scale) > _unformat(left["closing"], scale)
        or _unformat(right["low_point"], scale) > _unformat(left["low_point"], scale)
    ]
    return {
        "holds": not failures,
        "failure_dates": failures,
        "contract": (
            "under identical cuts, lower or later income and higher or earlier "
            "spending must never improve conservative cash on any day"
        ),
    }


def _has_no_history(
    estimate: dict[str, Any] | None, currency: str, residual_categories: Sequence[ResidualCategory]
) -> bool:
    """A usable baseline exists and has nothing for this currency's pocket."""
    return (
        not residual_categories and bool(estimate and estimate.get("available"))
        and currency not in (estimate or {}).get("by_currency", {})
    )


def build_cash_timeline(
    manifest: dict[str, Any],
    *,
    sourced_events: Sequence[ForecastEvent] = (),
    residual_categories: Sequence[ResidualCategory] = (),
    likely_income_policy: str = LIKELY_EXCLUDE,
    strong_income_interruption: bool = False,
    estimate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build independent base and conservative six-month cash timelines.

    The manifest owns readiness, account/currency scope, cutoff, reserve policy
    and provenance.  Extra sourced events are explicit because Stage 3A does
    not fabricate identities or dates that its adapters do not expose.
    """
    if likely_income_policy not in LIKELY_POLICIES:
        raise ValueError(f"likely_income_policy must be one of {LIKELY_POLICIES}")
    if manifest.get("stale"):
        return {
            "available": False,
            "reason": "the Stage 3A manifest is stale; mixed-generation inputs are not forecast",
            "generation": manifest.get("generation"),
            "by_currency": {},
            "limitations": list(manifest.get("stale_reasons", [])),
        }
    cutoff_cell = manifest.get("cutoff") or {}
    cutoff = cutoff_cell.get("supported_cutoff")
    if cutoff is None:
        return {
            "available": False,
            "reason": "the Stage 3A manifest has no supported cash cutoff",
            "generation": manifest.get("generation"),
            "by_currency": {},
            "limitations": list(cutoff_cell.get("reasons", [])),
        }
    end = add_months(date.fromisoformat(str(cutoff)), HORIZON_MONTHS).isoformat()
    categories = list(residual_categories) or _manifest_categories(manifest, estimate)
    result_by_currency: dict[str, Any] = {}
    global_limits: list[str] = []
    # Sentences for the reader: money that should be on the path but is not,
    # for a reason no page can fix yet.
    answer_gaps: list[str] = []
    pockets = set(manifest.get("cash", {}).get("by_currency", {}))
    for currency in sorted({event.currency for event in sourced_events} - pockets):
        labels = sorted({event.label for event in sourced_events if event.currency == currency})
        global_limits.append(
            f"{len(labels)} sourced item(s) in {currency} have no {currency} cash pocket "
            "to settle against and are not forecast"
        )
        answer_gaps.append(
            f"{', '.join(labels)} ({currency}): there is no {currency} account to pay "
            "it from or into, so it isn't counted."
        )
    spending_only = manifest.get("income", {}).get("classified_income_evidenced") is False
    possible_income = [
        event
        for event in sourced_events
        if event.direction == DIRECTION_IN and event.confidence == CONFIDENCE_POSSIBLE
    ]
    for currency, cash in manifest.get("cash", {}).get("by_currency", {}).items():
        if cash.get("readiness") == READINESS_UNAVAILABLE or cash.get("balance") is None:
            result_by_currency[currency] = {
                "available": False,
                "readiness": "unavailable",
                "reason": cash.get("reason") or "no supported opening balance",
            }
            continue
        values = [str(cash["balance"])]
        scoped_events = [event for event in sourced_events if event.currency == currency]
        scoped_categories = [category for category in categories if category.currency == currency]
        values.extend(event.amount for event in scoped_events)
        values.extend(
            event.residual_amount
            for event in scoped_events
            if event.residual_amount is not None
        )
        for category in scoped_categories:
            values.extend((category.mean_monthly, category.highest_observed_monthly))
        reserve = manifest.get("reserve_and_allowance", {}).get("by_currency", {}).get(
            currency, {}
        ).get("reserve", {})
        if reserve.get("available") and reserve.get("amount") is not None:
            values.append(str(reserve["amount"]))
        scale = max(2, *(ExactMoney.parse(value).scale for value in values))
        opening = _minor(str(cash["balance"]), scale)
        reserve_minor = (
            _minor(str(reserve["amount"]), scale)
            if reserve.get("available") and reserve.get("amount") is not None
            else None
        )
        base = _scenario(
            name=SCENARIO_BASE,
            currency=currency,
            scale=scale,
            opening_minor=opening,
            reserve_minor=reserve_minor,
            start=str(cutoff),
            end=end,
            sourced_events=scoped_events,
            residual_categories=scoped_categories,
            likely_income_policy=likely_income_policy,
            strong_income_interruption=False,
        )
        conservative = _scenario(
            name=SCENARIO_CONSERVATIVE,
            currency=currency,
            scale=scale,
            opening_minor=opening,
            reserve_minor=reserve_minor,
            start=str(cutoff),
            end=end,
            sourced_events=scoped_events,
            residual_categories=scoped_categories,
            likely_income_policy=likely_income_policy,
            strong_income_interruption=strong_income_interruption,
        )
        monotonicity = _monotonicity(base, conservative)
        if not monotonicity["holds"]:
            conservative["available"] = False
            conservative["limitations"].append(
                "the conservative-case monotonicity contract failed"
            )
        scoped_possible = [event for event in possible_income if event.currency == currency]
        case_available = base["available"] and conservative["available"]
        result_by_currency[currency] = {
            "available": case_available,
            # A partial cash boundary or unavailable reserve still supports
            # cash arithmetic.  Keep that distinction explicit rather than
            # turning a useful currency into a page-wide gap.
            "readiness": (
                "unavailable"
                if not case_available
                else "partial"
                if cash.get("readiness") != "established-input estimate" or reserve_minor is None
                else "available"
            ),
            "starting_balance": format_decimal(opening, scale),
            "starting_balance_as_of": cutoff,
            "forecast_kind": "spending-only stress view" if spending_only else "cash-flow forecast",
            "cash_cutoff": {
                "observation_as_of": cash.get("observation_as_of", cutoff),
                "stale_days": cash.get("stale_days"),
                "missing_intervals": cash.get("missing_intervals", []),
                "bridge_status": "supported" if cash.get("readiness") == "established-input estimate" else "partial",
            },
            "scale": scale,
            "base": base,
            "conservative": conservative,
            "gap_attribution": _gap_attribution(base, conservative),
            "monotonicity": monotonicity,
            "possible_income_upside": {
                "included_in_base": False,
                "included_in_conservative": False,
                "events": [
                    {
                        "source_id": event.source_id,
                        "event_id": event.event_id,
                        "amount": event.amount,
                        "currency": event.currency,
                        "label": event.label,
                        "timing": {
                            "basis": event.timing_basis,
                            "on": event.on,
                            "earliest": event.window_earliest,
                            "latest": event.window_latest,
                        },
                    }
                    for event in scoped_possible
                ],
                "policy": "Possible income is upside only and never enters either base total",
            },
            "reserve_readiness": (
                {"available": True, "target": format_decimal(reserve_minor, scale)}
                if reserve_minor is not None
                else {
                    "available": False,
                    "reason": reserve.get("reason") or "reserve readiness is unavailable",
                    "cash_balance_results_blocked": False,
                }
            ),
        }
        if not scoped_categories and scoped_events and _has_no_history(estimate, currency, residual_categories):
            # Sourced money (a planned bill, a debt schedule) is enough to
            # project the pocket; with no spending history the usual
            # spending is simply zero, and the case says so.
            note = (
                f"{currency} has no spending history, so its usual spending is taken as zero "
                "and only the money scheduled for it is counted"
            )
            base["notes"].append(note)
            conservative["notes"].append(note)
            result_by_currency[currency]["no_spending_history"] = True
        elif not scoped_categories:
            message = (
                f"{currency} has no supported residual-spending input; known-event "
                "cash arithmetic is shown but a complete base or conservative case is unavailable"
            )
            global_limits.append(message)
            result_by_currency[currency]["available"] = False
            result_by_currency[currency]["reason"] = message
            base["available"] = False
            conservative["available"] = False
            base["limitations"].append(message)
            conservative["limitations"].append(message)
            result_by_currency[currency]["readiness"] = "unavailable"
    return {
        # Currencies are independent.  A missing foreign-pocket boundary must not hide a
        # usable SGD or USD forecast.
        "available": any(
            cell.get("available", False) for cell in result_by_currency.values()
        ),
        "readiness": (
            "available"
            if result_by_currency and all(cell.get("available", False) for cell in result_by_currency.values())
            else "partial" if any(cell.get("available", False) for cell in result_by_currency.values())
            else "unavailable"
        ),
        "generation": manifest.get("generation"),
        "manifest_as_of": manifest.get("as_of"),
        "source": "Stage 3A forecast input manifest",
        "horizon": {
            "supported_cash_cutoff": cutoff,
            "end": end,
            "months": HORIZON_MONTHS,
            "boundary_rule": (
                "the cutoff is the opening close; modelled days run from the next "
                "day through the same calendar day six months later, so partial "
                "first and last months are retained"
            ),
        },
        "by_currency": result_by_currency,
        "currency_policy": "independent timelines; unlike currencies are never added",
        "case_policy": {
            "base": "Confirmed plus evidence-derived Likely income",
            "conservative": f"Confirmed income; Likely-inclusion policy: {likely_income_policy}",
            "strong_income_interruption": strong_income_interruption,
            "strong_income_interruption_note": (
                "all recurring salary is excluded explicitly; this is a strong "
                "interruption scenario and is not presented as the likely outcome"
                if strong_income_interruption
                else None
            ),
        },
        "metadata": {
            **CONVENTIONS,
            "timing_windows": (
                "known dates govern; inferred receipts use the later observed bound "
                "and inferred payments the earlier bound in the conservative case"
            ),
            "conditionality": (
                "conditional events remain labelled with their condition in every event row"
            ),
            "allocation_rules": [DAILY_ALLOCATION_RULE, PARTIAL_MONTH_RULE],
        },
        "provenance": manifest.get("sources", []),
        "limitations": [*manifest.get("limitations", []), *global_limits],
        "answer_gaps": answer_gaps,
    }


def _obligation_events(
    page: dict[str, Any],
    *,
    start: str,
    end: str,
) -> tuple[list[ForecastEvent], list[str]]:
    events: list[ForecastEvent] = []
    limitations: list[str] = []
    first = date.fromisoformat(start) + timedelta(days=1)
    last = date.fromisoformat(end)
    for obligation in page.get("obligations", []):
        if not obligation.get("stays_visible", True):
            continue
        origin = str(obligation["origin"])
        cash_effect = obligation.get("cash_effect")
        # Card cash is sourced only from the three-way conversion contract.
        if origin == "card_statement" or cash_effect == "via_card_statement":
            continue
        amount_cell = obligation.get("remaining_amount")
        if amount_cell is None:
            limitations.append(
                f"{obligation['obligation_id']} has no supported remaining amount"
            )
            continue
        if cash_effect not in ("bank_withdrawal", None):
            limitations.append(
                f"{obligation['obligation_id']} has unresolved cash effect {cash_effect!r}"
            )
            continue
        direction = (
            DIRECTION_IN if obligation.get("direction") == "inflow" else DIRECTION_OUT
        )
        column = COLUMN_INCOME if direction == DIRECTION_IN else (
            COLUMN_LIFESTYLE if origin == "recurring_commitment" else COLUMN_DEBT_CARD
        )
        confidence = str(obligation.get("confidence") or CONFIDENCE_LIKELY)
        receivable = origin == "personal_debt" and direction == DIRECTION_IN
        if receivable:
            # Money someone owes back is upside until it arrives: it is listed,
            # never relied on in either case.
            confidence = CONFIDENCE_POSSIBLE
        date_basis = obligation.get("date_basis", {})
        link = obligation.get("baseline_link", {})
        # A recurring event removes nothing from the residual: the estimator has
        # already lifted every bound occurrence out of it, and history that is
        # not bound yet is an action item rather than a guessed removal.
        if origin == "recurring_commitment" and date_basis.get("schedule"):
            scheduled = schedule_occurrences(date_basis["schedule"], first, last)
            # remaining_amount is only the next due occurrence's amount, which
            # may itself be one of the varied ones.
            varied = {
                str(cell["scheduled_on"]): str(cell["amount"])
                for cell in obligation.get("occurrence_amounts", [])
            }
            skipped = set(obligation.get("skipped_occurrences", []))
            for occurrence in scheduled:
                iso = occurrence.isoformat()
                if iso in skipped:
                    continue
                events.append(
                    ForecastEvent(
                        event_id=f"{obligation['obligation_id']}:{iso}",
                        source_id=f"{obligation['obligation_id']}:{iso}",
                        currency=str(obligation["currency"]),
                        amount=varied.get(iso, str(obligation["expected_amount"])),
                        column=column,
                        direction=direction,
                        label=str(obligation["label"]),
                        on=iso,
                        confidence=confidence,
                        provenance={
                            # The obligation family this cash belongs to, so a shortfall can be
                            # attributed to debt repayment, an installment plan or a recurring
                            # commitment rather than to one undifferentiated "expenses" total.
                            "origin": origin,
                            "source": obligation["source"],
                            "obligation_id": obligation["obligation_id"],
                            "date_basis": date_basis,
                            "baseline_link": link,
                        },
                    )
                )
            continue
        schedule = obligation.get("schedule")
        if schedule:
            for index, row in enumerate(schedule):
                on = row.get("due_on")
                if on is None:
                    limitations.append(
                        f"{obligation['obligation_id']} schedule row {index} has no due date"
                    )
                    continue
                events.append(
                    ForecastEvent(
                        event_id=f"{obligation['obligation_id']}:schedule:{index}",
                        source_id=(
                            f"{obligation['obligation_id']}:schedule:"
                            f"{row.get('schedule_index', index)}"
                        ),
                        currency=str(obligation["currency"]),
                        amount=str(row["amount"]),
                        column=column,
                        direction=direction,
                        label=str(obligation["label"]),
                        on=str(on),
                        confidence=confidence,
                        provenance={
                            # The obligation family this cash belongs to, so a shortfall can be
                            # attributed to debt repayment, an installment plan or a recurring
                            # commitment rather than to one undifferentiated "expenses" total.
                            "origin": origin,
                            "source": obligation["source"],
                            "obligation_id": obligation["obligation_id"],
                            "date_basis": date_basis,
                        },
                    )
                )
            continue
        on = date_basis.get("value")
        if on is None and receivable:
            continue
        if on is None:
            limitations.append(
                f"{obligation['obligation_id']} has no safe settlement date; "
                "the obligation remains explicit and is not guessed"
            )
            continue
        events.append(
            ForecastEvent(
                event_id=str(obligation["obligation_id"]),
                source_id=str(obligation["obligation_id"]),
                currency=str(obligation["currency"]),
                amount=str(amount_cell["value"]),
                column=column,
                direction=direction,
                label=str(obligation["label"]),
                on=str(on),
                confidence=confidence,
                provenance={
                    # The obligation family this cash belongs to, so a shortfall can be
                    # attributed to debt repayment, an installment plan or a recurring
                    # commitment rather than to one undifferentiated "expenses" total.
                    "origin": origin,
                    "source": obligation["source"],
                    "obligation_id": obligation["obligation_id"],
                    "date_basis": date_basis,
                },
            )
        )
    return events, limitations


def _candidate_income_events(
    page: dict[str, Any], *, start: str, end: str
) -> tuple[list[ForecastEvent], list[str]]:
    events: list[ForecastEvent] = []
    limitations: list[str] = []
    first = date.fromisoformat(start) + timedelta(days=1)
    last = date.fromisoformat(end)
    for candidate in page.get("income_candidates", []):
        candidate_id = candidate.get("candidate")
        if not candidate_id:
            limitations.append(
                f"income candidate {candidate.get('merchant_key', '<unknown>')} has no "
                "canonical identity and is not guessed"
            )
            continue
        timing = candidate.get("timing_window", {})
        cadence = candidate.get("cadence")
        windows: list[tuple[str, str]] = []
        if cadence == "monthly" and timing.get("day_of_month_min") is not None:
            current = date(first.year, first.month, 1)
            while current <= last:
                length = calendar.monthrange(current.year, current.month)[1]
                earliest = date(
                    current.year,
                    current.month,
                    min(int(timing["day_of_month_min"]), length),
                )
                latest = date(
                    current.year,
                    current.month,
                    min(int(timing.get("day_of_month_max", timing["day_of_month_min"])), length),
                )
                if latest >= first and earliest <= last:
                    windows.append(
                        (max(earliest, first).isoformat(), min(latest, last).isoformat())
                    )
                current = add_months(current, 1, 1)
        elif timing.get("next_expected_between"):
            low, high = (date.fromisoformat(value) for value in timing["next_expected_between"])
            min_gap = int(timing.get("min_gap_days", 7))
            max_gap = int(timing.get("max_gap_days", min_gap))
            while low <= last:
                if high >= first:
                    windows.append(
                        (max(low, first).isoformat(), min(high, last).isoformat())
                    )
                low += timedelta(days=min_gap)
                high += timedelta(days=max_gap)
        else:
            limitations.append(
                f"income candidate {candidate_id} has no observed timing window and "
                "is not assigned a forecast date"
            )
            continue
        for index, (window_start, window_end) in enumerate(windows):
            source_id = f"{candidate_id}:occurrence:{index}:{window_start}:{window_end}"
            events.append(
                ForecastEvent(
                    event_id=source_id,
                    source_id=source_id,
                    currency=str(candidate["currency"]),
                    amount=str(candidate["expected_amount"]),
                    column=COLUMN_INCOME,
                    direction=DIRECTION_IN,
                    label=str(candidate["merchant_key"]),
                    timing_basis=TIMING_INFERRED,
                    window_earliest=window_start,
                    window_latest=window_end,
                    confidence=CONFIDENCE_LIKELY,
                    recurring_salary=True,
                    provenance={
                        "candidate": candidate_id,
                        "source_count": candidate.get("source_count"),
                        "amount_variation": candidate.get("amount_variation"),
                        "observed_timing_window": timing,
                    },
                )
            )
    return events, limitations


def _card_events(
    conversion: dict[str, Any], *, prospective: Sequence[dict[str, Any]]
) -> tuple[list[ForecastEvent], list[str], list[dict[str, Any]]]:
    """Card settlements, the limitations of cards without a plan, and those cards.

    A card without a reconciled plan settles nothing in the forecast, so the
    third value names it: its pocket's answer is missing that card's bills.
    """
    events: list[ForecastEvent] = []
    limitations: list[str] = []
    missing: list[dict[str, Any]] = []
    prospective_by_id = {str(item["id"]): item for item in prospective if item.get("id")}
    for card in conversion.get("cards", []):
        plan = card.get("plan", {})
        reconciliation = card.get("reconciliation", {})
        if not plan.get("available") or not reconciliation.get("reconciled"):
            limitations.append(
                f"card {card.get('account_id')} {card.get('currency')} has no "
                "reconciled exactly-once settlement plan"
            )
            unresolved = [item for item in plan.get("unresolved", []) if isinstance(item, dict)]
            limitations.extend(
                str(item.get("detail") or item) for item in plan.get("unresolved", [])
            )
            missing.append({
                "account_id": card.get("account_id"),
                "currency": card.get("currency"),
                "label": card.get("label") or card.get("account_id"),
                "requirements": {
                    str(item.get("detail")): str(item.get("requirement"))
                    for item in unresolved
                },
            })
            continue
        for row in plan.get("settlements", []):
            source_id = str(row["source_id"])
            inferred = row.get("basis") == "projected_cycle_due_date"
            purchase = prospective_by_id.get(source_id)
            events.append(
                ForecastEvent(
                    event_id=f"card_cash:{card['account_id']}:{source_id}",
                    source_id=source_id,
                    currency=str(row["currency"]),
                    amount=str(row["amount"]),
                    column=COLUMN_DEBT_CARD,
                    direction=DIRECTION_OUT,
                    label=str(card["label"]),
                    on=str(row["settle_on"]),
                    timing_basis=TIMING_INFERRED if inferred else TIMING_KNOWN,
                    window_earliest=str(row["settle_on"]),
                    window_latest=str(row["settle_on"]),
                    confidence=CONFIDENCE_CONFIRMED,
                    residual_category=(
                        f"manifest:routine:{row['currency']}" if purchase else None
                    ),
                    residual_on=str(row.get("purchased_on")) if purchase else None,
                    residual_amount=str(row["amount"]) if purchase else None,
                    removes_from_residual=purchase is not None,
                    provenance={
                        "origin": "card_statement",
                        "contract": conversion.get("contract_version"),
                        "partition": row.get("partition"),
                        "basis": row.get("basis"),
                        "card_account_id": card.get("account_id"),
                        "card_reconciliation": reconciliation,
                    },
                )
            )
    return events, limitations, missing


def cash_timeline(
    database: Path,
    *,
    today: date,
    likely_income_policy: str = LIKELY_EXCLUDE,
    strong_income_interruption: bool = False,
    prospective_card_purchases: Sequence[dict[str, Any]] = (),
    sourced_events: Sequence[ForecastEvent] = (),
    residual_categories: Sequence[ResidualCategory] = (),
) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return cash_timeline_from(
            connection,
            today=today,
            likely_income_policy=likely_income_policy,
            strong_income_interruption=strong_income_interruption,
            prospective_card_purchases=prospective_card_purchases,
            sourced_events=sourced_events,
            residual_categories=residual_categories,
        )
    finally:
        connection.close()


def cash_timeline_from(
    connection: sqlite3.Connection,
    *,
    today: date,
    likely_income_policy: str = LIKELY_EXCLUDE,
    strong_income_interruption: bool = False,
    prospective_card_purchases: Sequence[dict[str, Any]] = (),
    sourced_events: Sequence[ForecastEvent] = (),
    residual_categories: Sequence[ResidualCategory] = (),
) -> dict[str, Any]:
    """Read the Stage 3A/adapters and produce the backend Stage 3B result.

    ``today`` is required (§5.3): resolved once by the caller, never by this
    reader — no function inside the money-flow call graph consults the wall
    clock.
    """
    with request_cache(connection):
        return _cash_timeline(
            connection, today=today, likely_income_policy=likely_income_policy,
            strong_income_interruption=strong_income_interruption,
            prospective_card_purchases=prospective_card_purchases,
            sourced_events=sourced_events, residual_categories=residual_categories,
        )


def _cash_timeline(
    connection: sqlite3.Connection,
    *,
    today: date,
    likely_income_policy: str,
    strong_income_interruption: bool,
    prospective_card_purchases: Sequence[dict[str, Any]],
    sourced_events: Sequence[ForecastEvent],
    residual_categories: Sequence[ResidualCategory],
) -> dict[str, Any]:
    manifest = forecast_input_manifest_from(connection, today=today)
    cutoff_cell = manifest.get("cutoff") or {}
    cutoff = cutoff_cell.get("supported_cutoff")
    if manifest.get("stale") or cutoff is None:
        return build_cash_timeline(
            manifest,
            sourced_events=sourced_events,
            residual_categories=residual_categories,
            likely_income_policy=likely_income_policy,
            strong_income_interruption=strong_income_interruption,
        )
    # Obligations and the manifest must describe the balance boundary, not a
    # later wall-clock day for which the position adapter has already said the
    # actual cash bridge is incomplete.
    if manifest.get("as_of") != cutoff:
        manifest = forecast_input_manifest_from(connection, today=today, as_of=str(cutoff))
    end = add_months(date.fromisoformat(str(cutoff)), HORIZON_MONTHS).isoformat()
    estimate = baseline_estimate_from(connection, today=today)
    obligations = obligations_page_from(connection, as_of=str(cutoff), today=today)
    recurring = recurring_page_from(connection)
    obligation_events, obligation_limits = _obligation_events(
        obligations, start=str(cutoff), end=end
    )
    income_events, income_limits = _candidate_income_events(
        recurring, start=str(cutoff), end=end
    )
    prospective = list(prospective_card_purchases)
    cards = card_cash_conversion_from(
        connection,
        as_of=str(cutoff),
        horizon_months=HORIZON_MONTHS,
        prospective_purchases=prospective,
    )
    card_events, card_limits, missing_cards = _card_events(cards, prospective=prospective)

    from .sources import EXTRA_SOURCES, SourceContext

    context = SourceContext(
        cutoff=str(cutoff),
        start=(date.fromisoformat(str(cutoff)) + timedelta(days=1)).isoformat(),
        end=end,
        today=today.isoformat(),
        residual_category_ids=frozenset(
            category.category_id
            for category in (
                list(residual_categories) or _manifest_categories(manifest, estimate)
            )
        ),
        existing_source_ids=frozenset(
            event.source_id
            for event in [*obligation_events, *income_events, *card_events, *sourced_events]
        ),
    )
    extra = [adapter(connection, context) for adapter in EXTRA_SOURCES]
    extra_prospective = [item for result in extra for item in result.prospective_card_purchases]
    if extra_prospective:
        prospective.extend(extra_prospective)
        cards = card_cash_conversion_from(
            connection,
            as_of=str(cutoff),
            horizon_months=HORIZON_MONTHS,
            prospective_purchases=prospective,
        )
        card_events, card_limits, missing_cards = _card_events(cards, prospective=prospective)

    reported = {
        "manifest": manifest.get("generation"),
        "baseline_estimate": estimate.get("generation"),
        "obligations_page": obligations.get("generation"),
        "recurring_page": recurring.get("generation"),
        "card_cash_conversion": cards.get("generation"),
        **{
            str((result.manifest_source or {}).get("input") or f"source_{index}"): result.generation
            for index, result in enumerate(extra)
        },
    }
    if len({token for token in reported.values() if token is not None}) != 1:
        return {
            "available": False,
            "reason": "Stage 3B refused mixed-generation source adapters",
            "generation": manifest.get("generation"),
            "reported_generations": reported,
            "by_currency": {},
            "limitations": ["refresh the ledger and retry"],
        }
    manifest = {
        **manifest,
        "sources": [
            *manifest.get("sources", []),
            *(
                {
                    **result.manifest_source,
                    "matches_resolved_generation": (
                        result.generation is None
                        or result.generation == manifest.get("generation")
                    ),
                }
                for result in extra
                if result.manifest_source is not None
            ),
        ],
    }
    result = build_cash_timeline(
        manifest,
        sourced_events=[
            *obligation_events,
            *income_events,
            *card_events,
            *sourced_events,
            *(event for source in extra for event in source.events),
        ],
        residual_categories=residual_categories,
        likely_income_policy=likely_income_policy,
        strong_income_interruption=strong_income_interruption,
        estimate=estimate,
    )
    adapter_limits = [
        *obligation_limits, *income_limits, *card_limits,
        *(limit for source in extra for limit in source.limitations),
    ]
    result["answer_gaps"].extend(
        f"{obligation['label']} ({obligation['currency']}): how it is paid isn't known, so it isn't counted."
        for obligation in obligations.get("obligations", [])
        if obligation.get("stays_visible", True) and obligation.get("direction") == "outflow"
        and obligation["origin"] != "card_statement"
        and obligation.get("cash_effect") not in ("bank_withdrawal", None, "via_card_statement")
    )
    result["answer_gaps"].extend(limit for source in extra for limit in source.limitations)
    result["limitations"].extend(adapter_limits)
    for card in missing_cards:
        cell = result["by_currency"].get(str(card["currency"]))
        if cell is not None:
            cell.setdefault("missing_outflows", []).append({
                "kind": "card_statement", "account_id": card["account_id"],
                "label": card["label"],
                "reason": "its bills can't be dated or sized from the statements, so none are counted",
            })
    receivables = [
        obligation for obligation in obligations.get("obligations", [])
        if obligation["origin"] == "personal_debt" and obligation.get("direction") == "inflow"
        and obligation.get("stays_visible", True)
        and (obligation.get("date_basis") or {}).get("value") is not None
        and str(obligation["date_basis"]["value"]) <= str(cutoff)
    ]
    card_requirements = {
        detail: (card, code)
        for card in missing_cards
        for detail, code in card["requirements"].items()
    }
    receivable_items = [
        {
            "kind": "needs_review", "output": "receivable", "currency": obligation["currency"],
            "rank": 55, "action": "Record whether this money came back",
            "detail": f"{obligation['label']} was due back on {obligation['date_basis']['value']}",
            "obligation_id": obligation["obligation_id"],
            "todo": {
                "key": "receivable-overdue", "page": "Debts",
                "title": "Did the money you're owed come back?",
                "detail": (
                    f"{obligation['label']} was due back on {obligation['date_basis']['value']}. "
                    "Record the repayment if it came back, or give it a new date."
                ),
            },
        }
        for obligation in receivables
    ]
    result["action_items"] = sorted(
        [
            *_action_items(manifest, card_limits), *receivable_items,
            *(item for source in extra for item in source.action_items),
        ],
        key=lambda item: (-item["rank"], item["output"], item["detail"]),
    )
    for item in result["action_items"]:
        if item["output"] == "card_settlement" and item["detail"] in card_requirements:
            card, code = card_requirements[item["detail"]]
            item.update(requirement=code, account_id=card["account_id"], label=card["label"])
    result["source_contracts"] = {
        "obligations": obligations.get("rules", []),
        "card_cash_conversion": {
            "contract_version": cards.get("contract_version"),
            "unresolved": cards.get("unresolved", []),
            "partition_policy": (
                "existing billed, posted-unbilled and prospective purchases are "
                "taken only from the card conversion plan; card obligations and "
                "via-card installments are not emitted a second time"
            ),
        },
    }
    # Adapter gaps are output-owned action items.  They make the page partial,
    # but must not hide a usable per-currency cash timeline.
    if adapter_limits and result["available"]:
        result["readiness"] = "partial"
    return result


forecast_cash_timeline = cash_timeline


__all__ = [
    "COLUMNS",
    "COLUMN_ALLOCATION",
    "COLUMN_DEBT_CARD",
    "COLUMN_INCOME",
    "COLUMN_IRREGULAR",
    "COLUMN_LIFESTYLE",
    "CONFIDENCE_CONFIRMED",
    "CONFIDENCE_LEVELS",
    "CONFIDENCE_LIKELY",
    "CONFIDENCE_POSSIBLE",
    "CONVENTIONS",
    "DAILY_ALLOCATION_RULE",
    "DIRECTION_IN",
    "DIRECTION_OUT",
    "ENGINE_VERSION",
    "EXPENSE_COLUMNS",
    "HORIZON_MONTHS",
    "LIKELY_EXCLUDE",
    "LIKELY_INCLUDE",
    "LIKELY_POLICIES",
    "PARTIAL_MONTH_RULE",
    "PRECISION_DAY",
    "PRECISION_UNRESOLVED",
    "PRECISION_WINDOW",
    "SAME_DAY_CONVENTION",
    "SCENARIO_BASE",
    "SCENARIO_CONSERVATIVE",
    "TIMING_INFERRED",
    "TIMING_KNOWN",
    "TIMING_UNRESOLVED",
    "ForecastEvent",
    "ResidualCategory",
    "TimelineEvent",
    "add_months",
    "allocate_daily_cents",
    "build_cash_timeline",
    "cash_timeline",
    "day_range",
    "days_in_month",
    "month_of",
    "forecast_cash_timeline",
    "remaining_day_amount",
    "run_timeline",
    "settlement_reconciliation",
]
