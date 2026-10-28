"""Planned flows as forecast events.

One event per open occurrence, identified ``planned:<flow>:<key>``. Certainty
becomes the engine's confidence (sure → confirmed, expected → likely, maybe →
possible), and the engine applies it per case. A day is a known date; a window
is an inferred one, which the cautious case reads as early spending and late
income.

Spending marked ``irregular_allowance`` replaces its own amount of that
month's irregular allowance rather than adding to it; everything else is extra.

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
    TIMING_INFERRED,
    TIMING_KNOWN,
    ForecastEvent,
)
from ..generation import current_generation
from .models import (
    CERTAINTY_EXPECTED,
    CERTAINTY_SURE,
    DIRECTION_OUTFLOW,
    STATE_OVERDUE,
    TREATMENT_IRREGULAR,
)
from .queries import Horizon, assessed, money
from .trip_spend import TripSpend, trip_spend_to_date

if TYPE_CHECKING:
    from .sources import SourceContext, SourceResult

CONFIDENCE = {
    CERTAINTY_SURE: CONFIDENCE_CONFIRMED,
    CERTAINTY_EXPECTED: CONFIDENCE_LIKELY,
}

PLANNED_PAGE = "Planned"


def planned_source(
    connection: sqlite3.Connection, context: SourceContext,
    *, trip_spend: TripSpend = trip_spend_to_date,
) -> SourceResult:
    """Every open planned occurrence inside the horizon, for every reader of the timeline."""
    from .sources import SourceResult

    horizon = Horizon(context.cutoff, context.cutoff, context.end)
    flows, _ = assessed(connection, horizon, trip_spend=trip_spend, today=date.fromisoformat(context.today))
    events: list[ForecastEvent] = []
    action_items: list[dict[str, Any]] = []
    for flow, rows, _ in flows:
        outflow = flow["direction"] == DIRECTION_OUTFLOW
        for row in rows:
            item, assessment = row.occurrence, row.assessment
            if assessment.remaining <= 0:
                continue
            overdue = assessment.state == STATE_OVERDUE
            if not overdue and (
                outflow and item.earliest > context.end or not outflow and item.latest > context.end
            ):
                continue
            source_id = f"planned:{flow['flow']}:{item.key}"
            if source_id in context.existing_source_ids:
                continue
            earliest, latest = item.earliest, item.latest
            if not overdue:
                earliest, latest = max(earliest, context.start), min(latest, context.end)
            confidence = (
                CONFIDENCE_POSSIBLE if overdue and not outflow
                else CONFIDENCE_CONFIRMED if overdue
                else CONFIDENCE.get(str(flow["certainty"]), CONFIDENCE_POSSIBLE)
            )
            irregular = outflow and flow["baseline_treatment"] == TREATMENT_IRREGULAR and not overdue
            amount = money(assessment.remaining)
            events.append(ForecastEvent(
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
                residual_category=f"manifest:irregular:{flow['currency']}" if irregular else None,
                residual_on=earliest if irregular else None,
                residual_amount=amount if irregular else None,
                removes_from_residual=irregular,
                provenance={
                    "origin": "planned" if outflow else "planned_income",
                    "flow": flow["flow"],
                    "occurrence": item.key,
                    "certainty": flow["certainty"],
                    "baseline_treatment": flow["baseline_treatment"],
                    "link": {"kind": flow["link_kind"], "id": flow["link_id"]} if flow["link_kind"] else None,
                    "planned": money(item.planned),
                    "matched": money(assessment.matched),
                    "overdue": overdue,
                },
            ))
            if overdue:
                action_items.append({
                    "kind": "needs_review", "output": "planned", "currency": flow["currency"], "rank": 75,
                    "action": "Say whether this planned item happened",
                    "detail": (
                        f"{flow['name']}: {amount} {flow['currency']} "
                        f"{'due' if outflow else 'expected'} by {item.latest} is not matched"
                    ),
                    "flow": flow["flow"], "occurrence": item.key,
                    "todo": {
                        "key": "planned-overdue", "page": PLANNED_PAGE,
                        "title": "Say whether your planned items happened",
                        "detail": (
                            f"{flow['name']} was {'due' if outflow else 'expected'} by {item.latest}. "
                            + ("It is kept in the forecast until you match it or say it didn't happen."
                               if outflow else "It is not counted until it arrives.")
                        ),
                    },
                })
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
            "consumed_by": ["known_irregular_costs", "income"],
        },
    )


__all__ = ["CONFIDENCE", "PLANNED_PAGE", "planned_source"]
