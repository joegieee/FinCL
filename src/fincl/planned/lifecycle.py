"""Where one planned occurrence stands, and what of it the forecast still carries.

Pure arithmetic over already-loaded rows, shared by the read model and the
forecast adapter so the page can never describe a different forecast from the
one the answer is built on.

Everything is judged against the *reference day*: the supported cash cutoff
when there is one, since money dated on or before it is already inside the
opening balance. An occurrence whose last possible day is on or before it is
overdue:

* overdue spending stays in the forecast on the first forecast day, in the
  cases its certainty allows, until it is matched or the owner says what
  happened — a bill that silently drops out is how a forecast turns optimistic;
* overdue income leaves the forecast — money that has not arrived is not
  counted — and is listed so the owner can say what happened.

Once matched, done or cancelled, an occurrence places nothing more of its
plan. A matched payment dated after the reference day is still placed, on its
own date: the opening balance does not contain it yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .models import (
    CERTAINTY_EXPECTED,
    CERTAINTY_MAYBE,
    CERTAINTY_SURE,
    DIRECTION_OUTFLOW,
    OUTCOME_CANCELLED,
    OUTCOME_DONE,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_MATCHED,
    STATE_OVERDUE,
    STATE_PARTLY_MATCHED,
    STATE_UPCOMING,
)

CASE_BASE = "base"
CASE_CAUTIOUS = "cautious"
BOTH_CASES = (CASE_BASE, CASE_CAUTIOUS)

# Which forecast cases an occurrence enters, by direction and certainty.
CASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("outflow", CERTAINTY_SURE): BOTH_CASES,
    ("outflow", CERTAINTY_EXPECTED): BOTH_CASES,
    ("outflow", CERTAINTY_MAYBE): (CASE_CAUTIOUS,),
    ("inflow", CERTAINTY_SURE): BOTH_CASES,
    ("inflow", CERTAINTY_EXPECTED): (CASE_BASE,),
    ("inflow", CERTAINTY_MAYBE): (),
}

_ZERO = Decimal(0)


@dataclass(frozen=True)
class Slice:
    """Actual money credited to one occurrence: a match, or part of a trip's spend."""

    on: str
    amount: Decimal


@dataclass(frozen=True)
class Occurrence:
    key: str
    earliest: str
    latest: str
    planned: Decimal
    outcome: str | None = None


@dataclass(frozen=True)
class Placement:
    """One amount the forecast carries for an occurrence."""

    amount: Decimal
    earliest: str
    latest: str
    cases: tuple[str, ...]
    overdue: bool = False
    # A matched payment dated after the reference day, placed on its own date.
    actual: bool = False


@dataclass(frozen=True)
class Assessment:
    state: str
    matched: Decimal
    remaining: Decimal
    overdue: bool
    placements: tuple[Placement, ...]
    note: str | None


def envelope(
    occurrences: Sequence[Occurrence], parts: Iterable[Slice]
) -> tuple[dict[str, list[Slice]], Decimal]:
    """Spread actual spend over the occurrences in date order, each up to its plan.

    A cancelled occurrence takes nothing. What is left once every occurrence is
    full is returned as spend beyond the plan. A refund (a negative part) gives
    back spend beyond the plan first, then the latest filled occurrence.
    """
    slices: dict[str, list[Slice]] = {item.key: [] for item in occurrences}
    keys = [item.key for item in occurrences if item.outcome != OUTCOME_CANCELLED]
    planned = [item.planned for item in occurrences if item.outcome != OUTCOME_CANCELLED]
    room = list(planned)
    index = 0
    over = _ZERO
    for part in sorted(parts, key=lambda part: part.on):
        amount = part.amount
        while amount > 0 and index < len(room):
            taken = min(amount, room[index])
            slices[keys[index]].append(Slice(part.on, taken))
            amount -= taken
            room[index] -= taken
            if room[index] <= 0:
                index += 1
        if amount > 0:
            over += amount
            continue
        back = min(-amount, over)
        over -= back
        back = -amount - back
        position = min(index, len(room) - 1)
        while back > 0 and position >= 0:
            given = min(back, planned[position] - room[position])
            if given > 0:
                slices[keys[position]].append(Slice(part.on, -given))
                room[position] += given
                back -= given
                index = min(index, position)
            position -= 1
    return slices, over


