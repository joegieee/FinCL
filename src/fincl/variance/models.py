"""Stage 5A vocabulary, the snapshot document contract and the variance arithmetic.

Three things live here and nothing else: the journalled decision kinds, the
canonical form of a forecast snapshot, and the pure arithmetic that turns an
expected and an actual figure into a reading a person can act on.  All of it is
database-free and forecast-free — in particular this module deliberately does
**not** import :mod:`fincl.forecast.engine`, because ``fincl.decisions`` imports
this module eagerly to close its kind vocabulary and the engine reaches back to
``fincl.decisions`` through the generation stamp.  The flow-class vocabulary is
therefore restated here and pinned to the engine's columns by a test rather than
by an import.

Why a snapshot is a journal record
----------------------------------
A forecast that can be recomputed is not a forecast that was *issued*.  The
question "was the plan right" is only answerable against the numbers the plan
actually stated at the time, so a snapshot is appended to the hash-chained
decision journal like any other originating decision, and the projection is a
verbatim copy of that record's document.  Immutability is therefore structural,
not a convention: nothing recomputes into a snapshot row, the digest below is
recorded next to the document, and re-issuing a snapshot id is refused rather
than merged.

The sign rule
-------------
``actual - expected`` is computed on *magnitudes*, so the same positive number
means opposite things in the two directions: more income than planned is cash
gained, more spending than planned is cash lost.  Reporting the bare difference
without saying which is how a variance table ends up congratulating someone for
overspending, so :func:`variance_reading` always states the cash effect and the
direction alongside the difference.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..money import format_decimal

FORECAST_SNAPSHOT_ISSUE = "FORECAST_SNAPSHOT_ISSUE"
FORECAST_MATCH_SET = "FORECAST_MATCH_SET"
FORECAST_MATCH_RETIRE = "FORECAST_MATCH_RETIRE"

VARIANCE_DECISION_KINDS = frozenset(
    {
        FORECAST_SNAPSHOT_ISSUE,
        FORECAST_MATCH_SET,
        FORECAST_MATCH_RETIRE,
    }
)

SNAPSHOT_CONTRACT_VERSION = "forecast-snapshot-v1"
VARIANCE_VERSION = "forecast-variance-v2"

# The Stage 3B timeline columns, restated (see the module docstring). A flow
# class is never netted into a neighbour: a month whose lifestyle held and whose
# card settlement doubled is not its own mirror image.
FLOW_INCOME = "income"
FLOW_LIFESTYLE = "lifestyle_costs"
FLOW_DEBT_CARD = "debt_and_card_payments"
FLOW_IRREGULAR = "known_irregular_costs"
FLOW_ALLOCATION = "future_allocations"
EXPENSE_FLOW_CLASSES = (
    FLOW_LIFESTYLE,
    FLOW_DEBT_CARD,
    FLOW_IRREGULAR,
    FLOW_ALLOCATION,
)
FLOW_CLASSES = (FLOW_INCOME, *EXPENSE_FLOW_CLASSES)

DIRECTION_IN = "inflow"
DIRECTION_OUT = "outflow"

# What became of one issued cash event. `unresolved` and `awaited` are first
# class: an event nobody can yet judge must never be scored as a zero actual.
EVENT_MATCHED = "matched"
EVENT_PARTIALLY_MATCHED = "partially_matched"
EVENT_MISSING = "missing"
EVENT_AWAITED = "awaited"
EVENT_UNRESOLVED = "unresolved"
EVENT_MODELLED = "modelled"
EVENT_STATUSES = (
    EVENT_MATCHED,
    EVENT_PARTIALLY_MATCHED,
    EVENT_MISSING,
    EVENT_AWAITED,
    EVENT_UNRESOLVED,
    EVENT_MODELLED,
)

# The engine spreads unscheduled spending into daily slices under this id prefix.
# Restated like the flow classes above, and pinned to the engine by a test.
RESIDUAL_EVENT_PREFIX = "residual:"

# What became of one actual cash movement.
MOVEMENT_MATCHED = "matched"
MOVEMENT_PARTIALLY_MATCHED = "partially_matched"
MOVEMENT_UNEXPECTED = "unexpected"

MATCH_FULL = "full"
MATCH_PARTIAL = "partial"
MATCH_KINDS = (MATCH_FULL, MATCH_PARTIAL)

# A month is only scored when the evidence covers all of it and the horizon has
# actually reached it. Anything else reads as unresolved.
PERIOD_SCORED = "scored"
PERIOD_UNRESOLVED_COVERAGE = "unresolved_incomplete_coverage"
PERIOD_UNRESOLVED_FUTURE = "unresolved_not_yet_elapsed"
PERIOD_STATUSES = (PERIOD_SCORED, PERIOD_UNRESOLVED_COVERAGE, PERIOD_UNRESOLVED_FUTURE)

NEVER_ZERO_RULE = (
    "an ambiguous or uncovered period reports a null actual and an explicit "
    "unresolved status; it is never reported as a measured zero, because a "
    "month nobody has evidence for is not a month in which nothing happened"
)

CASH_BASIS_RULE = (
    "actuals are bank cash receipts and payments on the day the money moved, "
    "never economic purchase recognition: a card purchase moved a liability and "
    "is not cash, and the bank payment that later settles that card is the cash "
    "movement the forecast modelled"
)

MANUAL_MATCH_RULE = (
    "matches are recorded by the operator and journalled; nothing here infers a "
    "match from proximity of amount or date, so an unmatched pair stays visible "
    "as two unresolved facts rather than becoming one confident wrong one"
)

DISCOUNT_RULE = (
    "a suggested Likely-income discount is presented with the observations that "
    "produced it and its sample count, and is never applied to any forecast; "
    "adopting it is a separate, explicit decision"
)


def canonical_document(value: object) -> str:
    """Compact key-sorted JSON — the exact bytes a snapshot digest covers."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_digest(document: object) -> str:
    """``sha256:`` over :func:`canonical_document`.

    Stored beside the document so a later read can prove the snapshot it is
    reporting against is the one that was issued, byte for byte.
    """
    return "sha256:" + hashlib.sha256(canonical_document(document).encode()).hexdigest()


