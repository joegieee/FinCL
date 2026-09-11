"""Stage 2B — the card cash-conversion contract.

A credit-card purchase is not a bank movement.  It is a movement on a liability
that becomes a bank movement later, once, on a date the issuer's cycle decides.
Every way of getting that wrong produces a plausible-looking forecast:

* adding the card lifestyle baseline as an immediate bank debit *and* modelling
  the repayments states the same money twice;
* mapping the statement due but not the postings made since it closed drops the
  most recent month of spending entirely;
* assuming the statement is always paid in full turns an unknown into a
  schedule the evidence never supported;
* treating an unknown unbilled balance as zero is the same error wearing a
  number.

So this module states the conversion as an explicit contract rather than an
arithmetic convenience.  Existing card liability is partitioned into **unpaid
billed** (on an issued statement, not yet paid) and **posted unbilled** (posted
since that statement closed, not yet billed); **prospective purchases** — the
future ones a forecast models — are partitioned separately because they are not
a liability yet.  Each partition maps to bank settlement *exactly once*, and
:func:`reconcile_settlements` is the check that says so: every source item is
settled, and no source item is settled twice.

Nothing here fabricates a schedule.  A cycle geometry that the statements do not
establish, a repayment behaviour that the ledger does not evidence, or an
unbilled window the coverage contract does not reach each return an explicit
unresolved cash-settlement requirement, and the settlement plan for that card
stays unavailable.  An unresolved partition is never a zero.

Two boundary rules matter to the caller:

``horizon_position``
    A modelled purchase may settle *after* the six-month horizon.  Its cash does
    not leave inside the horizon, so it is not an in-horizon outflow — but it is
    reported as ending unpaid liability, and
    :func:`additional_spending_capacity` subtracts it.  That is what makes
    moving spend onto a card unable to increase affordable spending at the
    boundary: paid from the bank it costs the horizon its cash, paid on a card
    it costs the horizon the same amount of ending liability.

``shared_purchase_cash_effects``
    A shared purchase the user pays needs the whole gross amount in cash on the
    payment date.  The reimbursement is a separately dated receipt, and it is
    cash only once it is actually received — never a reduction of the future
    gross debit, and never able to fund the payment it follows.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..intervals import DateInterval, contains_range, normalize
from . import projection as card_projection
from .models import ROLE_PAYMENT_DUE, ROLE_PAYMENT_DUE_DATE

CONTRACT_VERSION = "card-cash-conversion-v1"
HORIZON_MONTHS = 6

# The three partitions, in the order they convert to cash. `prospective` is
# deliberately not part of "existing liability": it is not owed yet, and folding
# it in is how a forecast ends up settling money that was never borrowed.
PARTITION_UNPAID_BILLED = "unpaid_billed"
PARTITION_POSTED_UNBILLED = "posted_unbilled"
PARTITION_PROSPECTIVE = "prospective_purchases"
PARTITIONS = (PARTITION_UNPAID_BILLED, PARTITION_POSTED_UNBILLED, PARTITION_PROSPECTIVE)

# Widest disagreement between observed cycles that still counts as one geometry.
# Issuers shift a due date around weekends; three days absorbs that without
# absorbing a genuine change of terms.
_CYCLE_TOLERANCE_DAYS = 3

_CENT = Decimal("0.01")


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _day(value: str) -> date:
    return date.fromisoformat(value)


def _add_months(anchor: date, months: int, day: int) -> date:
    """``anchor`` rolled ``months`` forward, on ``day``, clamped to month length."""
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def unresolved_requirement(
    code: str, detail: str, *, blocks: str, evidence_needed: str
) -> dict[str, Any]:
    """One explicit unresolved cash-settlement requirement.

    ``blocks`` names what cannot be computed until it is answered, so a caller
    never has to guess whether a missing number is a zero or a gap.
    """
    return {
        "requirement": code,
        "detail": detail,
        "blocks": blocks,
        "evidence_needed": evidence_needed,
        "resolved": False,
    }


def statement_cycle_geometry(
    statements: list[dict[str, Any]], *, as_of: str, project_until: str
) -> dict[str, Any]:
    """Derive the billing cycle from issued statements, or say it is unknown.

    Two closed cycles carrying both a period end and a printed due date are the
    minimum: one statement states a date, two state a *rhythm*. The due offset
    used is the shortest observed one — money can leave on the earliest date the
    evidence supports, and a schedule that assumes the latest one would flatter
    the horizon.
    """
    usable = [
        item
        for item in statements
        if item.get("period_end") and item.get("payment_due_date")
    ]
    if len(usable) < 2:
        return {
            "available": False,
            "observed_cycles": len(usable),
            "cycle_day": None,
            "due_offset_days": None,
            "latest_period_end": None,
            "projected_cycles": [],
            "unresolved": [
                unresolved_requirement(
                    "cycle_geometry_unevidenced",
                    (
                        f"{len(usable)} statement(s) carry both a period end and a "
                        "printed due date; two closed cycles are needed before a "
                        "future due date can be derived"
                    ),
                    blocks="settlement dates for posted-unbilled and prospective purchases",
                    evidence_needed="a second statement with its period end and payment due date",
                )
            ],
        }
    ordered = sorted(usable, key=lambda item: str(item["period_end"]))
    offsets = [
        (_day(str(item["payment_due_date"])) - _day(str(item["period_end"]))).days
        for item in ordered
    ]
    days = [_day(str(item["period_end"])).day for item in ordered]
    if max(offsets) - min(offsets) > _CYCLE_TOLERANCE_DAYS or (
        max(days) - min(days) > _CYCLE_TOLERANCE_DAYS
    ):
        return {
            "available": False,
            "observed_cycles": len(usable),
            "cycle_day": None,
            "due_offset_days": None,
            "latest_period_end": str(ordered[-1]["period_end"]),
            "projected_cycles": [],
            "unresolved": [
                unresolved_requirement(
                    "cycle_geometry_inconsistent",
                    (
                        "the observed cycles do not agree: statement days "
                        f"{sorted(set(days))}, due offsets {sorted(set(offsets))} "
                        f"(days); more than {_CYCLE_TOLERANCE_DAYS} days apart is a "
                        "change of terms, not a weekend shift"
                    ),
                    blocks="settlement dates for posted-unbilled and prospective purchases",
                    evidence_needed="the card terms, or statements from one consistent cycle",
                )
            ],
        }
    cycle_day = _day(str(ordered[-1]["period_end"])).day
    offset = min(offsets)
    latest_end = _day(str(ordered[-1]["period_end"]))
    horizon = _day(project_until)

    cycles: list[dict[str, Any]] = []
    index = 1
    # Project past the requested end by one cycle so a purchase landing in the
    # final days still has a cycle to settle in.
    while True:
        period_end = _add_months(latest_end, index, cycle_day)
        period_start = _add_months(latest_end, index - 1, cycle_day) + timedelta(days=1)
        cycles.append(
            {
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "due_on": (period_end + timedelta(days=offset)).isoformat(),
                "basis": "projected_cycle_due_date",
            }
        )
        if period_end > horizon:
            break
        index += 1
        if index > 120:  # pragma: no cover - guards a pathological project_until
            break
    return {
        "available": True,
        "observed_cycles": len(usable),
        "cycle_day": cycle_day,
        "due_offset_days": offset,
        "latest_period_end": latest_end.isoformat(),
        "as_of": as_of,
        "projected_cycles": cycles,
        "unresolved": [],
        "basis": (
            "derived from the observed period ends and printed due dates; the "
            "shortest observed offset is used so the schedule never assumes the "
            "issuer's latest possible date"
        ),
    }


def repayment_behaviour(
    observations: list[dict[str, Any]], *, as_of: str
) -> dict[str, Any]:
    """What the ledger evidences about how this card is actually repaid.

    The existence of statements says nothing about repayment.  Full settlement
    is concluded only from observed full settlements; anything else — no
    evidence, or partial payments — leaves the schedule unsupported and returns
    the requirement that would resolve it.

    Only a statement whose due date has passed is evidence either way: an
    unpaid statement that is not due yet is not a missed payment, and counting
    it as one would report every card as revolving on the day it is read.
    """
    scored = [
        item
        for item in observations
        if item.get("due") is not None
        and Decimal(str(item["due"])) > 0
        and item.get("payment_due_date") is not None
        and str(item["payment_due_date"]) < as_of
    ]
    if len(scored) < 2:
        return {
            "pattern": "unevidenced",
            "supports_settlement_schedule": False,
            "observed_statements": len(scored),
            "observations": scored,
            "unresolved": [
                unresolved_requirement(
                    "repayment_behaviour_unevidenced",
                    (
                        f"{len(scored)} statement(s) are past their due date with a "
                        "printed amount due; statements existing is not evidence that "
                        "they are paid, and no repayment schedule is assumed from them"
                    ),
                    blocks="how much of each statement converts to bank cash, and when",
                    evidence_needed=(
                        "recorded card repayments allocated to at least two statements, "
                        "or the card terms stating the repayment rule"
                    ),
                )
            ],
        }
    ratios = []
    for item in scored:
        due = Decimal(str(item["due"]))
        paid = Decimal(str(item.get("paid") or "0"))
        ratios.append(
            {
                "statement_id": item.get("statement_id"),
                "due": _money(due),
                "paid": _money(paid),
                "paid_in_full": paid >= due,
            }
        )
    if all(item["paid_in_full"] for item in ratios):
        return {
            "pattern": "pays_statement_in_full",
            "supports_settlement_schedule": True,
            "observed_statements": len(ratios),
            "observations": ratios,
            "unresolved": [],
            "basis": (
                f"every one of the {len(ratios)} statements already past its due date "
                "was settled in full by recorded payments"
            ),
        }
    return {
        "pattern": "partial_or_revolving",
        "supports_settlement_schedule": False,
        "observed_statements": len(ratios),
        "observations": ratios,
        "unresolved": [
            unresolved_requirement(
                "repayment_behaviour_partial",
                (
                    "at least one evidenced statement was not settled in full, so the "
                    "balance revolves; the carried amount, its interest and the "
                    "minimum-payment rule decide the settlement dates and none of "
                    "them is derivable from the statements alone"
                ),
                blocks="the settlement schedule for every partition of this card",
                evidence_needed=(
                    "the card's interest rate and minimum-payment terms, or a "
                    "recorded repayment plan"
                ),
            )
        ],
    }


def partition_card_liability(
    *,
    billed: list[dict[str, Any]],
    unbilled: dict[str, Any],
    prospective: list[dict[str, Any]],
    as_of: str,
    latest_period_end: str | None,
) -> dict[str, Any]:
    """Split card money into the three partitions, disjointly and exhaustively.

    The split is by billing state and date, never by amount: billed is what an
    issued statement already demanded, unbilled is what posted after that
    statement closed and up to ``as_of``, and prospective is dated after
    ``as_of``.  Those three tests cannot both accept the same item, which is
    what makes "exactly once" checkable rather than hoped for.
    """
    issues: list[dict[str, Any]] = []

    billed_total = sum((Decimal(str(item["amount"])) for item in billed), Decimal(0))
    billed_partition = {
        "partition": PARTITION_UNPAID_BILLED,
        "known": True,
        "amount": _money(billed_total),
        "items": billed,
        "definition": (
            "demanded by an issued statement and not yet settled by a recorded payment"
        ),
        "unresolved": [],
    }

    if unbilled.get("known"):
        unbilled_partition = {
            "partition": PARTITION_POSTED_UNBILLED,
            "known": True,
            "amount": _money(Decimal(str(unbilled["amount"]))),
            "items": list(unbilled.get("items", [])),
            "window": {"after": latest_period_end, "through": as_of},
            "definition": "posted to the card after the last statement closed, not yet billed",
            "unresolved": [],
        }
    else:
        reason = str(unbilled.get("reason") or "the unbilled window is not established")
        requirement = unresolved_requirement(
            "unbilled_balance_unknown",
            (
                f"{reason}; an unknown unbilled balance is unresolved, never zero — "
                "reading it as zero would silently drop the most recent card spending "
                "out of the forecast"
            ),
            blocks="the posted-unbilled partition and every total derived from it",
            evidence_needed=(
                "card activity covering the window after the last statement period end, "
                "or a dated balance observation for the card"
            ),
        )
        issues.append(requirement)
        unbilled_partition = {
            "partition": PARTITION_POSTED_UNBILLED,
            "known": False,
            "amount": None,
            "items": [],
            "window": {"after": latest_period_end, "through": as_of},
            "definition": "posted to the card after the last statement closed, not yet billed",
            "unresolved": [requirement],
        }

    future = [item for item in prospective if str(item["on"]) > as_of]
    misdated = [item for item in prospective if str(item["on"]) <= as_of]
    if misdated:
        issues.append(
            unresolved_requirement(
                "prospective_purchase_not_in_the_future",
                (
                    "a modelled purchase is dated on or before the as-of date: "
                    + ", ".join(f"{item['id']} on {item['on']}" for item in misdated)
                    + "; a past date is either already posted (and belongs to a "
                    "recorded partition) or it is unevidenced, and modelling it "
                    "again would settle the same money twice"
                ),
                blocks="the prospective partition",
                evidence_needed="a future date for the modelled purchase, or its posted entry",
            )
        )
    prospective_total = sum((Decimal(str(item["amount"])) for item in future), Decimal(0))
    prospective_partition = {
        "partition": PARTITION_PROSPECTIVE,
        "known": not misdated,
        "amount": _money(prospective_total) if not misdated else None,
        "items": future,
        "definition": "modelled future purchases; not a liability until they post",
        "unresolved": [
            item
            for item in issues
            if item["requirement"] == "prospective_purchase_not_in_the_future"
        ],
    }

    return {
        "as_of": as_of,
        "partitions": [billed_partition, unbilled_partition, prospective_partition],
        "existing_liability_partitions": [PARTITION_UNPAID_BILLED, PARTITION_POSTED_UNBILLED],
        "disjointness_basis": (
            "billed is what a statement demanded; unbilled posted after that "
            "statement closed and through the as-of date; prospective is dated "
            "after it — no item satisfies two of the three"
        ),
        "unresolved": issues,
    }


def settlement_plan(
    *,
    partitions: dict[str, Any],
    geometry: dict[str, Any],
    behaviour: dict[str, Any],
    currency: str,
    as_of: str,
) -> dict[str, Any]:
    """Map each partition to dated bank settlements, or refuse to.

    One settlement row per source item, so the mapping is one-to-one by
    construction and :func:`reconcile_settlements` can check it by identity
    rather than by a total that could balance for the wrong reasons.
    """
    blocking = list(behaviour.get("unresolved", [])) + list(geometry.get("unresolved", []))
    by_partition = {item["partition"]: item for item in partitions["partitions"]}
    for partition in by_partition.values():
        blocking.extend(partition.get("unresolved", []))

    if not behaviour.get("supports_settlement_schedule") or not geometry.get("available"):
        return {
            "available": False,
            "currency": currency,
            "settlements": [],
            "unresolved": blocking,
            "reason": (
                "no settlement schedule is produced: the evidence does not establish "
                "how or when this card is repaid, and inventing one would state a "
                "cash date the ledger never supported"
            ),
        }

    settlements: list[dict[str, Any]] = []

    for item in by_partition[PARTITION_UNPAID_BILLED]["items"]:
        due_on = item.get("due_on")
        if due_on is None:
            blocking.append(
                unresolved_requirement(
                    "billed_due_date_not_printed",
                    (
                        f"statement {item.get('statement_id')} states an amount due but "
                        "no due date was parsed from it, so its settlement date is unknown"
                    ),
                    blocks=f"settlement of billed item {item['id']}",
                    evidence_needed="the payment due date printed on that statement",
                )
            )
            continue
        settlements.append(
            {
                "source_id": item["id"],
                "partition": PARTITION_UNPAID_BILLED,
                "settle_on": str(due_on),
                "amount": _money(Decimal(str(item["amount"]))),
                "currency": currency,
                "basis": "evidenced_statement_due_date",
            }
        )

    cycles = geometry["projected_cycles"]
    unbilled = by_partition[PARTITION_POSTED_UNBILLED]
    if unbilled["known"] and Decimal(str(unbilled["amount"])) != 0:
        # Everything posted since the last statement closed is billed by the very
        # next cycle, and settles on that cycle's due date — once.
        first = cycles[0]
        settlements.append(
            {
                "source_id": f"{PARTITION_POSTED_UNBILLED}:{unbilled['window']['after']}",
                "partition": PARTITION_POSTED_UNBILLED,
                "settle_on": first["due_on"],
                "amount": _money(Decimal(str(unbilled["amount"]))),
                "currency": currency,
                "basis": "projected_cycle_due_date",
                "billed_by_cycle": {
                    "period_start": first["period_start"],
                    "period_end": first["period_end"],
                },
            }
        )

    for item in by_partition[PARTITION_PROSPECTIVE]["items"]:
        on = str(item["on"])
        cycle = next(
            (
                candidate
                for candidate in cycles
                if candidate["period_start"] <= on <= candidate["period_end"]
            ),
            None,
        )
        if cycle is None:
            blocking.append(
                unresolved_requirement(
                    "prospective_purchase_outside_projected_cycles",
                    (
                        f"modelled purchase {item['id']} on {on} falls outside every "
                        "projected billing cycle, so no settlement date can be assigned"
                    ),
                    blocks=f"settlement of prospective item {item['id']}",
                    evidence_needed="a cycle geometry that reaches that date",
                )
            )
            continue
        settlements.append(
            {
                "source_id": item["id"],
                "partition": PARTITION_PROSPECTIVE,
                "settle_on": cycle["due_on"],
                "amount": _money(Decimal(str(item["amount"]))),
                "currency": currency,
                "basis": "projected_cycle_due_date",
                "purchased_on": on,
                "billed_by_cycle": {
                    "period_start": cycle["period_start"],
                    "period_end": cycle["period_end"],
                },
            }
        )

    settlements.sort(key=lambda row: (row["settle_on"], str(row["source_id"])))
    return {
        "available": not blocking,
        "currency": currency,
        "as_of": as_of,
        "settlements": settlements,
        "unresolved": blocking,
        "immediate_bank_debit": {
            "included": False,
            "reason": (
                "card spending is never an immediate bank debit. It settles through "
                "the cycles above, and adding the card lifestyle baseline as cash on "
                "the purchase date as well would state the same money twice"
            ),
        },
    }


def reconcile_settlements(
    partitions: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Check every partition converts to bank settlement exactly once.

    "Exactly once" is two separate failures — an omission and a duplication —
    and this reports them separately because they need different fixes.
    """
    settlements = plan["settlements"]
    seen: dict[str, int] = {}
    for row in settlements:
        seen[str(row["source_id"])] = seen.get(str(row["source_id"]), 0) + 1
    duplicated = sorted(key for key, count in seen.items() if count > 1)

    per_partition = []
    omitted: list[str] = []
    for partition in partitions["partitions"]:
        name = partition["partition"]
        rows = [row for row in settlements if row["partition"] == name]
        settled = sum((Decimal(row["amount"]) for row in rows), Decimal(0))
        if not partition["known"]:
            per_partition.append(
                {
                    "partition": name,
                    "liability": None,
                    "settled": _money(settled),
                    "difference": None,
                    "reconciled": False,
                    "reason": "the partition amount is unresolved, so it cannot be reconciled",
                }
            )
            continue
        # The unbilled partition settles as one balance rather than per posting,
        # so it is identified by its window; the other two settle item by item.
        if name == PARTITION_POSTED_UNBILLED:
            expected_ids = (
                {f"{PARTITION_POSTED_UNBILLED}:{partition['window']['after']}"}
                if Decimal(str(partition["amount"])) != 0
                else set()
            )
        else:
            expected_ids = {str(item["id"]) for item in partition["items"]}
        missing = sorted(expected_ids - set(seen))
        omitted.extend(missing)
        liability = Decimal(str(partition["amount"]))
        per_partition.append(
            {
                "partition": name,
                "liability": _money(liability),
                "settled": _money(settled),
                "difference": _money(settled - liability),
                "reconciled": settled == liability and not missing,
                "omitted_source_ids": missing,
            }
        )

    return {
        "reconciled": (
            not duplicated
            and not omitted
            and all(row["reconciled"] for row in per_partition)
        ),
        "per_partition": per_partition,
        "duplicated_source_ids": duplicated,
        "omitted_source_ids": sorted(omitted),
        "contract": (
            "each partition maps to bank settlement exactly once: the settled total "
            "equals the partition liability, every source item appears in a "
            "settlement, and none appears twice"
        ),
    }


