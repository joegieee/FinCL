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

import re
import sqlite3
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..forecast.engine import HORIZON_MONTHS, cash_timeline_from
from ..money import ExactMoney, format_decimal
from ..obligations.queries import obligations_page_from
from ..read_cache import request_cache
from ..reporting_currency.models import CURRENCY_REGISTRY
from ..reporting_currency.queries import reporting_policy
from ..reserve import reserve_policy
from ..valuation import (
    FORWARD_MONTHLY_CLOSED_V1,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    quantize,
    value_one,
)

__all__ = ["combined_answer", "sufficiency_page", "sufficiency_page_from"]

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
    ("planned", "Planned spending"),
    ("planned_income", "Planned income"),
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


# --- The combined answer ----------------------------------------------------
#
# Every pocket is one pool of money for one purpose, so the question is asked
# once, across all of them, in the reporting currency. Each pocket's own path
# is copied from the forecast unchanged; the only new arithmetic is the same
# calendar-day sum the Forecast tab's combined minimum already uses (each
# pocket's low point that day, times its one held forecast rate), never a sum
# of lows that happen on different days.
#
# A pocket is counted in one of three ways, and the answer says which:
#   * projected — its forecast was built, so its day-by-day path is used;
#   * held as it stands — its forecast could not be built only because it has
#     no spending history, and nothing is scheduled in or out of it, so its
#     path is its balance, flat. Nothing is projected for it;
#   * not counted — anything else (no balance, no rate, scheduled money with no
#     spending model). The answer is then marked incomplete and names it.
#
# A fourth case sits outside the pool: a pocket with nothing scheduled in or
# out and less than one whole unit of its own currency is ignored. One unit is
# worth at most a few dollars in any currency, so this needs no exchange rate,
# and it neither blocks the answer nor asks for a fix.

COUNTED_PROJECTED = "projected"
COUNTED_AS_IS = "held_as_is"
COUNTED_NOT = "not_counted"
COUNTED_IGNORED = "ignored"
_NO_RATE = "no exchange rate to the reporting currency is available"

# Where each kind of fix is made, as the app's own tab name.
_FIX_PAGE = {
    "imports": "Setup",
    "cards": "Cards",
    "debts": "Debts",
    "income": "Income",
    "essential": "Categorization",
}


def _fraction(value: Any) -> Fraction:
    money = ExactMoney.parse(str(value))
    return Fraction(money.coefficient, 10**money.scale)


def _quantized(value: Fraction, scale: int) -> str:
    rounded = quantize(value, scale=scale)
    return format_decimal(rounded.coefficient, rounded.scale)


def _counting(cell: dict[str, Any], rate: Fraction | None) -> tuple[str, str | None]:
    """How a pocket enters the combined answer, and why when it does not."""
    base = cell.get("base") or {}
    events = (base.get("events") or {}).get("items", [])
    balance = cell.get("starting_balance")
    if not cell.get("available") and not events and balance is not None and 0 <= _fraction(balance) < 1:
        return COUNTED_IGNORED, "it holds less than one unit of its currency and nothing is scheduled"
    if rate is None:
        return COUNTED_NOT, _NO_RATE
    if cell.get("available"):
        return COUNTED_PROJECTED, None
    if base.get("days") and not events and cell.get("starting_balance") is not None:
        return COUNTED_AS_IS, "it has no spending history and nothing is scheduled in or out of it"
    return COUNTED_NOT, str(cell.get("reason") or "its forecast could not be built")


def _combined_path(
    cells: dict[str, dict[str, Any]], rates: dict[str, Fraction], *, name: str
) -> list[tuple[str, Fraction, Fraction]]:
    """(date, combined low point, combined closing) for every modelled day."""
    by_day: dict[str, list[Fraction]] = {}
    for currency, cell in cells.items():
        for row in (cell.get(name) or {}).get("days", []):
            totals = by_day.setdefault(row["date"], [Fraction(0), Fraction(0)])
            totals[0] += _fraction(row["low_point"]) * rates[currency]
            totals[1] += _fraction(row["closing"]) * rates[currency]
    return [(day, low, closing) for day, (low, closing) in sorted(by_day.items())]