def flow_direction(flow_class: str) -> str:
    if flow_class == FLOW_INCOME:
        return DIRECTION_IN
    if flow_class in EXPENSE_FLOW_CLASSES:
        return DIRECTION_OUT
    raise ValueError(f"unknown flow class: {flow_class!r}")


def variance_reading(
    *,
    flow_class: str,
    expected_minor: int | None,
    actual_minor: int | None,
    scale: int,
    unresolved_reason: str | None = None,
    open_minor: int = 0,
    modelled_minor: int = 0,
) -> dict[str, Any]:
    """One month/flow-class cell, with the sign explained rather than implied.

    ``actual_minor`` of ``None`` is the unresolved case and produces a null
    difference and an explanation — never a difference computed against zero.

    ``open_minor`` is the part of ``expected_minor`` whose events nobody can yet
    judge. It stays in the issued figure but is held out of the difference: the
    cash may still move, so it is not a shortfall against this class.
    ``modelled_minor`` is held out the same way: a modelled allowance is scored
    on the month's total, not class by class.
    """
    if flow_class not in FLOW_CLASSES:
        raise ValueError(f"unknown flow class: {flow_class!r}")
    if expected_minor is not None and expected_minor < 0:
        raise ValueError("an expected flow is a magnitude, never a sign")
    if actual_minor is not None and actual_minor < 0:
        raise ValueError("an actual flow is a magnitude, never a sign")
    direction = flow_direction(flow_class)
    if actual_minor is None or expected_minor is None:
        return {
            "flow_class": flow_class,
            "direction": direction,
            "expected": (
                None if expected_minor is None else format_decimal(expected_minor, scale)
            ),
            "actual": None if actual_minor is None else format_decimal(actual_minor, scale),
            "open_expected": format_decimal(open_minor, scale),
            "modelled_expected": format_decimal(modelled_minor, scale),
            "actual_minus_expected": None,
            "cash_effect": None,
            "favourable": None,
            "resolved": False,
            "explanation": unresolved_reason or NEVER_ZERO_RULE,
        }
    difference = actual_minor - (expected_minor - open_minor - modelled_minor)
    # The same difference, read as cash: more income helps, more spending hurts.
    cash_effect = difference if direction == DIRECTION_IN else -difference
    if difference == 0:
        explanation = "the actual cash matched the issued forecast for this class"
    elif direction == DIRECTION_IN:
        explanation = (
            f"{format_decimal(abs(difference), scale)} "
            + ("more" if difference > 0 else "less")
            + " income arrived than the forecast issued; "
            + ("cash is ahead" if difference > 0 else "cash is behind")
        )
    else:
        explanation = (
            f"{format_decimal(abs(difference), scale)} "
            + ("more" if difference > 0 else "less")
            + " left than the forecast issued; "
            + ("cash is behind" if difference > 0 else "cash is ahead")
        )
    if open_minor:
        explanation += (
            f"; {format_decimal(open_minor, scale)} of the issued amount is still "
            "open and is not scored"
        )
    if modelled_minor:
        explanation += (
            f"; {format_decimal(modelled_minor, scale)} is a modelled allowance, "
            "scored on the month's total"
        )
    return {
        "flow_class": flow_class,
        "direction": direction,
        "expected": format_decimal(expected_minor, scale),
        "actual": format_decimal(actual_minor, scale),
        "open_expected": format_decimal(open_minor, scale),
        "modelled_expected": format_decimal(modelled_minor, scale),
        "actual_minus_expected": format_decimal(difference, scale),
        "cash_effect": format_decimal(cash_effect, scale),
        "favourable": cash_effect > 0 if cash_effect != 0 else None,
        "resolved": True,
        "explanation": explanation,
    }