def horizon_position(
    plan: dict[str, Any], *, as_of: str, horizon_months: int = HORIZON_MONTHS
) -> dict[str, Any]:
    """Split settlements at the horizon and report the liability left standing.

    A settlement dated past the horizon is not in-horizon cash — and it is not
    free either.  It is reported as ending unpaid liability so the boundary
    cannot be used to hide it.
    """
    start = _day(as_of)
    horizon_end = _add_months(start, horizon_months, start.day).isoformat()
    inside = [row for row in plan["settlements"] if row["settle_on"] <= horizon_end]
    beyond = [row for row in plan["settlements"] if row["settle_on"] > horizon_end]
    return {
        "as_of": as_of,
        "horizon_end": horizon_end,
        "horizon_months": horizon_months,
        "in_horizon_bank_outflow": _money(
            sum((Decimal(row["amount"]) for row in inside), Decimal(0))
        ),
        "in_horizon_settlements": inside,
        "ending_unpaid_liability": _money(
            sum((Decimal(row["amount"]) for row in beyond), Decimal(0))
        ),
        "settling_beyond_horizon": beyond,
        "basis": (
            "a purchase that settles after the horizon leaves no cash inside it, so "
            "it is carried as liability standing at the horizon instead"
        ),
    }


def additional_spending_capacity(
    *,
    cash_available: str,
    position: dict[str, Any],
    unresolved: list[dict[str, Any]],
    currency: str,
) -> dict[str, Any]:
    """Spending the horizon can still afford, with ending liability subtracted.

    Subtracting the ending liability is the whole point: without it, putting a
    purchase on a card whose due date falls after the horizon would *increase*
    affordable spending, because the cash never left inside the window.  With
    it, a card purchase and a bank purchase cost the boundary the same amount,
    which is the only answer that is not an arbitrage on the calendar.
    """
    if unresolved:
        return {
            "available": False,
            "currency": currency,
            "value": None,
            "reason": (
                "capacity is not stated while a cash-settlement requirement is "
                "unresolved; a number here would be an estimate wearing the "
                "authority of a measurement"
            ),
            "unresolved": unresolved,
        }
    cash = Decimal(cash_available)
    outflow = Decimal(position["in_horizon_bank_outflow"])
    ending = Decimal(position["ending_unpaid_liability"])
    return {
        "available": True,
        "currency": currency,
        "value": _money(cash - outflow - ending),
        "cash_available": _money(cash),
        "in_horizon_bank_outflow": _money(outflow),
        "ending_unpaid_liability": _money(ending),
        "unresolved": [],
        "basis": (
            "cash available, less settlements landing inside the horizon, less the "
            "card liability still unpaid at the horizon"
        ),
    }