def _combined_case(
    path: list[tuple[str, Fraction, Fraction]], *, reserve: Fraction | None, scale: int
) -> dict[str, Any]:
    if not path:
        return {"verdict": VERDICT_UNAVAILABLE, "first_short_on": None, "shortfall": None,
                "shortfall_on": None, "lowest": None, "lowest_on": None, "closing": None}
    lowest_on, lowest, _ = min(path, key=lambda row: row[1])
    first_zero = next((day for day, low, _ in path if low <= 0), None)
    below = [(day, reserve - low) for day, low, _ in path if reserve is not None and low < reserve]
    result: dict[str, Any] = {
        "lowest": _quantized(lowest, scale),
        "lowest_on": lowest_on,
        "closing": _quantized(path[-1][2], scale),
    }
    if first_zero is not None:
        return {**result, "verdict": VERDICT_SHORT, "first_short_on": first_zero,
                "shortfall": _quantized(-lowest, scale), "shortfall_on": lowest_on}
    if below:
        worst_day, worst_gap = max(below, key=lambda row: row[1])
        return {**result, "verdict": VERDICT_BELOW_RESERVE, "first_short_on": below[0][0],
                "shortfall": _quantized(worst_gap, scale), "shortfall_on": worst_day}
    return {**result, "verdict": VERDICT_ENOUGH, "first_short_on": None,
            "shortfall": None, "shortfall_on": None}


def _where_it_goes(
    cells: dict[str, dict[str, Any]], rates: dict[str, Fraction], *, scale: int
) -> list[dict[str, Any]]:
    """Every outflow family across the horizon, all pockets together.

    One list replaces the old pair ("what drives it" for the breach month and
    "everything this answer counts" for the horizon): the same families, the
    same provenance, valued once in the reporting currency.
    """
    totals: dict[str, Fraction] = {}
    examples: dict[str, list[dict[str, Any]]] = {}
    for currency, cell in cells.items():
        native_scale = int(cell.get("scale") or 2)
        events = ((cell.get("base") or {}).get("events") or {}).get("items", [])
        for row in _drivers(events, month=None, scale=native_scale):
            origin = str(row["origin"])
            totals[origin] = totals.get(origin, Fraction(0)) + (
                Fraction(int(row["amount_minor"]), 10**native_scale) * rates[currency]
            )
            examples.setdefault(origin, []).extend(
                {**item, "currency": currency} for item in row["items"]
            )
    rows = [
        {
            "origin": origin,
            "label": _DRIVER_LABEL.get(origin, origin),
            "total": _quantized(amount, scale),
            "monthly": _quantized(amount / HORIZON_MONTHS, scale),
            "items": sorted(examples.get(origin, []), key=lambda item: (item["on"], item["label"]))[:6],
        }
        for origin, amount in totals.items()
        if amount
    ]
    rows.sort(key=lambda row: (-_fraction(row["total"]), _DRIVER_ORDER.get(str(row["origin"]), 99)))
    return rows


