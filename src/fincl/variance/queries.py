"""Stage 5A — the forecast-versus-actual read.

One read holds an *issued* forecast to account against the cash that actually
moved.  Everything it reports is arithmetic over two immutable things — the
snapshot document and the journalled matches — plus the ledger's own cash
movements, so no number here can disagree with the evidence behind it.

Four shapes of honesty carry the report.

*Nothing unresolved becomes a zero.*  A month is scored only when it has fully
elapsed **and** the accepted evidence covers all of it.  Otherwise the cell
carries a null actual and says which of the two is missing.  A month with no
statement is not a month in which no money moved.

*Delay and absence are different answers.*  An unmatched event whose timing
window has not closed is ``awaited`` — the receipt may still arrive.  One whose
window closed inside covered evidence is ``missing`` — it did not.  One matched
to a movement in a later month is ``matched`` with a stated timing shift, which
is what makes a late salary legible as late rather than as a permanent loss.

*A purchase is not its settlement.*  Actuals are bank cash only
(:mod:`fincl.variance.actuals`), so a card purchase cannot be matched to the
card settlement the forecast modelled; the bank payment that clears the
statement can, which is the movement the forecast was talking about.

*An actual is only classified when something classified it.*  A movement with no
recorded match has no flow class, so it is reported as unclassified actual for
that month rather than folded into a class it was never shown to belong to.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..intervals import contains_range
from ..money import ExactMoney, format_decimal
from ..report import _coverage_contract
from .actuals import cash_movements
from .models import (
    CASH_BASIS_RULE,
    DIRECTION_IN,
    EVENT_AWAITED,
    EVENT_MATCHED,
    EVENT_MISSING,
    EVENT_PARTIALLY_MATCHED,
    EVENT_UNRESOLVED,
    FLOW_CLASSES,
    MANUAL_MATCH_RULE,
    MATCH_FULL,
    MATCH_PARTIAL,
    NEVER_ZERO_RULE,
    PERIOD_SCORED,
    PERIOD_UNRESOLVED_COVERAGE,
    PERIOD_UNRESOLVED_FUTURE,
    VARIANCE_VERSION,
    realization,
    variance_reading,
)
from .snapshots import stored_snapshot

CONFIDENCE_LIKELY = "likely"
_BIGGEST_MISSES = 5

TRANSFER_SEMANTICS = (
    "transfers and repayments are matched the way the forecast modelled them: a "
    "card settlement is answered by the bank payment that cleared it, never by "
    "the purchases on the statement; an internal transfer between two accounts "
    "in this currency appears as both of its bank legs, so it nets to zero "
    "across the position exactly as it does in the forecast"
)


def _minor(value: str, scale: int) -> int:
    return ExactMoney.parse(str(value)).rescaled_coefficient(scale)


def _month_of(day: str) -> str:
    return day[:7]


def _live_matches(
    connection: sqlite3.Connection, snapshot: str
) -> list[dict[str, Any]]:
    return [
        {
            "match": str(row["match"]),
            "event_id": str(row["event_id"]),
            "movement_id": str(row["movement_id"]),
            "matched_on": str(row["matched_on"]),
            "amount_coefficient": int(row["amount_coefficient"]),
            "amount_scale": int(row["amount_scale"]),
            "match_kind": str(row["match_kind"]),
        }
        for row in connection.execute(
            "SELECT match, event_id, movement_id, matched_on, amount_coefficient, "
            "amount_scale, match_kind FROM forecast_actual_matches "
            "WHERE snapshot = ? AND retired = 0 ORDER BY matched_on, match",
            (snapshot,),
        )
    ]


def _working_scale(
    document: dict[str, Any],
    matches: list[dict[str, Any]],
    movements: list[dict[str, Any]],
) -> int:
    """A scale every figure can rescale *up* into; rescaling down is lossy."""
    scales = [int(document["scale"])]
    scales.extend(ExactMoney.parse(str(item["amount"])).scale for item in document["events"])
    scales.extend(item["amount_scale"] for item in matches)
    scales.extend(item["amount_scale"] for item in movements)
    return max(scales)


def _period_status(
    *, first_day: str, last_day: str, as_of: str, covered: list[Any]
) -> tuple[str, str | None]:
    if last_day > as_of:
        return (
            PERIOD_UNRESOLVED_FUTURE,
            (
                f"this period runs to {last_day} and only {as_of} has passed; it is "
                "reported unresolved rather than scored against a partial month"
            ),
        )
    if not contains_range(covered, first_day, last_day):
        return (
            PERIOD_UNRESOLVED_COVERAGE,
            (
                f"the accepted evidence does not cover all of {first_day}..{last_day}; "
                "the actual is unknown for this period and is not stated as zero"
            ),
        )
    return PERIOD_SCORED, None


def _event_rows(
    *,
    document: dict[str, Any],
    matches: list[dict[str, Any]],
    movements_by_id: dict[str, dict[str, Any]],
    covered: list[Any],
    as_of: str,
    scale: int,
) -> list[dict[str, Any]]:
    """Resolve every issued event to a status, an actual and any timing shift.

    An event is judged on *its own* window and the evidence over that window,
    not on whether its calendar month happens to be scoreable.  A receipt due on
    the 3rd is answerable on the 4th even though its month has weeks to run, and
    a receipt whose window is still open is awaited even in a month that closed
    — collapsing either into the month's status would call a delay a loss.
    """
    by_event: dict[str, list[dict[str, Any]]] = {}
    for item in matches:
        by_event.setdefault(item["event_id"], []).append(item)

    rows: list[dict[str, Any]] = []
    for event in document["events"]:
        event_id = str(event["event_id"])
        modelled_month = _month_of(str(event["on"]))
        expected = _minor(str(event["amount"]), scale)
        bound = by_event.get(event_id, [])
        actual = sum(
            item["amount_coefficient"] * (10 ** (scale - item["amount_scale"]))
            for item in bound
        )
        closed = any(item["match_kind"] == MATCH_FULL for item in bound)

        landings: list[dict[str, Any]] = []
        for item in bound:
            movement = movements_by_id.get(item["movement_id"])
            landed_on = str(movement["on"]) if movement is not None else item["matched_on"]
            landings.append(
                {
                    "match": item["match"],
                    "movement_id": item["movement_id"],
                    "amount": format_decimal(
                        item["amount_coefficient"] * (10 ** (scale - item["amount_scale"])),
                        scale,
                    ),
                    "landed_on": landed_on,
                    "match_kind": item["match_kind"],
                    "movement_resolved": movement is not None,
                    "movement_note": (
                        None
                        if movement is not None
                        else (
                            "this movement is not in the ledger's covered cash for "
                            "the compared window; the operator's stated match date "
                            "is used and the binding is reported unresolved"
                        )
                    ),
                }
            )

        earliest = str(event["timing"]["earliest"])
        latest = str(event["timing"]["latest"])
        if bound and closed:
            status = EVENT_MATCHED
            reason = "an operator match answers this event in full"
        elif bound:
            status = EVENT_PARTIALLY_MATCHED
            reason = (
                "part of this event is matched; the remainder has not been bound to "
                "any actual movement"
            )
        elif latest > as_of:
            status = EVENT_AWAITED
            reason = (
                f"the issued timing window runs to {latest}, which has not passed as "
                f"of {as_of}; the money may still move and this is not a miss"
            )
        elif not contains_range(covered, earliest, min(latest, as_of)):
            status = EVENT_UNRESOLVED
            reason = (
                f"the accepted evidence does not cover all of {earliest}..{latest}, so "
                "whether this cash moved is unknown; it is not reported as missing "
                "and its actual is not reported as zero"
            )
        else:
            status = EVENT_MISSING
            reason = (
                f"the issued timing window closed on {latest} inside fully covered "
                "evidence and no actual cash was matched to it"
            )

        landed_months = sorted({_month_of(item["landed_on"]) for item in landings})
        shifted = [month for month in landed_months if month != modelled_month]
        rows.append(
            {
                "event_id": event_id,
                "source_id": str(event["source_id"]),
                "flow_class": str(event["flow_class"]),
                "direction": str(event["direction"]),
                "label": str(event["label"]),
                "confidence": str(event["confidence"]),
                "recurring_salary": bool(event["recurring_salary"]),
                "modelled_on": str(event["on"]),
                "modelled_month": modelled_month,
                "window": {
                    "earliest": str(event["timing"]["earliest"]),
                    "latest": str(event["timing"]["latest"]),
                    "precision": str(event["timing"]["precision"]),
                },
                "expected": format_decimal(expected, scale),
                "expected_minor": expected,
                "actual_minor": actual if bound else None,
                "actual": format_decimal(actual, scale) if bound else None,
                "status": status,
                "status_reason": reason,
                "matches": landings,
                "landed_months": landed_months,
                "timing_difference": (
                    None
                    if not shifted
                    else {
                        "modelled_month": modelled_month,
                        "landed_months": shifted,
                        "note": (
                            "the cash moved, in a different month than the forecast "
                            "placed it; this is a timing difference, not a missing "
                            "receipt or an extra cost"
                        ),
                    }
                ),
            }
        )
    return rows


def _month_rows(
    *,
    document: dict[str, Any],
    events: list[dict[str, Any]],
    movements: list[dict[str, Any]],
    bound_by_movement: dict[str, int],
    month_status: dict[str, tuple[str, str | None]],
    scale: int,
) -> list[dict[str, Any]]:
    """One row per issued month: expected, actual, variance and timing, by class."""
    expected: dict[str, dict[str, int]] = {}
    landed: dict[str, dict[str, int]] = {}
    timing_out: dict[str, list[dict[str, Any]]] = {}
    timing_in: dict[str, list[dict[str, Any]]] = {}

    for event in events:
        month = str(event["modelled_month"])
        cell = expected.setdefault(month, dict.fromkeys(FLOW_CLASSES, 0))
        cell[str(event["flow_class"])] += int(event["expected_minor"])
        for item in event["matches"]:
            landed_month = _month_of(str(item["landed_on"]))
            landed.setdefault(landed_month, dict.fromkeys(FLOW_CLASSES, 0))[
                str(event["flow_class"])
            ] += _minor(str(item["amount"]), scale)
            if landed_month != month:
                shift = {
                    "event_id": event["event_id"],
                    "flow_class": event["flow_class"],
                    "amount": item["amount"],
                    "modelled_month": month,
                    "landed_month": landed_month,
                }
                timing_out.setdefault(month, []).append(shift)
                timing_in.setdefault(landed_month, []).append(shift)

    # Cash bound to an event carries that event's class; whatever is left of a
    # movement does not. A movement bound for part of its value therefore keeps
    # its *remainder* visible here rather than disappearing behind the fraction
    # of it somebody classified.
    unclassified: dict[str, dict[str, int]] = {}
    unclassified_items: dict[str, list[dict[str, Any]]] = {}
    for movement in movements:
        month = _month_of(str(movement["on"]))
        amount = movement["amount_coefficient"] * (
            10 ** (scale - movement["amount_scale"])
        )
        bound = bound_by_movement.get(str(movement["movement_id"]), 0)
        remainder = amount - bound
        cell = unclassified.setdefault(month, {DIRECTION_IN: 0, "outflow": 0})
        if remainder <= 0:
            continue
        cell[str(movement["direction"])] += remainder
        unclassified_items.setdefault(month, []).append(
            {
                "movement_id": movement["movement_id"],
                "on": movement["on"],
                "direction": movement["direction"],
                "amount": format_decimal(amount, scale),
                "matched_amount": format_decimal(bound, scale),
                "unclassified_amount": format_decimal(remainder, scale),
                "label": movement["label"],
                "account": movement["account"],
                "internal_transfer": movement["internal_transfer"],
                "link_purpose": movement["link_purpose"],
            }
        )

    rows: list[dict[str, Any]] = []
    for issued in document["outputs"]["months"]:
        month = str(issued["month"])
        status, reason = month_status[month]
        scored = status == PERIOD_SCORED
        expected_cell = expected.get(month, dict.fromkeys(FLOW_CLASSES, 0))
        landed_cell = landed.get(month, dict.fromkeys(FLOW_CLASSES, 0))
        classes = [
            variance_reading(
                flow_class=flow_class,
                expected_minor=expected_cell[flow_class],
                actual_minor=landed_cell[flow_class] if scored else None,
                scale=scale,
                unresolved_reason=reason,
            )
            for flow_class in FLOW_CLASSES
        ]
        cash_effect = (
            sum(
                _minor(str(cell["cash_effect"]), scale)
                for cell in classes
                if cell["cash_effect"] is not None
            )
            if scored
            else None
        )
        unclassified_cell = unclassified.get(month, {DIRECTION_IN: 0, "outflow": 0})
        rows.append(
            {
                "month": month,
                "first_day": str(issued["first_day"]),
                "last_day": str(issued["last_day"]),
                "partial_month": bool(issued["partial"]),
                "period_status": status,
                "period_reason": reason,
                "scored": scored,
                "by_flow_class": classes,
                "net_cash_effect": (
                    None if cash_effect is None else format_decimal(cash_effect, scale)
                ),
                "net_cash_effect_note": (
                    "the sum of every class's cash effect: positive means the month "
                    "left more cash in the account than the forecast issued"
                ),
                "timing": {
                    "expected_here_landed_elsewhere": timing_out.get(month, []),
                    "expected_elsewhere_landed_here": timing_in.get(month, []),
                    "note": (
                        "a shift moves cash between months without changing the "
                        "horizon total; it is reported separately from a real "
                        "difference in level"
                    ),
                },
                "unclassified_actual": {
                    "receipts": (
                        format_decimal(unclassified_cell[DIRECTION_IN], scale)
                        if scored
                        else None
                    ),
                    "payments": (
                        format_decimal(unclassified_cell["outflow"], scale)
                        if scored
                        else None
                    ),
                    "items": unclassified_items.get(month, []) if scored else [],
                    "rule": (
                        "cash with no recorded match carries no flow class; it is "
                        "reported here rather than attributed to a class nothing "
                        "showed it belongs to"
                    ),
                },
            }
        )
    return rows


def _biggest_misses(events: list[dict[str, Any]], scale: int) -> list[dict[str, Any]]:
    """The events whose cash effect diverged most, largest first.

    Only resolved events rank: an unresolved or still-awaited event has no
    measured miss, and letting one head this list would turn missing evidence
    into the operator's biggest problem.
    """
    scored: list[tuple[int, dict[str, Any]]] = []
    for event in events:
        if event["status"] == EVENT_UNRESOLVED or event["status"] == EVENT_AWAITED:
            continue
        actual = int(event["actual_minor"] or 0)
        difference = actual - int(event["expected_minor"])
        cash_effect = difference if event["direction"] == DIRECTION_IN else -difference
        if cash_effect == 0:
            continue
        scored.append((cash_effect, event))
    scored.sort(key=lambda pair: (-abs(pair[0]), pair[1]["event_id"]))
    return [
        {
            "event_id": event["event_id"],
            "flow_class": event["flow_class"],
            "label": event["label"],
            "modelled_month": event["modelled_month"],
            "status": event["status"],
            "expected": event["expected"],
            "actual": event["actual"],
            "cash_effect": format_decimal(cash_effect, scale),
            "favourable": cash_effect > 0,
            "explanation": event["status_reason"],
            "timing_difference": event["timing_difference"],
        }
        for cash_effect, event in scored[:_BIGGEST_MISSES]
    ]


def _likely_income(events: list[dict[str, Any]], scale: int) -> dict[str, Any]:
    """Realization of the Likely income the snapshot issued, with its sample."""
    candidates = [
        event
        for event in events
        if event["flow_class"] == "income"
        and event["confidence"] == CONFIDENCE_LIKELY
        and event["recurring_salary"]
    ]
    resolved = [
        event
        for event in candidates
        if event["status"] in (EVENT_MATCHED, EVENT_PARTIALLY_MATCHED, EVENT_MISSING)
    ]
    realized = [event for event in resolved if event["actual_minor"]]
    summary = realization(
        realized=len(realized),
        resolved=len(resolved),
        realized_minor=sum(int(event["actual_minor"] or 0) for event in resolved),
        expected_minor=sum(int(event["expected_minor"]) for event in resolved),
        scale=scale,
    )
    summary["issued_occurrences"] = len(candidates)
    summary["observations"] = [
        {
            "event_id": event["event_id"],
            "label": event["label"],
            "modelled_on": event["modelled_on"],
            "window": event["window"],
            "expected": event["expected"],
            "actual": event["actual"],
            "status": event["status"],
            "landed_months": event["landed_months"],
        }
        for event in resolved
    ]
    summary["excluded_from_sample"] = [
        {
            "event_id": event["event_id"],
            "status": event["status"],
            "reason": event["status_reason"],
        }
        for event in candidates
        if event not in resolved
    ]
    return summary


def _match_suggestions(
    events: list[dict[str, Any]],
    unmatched: list[dict[str, Any]],
    *,
    scale: int,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Candidate movements for each event still awaiting an answer.

    A suggestion is a shortlist, never a decision. Nothing here writes a match,
    changes a status or affects a figure: the operator still binds each one
    explicitly, because only they know whether that transfer really was the
    salary. Its whole job is to remove the work of scanning a long movement list
    for the one row that plausibly answers an event.

    Ranking is deliberately dumb and inspectable — direction must agree, then
    closest amount, then closest date to the issued window — so a reviewer can
    see why a row was offered and disagree with it. ``amount_difference`` and
    ``days_from_window`` are published for exactly that reason. A movement
    already bound to something is not offered, and a candidate on the far side
    of the window is still offered with its distance stated rather than hidden,
    since a late salary is precisely the case worth surfacing.
    """
    open_statuses = {EVENT_MISSING, EVENT_AWAITED, EVENT_UNRESOLVED, EVENT_PARTIALLY_MATCHED}
    suggestions: list[dict[str, Any]] = []
    for event in events:
        if event["status"] not in open_statuses:
            continue
        outstanding = event["expected_minor"] - (event["actual_minor"] or 0)
        if outstanding <= 0:
            continue
        earliest, latest = event["window"]["earliest"], event["window"]["latest"]
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for movement in unmatched:
            if movement["direction"] != event["direction"]:
                continue
            amount = _minor(str(movement["amount"]), scale)
            difference = abs(amount - outstanding)
            day = str(movement["on"])
            if day < earliest:
                distance = (date.fromisoformat(earliest) - date.fromisoformat(day)).days
            elif day > latest:
                distance = (date.fromisoformat(day) - date.fromisoformat(latest)).days
            else:
                distance = 0
            scored.append((
                difference,
                distance,
                {
                    "movement_id": movement["movement_id"],
                    "on": day,
                    "amount": movement["amount"],
                    "label": movement["label"],
                    "account": movement["account"],
                    "amount_difference": format_decimal(difference, scale),
                    "exact_amount": difference == 0,
                    "days_from_window": distance,
                    "match_kind": MATCH_FULL if amount >= outstanding else MATCH_PARTIAL,
                    "suggested_amount": format_decimal(min(amount, outstanding), scale),
                },
            ))
        scored.sort(key=lambda item: (item[0], item[1], item[2]["movement_id"]))
        if not scored:
            continue
        suggestions.append({
            "event_id": event["event_id"],
            "label": event["label"],
            "direction": event["direction"],
            "status": event["status"],
            "outstanding": format_decimal(outstanding, scale),
            "window": dict(event["window"]),
            "candidates": [item[2] for item in scored[:limit]],
            "rule": (
                "candidates are ranked by amount difference then distance from the "
                "issued window; a suggestion binds nothing until the operator "
                "confirms it"
            ),
        })
    return suggestions