def shared_purchase_cash_effects(
    *,
    gross: str,
    owner_share: str,
    paid_on: str,
    currency: str,
    reimbursement_expected_on: str | None = None,
    reimbursement_received_on: str | None = None,
) -> dict[str, Any]:
    """The dated cash effects of a shared purchase the user paid for.

    The owner share is a *cost* split.  Cash is not split: the payer funds the
    whole gross amount on the payment date and is repaid later, as a separate
    receipt on its own date.  Netting the two would understate the cash needed
    on the day and would credit the account with money nobody has sent yet.
    """
    gross_amount = Decimal(gross)
    owner_amount = Decimal(owner_share)
    if owner_amount > gross_amount:
        raise FinCLError("owner_share cannot exceed the gross amount")
    expected = gross_amount - owner_amount

    effects: list[dict[str, Any]] = [
        {
            "kind": "debit",
            "on": paid_on,
            "amount": _money(gross_amount),
            "currency": currency,
            "is_cash": True,
            "basis": (
                "the full gross amount leaves the account on the payment date; the "
                "owner share describes who bears the cost, not how much cash moves"
            ),
        }
    ]
    issues: list[dict[str, Any]] = []

    received = reimbursement_received_on is not None
    if received:
        assert reimbursement_received_on is not None
        effects.append(
            {
                "kind": "receipt",
                "on": reimbursement_received_on,
                "amount": _money(expected),
                "currency": currency,
                "is_cash": True,
                "basis": "the reimbursement is cash on the date it was actually received",
            }
        )
    elif expected != 0:
        issues.append(
            unresolved_requirement(
                "reimbursement_not_received",
                (
                    f"{_money(expected)} {currency} is expected back"
                    + (
                        f" around {reimbursement_expected_on}"
                        if reimbursement_expected_on
                        else " on no stated date"
                    )
                    + ", but it has not been received; an expected receipt is not "
                    "cash and never reduces the gross debit that precedes it"
                ),
                blocks="the cash position from the payment date until the receipt lands",
                evidence_needed="the received repayment entry, with its actual date",
            )
        )

    funding_date = reimbursement_received_on or reimbursement_expected_on
    can_fund = received and str(reimbursement_received_on) <= paid_on
    return {
        "currency": currency,
        "gross": _money(gross_amount),
        "owner_share": _money(owner_amount),
        "expected_reimbursement": _money(expected),
        "paid_on": paid_on,
        "cash_effects": effects,
        "cash_required_on_payment_date": _money(gross_amount),
        "netting": {
            "applied": False,
            "would_have_shown": _money(owner_amount),
            "understated_by": _money(expected),
            "reason": (
                "reducing the gross debit by the owner share would model a payment "
                "of the owner share only, which is not the payment that is made"
            ),
        },
        "reimbursement": {
            "status": "received" if received else "expected_not_received",
            "on": funding_date,
            "amount": _money(expected),
            "is_cash": received,
            "can_fund_the_payment": can_fund,
            "reason": (
                "the receipt is dated after the payment it reimburses, so it cannot "
                "fund it: the full gross amount must be available first"
                if not can_fund
                else "the receipt landed on or before the payment date"
            ),
        },
        "unresolved": issues,
    }