def _income(
    cells: dict[str, dict[str, Any]], rates: dict[str, Fraction], *, scale: int
) -> list[dict[str, Any]]:
    """Each income source the main answer counts, and whether the cautious
    case also counts it (Likely income is left out of the cautious case)."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for currency, cell in cells.items():
        for event in ((cell.get("base") or {}).get("events") or {}).get("items", []):
            if event["direction"] != "inflow":
                continue
            key = (currency, str(event["label"]))
            group = groups.setdefault(key, {
                "label": event["label"], "currency": currency, "count": 0,
                "native_total": Fraction(0), "next_on": event["on"],
                "confidence": (event.get("provenance") or {}).get("confidence") or "confirmed",
            })
            group["count"] += 1
            group["native_total"] += _fraction(event["amount"])
            group["next_on"] = min(group["next_on"], event["on"])
    rows = []
    for (currency, _), group in sorted(groups.items()):
        native_scale = int(cells[currency].get("scale") or 2)
        rows.append({
            "label": group["label"], "currency": currency, "count": group["count"],
            "next_on": group["next_on"], "confidence": group["confidence"],
            "native_total": _quantized(group["native_total"], native_scale),
            "total": _quantized(group["native_total"] * rates[currency], scale),
            "in_cautious_case": group["confidence"] != "likely",
        })
    return rows


def _todos(
    timeline: dict[str, Any], *, account_names: dict[str, str], cutoff: str | None,
    pockets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """What would make the answer better, most useful first, each pointing at
    the page that fixes it. Grouped so one fix reads as one item, never as one
    line per account per currency."""
    items = list(timeline.get("action_items") or [])
    todos: list[dict[str, Any]] = []

    def name(account: str | None) -> str:
        return account_names.get(account or "", account or "an account")

    recent = sorted({name(i.get("account_id")) for i in items
                     if i.get("output") == "cash" and cutoff and str(i.get("start", "")) > cutoff})
    if recent:
        todos.append({
            "key": "recent-statements", "page": _FIX_PAGE["imports"],
            "title": "Import your latest statements",
            "detail": f"Nothing has been imported after {cutoff}, so the answer starts from that day. "
                      f"Accounts: {', '.join(recent)}.",
            "since": cutoff,
        })

    for item in items:
        if item.get("output") != "card_settlement":
            continue
        detail = str(item.get("detail", ""))
        window = re.search(r"covering (\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", detail)
        if window:
            todos.append({
                "key": "card-statement", "page": _FIX_PAGE["imports"],
                "title": "Import the missing card statement",
                "detail": f"Card spending between {window.group(1)} and {window.group(2)} is unknown, "
                          "so it is not counted.",
            })
    history = sorted({str(i.get("label")) for i in items
                      if i.get("requirement") == "cycle_geometry_unevidenced"})
    if history:
        todos.append({
            "key": "card-history", "page": _FIX_PAGE["imports"],
            "title": "Import another statement for your card",
            "detail": "Two statements with a due date are needed before the next card bill can be "
                      f"dated, so these cards' bills aren't counted yet: {', '.join(history)}.",
        })
    # Only when how the card is repaid is the unknown: a missing statement is
    # its own to-do above, and the generic "no settlement plan" line is just
    # the consequence of one of the two.
    repayment_unknown = ("revolves", "no repayment schedule is assumed")
    if any(i.get("output") == "card_settlement" and any(m in str(i.get("detail", "")) for m in repayment_unknown)
           for i in items):
        todos.append({
            "key": "card-plan", "page": _FIX_PAGE["cards"],
            "title": "Say how your credit card is being paid off",
            "detail": "Your card repayments aren't matched to its statements, so when and how much you "
                      "pay can't be read from them. Until you say, card repayments are not counted.",
        })

    debts = sorted({str(i.get("label")) for i in items
                    if i.get("output") == "obligation_timing" and i.get("origin") == "personal_debt"})
    if debts:
        todos.append({
            "key": "debt-dates", "page": _FIX_PAGE["debts"], "title": "Give your debts a due date",
            "detail": f"Without a date they are not in the answer: {', '.join(debts)}.",
        })

    # An action item may name its own fix. Items sharing a key are one to-do.
    own: dict[str, dict[str, Any]] = {}
    for item in items:
        todo = item.get("todo")
        if not todo:
            continue
        if todo["key"] in own:
            if todo["detail"] not in own[todo["key"]]["detail"]:
                own[todo["key"]]["detail"] += f" {todo['detail']}"
            continue
        own[todo["key"]] = {
            "key": str(todo["key"]), "page": str(todo["page"]),
            "title": str(todo["title"]), "detail": str(todo["detail"]),
        }
    todos.extend(own.values())

    if any(i.get("output") == "income" for i in items):
        todos.append({
            "key": "income", "page": _FIX_PAGE["income"],
            "title": "Review money coming in",
            "detail": "Some incoming payments haven't been classified, so they may be missing as income.",
        })

    older = sorted(
        {name(i.get("account_id")) for i in items
         if i.get("output") == "cash" and cutoff and str(i.get("end", "")) <= cutoff}
        | {str(i.get("account_name") or name(i.get("account_id"))) for i in items
           if i.get("output") == "baseline" and i.get("task") == "import-missing"}
    )
    if older:
        todos.append({
            "key": "statement-gaps", "page": _FIX_PAGE["imports"],
            "title": "Fill gaps in older statements",
            "detail": "Some months are missing, so the starting balance and your usual spending "
                      f"can't be fully checked: {', '.join(older)}.",
        })

    observed = sorted({str(i.get("account_name") or name(i.get("account_id"))) for i in items
                       if i.get("output") == "baseline" and i.get("task") == "confirm-export-period"})
    if observed:
        todos.append({
            "key": "export-period", "page": _FIX_PAGE["imports"],
            "title": "Import full statements for these accounts",
            "detail": "Only individual transactions are imported for them, so their months can't be "
                      f"confirmed complete: {', '.join(observed)}.",
        })

    if any(i.get("output") == "reserve" for i in items):
        todos.append({
            "key": "essential", "page": _FIX_PAGE["essential"],
            "title": "Mark which spending is essential",
            "detail": "Your safety reserve is worked out from essential spending. Until that's marked, "
                      "the answer can't tell you whether you dip into the reserve.",
        })

    # Only a missing rate is fixed on a page; any other reason a pocket is
    # left out is named beside the answer instead.
    for pocket in pockets:
        if pocket["counted"] == COUNTED_NOT and pocket["why"] == _NO_RATE:
            currency, balance = pocket["currency"], pocket["starting_balance"]
            todos.append({
                "key": f"pocket-{currency}", "page": _FIX_PAGE["imports"],
                "title": f"Add an exchange rate for {currency}",
                "detail": f"Your {currency} balance{f' ({balance})' if balance is not None else ''} "
                          "isn't counted until it can be converted.",
            })
    return todos


def _gaps(timeline: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Money the path leaves out, and evidence it can't vouch for, that no page
    can fix yet, in the reader's words."""
    items = list(timeline.get("action_items") or [])
    gaps = [
        f"{i.get('label')}: its remaining due dates aren't known, so it isn't counted."
        for i in items
        if i.get("output") == "obligation_timing" and i.get("origin") == "installment_plan"
    ]
    gaps.extend(
        f"{i.get('label')}: it has no due date, so it isn't counted."
        for i in items
        if i.get("output") == "obligation_timing"
        and i.get("origin") not in ("personal_debt", "installment_plan", "card_statement")
    )
    gaps.extend(timeline.get("answer_gaps") or [])
    caveats = [
        f"{i.get('detail')}, so your usual spending may be off."
        for i in items
        if i.get("output") == "baseline" and i.get("task") == "resolve-reconciliation"
    ]
    return gaps, caveats