def assess(
    occurrence: Occurrence,
    slices: Sequence[Slice],
    *,
    direction: str,
    certainty: str,
    reference: str,
    horizon_end: str | None,
) -> Assessment:
    """The occurrence's state and where the forecast places what is left of it.

    ``horizon_end`` is ``None`` when there is no supported cash cutoff: the
    state is still reported, but nothing is placed.
    """
    matched = sum((item.amount for item in slices), _ZERO)
    overdue = occurrence.latest <= reference
    outflow = direction == DIRECTION_OUTFLOW
    paid_later: dict[str, Decimal] = {}
    if horizon_end is not None:
        for item in slices:
            if reference < item.on <= horizon_end:
                paid_later[item.on] = paid_later.get(item.on, _ZERO) + item.amount
    # A day whose trip refunds exceed its spend moves no cash out; it is not counted as income either.
    later = tuple(
        Placement(amount, on, on, BOTH_CASES, actual=True) for on, amount in sorted(paid_later.items()) if amount > 0
    )
    later_note = "Paid after your statements end, so that payment is counted on its own date." if later else None

    if occurrence.outcome in (OUTCOME_DONE, OUTCOME_CANCELLED):
        state = STATE_DONE if occurrence.outcome == OUTCOME_DONE else STATE_CANCELLED
        return Assessment(state, matched, _ZERO, False, later, later_note)
    if matched >= occurrence.planned:
        return Assessment(STATE_MATCHED, matched, _ZERO, False, later, later_note)

    remaining = occurrence.planned - matched
    if overdue:
        state = STATE_OVERDUE
    elif matched > 0:
        state = STATE_PARTLY_MATCHED
    else:
        state = STATE_UPCOMING
    if horizon_end is None:
        return Assessment(state, matched, remaining, overdue, (), "There is no supported cash cutoff, so nothing is forecast yet.")

    start = (date.fromisoformat(reference) + timedelta(days=1)).isoformat()
    cases = CASES[(direction, certainty)]
    if overdue:
        if not outflow:
            return Assessment(
                state, matched, remaining, True, later,
                f"Expected by {occurrence.latest} and not received, so it is not counted.",
            )
        return Assessment(
            state, matched, remaining, True,
            (Placement(remaining, start, start, cases, overdue=True), *later),
            f"Due by {occurrence.latest} and not matched, so it is kept on {start} until you say what happened."
            + (" It may not happen, so only the cautious case counts it." if cases == (CASE_CAUTIOUS,) else ""),
        )

    if outflow and occurrence.earliest > horizon_end or not outflow and occurrence.latest > horizon_end:
        return Assessment(state, matched, remaining, False, later, "After the forecast horizon.")
    if not cases:
        return Assessment(state, matched, remaining, False, later, "Possible income is listed as upside and never counted.")
    window = (max(occurrence.earliest, start), min(occurrence.latest, horizon_end))
    note = later_note
    if cases == (CASE_CAUTIOUS,):
        note = "Counted in the cautious case only, because it may not happen."
    elif cases == (CASE_BASE,):
        note = "Counted in the base case only; the cautious case does not rely on expected income."
    return Assessment(state, matched, remaining, False, (Placement(remaining, *window, cases), *later), note)


__all__ = [
    "BOTH_CASES",
    "CASES",
    "CASE_BASE",
    "CASE_CAUTIOUS",
    "Assessment",
    "Occurrence",
    "Placement",
    "Slice",
    "assess",
    "envelope",
]