# ---------------------------------------------------------------------------
# The read: the contract applied to one ledger.
# ---------------------------------------------------------------------------


def _generation(connection: sqlite3.Connection) -> str:
    row = current_generation(connection)
    if row is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    return str(row["generation_token"])


def _statement_rows(
    connection: sqlite3.Connection, account_id: str, pocket: str
) -> list[dict[str, Any]]:
    """Issued statements for one card pocket, with their printed money facts."""
    rows: list[dict[str, Any]] = []
    for statement in card_projection.selected_statements(connection, account_id):
        facts = card_projection.statement_facts_by_pocket(
            connection, str(statement["id"])
        ).get(pocket, {})
        due_fact = facts.get(ROLE_PAYMENT_DUE)
        due_date_fact = facts.get(ROLE_PAYMENT_DUE_DATE)
        due = (
            Decimal(int(due_fact.value_coefficient)).scaleb(-int(due_fact.value_scale or 0))
            if due_fact is not None and due_fact.value_coefficient is not None
            else None
        )
        allocations = connection.execute(
            "SELECT amount_coefficient, amount_scale FROM card_payment_allocations "
            "WHERE statement_id = ? AND currency = ? AND bound = 1",
            (str(statement["id"]), pocket),
        ).fetchall()
        paid = sum(
            (
                Decimal(int(row["amount_coefficient"])).scaleb(-int(row["amount_scale"]))
                for row in allocations
            ),
            Decimal(0),
        )
        rows.append(
            {
                "statement_id": str(statement["id"]),
                "period_start": statement["period_start"],
                "period_end": statement["period_end"],
                "statement_date": statement["statement_date"],
                "payment_due_date": due_date_fact.value_text if due_date_fact else None,
                "due": None if due is None else _money(due),
                "paid": _money(paid),
            }
        )
    return rows