def forecast_variance(
    connection: sqlite3.Connection, *, snapshot: str, as_of: str
) -> dict[str, Any]:
    stored = stored_snapshot(connection, snapshot)
    document = stored["document"]
    if not stored["digest_verified"]:
        raise FinCLError(
            f"forecast snapshot {snapshot} does not match the digest it was issued "
            "with; it is not compared against"
        )
    currency = str(document["currency"])
    first_day = str(document["horizon"]["first_modelled_day"])
    horizon_end = str(document["horizon"]["end"])
    window_end = min(horizon_end, as_of)

    if window_end < first_day:
        movements_page: dict[str, Any] = {
            "movements": [],
            "excluded_card_channel": [],
            "undated": [],
            "coverage": {
                "covered": False,
                "status": "NOT_YET_ELAPSED",
                "reasons": [
                    f"the horizon opens on {first_day} and only {as_of} has passed"
                ],
                "uncovered": [],
                "rule": NEVER_ZERO_RULE,
            },
        }
    else:
        movements_page = cash_movements(
            connection, currency=currency, start=first_day, end=window_end
        )
    movements = list(movements_page["movements"])
    matches = _live_matches(connection, snapshot)
    scale = _working_scale(document, matches, movements)
    movements_by_id = {str(item["movement_id"]): item for item in movements}
    bound_by_movement: dict[str, int] = {}
    for binding in matches:
        bound_by_movement[binding["movement_id"]] = bound_by_movement.get(
            binding["movement_id"], 0
        ) + binding["amount_coefficient"] * (10 ** (scale - binding["amount_scale"]))

    covered = _coverage_contract(connection).covered_intervals
    month_status = {
        str(issued["month"]): _period_status(
            first_day=str(issued["first_day"]),
            last_day=str(issued["last_day"]),
            as_of=as_of,
            covered=covered,
        )
        for issued in document["outputs"]["months"]
    }

    events = _event_rows(
        document=document,
        matches=matches,
        movements_by_id=movements_by_id,
        covered=covered,
        as_of=as_of,
        scale=scale,
    )
    months = _month_rows(
        document=document,
        events=events,
        movements=movements,
        bound_by_movement=bound_by_movement,
        month_status=month_status,
        scale=scale,
    )

    # The operator's matching queue: cash nothing has been bound to at all.
    unmatched = [
        {
            "movement_id": item["movement_id"],
            "on": item["on"],
            "direction": item["direction"],
            "amount": item["amount"],
            "label": item["label"],
            "account": item["account"],
            "internal_transfer": item["internal_transfer"],
            "link_purpose": item["link_purpose"],
        }
        for item in movements
        if item["movement_id"] not in bound_by_movement
    ]
    unresolved_matches = [
        {"match": binding["match"], "event_id": row["event_id"], "note": binding["movement_note"]}
        for row in events
        for binding in row["matches"]
        if not binding["movement_resolved"]
    ]
    scored_months = [row["month"] for row in months if row["scored"]]

    return {
        "version": VARIANCE_VERSION,
        "snapshot": {
            "snapshot": stored["snapshot"],
            "issued_at": stored["issued_at"],
            "digest": stored["digest"],
            "digest_verified": stored["digest_verified"],
            "case": str(document["case"]),
            "settings": document["settings"],
            "basis": document["basis"],
            "horizon": document["horizon"],
            "reserve": document["reserve"],
            "opening_balance": document["opening_balance"],
            "generation_issued_against": str(document["generation"]),
            "issued_limitations": list(document["limitations"]),
        },
        "as_of": as_of,
        "currency": currency,
        "scale": scale,
        "window": {"start": first_day, "end": window_end, "horizon_end": horizon_end},
        "coverage": movements_page["coverage"],
        "scored_months": scored_months,
        "months": months,
        "events": events,
        "unmatched_movements": unmatched,
        "match_suggestions": _match_suggestions(events, unmatched, scale=scale),
        "excluded_card_channel": movements_page["excluded_card_channel"],
        "undated_movements": movements_page["undated"],
        "unresolved_matches": unresolved_matches,
        "biggest_misses": _biggest_misses(events, scale),
        "likely_income_realization": _likely_income(events, scale),
        "semantics": {
            "cash_basis": CASH_BASIS_RULE,
            "transfers_and_repayments": TRANSFER_SEMANTICS,
            "matching": MANUAL_MATCH_RULE,
            "never_zero": NEVER_ZERO_RULE,
            "sign": (
                "every class states its cash effect as well as its difference, so "
                "more income and more spending are never read as the same result"
            ),
        },
    }


def forecast_variance_page(
    database: Path, *, snapshot: str, as_of: str | None = None
) -> dict[str, Any]:
    """Compare one issued forecast snapshot against the cash that actually moved."""
    if as_of is None:
        resolved = date.today().isoformat()
    else:
        try:
            resolved = date.fromisoformat(as_of).isoformat()
        except ValueError as exc:
            raise FinCLError("as_of must be an ISO calendar date (YYYY-MM-DD)") from exc
    connection = connect_readonly(database)
    try:
        return forecast_variance(connection, snapshot=snapshot, as_of=resolved)
    finally:
        connection.close()


__all__ = ["TRANSFER_SEMANTICS", "forecast_variance", "forecast_variance_page"]
