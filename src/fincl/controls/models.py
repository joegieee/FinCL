"""Pure Stage 4B spending-control arithmetic.

The functions here accept already-sourced Stage 3B rows.  They never write to
the ledger and never promote a hypothetical amount to recorded or Confirmed.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..forecast.engine import (
    COLUMN_DEBT_CARD,
    COLUMN_INCOME,
    COLUMN_IRREGULAR,
    COLUMN_LIFESTYLE,
    DIRECTION_IN,
    DIRECTION_OUT,
    TIMING_KNOWN,
    TimelineEvent,
    run_timeline,
)
from ..money import ExactMoney, format_decimal

GROUP_MANDATORY = "mandatory_lifestyle"
GROUP_DEBTS = "debts_and_cards"
GROUP_IMPORTANT = "important_commitments"
GROUP_ALLOCATIONS = "planned_allocations"
COMMITMENT_GROUPS = (GROUP_MANDATORY, GROUP_DEBTS, GROUP_IMPORTANT, GROUP_ALLOCATIONS)


def _minor(value: str, scale: int) -> int:
    return ExactMoney.parse(value).rescaled_coefficient(scale)


def budget_pace(
    *,
    month: str,
    as_of: str,
    limit: str,
    observed_spending: str,
    coverage_supported: bool,
) -> dict[str, Any]:
    """Budget consumed versus elapsed time for the selected calendar month.

    This deliberately adds pace-named fields.  The budget read's strict
    ``remaining`` field is owned elsewhere and is never accepted or returned by
    this function.
    """
    first = date.fromisoformat(f"{month}-01")
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    observed_on = date.fromisoformat(as_of)
    if observed_on < first:
        elapsed = 0
        observation_end = None
    else:
        observation_end = min(observed_on, last)
        elapsed = (observation_end - first).days + 1
    total_days = last.day
    limit_value = Decimal(limit)
    spent = Decimal(observed_spending)
    consumed = (
        Decimal(0)
        if limit_value == 0
        else (spent / limit_value * Decimal(100)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
    )
    elapsed_pct = (Decimal(elapsed) / Decimal(total_days) * Decimal(100)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    projected = None
    headroom = None
    if elapsed and coverage_supported:
        projected_value = (spent * Decimal(total_days) / Decimal(elapsed)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        projected = str(projected_value)
        headroom = str((limit_value - projected_value).quantize(Decimal("0.01")))
    return {
        "selected_month": month,
        "observed_through": observation_end.isoformat() if observation_end else None,
        "elapsed_days": elapsed,
        "days_in_month": total_days,
        "calendar_elapsed_percent": str(elapsed_pct),
        "budget_consumed_percent": str(consumed),
        "coverage": {
            "supported": coverage_supported,
            "badge": (
                "Covered through observed date"
                if coverage_supported and observation_end
                else "Future month"
                if observation_end is None
                else "Coverage gap"
            ),
        },
        "pace_projected_spend": projected,
        "pace_estimated_headroom": headroom,
        "pace_basis": (
            "observed spending divided by elapsed calendar days, projected over "
            "the selected month's own day count"
        ),
    }


def _event_group(event: dict[str, Any], role_by_category: dict[str, dict[str, Any]]) -> tuple[str, int | None, bool]:
    if event.get("column") == COLUMN_DEBT_CARD:
        return GROUP_DEBTS, None, True
    provenance = event.get("provenance") or {}
    if event.get("column") == COLUMN_IRREGULAR or provenance.get("kind") == "sinking_fund_due_expense":
        return GROUP_IMPORTANT, provenance.get("tier"), True
    category = provenance.get("category_key")
    if category is None:
        category = (provenance.get("baseline_link") or {}).get("category_key")
    role = role_by_category.get(str(category), {}) if category else {}
    tier = provenance.get("tier", role.get("tier"))
    if event.get("column") == COLUMN_LIFESTYLE:
        if tier in (3, 4):
            return GROUP_IMPORTANT, int(tier), True
        return GROUP_MANDATORY, int(tier) if tier in (1, 2) else None, tier in (1, 2)
    return GROUP_IMPORTANT, int(tier) if tier in (1, 2, 3, 4) else None, False


def group_commitments(
    *,
    events: Iterable[dict[str, Any]],
    role_by_category: dict[str, dict[str, Any]],
    internal_allocations: Iterable[dict[str, Any]] = (),
    scale: int = 2,
) -> dict[str, Any]:
    """Partition unique commitments into four disjoint groups by source id."""
    groups: dict[str, dict[str, Any]] = {
        name: {"amount_minor": 0, "items": []} for name in COMMITMENT_GROUPS
    }
    seen: set[str] = set()
    duplicates: set[str] = set()
    unresolved: list[str] = []
    for event in events:
        if event.get("direction") != DIRECTION_OUT:
            continue
        source_id = str(event.get("source_id") or "")
        if not source_id:
            unresolved.append(f"event {event.get('event_id', '<unknown>')} has no canonical source id")
            continue
        if source_id in seen:
            duplicates.add(source_id)
            continue
        seen.add(source_id)
        group, tier, classification_supported = _event_group(event, role_by_category)
        amount_minor = _minor(str(event["amount"]), scale)
        item = {
            "event_id": str(event.get("event_id") or source_id),
            "source_id": source_id,
            "label": str(event.get("label") or source_id),
            "on": event.get("on"),
            "amount": format_decimal(amount_minor, scale),
            "cash_effect": "external_cash_outflow",
            "tier": tier,
            "classification_supported": classification_supported,
            "edit_href": _edit_href(event),
        }
        groups[group]["amount_minor"] += amount_minor
        groups[group]["items"].append(item)
        if not classification_supported:
            unresolved.append(f"{source_id} has no supported Tier 1–4 commitment role")

    for allocation in internal_allocations:
        source_id = str(allocation["source_id"])
        if source_id in seen:
            duplicates.add(source_id)
            continue
        seen.add(source_id)
        amount_minor = _minor(str(allocation["amount"]), scale)
        groups[GROUP_ALLOCATIONS]["amount_minor"] += amount_minor
        groups[GROUP_ALLOCATIONS]["items"].append(
            {
                **allocation,
                "source_id": source_id,
                "amount": format_decimal(amount_minor, scale),
                "cash_effect": "internal_earmark",
                "classification_supported": True,
            }
        )

    external = sum(
        int(groups[name]["amount_minor"])
        for name in (GROUP_MANDATORY, GROUP_DEBTS, GROUP_IMPORTANT)
    )
    internal = int(groups[GROUP_ALLOCATIONS]["amount_minor"])
    return {
        "groups": {
            name: {
                "amount": format_decimal(int(cell["amount_minor"]), scale),
                "cash_effect": "internal_earmark" if name == GROUP_ALLOCATIONS else "external_cash_outflow",
                "items": cell["items"],
            }
            for name, cell in groups.items()
        },
        "external_cash_outflows": format_decimal(external, scale),
        "internal_earmarks": format_decimal(internal, scale),
        "distinct_source_ids": len(seen),
        "duplicated_source_ids": sorted(duplicates),
        "disjoint": not duplicates,
        "unresolved": unresolved,
        "rule": (
            "each canonical source id belongs to exactly one group; internal earmarks "
            "reduce unallocated cash but are not external cash outflows"
        ),
    }


def _edit_href(event: dict[str, Any]) -> str:
    source = (event.get("provenance") or {}).get("source") or {}
    # Some obligation adapters carry a concise source label while richer
    # adapters carry a mapping. Presentation routing must not make the whole
    # controls read fail merely because provenance is the concise form.
    kind = source.get("kind") if isinstance(source, dict) else None
    if kind == "personal_debt":
        return "?view=Debts"
    if kind in {"card_statement", "installment_plan"}:
        return "?view=Cards" if kind == "card_statement" else "?view=Installments"
    if kind == "recurring_commitment":
        return "?view=Planning"
    if (event.get("provenance") or {}).get("fund"):
        return "?view=Controls#commitments"
    return "?view=Baseline"


def conservative_capacity(
    *,
    case: dict[str, Any],
    earmarked_minor: int,
    reserve_minor: int,
    scale: int,
    supported: bool,
    unsupported_reasons: Sequence[str] = (),
) -> dict[str, Any]:
    """Capacity from the minimum daily low after earmarks and reserve."""
    balances = [
        _minor(str(row["low_point"]), scale) - earmarked_minor - reserve_minor
        for row in case.get("days", [])
    ]
    minimum = min(balances) if balances else None
    if not supported or minimum is None:
        return {
            "available": False,
            "status": "unresolved",
            "reasons": list(unsupported_reasons) or ["the conservative daily path is unavailable"],
            "minimum_conservative_unallocated_reserve_adjusted_balance": (
                format_decimal(minimum, scale) if minimum is not None else None
            ),
            "additional_spending_capacity": None,
            "funding_need": format_decimal(-minimum, scale) if minimum is not None and minimum < 0 else None,
            "weekly": None,
        }
    if minimum < 0:
        return {
            "available": True,
            "status": "funding_need",
            "reasons": [],
            "minimum_conservative_unallocated_reserve_adjusted_balance": format_decimal(minimum, scale),
            "additional_spending_capacity": None,
            "funding_need": format_decimal(-minimum, scale),
            "weekly": None,
        }
    weekly = weekly_allowance(case=case, earmarked_minor=earmarked_minor, reserve_minor=reserve_minor, scale=scale)
    return {
        "available": True,
        "status": "capacity",
        "reasons": [],
        "minimum_conservative_unallocated_reserve_adjusted_balance": format_decimal(minimum, scale),
        "additional_spending_capacity": format_decimal(minimum, scale),
        "funding_need": None,
        "weekly": weekly,
    }


def weekly_allowance(*, case: dict[str, Any], earmarked_minor: int, reserve_minor: int, scale: int) -> dict[str, Any]:
    """Maximum equal dated weekly spend whose entire daily path stays funded."""
    days = case.get("days", [])
    if not days:
        return {"available": False, "reason": "no daily path", "schedule": []}
    first = date.fromisoformat(str(days[0]["date"]))
    last = date.fromisoformat(str(days[-1]["date"]))
    schedule_dates: list[str] = []
    cursor = first
    while cursor <= last:
        schedule_dates.append(cursor.isoformat())
        cursor += timedelta(days=7)
    per_week: int | None = None
    for row in days:
        count = sum(day <= str(row["date"]) for day in schedule_dates)
        if count == 0:
            continue
        available = _minor(str(row["low_point"]), scale) - earmarked_minor - reserve_minor
        candidate = available // count
        per_week = candidate if per_week is None else min(per_week, candidate)
    per_week = max(0, per_week or 0)
    verified = all(
        _minor(str(row["low_point"]), scale)
        - earmarked_minor
        - reserve_minor
        - per_week * sum(day <= str(row["date"]) for day in schedule_dates)
        >= 0
        for row in days
    )
    return {
        "available": verified,
        "amount_per_week": format_decimal(per_week, scale) if verified else None,
        "schedule": [
            {"on": day, "amount": format_decimal(per_week, scale)}
            for day in schedule_dates
        ],
        "entire_path_verified": verified,
        "total_scheduled": format_decimal(per_week * len(schedule_dates), scale),
        "rule": (
            "equal dated weekly outflows are tested against every later daily low; "
            "the amount is not an ending surplus divided by weeks"
        ),
    }


def _timeline_from_row(row: dict[str, Any], scale: int) -> TimelineEvent:
    timing = row.get("timing") or {}
    return TimelineEvent(
        event_id=str(row["event_id"]),
        source_id=str(row["source_id"]),
        column=str(row["column"]),
        direction=str(row["direction"]),
        on=str(row["on"]),
        amount_minor=_minor(str(row["amount"]), scale),
        label=str(row["label"]),
        timing_basis=str(timing.get("basis") or TIMING_KNOWN),
        window_earliest=timing.get("earliest"),
        window_latest=timing.get("latest"),
        conditional=bool(row.get("conditional")),
        condition=row.get("condition"),
        provenance=dict(row.get("provenance") or {}),
    )


def apply_what_if_case(
    *,
    case: dict[str, Any],
    currency: str,
    category_cuts: Sequence[dict[str, str]],
    date_changes: Sequence[dict[str, str]],
    hypothetical_income: Sequence[dict[str, str]],
) -> tuple[dict[str, Any], list[str]]:
    """Re-run one case with isolated proposed changes."""
    scale = int(case["scale"])
    events = [_timeline_from_row(row, scale) for row in case["events"]["items"]]
    unresolved: list[str] = []

    changes = {row["event_id"]: row["new_date"] for row in date_changes}
    found_changes: set[str] = set()
    changed_events: list[TimelineEvent] = []
    for event in events:
        new_on = changes.get(event.event_id)
        if new_on is None:
            changed_events.append(event)
            continue
        found_changes.add(event.event_id)
        changed_events.append(
            TimelineEvent(
                event_id=event.event_id,
                source_id=event.source_id,
                column=event.column,
                direction=event.direction,
                on=new_on,
                amount_minor=event.amount_minor,
                label=event.label,
                timing_basis=TIMING_KNOWN,
                conditional=True,
                condition="proposed date change; not recorded",
                provenance={**event.provenance, "hypothetical": True, "original_date": event.on},
            )
        )
    for missing in sorted(set(changes) - found_changes):
        unresolved.append(f"date change event {missing} is not present in this case")
    events = changed_events

    for cut in category_cuts:
        category_id = cut["category_id"]
        requested = _minor(cut["monthly_reduction"], scale)
        matched = [event for event in events if event.provenance.get("category_id") == category_id and event.direction == DIRECTION_OUT]
        if not matched:
            unresolved.append(f"category {category_id} has no reducible spending event in this case")
            continue
        by_month: dict[str, list[TimelineEvent]] = {}
        for event in matched:
            by_month.setdefault(event.on[:7], []).append(event)
        replacements: dict[str, TimelineEvent] = {}
        for month, rows in by_month.items():
            remaining = requested
            for event in sorted(rows, key=lambda item: item.on, reverse=True):
                reduction = min(remaining, event.amount_minor)
                remaining -= reduction
                replacements[event.event_id] = TimelineEvent(
                    event_id=event.event_id,
                    source_id=event.source_id,
                    column=event.column,
                    direction=event.direction,
                    on=event.on,
                    amount_minor=event.amount_minor - reduction,
                    label=event.label,
                    timing_basis=event.timing_basis,
                    window_earliest=event.window_earliest,
                    window_latest=event.window_latest,
                    conditional=True,
                    condition="proposed Tier 3/4 cut; not recorded",
                    provenance={**event.provenance, "hypothetical": True, "proposed_cut": format_decimal(reduction, scale)},
                )
                if remaining == 0:
                    break
            if remaining:
                unresolved.append(
                    f"{format_decimal(remaining, scale)} {currency} of the proposed {category_id} cut exceeds modelled {month} spending"
                )
        events = [replacements.get(event.event_id, event) for event in events]

    for index, income in enumerate(hypothetical_income):
        amount = _minor(income["amount"], scale)
        events.append(
            TimelineEvent(
                event_id=f"hypothetical-income:{index}",
                source_id=f"hypothetical-income:{index}",
                column=COLUMN_INCOME,
                direction=DIRECTION_IN,
                on=income["on"],
                amount_minor=amount,
                label=income.get("label") or "Hypothetical income",
                timing_basis=TIMING_KNOWN,
                conditional=True,
                condition="hypothetical income; not recorded or Confirmed",
                provenance={"hypothetical": True, "confidence": "hypothetical", "confirmed": False},
            )
        )

    reserve = case.get("first_reserve_breach") or {}
    reserve_minor = _minor(str(reserve["reserve_target"]), scale) if reserve.get("available") else None
    result = run_timeline(
        currency=currency,
        scale=scale,
        opening_minor=_minor(str(case["starting_balance"]), scale),
        start=str(case["starting_balance_as_of"]),
        end=str(case["horizon"]["end"]),
        events=events,
        reserve_minor=reserve_minor,
    )
    result["available"] = not unresolved
    result["limitations"] = unresolved
    result["hypothetical"] = True
    result["statement"] = "Proposed changes only; no cut, date change or income has been recorded."
    return result, unresolved


def case_comparison(before: dict[str, Any], after: dict[str, Any], *, earmarked_minor: int, reserve_minor: int) -> dict[str, Any]:
    scale = int(before["scale"])
    before_low = min(_minor(str(row["low_point"]), scale) for row in before["days"]) - earmarked_minor - reserve_minor
    after_low = min(_minor(str(row["low_point"]), scale) for row in after["days"]) - earmarked_minor - reserve_minor
    return {
        "before_minimum_conservative_unallocated_reserve_adjusted_balance": format_decimal(before_low, scale),
        "after_minimum_conservative_unallocated_reserve_adjusted_balance": format_decimal(after_low, scale),
        "before_gap": format_decimal(max(0, -before_low), scale),
        "after_gap": format_decimal(max(0, -after_low), scale),
        "gap_improvement": format_decimal(max(0, -before_low) - max(0, -after_low), scale),
        "cash_improvement": format_decimal(after_low - before_low, scale),
        "unchanged_case_preserved": True,
    }


__all__ = [
    "COMMITMENT_GROUPS",
    "GROUP_ALLOCATIONS",
    "GROUP_DEBTS",
    "GROUP_IMPORTANT",
    "GROUP_MANDATORY",
    "apply_what_if_case",
    "budget_pace",
    "case_comparison",
    "conservative_capacity",
    "group_commitments",
    "weekly_allowance",
]