def _pockets(connection: sqlite3.Connection, account_id: str) -> list[str]:
    found: list[str] = []
    for statement in card_projection.selected_statements(connection, account_id):
        for pocket in card_projection.statement_facts_by_pocket(
            connection, str(statement["id"])
        ):
            if pocket not in found:
                found.append(pocket)
    return sorted(found)


def account_evidence_intervals(
    connection: sqlite3.Connection, account_id: str
) -> list[DateInterval]:
    """Dates this account's own accepted, selected evidence actually speaks for.

    Statement periods where the source states one, and the observed posting span
    of an accepted CSV run otherwise.  This is deliberately per account: the
    ledger-wide coverage contract answers a different question, and a window
    covered by *some other* account's statement says nothing about what posted
    on this card.
    """
    rows = connection.execute(
        """
        SELECT s.period_start AS lo, s.period_end AS hi
        FROM statements s JOIN extraction_runs x ON x.id = s.extraction_run_id
        WHERE s.account_id = ? AND x.status = 'ACCEPTED'
          AND s.selection_state = 'SELECTED'
          AND s.period_start IS NOT NULL AND s.period_end IS NOT NULL
        UNION ALL
        SELECT min(substr(e.effective_at, 1, 10)) AS lo,
               max(substr(e.effective_at, 1, 10)) AS hi
        FROM ledger_entries e JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE e.account_id = ? AND e.statement_id IS NULL
          AND x.status = 'ACCEPTED' AND x.selection_state = 'SELECTED'
        GROUP BY x.id
        """,
        (account_id, account_id),
    ).fetchall()
    return normalize(
        [DateInterval(str(row["lo"]), str(row["hi"])) for row in rows if row["lo"] and row["hi"]]
    )


