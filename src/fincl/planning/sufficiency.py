"""Will I have enough money, and if not, how short will I be?

Everything needed to answer this already existed, scattered across three reads:
the forecast timeline knows when cash runs out, the reserve policy knows how
much must stay untouched, and the obligations page knows what is owed. What did
not exist was the answer itself — a single verdict, a date, an amount, and the
drivers behind it.

This module composes those reads and adds no arithmetic of its own beyond
attribution. Every figure it quotes is copied from the timeline that produced
it, which is why a shortfall here can never disagree with the forecast it came
from.

Three distinctions are load-bearing.

*Running out of cash and dipping below the reserve are different answers.* The
first is insolvency: money the ledger says will not be there. The second is
spending the buffer, which is a decision rather than a failure. They are
reported as different verdicts with different amounts, never merged into one
"shortfall".

*The base case and the conservative case are both answers.* "Enough, unless
spending runs at its observed high" is a genuinely different position from
"enough either way", and collapsing them would hide exactly the risk the
conservative case exists to show. The headline verdict is the base case; the
conservative one is stated beside it.

*A shortfall names what caused it.* A verdict with no drivers leaves nothing to
act on, so the breach month's outflows are attributed to the obligation family
each came from — debt repayment, an installment or pay-later plan, a recurring
commitment, a card settlement, a sinking-fund allocation, or baseline spending —
using the provenance the forecast already carries. Nothing is inferred from a
label.

*Unavailable is not zero.* A currency whose forecast could not be built reports
its own reasons and no verdict at all. A missing answer must never read as
"yes, you have enough".
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..forecast.engine import cash_timeline_from
from ..money import ExactMoney, format_decimal
from ..reserve import reserve_policy

__all__ = ["sufficiency_page", "sufficiency_page_from"]

SUFFICIENCY_VERSION = "sufficiency-v1"

VERDICT_ENOUGH = "enough"
VERDICT_BELOW_RESERVE = "below_reserve"
VERDICT_SHORT = "short"
VERDICT_UNAVAILABLE = "unavailable"

# How an outflow is described to someone deciding what to change. The key is the
# `origin` the obligations page assigns and the forecast carries through in each
# event's provenance; the order is the order a shortfall breakdown lists them,
# most committed first, because that is the order in which they are hardest to
# change.
_DRIVER_LABELS: tuple[tuple[str, str], ...] = (
    ("installment_plan", "Installment and pay-later bills"),
    ("personal_debt", "Debt repayments"),
    ("card_statement", "Card settlements"),
    ("recurring_commitment", "Recurring commitments"),
    ("sinking_fund", "Annual funds set aside"),
    ("baseline_spending", "Baseline spending"),
    ("other", "Other committed outflows"),
)
_DRIVER_ORDER = {origin: index for index, (origin, _) in enumerate(_DRIVER_LABELS)}
_DRIVER_LABEL = dict(_DRIVER_LABELS)


def _minor(value: str, scale: int) -> int:
    return ExactMoney.parse(str(value)).rescaled_coefficient(scale)


def _driver_origin(event: dict[str, Any]) -> str:
    """Which family of committed money this outflow belongs to.

    Read from the provenance the forecast already attached, never guessed from
    the label: a card purchase and a bank purchase at the same merchant read
    identically by name and settle completely differently.
    """
    provenance = event.get("provenance") or {}
    origin = provenance.get("origin")
    if isinstance(origin, str) and origin in _DRIVER_ORDER:
        return origin
    if provenance.get("kind") == "unscheduled_residual_allocation":
        return "baseline_spending"
    if provenance.get("fund") or str(event.get("source_id", "")).startswith("fund:"):
        return "sinking_fund"
    if str(event.get("source_id", "")).startswith("card:"):
        return "card_statement"
    return "other"


def _drivers(
    events: list[dict[str, Any]], *, month: str | None, scale: int
) -> list[dict[str, Any]]:
    """Outflows grouped by what committed them: the breach month's, or with
    ``month=None`` the whole horizon's."""
    totals: dict[str, int] = {}
    examples: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event["direction"] != "outflow" or (month is not None and event["on"][:7] != month):
            continue
        origin = _driver_origin(event)
        totals[origin] = totals.get(origin, 0) + _minor(event["amount"], scale)
        # Residual allocations are one slice per day, so naming each one would
        # bury the obligations that a reader can actually act on.
        if origin != "baseline_spending":
            examples.setdefault(origin, []).append({
                "label": event["label"],
                "on": event["on"],
                "amount": event["amount"],
                "conditional": bool(event.get("conditional")),
            })
    rows: list[dict[str, Any]] = [
        {
            "origin": origin,
            "label": _DRIVER_LABEL.get(origin, origin),
            "amount": format_decimal(amount, scale),
            "amount_minor": amount,
            "items": sorted(
                examples.get(origin, []), key=lambda item: (item["on"], item["label"])
            )[:6],
            "item_count": len(examples.get(origin, [])),
        }
        for origin, amount in totals.items()
        if amount
    ]
    rows.sort(
        key=lambda row: (-int(row["amount_minor"]), _DRIVER_ORDER.get(str(row["origin"]), 99))
    )
    return rows


def _deepest_deficit(scenario: dict[str, Any], scale: int) -> tuple[str | None, int]:
    """The worst point at or below zero across the horizon, and when.

    The first zero is when the problem starts; this is how big it gets, which is
    the figure that answers "how much would I need". They are different
    questions and both are reported.
    """
    worst_day: str | None = None
    worst = 0
    for row in scenario.get("days", []):
        low = _minor(row["low_point"], scale)
        if low < worst:
            worst, worst_day = low, row["date"]
    return worst_day, worst


