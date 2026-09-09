"""Stage 2B — the four decision rules every obligation adapter shares.

These are pure functions on purpose.  Each one encodes a rule that is easy to
get quietly wrong inside a larger read, and each is exercised directly by the
tests so a regression names the rule rather than a page total:

``resolve_amount_basis``
    A derived remaining schedule *replaces* the lump-sum due, it never
    supplements it.  The lump sum is still returned — visibly superseded and
    explicitly not counted — because hiding it would make the two numbers
    impossible to reconcile against the source.

``payment_state``
    A due date in the past is a date, not an event.  Without evidence of a
    payment, a forgiveness or a correction, the obligation stays open and
    visible and its payment status stays *unknown*.  Nothing here ever infers a
    missed payment from the calendar.

``history_confidence``
    History supports "Likely" and stops there.  "Confirmed" needs evidence of a
    future receipt or a recorded commitment.

``baseline_component_link``
    A recurring event replaces the portion of the historical baseline that
    belongs to its own component, established by its own bound sources.  One
    unambiguous category links; anything else stays unknown rather than being
    attached to a neighbouring category or to an unrelated total.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_CENT = Decimal("0.01")


def money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def resolve_amount_basis(
    *,
    lump_sum: str | None,
    schedule: list[dict[str, Any]] | None,
    currency: str,
) -> dict[str, Any]:
    """Choose between a remaining schedule and a lump-sum due.

    ``schedule`` rows are the *remaining* rows only; each carries ``due_on`` and
    ``amount``.  When any remains, they are the obligation and the lump sum is
    reported as superseded with ``counted`` false, so a caller that sums both
    fields cannot double the liability.
    """
    if schedule:
        total = sum((Decimal(row["amount"]) for row in schedule), Decimal(0))
        return {
            "amount_basis": "remaining_schedule",
            "remaining_amount": {"value": money(total), "currency": currency},
            "remaining_unknown_reason": None,
            "schedule": schedule,
            "superseded_lump_sum": (
                None
                if lump_sum is None
                else {
                    "value": lump_sum,
                    "currency": currency,
                    "counted": False,
                    "reason": (
                        "the remaining schedule replaces this lump-sum due; "
                        "counting both would state the liability twice"
                    ),
                }
            ),
        }
    if lump_sum is None:
        return {
            "amount_basis": "unknown",
            "remaining_amount": None,
            "remaining_unknown_reason": "no remaining schedule and no evidenced lump-sum due",
            "schedule": None,
            "superseded_lump_sum": None,
        }
    return {
        "amount_basis": "lump_sum_due",
        "remaining_amount": {"value": lump_sum, "currency": currency},
        "remaining_unknown_reason": None,
        "schedule": None,
        "superseded_lump_sum": None,
    }


def payment_state(
    *,
    due_on: str | None,
    as_of: str,
    payment_count: int,
    remaining: str | None,
    closed: bool = False,
    closed_reason: str | None = None,
) -> dict[str, Any]:
    """The visible state of one obligation, without inferring a missed payment."""
    if closed:
        return {
            "status": "settled",
            "payment_status": "evidenced",
            "stays_visible": False,
            "overdue": False,
            "basis": closed_reason or "recorded evidence of payment, forgiveness or correction",
        }
    outstanding = remaining is None or Decimal(remaining) != 0
    if not outstanding:
        return {
            "status": "settled",
            "payment_status": "evidenced",
            "stays_visible": False,
            "overdue": False,
            "basis": "recorded payments account for the whole obligation",
        }
    past_due = due_on is not None and due_on < as_of
    if not past_due:
        return {
            "status": "open",
            "payment_status": "partially_evidenced" if payment_count else "unknown",
            "stays_visible": True,
            "overdue": False,
            "basis": "due date has not passed",
        }
    return {
        "status": "due_date_passed_no_payment_evidence"
        if payment_count == 0
        else "due_date_passed_partially_paid",
        "payment_status": "partially_evidenced" if payment_count else "unknown",
        "stays_visible": True,
        "overdue": True,
        "basis": (
            "the due date has passed and no payment is recorded; a passed due "
            "date is not evidence that a payment was missed, so this stays "
            "visible until payment, forgiveness or correction is recorded"
        ),
    }


def history_confidence(
    *, future_receipt_evidenced: bool = False, commitment_recorded: bool = False
) -> dict[str, str]:
    """Confidence for a stream or obligation derived from recorded history."""
    if commitment_recorded:
        return {
            "confidence": "confirmed",
            "confidence_basis": "a recorded commitment states the future obligation",
        }
    if future_receipt_evidenced:
        return {
            "confidence": "confirmed",
            "confidence_basis": "a future receipt is evidenced",
        }
    return {
        "confidence": "likely",
        "confidence_basis": (
            "derived from recorded history only; a past receipt does not "
            "evidence a future one, so this is never automatically confirmed"
        ),
    }


def baseline_component_link(
    *,
    categories: set[str] | frozenset[str],
    currency: str,
    portions: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Link a recurring event to the one baseline component its sources name.

    ``portions`` maps ``(currency, category_key)`` to the monthly historical
    amount that component contributes to the baseline remainder.
    """
    named = {category for category in categories if not category.startswith("Unclassified")}
    if len(named) != 1:
        return {
            "available": False,
            "category_key": None,
            "relation": "unknown",
            "reason": (
                "no bound source establishes a category"
                if not named
                else "bound sources name more than one category: "
                + ", ".join(sorted(named))
            ),
        }
    category = next(iter(named))
    portion = portions.get((currency, category))
    return {
        "available": True,
        "category_key": category,
        "currency": currency,
        "relation": "replaces_category_portion",
        "monthly_baseline_portion": portion,
        "portion_unknown_reason": (
            None if portion is not None else "the baseline window has no cost in this component"
        ),
        "note": (
            "this replaces the historical baseline remainder for "
            f"{category} only — no other component and no total is reduced"
        ),
    }


__all__ = [
    "baseline_component_link",
    "history_confidence",
    "money",
    "payment_state",
    "resolve_amount_basis",
]