def _posted_unbilled(
    connection: sqlite3.Connection,
    token: str,
    *,
    account_id: str,
    pocket: str,
    latest_period_end: str | None,
    as_of: str,
    covered: list[DateInterval],
) -> dict[str, Any]:
    """Card spending posted after the last statement closed, or why it is unknown.

    Provenance decides membership: the components counted are the ones whose
    *account* is this card, never the ones whose merchant looks like one.
    """
    if latest_period_end is None:
        return {
            "known": False,
            "amount": None,
            "reason": "the latest statement states no period end, so the unbilled window has no start",
        }
    window_start = (_day(latest_period_end) + timedelta(days=1)).isoformat()
    if window_start > as_of:
        return {
            "known": True,
            "amount": "0.00",
            "items": [],
            "basis": "the latest statement closed on or after the as-of date; the window is empty",
        }
    if not contains_range(covered, window_start, as_of):
        return {
            "known": False,
            "amount": None,
            "reason": (
                f"this card has no accepted evidence covering {window_start}..{as_of}, "
                "so postings in the unbilled window may be missing"
            ),
        }
    rows = connection.execute(
        """
        SELECT c.component_id, c.signed_coefficient, c.scale, c.represented_start,
               c.merchant_display
        FROM analysis_components c
        JOIN component_provenance p ON p.component_id = c.component_id
        WHERE c.generation_token = ? AND c.lens = 'recorded_flow'
          AND p.payment_channel = 'card' AND p.source_account_id = ?
          AND c.currency = ? AND c.bucket IN ('spending', 'fees')
          AND c.represented_start >= ? AND c.represented_start <= ?
        ORDER BY c.represented_start, c.component_id
        """,
        (token, account_id, pocket, window_start, as_of),
    ).fetchall()
    items = [
        {
            "id": str(row["component_id"]),
            "on": str(row["represented_start"]),
            "amount": _money(
                abs(Decimal(int(row["signed_coefficient"])).scaleb(-int(row["scale"])))
            ),
            "label": str(row["merchant_display"]),
        }
        for row in rows
    ]
    total = sum((Decimal(item["amount"]) for item in items), Decimal(0))
    return {
        "known": True,
        "amount": _money(total),
        "items": items,
        "basis": (
            "card-channel components whose ledger account is this card, posted inside "
            "the covered unbilled window"
        ),
    }