def _not_in_the_path(
    timeline: dict[str, Any], owed: list[dict[str, Any]], rates: dict[str, Fraction | None], *,
    lowest: Fraction | None, scale: int,
) -> dict[str, Any]:
    """Money owed that the path does not pay, because nothing says when.

    A debt with no due date is not in any day of the forecast, so a "yes" can
    sit beside a debt larger than everything left at the end. This states
    that total and what repaying it now would do to the lowest point; it never
    picks a date for it.
    """
    undated = {str(i.get("obligation_id")) for i in timeline.get("action_items") or []
               if i.get("output") == "obligation_timing" and i.get("obligation_id")}
    items, total, unconverted = [], Fraction(0), []
    for obligation in owed:
        if obligation.get("obligation_id") not in undated or obligation.get("direction") != "outflow":
            continue
        amount = obligation.get("remaining_amount")
        if amount is None or _fraction(amount["value"]) <= 0:
            continue
        rate = rates.get(str(amount["currency"]))
        if rate is None:
            unconverted.append({"label": obligation["label"], **amount})
            continue
        value = _fraction(amount["value"]) * rate
        total += value
        items.append({"label": obligation["label"], "origin": obligation["origin"],
                      "amount": _quantized(value, scale), "native": amount})
    items.sort(key=lambda item: -_fraction(item["amount"]))
    short_now = None if lowest is None or not items or total <= lowest else total - lowest
    return {
        "total": _quantized(total, scale),
        "items": items,
        "unconverted": unconverted,
        # Repaying it all on day one lowers every day by the same amount; None
        # when the lowest point would stay at or above zero.
        "short_if_repaid_now": None if short_now is None else _quantized(short_now, scale),
    }


