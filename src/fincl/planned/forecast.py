"""Planned flows as forecast events.

One event per open occurrence, identified ``planned:<flow>:<key>``. Certainty
becomes the engine's confidence (sure → confirmed, expected → likely, maybe →
possible), and the engine applies it per case. A day is a known date; a window
is an inferred one, which the cautious case reads as early spending and late
income. A matched payment dated after the cutoff is not in the opening balance,
so it is its own event, ``planned:<flow>:<key>:paid:<day>``, on its own date.

What an outflow replaces is explicit: ``irregular_allowance`` spending replaces
its own amount of that month's irregular allowance, a trip budget replaces the
trip allowance in its own month when the forecast has one, and everything else
is extra.

Spending paid by a card with a settlement plan is handed to the card as a
modelled purchase, so its cash leaves on the card's due date rather than the
day of purchase.

An overdue occurrence keeps its original dates: spending is still emitted and
the engine moves it to the first forecast day; income becomes possible, which
the engine lists as upside and never counts. Either way the owner is asked what
happened.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import TYPE_CHECKING, Any

from ..forecast.engine import (
    COLUMN_INCOME,
    COLUMN_IRREGULAR,
    CONFIDENCE_CONFIRMED,
    CONFIDENCE_LIKELY,
    CONFIDENCE_POSSIBLE,
    DIRECTION_IN,
    DIRECTION_OUT,
    HORIZON_MONTHS,
    TIMING_INFERRED,
    TIMING_KNOWN,
    ForecastEvent,
)
from ..generation import current_generation
from .lifecycle import Placement
from .models import (
    CERTAINTY_EXPECTED,
    CERTAINTY_MAYBE,
    CERTAINTY_SURE,
    DIRECTION_OUTFLOW,
    LINK_TRIP,
    STATE_OVERDUE,
    TREATMENT_IRREGULAR,
)
from .queries import AssessedOccurrence, Horizon, assessed, money
from .trip_spend import TripSpend, trip_spend_to_date

if TYPE_CHECKING:
    from ..forecast.sources import SourceContext, SourceResult

CONFIDENCE = {
    CERTAINTY_SURE: CONFIDENCE_CONFIRMED,
    CERTAINTY_EXPECTED: CONFIDENCE_LIKELY,
    CERTAINTY_MAYBE: CONFIDENCE_POSSIBLE,
}

PLANNED_PAGE = "Planned"


def card_plans(connection: sqlite3.Connection, cutoff: str) -> frozenset[tuple[str, str]]:
    """The (card account, currency) pockets whose bills the forecast can settle."""
    from ..cards.cash_conversion import card_cash_conversion_from

    conversion = card_cash_conversion_from(connection, as_of=cutoff, horizon_months=HORIZON_MONTHS)
    return frozenset(
        (str(card["account_id"]), str(card["currency"]))
        for card in conversion.get("cards", [])
        if (card.get("plan") or {}).get("available") and (card.get("reconciliation") or {}).get("reconciled")
    )


def _card_accounts(connection: sqlite3.Connection) -> frozenset[str]:
    from ..cards.projection import selected_card_accounts

    return frozenset(str(row["id"]) for row in selected_card_accounts(connection))


def _residual(flow: dict[str, Any], context: SourceContext) -> tuple[str | None, str | None]:
    """The residual category an outflow replaces, and a note when a trip has none to replace."""
    if flow["direction"] != DIRECTION_OUTFLOW:
        return None, None
    if flow["link_kind"] == LINK_TRIP:
        trip = f"manifest:trip:{flow['currency']}"
        if trip in context.residual_category_ids:
            return trip, None
        return None, "the forecast has no trip allowance for this pocket, so the trip budget is counted on top"
    if flow["baseline_treatment"] == TREATMENT_IRREGULAR:
        return f"manifest:irregular:{flow['currency']}", None
    return None, None


def _upside(row: AssessedOccurrence, context: SourceContext) -> Placement | None:
    """Income the cases do not count is still emitted, as possible upside."""
    item, assessment = row.occurrence, row.assessment
    if assessment.remaining <= 0 or any(not placement.actual for placement in assessment.placements):
        return None
    if assessment.state == STATE_OVERDUE:
        return Placement(assessment.remaining, item.earliest, item.latest, (), overdue=True)
    if item.latest > context.end:
        return None
    return Placement(assessment.remaining, max(item.earliest, context.start), item.latest, ())


def planned_source(
    connection: sqlite3.Connection, context: SourceContext,
    *, trip_spend: TripSpend = trip_spend_to_date,
) -> SourceResult:
    """Every open planned occurrence inside the horizon, for every reader of the timeline."""
    from ..forecast.sources import SourceResult

    horizon = Horizon(context.cutoff, context.cutoff, context.end)
    flows, _ = assessed(connection, horizon, trip_spend=trip_spend, today=date.fromisoformat(context.today))
    events: list[ForecastEvent] = []
    purchases: list[dict[str, Any]] = []
    action_items: list[dict[str, Any]] = []
    cards = _card_accounts(connection)
    plans: frozenset[tuple[str, str]] | None = None
    for flow, rows, _ in flows:
        outflow = flow["direction"] == DIRECTION_OUTFLOW
        residual, residual_note = _residual(flow, context)
        on_card = outflow and flow["account_id"] in cards and flow["certainty"] != CERTAINTY_MAYBE
        if on_card and plans is None:
            plans = card_plans(connection, context.cutoff)
        card_note = None
        if on_card and (str(flow["account_id"]), str(flow["currency"])) not in (plans or frozenset()):
            on_card = False
            card_note = "the card has no settlement plan the forecast can use, so it is counted on the purchase date"
        for row in rows:
            if row.assessment.state == STATE_OVERDUE and row.assessment.remaining > 0:
                action_items.append(_overdue_item(flow, row))
            upside = None if outflow else _upside(row, context)
            for placement in (*row.assessment.placements, *((upside,) if upside else ())):
                source_id = (
                    f"planned:{flow['flow']}:{row.occurrence.key}:paid:{placement.earliest}" if placement.actual
                    else f"planned:{flow['flow']}:{row.occurrence.key}"
                )
                if source_id in context.existing_source_ids:
                    continue
                late = placement.overdue
                earliest, latest = (
                    (row.occurrence.earliest, row.occurrence.latest) if late
                    else (placement.earliest, placement.latest)
                )
                amount = money(placement.amount)
                replaces = residual if not late else None
                provenance = {
                    "origin": "planned" if outflow else "planned_income",
                    "flow": flow["flow"],
                    "occurrence": row.occurrence.key,
                    "certainty": flow["certainty"],
                    "baseline_treatment": flow["baseline_treatment"],
                    "link": {"kind": flow["link_kind"], "id": flow["link_id"]} if flow["link_kind"] else None,
                    "planned": money(row.occurrence.planned),
                    "matched": money(row.assessment.matched),
                    "overdue": late,
                    "paid": placement.actual,
                    "notes": [note for note in (residual_note, card_note) if note],
                }
                if on_card and not late:
                    purchases.append({
                        "id": source_id, "account_id": flow["account_id"], "currency": flow["currency"],
                        "on": earliest, "amount": amount, "label": str(flow["name"]),
                        "residual_category": replaces, "provenance": provenance,
                    })
                    continue
                confidence = (
                    CONFIDENCE_CONFIRMED if placement.actual
                    else CONFIDENCE_POSSIBLE if not placement.cases
                    else CONFIDENCE[str(flow["certainty"])]
                )
                events.append(_event(flow, source_id, earliest, latest, amount, confidence, replaces, provenance))
    generation = current_generation(connection)
    token = str(generation["generation_token"]) if generation else None
    return SourceResult(
        events=events,
        action_items=action_items,
        generation=token,
        manifest_source={
            "input": "planned_flows",
            "module": "fincl.planned.forecast.planned_source",
            "generation_pinned": True,
            "reported_generation": token,
            "matches_resolved_generation": True,
            "available": True,
            "reason": None,
            "consumed_by": ["known_irregular_costs", "income", "debt_and_card_payments"],
        },
        prospective_card_purchases=purchases,
    )


def _event(
    flow: dict[str, Any], source_id: str, earliest: str, latest: str, amount: str,
    confidence: str, replaces: str | None, provenance: dict[str, Any],
) -> ForecastEvent:
    outflow = flow["direction"] == DIRECTION_OUTFLOW
    return ForecastEvent(
        event_id=source_id,
        source_id=source_id,
        currency=str(flow["currency"]),
        amount=amount,
        column=COLUMN_IRREGULAR if outflow else COLUMN_INCOME,
        direction=DIRECTION_OUT if outflow else DIRECTION_IN,
        label=str(flow["name"]),
        on=earliest if earliest == latest else None,
        timing_basis=TIMING_KNOWN if earliest == latest else TIMING_INFERRED,
        window_earliest=earliest,
        window_latest=latest,
        confidence=confidence,
        residual_category=replaces,
        residual_on=earliest if replaces else None,
        residual_amount=amount if replaces else None,
        removes_from_residual=replaces is not None,
        provenance=provenance,
    )


def _overdue_item(flow: dict[str, Any], row: AssessedOccurrence) -> dict[str, Any]:
    outflow = flow["direction"] == DIRECTION_OUTFLOW
    amount = money(row.assessment.remaining)
    due = "due" if outflow else "expected"
    return {
        "kind": "needs_review", "output": "planned", "currency": flow["currency"], "rank": 75,
        "action": "Say whether this planned item happened",
        "detail": f"{flow['name']}: {amount} {flow['currency']} {due} by {row.occurrence.latest} is not matched",
        "flow": flow["flow"], "occurrence": row.occurrence.key,
        "todo": {
            "key": "planned-overdue", "page": PLANNED_PAGE,
            "title": "Say whether your planned items happened",
            "detail": (
                f"{flow['name']} was {due} by {row.occurrence.latest}. "
                + ("It is kept in the forecast until you match it or say it didn't happen."
                   if outflow else "It is not counted until it arrives.")
            ),
        },
    }


__all__ = ["CONFIDENCE", "PLANNED_PAGE", "card_plans", "planned_source"]