def baseline_channel_split(
    database: Path, *, start: str, end: str, currency: str
) -> dict[str, Any]:
    """Split recorded baseline cost by the channel the money actually left through.

    This is the split a forecast needs before it adds anything to a bank
    projection.  The bank portion is spending that already left an account and
    can be modelled as an immediate debit.  The card portion must not be: it
    settles through :func:`settlement_plan`, and adding it as a same-day bank
    debit *as well as* modelling the repayments would state the same money
    twice.  The channel comes from `component_provenance` — the ledger account —
    so two identical-looking purchases at one merchant are split correctly.
    """
    connection = connect_readonly(database)
    try:
        token = _generation(connection)
        rows = connection.execute(
            """
            SELECT p.payment_channel AS channel,
                   c.signed_coefficient AS coefficient, c.scale AS scale
            FROM analysis_components c
            JOIN component_provenance p ON p.component_id = c.component_id
            WHERE c.generation_token = ? AND c.lens = 'recorded_flow'
              AND c.bucket IN ('spending', 'fees') AND c.currency = ?
              AND c.represented_start >= ? AND c.represented_end <= ?
            """,
            (token, currency, start, end),
        ).fetchall()
    finally:
        connection.close()

    totals: dict[str, Decimal] = {"card": Decimal(0), "bank": Decimal(0), "off_ledger": Decimal(0)}
    for row in rows:
        totals[str(row["channel"])] += abs(
            Decimal(int(row["coefficient"])).scaleb(-int(row["scale"]))
        )
    return {
        "generation": token,
        "currency": currency,
        "window": {"start": start, "end": end},
        "bank": {
            "amount": _money(totals["bank"]),
            "immediate_bank_debit": True,
            "basis": "this money already left a bank account and settles on the day it is spent",
        },
        "card": {
            "amount": _money(totals["card"]),
            "immediate_bank_debit": False,
            "basis": (
                "this money moved a card liability, not a bank balance; it converts to "
                "cash through the statement cycles and must never be added as an "
                "immediate bank debit alongside the modelled repayments"
            ),
        },
        "off_ledger": {
            "amount": _money(totals["off_ledger"]),
            "immediate_bank_debit": False,
            "basis": "a synthetic component with no account of its own; no bank movement",
        },
        "channel_basis": (
            "the ledger account each component belongs to, never its merchant label"
        ),
    }