def realization(
    *, realized: int, resolved: int, realized_minor: int, expected_minor: int, scale: int
) -> dict[str, Any]:
    """Likely-income realization and the discount it would imply, with its sample.

    ``resolved`` counts only the occurrences an answer exists for: an occurrence
    still inside its window, or one falling in an uncovered period, is excluded
    from both numerator and denominator rather than counted as a failure.  With
    no resolved occurrence there is no rate — a discount from a sample of zero
    is a guess wearing a percentage.
    """
    if realized < 0 or resolved < 0:
        raise ValueError("occurrence counts are never negative")
    if realized > resolved:
        raise ValueError("more occurrences were realized than were resolved")
    if realized_minor < 0 or expected_minor < 0:
        raise ValueError("realization amounts are magnitudes, never signs")
    if resolved == 0:
        return {
            "available": False,
            "reason": (
                "no Likely-income occurrence has both fully elapsed and fallen "
                "inside covered evidence, so no realization rate is stated"
            ),
            "sample_count": 0,
            "realized_count": realized,
            "occurrence_rate": None,
            "amount_rate": None,
            "suggested_discount": None,
            "auto_applied": False,
            "policy": DISCOUNT_RULE,
        }
    occurrence_rate = f"{realized}/{resolved}"
    shortfall = max(expected_minor - realized_minor, 0)
    return {
        "available": True,
        "reason": None,
        "sample_count": resolved,
        "realized_count": realized,
        "occurrence_rate": occurrence_rate,
        "expected_amount": format_decimal(expected_minor, scale),
        "realized_amount": format_decimal(realized_minor, scale),
        "amount_rate": (
            None if expected_minor == 0 else f"{realized_minor}/{expected_minor}"
        ),
        "suggested_discount": {
            "amount": format_decimal(shortfall, scale),
            "of_expected": (
                None if expected_minor == 0 else f"{shortfall}/{expected_minor}"
            ),
            "basis": (
                f"the issued Likely income for {resolved} resolved occurrence(s) "
                f"less what actually arrived against them"
            ),
        },
        "auto_applied": False,
        "policy": DISCOUNT_RULE,
    }


__all__ = [
    "CASH_BASIS_RULE",
    "DIRECTION_IN",
    "DIRECTION_OUT",
    "DISCOUNT_RULE",
    "EVENT_AWAITED",
    "EVENT_MATCHED",
    "EVENT_MISSING",
    "EVENT_MODELLED",
    "EVENT_PARTIALLY_MATCHED",
    "EVENT_STATUSES",
    "EVENT_UNRESOLVED",
    "EXPENSE_FLOW_CLASSES",
    "FLOW_ALLOCATION",
    "FLOW_CLASSES",
    "FLOW_DEBT_CARD",
    "FLOW_INCOME",
    "FLOW_IRREGULAR",
    "FLOW_LIFESTYLE",
    "FORECAST_MATCH_RETIRE",
    "FORECAST_MATCH_SET",
    "FORECAST_SNAPSHOT_ISSUE",
    "MANUAL_MATCH_RULE",
    "MATCH_FULL",
    "MATCH_KINDS",
    "MATCH_PARTIAL",
    "MOVEMENT_MATCHED",
    "MOVEMENT_PARTIALLY_MATCHED",
    "MOVEMENT_UNEXPECTED",
    "NEVER_ZERO_RULE",
    "PERIOD_SCORED",
    "PERIOD_STATUSES",
    "PERIOD_UNRESOLVED_COVERAGE",
    "PERIOD_UNRESOLVED_FUTURE",
    "RESIDUAL_EVENT_PREFIX",
    "SNAPSHOT_CONTRACT_VERSION",
    "VARIANCE_DECISION_KINDS",
    "VARIANCE_VERSION",
    "canonical_document",
    "flow_direction",
    "realization",
    "snapshot_digest",
    "variance_reading",
]
