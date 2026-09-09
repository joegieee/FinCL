"""Link a fund's sourced due expense into the Stage 3B cash timeline.

Two things must be true at once and they pull in opposite directions.  The
expense a fund is saving for is a real future outflow and belongs on the
timeline.  But it is very likely *already* there — as the obligation the fund
was proposed from, or inside the irregular allowance the estimator derived from
the historical expense itself.  Counting it twice would show a month spending
money it will only spend once.

So identity does the work.  A fund names the future event it settles: either
``linked_event_id``, the canonical ``source_id`` of an event some other adapter
already emits, or — when no such event exists — ``fund:<fund>:due``, which the
fund creates itself.  A fund whose linked event is already on the timeline
contributes no event at all, and a fund that creates one removes its own amount
from the **irregular** allowance only, on its own due date.  The routine
residual is untouched, because a once-a-year insurance bill was never part of
the weekly groceries.

Contributions deliberately produce nothing here.  A contribution moves money
between two pockets of the same liquid cash; it changes no balance on any day.
That is why a fund's schedule cannot rescue a cash path that runs out — the
arithmetic has no way to express "saving" as new money, which is the correct
answer rather than a limitation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..forecast.engine import (
    COLUMN_IRREGULAR,
    CONFIDENCE_CONFIRMED,
    DIRECTION_OUT,
    TIMING_KNOWN,
    ForecastEvent,
)

CONTRIBUTIONS_ARE_NOT_CASH_EVENTS = (
    "a planned or recorded contribution is a movement inside liquid cash and "
    "emits no timeline event; scheduling contributions cannot improve any "
    "day's balance, so it can never turn an insolvent cash path solvent"
)

IRREGULAR_REPLACEMENT_RULE = (
    "a fund's dated expense replaces its own matching irregular allowance on "
    "its due date and nothing else; the routine residual and every other "
    "category keep their full portion"
)


def fund_due_source_id(fund: dict[str, Any]) -> str:
    """The canonical identity of the expense this fund settles.

    A fund that names an existing future event borrows that event's identity,
    which is what makes the duplicate impossible rather than merely unlikely.
    """
    linked = fund.get("linked_event_id")
    return str(linked) if linked else f"fund:{fund['fund']}:due"


def fund_forecast_events(
    page: dict[str, Any],
    *,
    existing_source_ids: Iterable[str] = (),
    start: str | None = None,
    end: str | None = None,
) -> tuple[list[ForecastEvent], list[str]]:
    """Timeline events for the funds whose expense is not already modelled.

    ``existing_source_ids`` are the canonical identifiers the other Stage 3B
    adapters already emit.  A fund matching one of them is reported as linked
    and creates nothing.
    """
    existing = set(existing_source_ids)
    events: list[ForecastEvent] = []
    notes: list[str] = []
    emitted: set[str] = set()
    for fund in page.get("funds", []):
        target = fund.get("target", {})
        amount = target.get("amount_due")
        due_on = target.get("due_on")
        if fund.get("status") == "released":
            notes.append(
                f"{fund['fund']} has released its earmark; its expense is settled "
                "and is not modelled again"
            )
            continue
        if amount is None or due_on is None:
            notes.append(
                f"{fund['fund']} has no sourced amount or due date; it is not "
                "given a guessed date and its matching irregular allowance is retained"
            )
            continue
        if start is not None and not start < str(due_on):
            continue
        if end is not None and str(due_on) > end:
            continue
        source_id = fund_due_source_id(fund)
        if source_id in existing:
            notes.append(
                f"{fund['fund']} settles the existing future event {source_id}; "
                "no duplicate event is created"
            )
            continue
        if source_id in emitted:
            notes.append(
                f"{fund['fund']} repeats the canonical identity {source_id}; the "
                "second claim is withheld rather than double-settled"
            )
            continue
        emitted.add(source_id)
        events.append(
            ForecastEvent(
                event_id=f"fund:{fund['fund']}:due",
                source_id=source_id,
                currency=str(fund["currency"]),
                amount=str(amount),
                column=COLUMN_IRREGULAR,
                direction=DIRECTION_OUT,
                label=str(fund["name"]),
                on=str(due_on),
                timing_basis=TIMING_KNOWN,
                confidence=CONFIDENCE_CONFIRMED,
                residual_category=f"manifest:irregular:{fund['currency']}",
                residual_on=str(due_on),
                residual_amount=str(amount),
                removes_from_residual=True,
                provenance={
                    "kind": "sinking_fund_due_expense",
                    "fund": fund["fund"],
                    "amount_basis": target.get("amount_basis"),
                    "date_basis": target.get("date_basis"),
                    "source_reference": target.get("source_reference"),
                    "linked_event_id": fund.get("linked_event_id"),
                    "replacement_rule": IRREGULAR_REPLACEMENT_RULE,
                    "contribution_rule": CONTRIBUTIONS_ARE_NOT_CASH_EVENTS,
                },
            )
        )
    return events, notes


__all__ = [
    "CONTRIBUTIONS_ARE_NOT_CASH_EVENTS",
    "IRREGULAR_REPLACEMENT_RULE",
    "fund_due_source_id",
    "fund_forecast_events",
]