def _case(scenario: dict[str, Any], *, scale: int) -> dict[str, Any]:
    """One scenario's verdict, its amount, its date and what drove it."""
    if not scenario.get("available"):
        return {
            "verdict": VERDICT_UNAVAILABLE,
            "shortfall": None,
            "shortfall_on": None,
            "first_short_on": None,
            "reserve_target": None,
            "drivers": [],
            "accounted_for": [],
            "reasons": list(scenario.get("limitations", [])),
            "headline": "This case could not be built from the available evidence.",
        }
    events = list((scenario.get("events") or {}).get("items", []))
    # Everything the path already pays for, whatever the verdict. A "yes" with
    # no breakdown gives no way to tell whether a debt just recorded was
    # counted at all.
    accounted_for = _drivers(events, month=None, scale=scale)
    first_zero = scenario.get("first_zero") or {}
    breach = scenario.get("first_reserve_breach") or {}
    gaps = scenario.get("reserve_funding_gaps") or {}

    if first_zero.get("occurs"):
        worst_day, worst = _deepest_deficit(scenario, scale)
        on = str(first_zero.get("on") or first_zero.get("modelled_on") or worst_day or "")
        return {
            "verdict": VERDICT_SHORT,
            # The magnitude of the deepest point below zero: what would have to
            # arrive for the path to stay solvent, not merely to touch zero.
            "shortfall": format_decimal(-worst, scale),
            "shortfall_on": worst_day,
            "first_short_on": on,
            "reserve_target": breach.get("reserve_target"),
            "drivers": _drivers(events, month=on[:7], scale=scale) if on else [],
            "accounted_for": accounted_for,
            "reasons": [],
            "headline": f"Cash runs out on {on}.",
        }

    if breach.get("available") and breach.get("occurs"):
        on = str(breach.get("on") or breach.get("modelled_on") or "")
        return {
            "verdict": VERDICT_BELOW_RESERVE,
            "shortfall": gaps.get("largest_gap"),
            "shortfall_on": on,
            "first_short_on": on,
            "reserve_target": breach.get("reserve_target"),
            "drivers": _drivers(events, month=on[:7], scale=scale) if on else [],
            "accounted_for": accounted_for,
            "reasons": [],
            "headline": (
                f"Cash stays positive but drops below your reserve on {on}."
            ),
        }

    return {
        "verdict": VERDICT_ENOUGH,
        "shortfall": None,
        "shortfall_on": None,
        "first_short_on": None,
        "reserve_target": breach.get("reserve_target"),
        "drivers": [],
        "accounted_for": accounted_for,
        "reasons": [],
        "headline": "Cash stays above zero and above your reserve across the horizon.",
    }


_ANSWER = {
    VERDICT_ENOUGH: "Yes",
    VERDICT_BELOW_RESERVE: "Yes, but it eats into your reserve",
    VERDICT_SHORT: "No",
    VERDICT_UNAVAILABLE: "Not enough evidence to say",
}


def sufficiency_page_from(
    connection: sqlite3.Connection, *, today: date
) -> dict[str, Any]:
    timeline = cash_timeline_from(connection, today=today)
    policy = reserve_policy(connection)
    by_currency: dict[str, Any] = {}
    for currency, cell in (timeline.get("by_currency") or {}).items():
        scale = int(cell.get("scale") or 2)
        base = _case(cell.get("base") or {}, scale=scale)
        conservative = _case(cell.get("conservative") or {}, scale=scale)
        by_currency[currency] = {
            "currency": currency,
            "scale": scale,
            "answer": _ANSWER[base["verdict"]],
            "verdict": base["verdict"],
            "readiness": cell.get("readiness"),
            "starting_balance": cell.get("starting_balance"),
            "starting_balance_as_of": cell.get("starting_balance_as_of"),
            "horizon_end": (cell.get("base") or {}).get("horizon", {}).get("end"),
            "base": base,
            "conservative": conservative,
            # The two cases disagreeing is itself the finding: the position is
            # only safe if nothing goes the way the observed high month went.
            "only_safe_in_the_base_case": (
                base["verdict"] == VERDICT_ENOUGH
                and conservative["verdict"] in (VERDICT_SHORT, VERDICT_BELOW_RESERVE)
            ),
        }
    return {
        "version": SUFFICIENCY_VERSION,
        "generated_for": today.isoformat(),
        "reserve_policy": {
            "months": str(policy.months),
            "is_default": policy.is_default,
            "decided_at": policy.decided_at,
            "reason": policy.reason,
            "basis": (
                "the month count is your policy; the monthly amount it multiplies "
                "is always the evidence-derived mean essential spend"
            ),
        },
        "by_currency": by_currency,
        "limitations": list(timeline.get("limitations", [])),
        "semantics": {
            "cases": (
                "the headline verdict is the base case; the conservative case is "
                "reported beside it and never merged into it"
            ),
            "reserve_versus_zero": (
                "running out of cash and dipping into the reserve are separate "
                "verdicts with separate amounts"
            ),
            "unavailable": (
                "a currency whose forecast could not be built reports its reasons "
                "and no verdict; it is never reported as having enough"
            ),
        },
    }


def sufficiency_page(database: Path, *, today: date) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return sufficiency_page_from(connection, today=today)
    finally:
        connection.close()