def card_cash_conversion(
    database: Path,
    *,
    as_of: str,
    horizon_months: int = HORIZON_MONTHS,
    prospective_purchases: list[dict[str, Any]] | None = None,
    cash_available: str | None = None,
) -> dict[str, Any]:
    """The cash-conversion contract for every card in one ledger.

    ``prospective_purchases`` is the forecast's side of the contract: modelled
    future purchases, each ``{"id", "account_id", "currency", "on", "amount"}``.
    They are converted here rather than in the forecast so a modelled card
    purchase can never be booked as same-day bank cash. ``as_of`` is required
    (§5.3): this reader sits on the money-flow call graph and must never
    consult the wall clock itself.
    """
    day = as_of
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise FinCLError("as_of must be an ISO calendar date (YYYY-MM-DD)") from exc
    modelled = list(prospective_purchases or [])

    connection = connect_readonly(database)
    try:
        token = _generation(connection)
        cards: list[dict[str, Any]] = []
        for account in card_projection.selected_card_accounts(connection):
            account_id = str(account["id"])
            for pocket in _pockets(connection, account_id):
                cards.append(
                    _one_card(
                        connection,
                        token,
                        account=account,
                        pocket=pocket,
                        as_of=day,
                        horizon_months=horizon_months,
                        covered=account_evidence_intervals(connection, account_id),
                        modelled=[
                            item
                            for item in modelled
                            if str(item.get("account_id")) == account_id
                            and str(item.get("currency")) == pocket
                        ],
                        cash_available=cash_available,
                    )
                )
    finally:
        connection.close()

    return {
        "generation": token,
        "as_of": day,
        "contract_version": CONTRACT_VERSION,
        "horizon_months": horizon_months,
        "cards": cards,
        "unresolved": [
            item for card in cards for item in card["plan"]["unresolved"]
        ],
    }


def _one_card(
    connection: sqlite3.Connection,
    token: str,
    *,
    account: Any,
    pocket: str,
    as_of: str,
    horizon_months: int,
    covered: list[DateInterval],
    modelled: list[dict[str, Any]],
    cash_available: str | None,
) -> dict[str, Any]:
    account_id = str(account["id"])
    statements = _statement_rows(connection, account_id, pocket)
    issued = [item for item in statements if str(item["statement_date"]) <= as_of]

    billed = [
        {
            "id": f"card_statement:{item['statement_id']}:{pocket}",
            "statement_id": item["statement_id"],
            "amount": _money(max(Decimal(item["due"]) - Decimal(item["paid"]), Decimal(0))),
            "due_on": item["payment_due_date"],
        }
        for item in issued
        if item["due"] is not None
        and Decimal(item["due"]) - Decimal(item["paid"]) > 0
    ]

    latest_period_end = next(
        (
            str(item["period_end"])
            for item in reversed(issued)
            if item["period_end"] is not None
        ),
        None,
    )
    unbilled = _posted_unbilled(
        connection,
        token,
        account_id=account_id,
        pocket=pocket,
        latest_period_end=latest_period_end,
        as_of=as_of,
        covered=covered,
    )

    start = _day(as_of)
    horizon_end = _add_months(start, horizon_months, start.day).isoformat()
    project_until = max(
        [horizon_end] + [str(item["on"]) for item in modelled]
    )
    geometry = statement_cycle_geometry(issued, as_of=as_of, project_until=project_until)
    behaviour = repayment_behaviour(
        [item for item in issued if item["due"] is not None], as_of=as_of
    )
    partitions = partition_card_liability(
        billed=billed,
        unbilled=unbilled,
        prospective=modelled,
        as_of=as_of,
        latest_period_end=latest_period_end,
    )
    plan = settlement_plan(
        partitions=partitions,
        geometry=geometry,
        behaviour=behaviour,
        currency=pocket,
        as_of=as_of,
    )
    reconciliation = reconcile_settlements(partitions, plan)
    position = horizon_position(plan, as_of=as_of, horizon_months=horizon_months)
    capacity = (
        additional_spending_capacity(
            cash_available=cash_available,
            position=position,
            unresolved=plan["unresolved"],
            currency=pocket,
        )
        if cash_available is not None
        else None
    )
    return {
        "account_id": account_id,
        "label": str(account["display_name"]),
        "currency": pocket,
        "cycle_geometry": geometry,
        "repayment_behaviour": behaviour,
        "partitions": partitions,
        "plan": plan,
        "reconciliation": reconciliation,
        "horizon": position,
        "additional_spending_capacity": capacity,
    }


__all__ = [
    "CONTRACT_VERSION",
    "HORIZON_MONTHS",
    "PARTITIONS",
    "PARTITION_POSTED_UNBILLED",
    "PARTITION_PROSPECTIVE",
    "PARTITION_UNPAID_BILLED",
    "account_evidence_intervals",
    "additional_spending_capacity",
    "baseline_channel_split",
    "card_cash_conversion",
    "horizon_position",
    "partition_card_liability",
    "reconcile_settlements",
    "repayment_behaviour",
    "settlement_plan",
    "shared_purchase_cash_effects",
    "statement_cycle_geometry",
    "unresolved_requirement",
]