def combined_answer(
    timeline: dict[str, Any], *, rates: dict[str, Fraction | None], reporting_currency: str,
    scale: int, account_names: dict[str, str] | None = None, owed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One answer across every pocket, in the reporting currency."""
    by_currency: dict[str, Any] = timeline.get("by_currency") or {}
    cutoff = (timeline.get("horizon") or {}).get("supported_cash_cutoff")
    pockets: list[dict[str, Any]] = []
    counted: dict[str, dict[str, Any]] = {}
    held: dict[str, Fraction] = {}
    for currency in sorted(by_currency):
        cell = by_currency[currency]
        rate = rates.get(currency)
        how, why = _counting(cell, rate)
        balance = cell.get("starting_balance")
        pockets.append({
            "currency": currency,
            "counted": how,
            "why": why,
            "missing": [
                {"kind": item["kind"], "label": item["label"], "reason": item["reason"]}
                for item in cell.get("missing_outflows", [])
            ],
            "starting_balance": balance,
            "starting_balance_reported": (
                _quantized(_fraction(balance) * rate, scale)
                if balance is not None and rate is not None else None
            ),
            "rate": str(rate) if rate is not None and currency != reporting_currency else None,
            "rate_display": (
                _quantized(rate, 4) if rate is not None and currency != reporting_currency else None
            ),
        })
        if how not in (COUNTED_NOT, COUNTED_IGNORED) and rate is not None:
            counted[currency] = cell
            held[currency] = rate

    # The reserve is only stated when every projected pocket has one: half a
    # reserve would read as a smaller, easier target.
    projected = [c for c, cell in counted.items() if cell.get("available")]
    targets = [(c, (counted[c].get("reserve_readiness") or {})) for c in projected]
    reserve: Fraction | None = None
    if projected and all(t.get("available") and t.get("target") is not None for _, t in targets):
        reserve = sum(
            (_fraction(t["target"]) * held[c] for c, t in targets),
            start=Fraction(0),
        )

    base = _combined_case(_combined_path(counted, held, name="base"), reserve=reserve, scale=scale)
    cautious = _combined_case(
        _combined_path(counted, held, name="conservative"), reserve=reserve, scale=scale
    )
    gaps, caveats = _gaps(timeline)
    missing_money = [
        f"{item['label']} ({pocket['currency']}): {item['reason']}."
        for pocket in pockets if pocket["currency"] in counted
        for item in pocket["missing"]
    ] + gaps
    incomplete_because = missing_money + caveats
    complete = (
        bool(counted)
        and all(p["counted"] != COUNTED_NOT for p in pockets)
        and not incomplete_because
    )
    answer = _ANSWER[base["verdict"]]
    if base["verdict"] == VERDICT_ENOUGH and missing_money:
        # Bills the path never pays can't stand behind a plain yes.
        answer = "Yes, but not every bill is counted"
    first = next(iter(counted.values()), {})
    where_it_goes = _where_it_goes(counted, held, scale=scale)
    return {
        "reporting_currency": reporting_currency,
        "answer": answer,
        "verdict": base["verdict"],
        "complete": complete,
        "incomplete_because": incomplete_because,
        "starts_on": cutoff,
        "ends_on": ((first.get("base") or {}).get("horizon") or {}).get("end")
        or (timeline.get("horizon") or {}).get("end"),
        "starting_total": (
            _quantized(sum((_fraction(p["starting_balance_reported"])
                            for p in pockets if p["currency"] in counted), start=Fraction(0)), scale)
            if counted else None
        ),
        "base": base,
        "cautious": cautious,
        "only_safe_in_the_base_case": (
            base["verdict"] == VERDICT_ENOUGH
            and cautious["verdict"] in (VERDICT_SHORT, VERDICT_BELOW_RESERVE)
        ),
        "reserve": (
            {"available": True, "target": _quantized(reserve, scale)}
            if reserve is not None else {"available": False, "target": None}
        ),
        "pockets": pockets,
        "income": _income(counted, held, scale=scale),
        # Possible inflows: named so they are not forgotten, never counted.
        "upside": [
            {"label": event["label"], "currency": currency, "amount": event["amount"],
             "on": event["timing"].get("on") or event["timing"].get("latest")}
            for currency, cell in by_currency.items()
            for event in (cell.get("possible_income_upside") or {}).get("events", [])
        ],
        "where_it_goes": where_it_goes,
        # The monthly figure the page heads that list with; summed here so the
        # page never adds money itself.
        "monthly_out": _quantized(
            sum((_fraction(row["total"]) for row in where_it_goes), start=Fraction(0)) / HORIZON_MONTHS,
            scale,
        ),
        "todos": _todos(timeline, account_names=account_names or {}, cutoff=cutoff, pockets=pockets),
        "not_in_the_path": {"as_of": cutoff, **_not_in_the_path(
            timeline, owed or [], {c: rates.get(c) for c in rates},
            lowest=_fraction(base["lowest"]) if base.get("lowest") is not None else None, scale=scale,
        )},
    }


def _forecast_rates(
    connection: sqlite3.Connection, currencies: list[str], *, reporting_currency: str, today: date
) -> dict[str, Fraction | None]:
    """One held forecast rate per pocket — the same rate the Forecast tab uses."""
    context = ValuationContext(
        reporting_currency=reporting_currency, purpose=ValuationPurpose.FORECAST,
        valuation_as_of=today, rate_selection_policy=FORWARD_MONTHLY_CLOSED_V1,
    )
    rates: dict[str, Fraction | None] = {}
    for currency in currencies:
        valued = value_one(
            connection,
            ValuationItem(native=ExactMoney.parse("1"), native_currency=currency,
                          purpose=ValuationPurpose.FORECAST),
            context=context,
        )
        rates[currency] = valued.reported_exact if valued.unavailable is None else None
    return rates


def sufficiency_page_from(
    connection: sqlite3.Connection, *, today: date
) -> dict[str, Any]:
    with request_cache(connection):
        return _sufficiency_page(connection, today=today)


def _sufficiency_page(connection: sqlite3.Connection, *, today: date) -> dict[str, Any]:
    timeline = cash_timeline_from(connection, today=today)
    policy = reserve_policy(connection)
    reporting_currency = reporting_policy(connection).currency
    definition = CURRENCY_REGISTRY.get(reporting_currency)
    rates = _forecast_rates(
        connection, sorted(timeline.get("by_currency") or {}),
        reporting_currency=reporting_currency, today=today,
    )
    account_names = {
        str(row[0]): str(row[1] or row[0])
        for row in connection.execute("SELECT id, display_name FROM accounts")
    }
    cutoff = (timeline.get("horizon") or {}).get("supported_cash_cutoff")
    owed = obligations_page_from(connection, as_of=str(cutoff) if cutoff else None, today=today)["obligations"]
    owed_rates = dict(rates)
    for currency in {str((o.get("remaining_amount") or {}).get("currency")) for o in owed if o.get("remaining_amount")}:
        if currency not in owed_rates:
            owed_rates.update(_forecast_rates(connection, [currency], reporting_currency=reporting_currency, today=today))
    combined = combined_answer(
        timeline, rates=owed_rates, reporting_currency=reporting_currency,
        scale=definition.minor_unit_scale if definition else 2, account_names=account_names, owed=owed,
    )
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
        "combined_answer": combined,
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
