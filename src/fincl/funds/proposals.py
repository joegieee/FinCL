"""Stage 4A fund proposals, derived from evidence that already exists.

A proposal is an offer to turn something the ledger already knows into a fund.
It never asks the operator to supply a number: every proposal carries the
amount and date that were actually observed, and says which of them is missing
when one is.

Three sources, three different strengths of evidence:

* an **identified irregular source expense** — one of the entries the Stage 1
  estimator held out of the routine residual.  It gives a real amount and the
  month it landed in, and nothing more.  One isolated purchase is *not*
  evidence of a yearly bill; calling it annual would be an interpretation
  dressed as a reading, so recurrence stays unknown and the proposal stays
  incomplete;
* a **known obligation** — a debt, plan or commitment already recorded, whose
  amount and due date come with their own basis, and whose recurrence is known
  only when a schedule was actually recorded; and
* an **evidenced future invoice** — an obligation whose date basis is a printed
  or stated document date.

A goal record may also back a fund, but only with its manual basis disclosed:
a goal target is something a person typed, and a goal's "saved" figure is a
manually maintained tracking number.  Neither is evidence that cash exists, so
neither is ever counted as a recorded allocation.

Nothing here replaces the irregular allowance.  A proposal is not a fund; the
data-derived allowance is retained in full until a fund is confirmed with a
sourced amount and date, and even then only its own matching amount is removed.
"""

from __future__ import annotations

from typing import Any

from .models import (
    AMOUNT_BASIS_FUTURE_INVOICE,
    AMOUNT_BASIS_GOAL_RECORD,
    AMOUNT_BASIS_OBLIGATION,
    AMOUNT_BASIS_OBSERVED_EXPENSE,
    AMOUNT_BASIS_UNKNOWN,
    DATE_BASIS_GOAL_TARGET_DATE,
    DATE_BASIS_INVOICE_DATE,
    DATE_BASIS_OBSERVED_MONTH,
    DATE_BASIS_SCHEDULED_OCCURRENCE,
    DATE_BASIS_STATED_DUE_DATE,
    DATE_BASIS_UNKNOWN,
)

PROPOSAL_POLICY = (
    "a proposal reports the amount and date that were observed and names what "
    "is missing; it never asks for a guessed figure, and it never replaces the "
    "data-derived irregular allowance — only a confirmed fund with a sourced "
    "amount and due date does that, and only for its own matching amount"
)

RECURRENCE_POLICY = (
    "recurrence is known only where a schedule was actually recorded; repeated "
    "observations of a similar expense are evidence of repetition, never proof "
    "of an annual pattern, and one isolated purchase is proof of nothing"
)

# Date bases that place a fund on a real day. An observed month does not.
_DATED_BASES = frozenset(
    {
        DATE_BASIS_STATED_DUE_DATE,
        DATE_BASIS_SCHEDULED_OCCURRENCE,
        DATE_BASIS_INVOICE_DATE,
        DATE_BASIS_GOAL_TARGET_DATE,
    }
)


def _proposal(
    *,
    proposal_id: str,
    label: str,
    currency: str,
    source: dict[str, Any],
    amount: str | None,
    amount_basis: str,
    observed_on: str | None,
    date_basis: str,
    date_precision: str,
    recurrence: dict[str, Any],
    manual_basis: bool = False,
    manual_disclosure: str | None = None,
) -> dict[str, Any]:
    missing: list[str] = []
    if amount is None:
        missing.append("no sourced amount is available for this expense")
    if observed_on is None or date_basis not in _DATED_BASES:
        missing.append(
            "no sourced due date is available; the evidence places the expense in "
            f"{observed_on or 'no known period'} without stating a day"
        )
    complete = not missing
    return {
        "proposal_id": proposal_id,
        "label": label,
        "currency": currency,
        "source": source,
        "observed_amount": (
            None
            if amount is None
            else {"value": amount, "basis": amount_basis}
        ),
        "observed_date": (
            None
            if observed_on is None
            else {
                "value": observed_on,
                "basis": date_basis,
                "precision": date_precision,
            }
        ),
        "amount_basis": amount_basis,
        "date_basis": date_basis if observed_on is not None else DATE_BASIS_UNKNOWN,
        "recurrence": recurrence,
        "complete": complete,
        "incomplete_reasons": missing,
        "irregular_allowance_retained": True,
        "irregular_allowance_reason": (
            "a proposal is not a fund; the data-derived allowance stands until "
            "this fund is confirmed"
            if complete
            else "the amount or due date is unknown, so the data-derived "
            "irregular allowance is kept in full"
        ),
        "manual_basis": manual_basis,
        "manual_basis_disclosure": manual_disclosure,
    }


def _irregular_proposals(
    estimate: dict[str, Any], *, taken: set[str]
) -> list[dict[str, Any]]:
    """One proposal per held-out irregular entry the estimator identified."""
    if not estimate.get("available"):
        return []
    proposals: list[dict[str, Any]] = []
    for currency, cell in estimate.get("by_currency", {}).items():
        entries = list(cell.get("irregular_risk_entries", []))
        occurrences: dict[str, list[str]] = {}
        for entry in entries:
            occurrences.setdefault(str(entry["category_key"]), []).append(
                str(entry["month"])
            )
        for entry in entries:
            category = str(entry["category_key"])
            month = str(entry["month"])
            proposal_id = f"fund-proposal:irregular:{currency}:{category}:{month}"
            if proposal_id in taken:
                continue
            months = sorted(set(occurrences[category]))
            proposals.append(
                _proposal(
                    proposal_id=proposal_id,
                    label=f"{category} ({month})",
                    currency=str(currency),
                    source={
                        "kind": "identified_irregular_expense",
                        "category_key": category,
                        "month": month,
                        "held_out_threshold": entry.get("threshold"),
                        "method": estimate.get("sample", {}).get("outlier_method"),
                    },
                    amount=str(entry["amount"]),
                    amount_basis=AMOUNT_BASIS_OBSERVED_EXPENSE,
                    observed_on=month,
                    date_basis=DATE_BASIS_OBSERVED_MONTH,
                    date_precision="month",
                    recurrence={
                        "known": False,
                        "basis": "no schedule is recorded for this expense",
                        "observed_months": months,
                        "observed_month_count": len(months),
                        "note": (
                            "one isolated purchase does not prove a yearly bill; "
                            "confirming an annual pattern is an interpretation and "
                            "is not made here"
                            if len(months) < 2
                            else "the expense was observed in more than one month, "
                            "which is repetition, not a proven annual cadence"
                        ),
                    },
                )
            )
    return proposals


def _obligation_proposals(
    obligations: dict[str, Any], *, taken: set[str]
) -> list[dict[str, Any]]:
    """One proposal per visible outflow obligation, keeping its own bases."""
    proposals: list[dict[str, Any]] = []
    for obligation in obligations.get("obligations", []):
        if obligation.get("direction") != "outflow" or not obligation.get(
            "stays_visible", True
        ):
            continue
        proposal_id = f"fund-proposal:obligation:{obligation['obligation_id']}"
        if proposal_id in taken:
            continue
        amount_cell = obligation.get("remaining_amount")
        date_basis_cell = obligation.get("date_basis", {})
        source_basis = str(date_basis_cell.get("kind") or "")
        observed_on = date_basis_cell.get("value")
        invoice = source_basis == "statement_due_date"
        scheduled = source_basis in ("scheduled_due_date", "scheduled_occurrence")
        date_basis = (
            DATE_BASIS_INVOICE_DATE
            if invoice
            else DATE_BASIS_SCHEDULED_OCCURRENCE
            if scheduled
            else DATE_BASIS_STATED_DUE_DATE
            if source_basis == "stated_due_date"
            else DATE_BASIS_UNKNOWN
        )
        has_schedule = bool(
            date_basis_cell.get("schedule") or obligation.get("schedule")
        )
        proposals.append(
            _proposal(
                proposal_id=proposal_id,
                label=str(obligation["label"]),
                currency=str(obligation["currency"]),
                source={
                    "kind": "known_obligation",
                    "obligation_id": obligation["obligation_id"],
                    "origin": obligation.get("origin"),
                    "obligation_source": obligation.get("source"),
                    "obligation_date_basis": date_basis_cell.get("kind"),
                },
                amount=None if amount_cell is None else str(amount_cell["value"]),
                amount_basis=(
                    AMOUNT_BASIS_UNKNOWN
                    if amount_cell is None
                    else AMOUNT_BASIS_FUTURE_INVOICE
                    if invoice
                    else AMOUNT_BASIS_OBLIGATION
                ),
                observed_on=None if date_basis == DATE_BASIS_UNKNOWN else observed_on,
                date_basis=date_basis,
                date_precision="day",
                recurrence={
                    "known": has_schedule,
                    "basis": (
                        "a schedule is recorded on this obligation"
                        if has_schedule
                        else "no schedule is recorded on this obligation"
                    ),
                    "schedule": date_basis_cell.get("schedule"),
                    "note": None if has_schedule else RECURRENCE_POLICY,
                },
            )
        )
    return proposals


def _goal_proposals(goals: dict[str, Any], *, taken: set[str]) -> list[dict[str, Any]]:
    """Goal records offered as fund targets, with their manual basis disclosed."""
    proposals: list[dict[str, Any]] = []
    for goal in goals.get("goals", []):
        if goal.get("archived"):
            continue
        proposal_id = f"fund-proposal:goal:{goal['plan']}"
        if proposal_id in taken:
            continue
        proposal = _proposal(
            proposal_id=proposal_id,
            label=str(goal["name"]),
            currency=str(goal["currency"]),
            source={
                "kind": "goal_record",
                "plan": goal["plan"],
                "target_on": goal.get("target_on"),
                "recorded_saved": goal.get("saved"),
                "recorded_saved_observed_on": goal.get("observed_on"),
                "legacy_undated_saved": goal.get("legacy_undated_saved"),
            },
            amount=str(goal["target"]),
            amount_basis=AMOUNT_BASIS_GOAL_RECORD,
            observed_on=goal.get("target_on"),
            date_basis=DATE_BASIS_GOAL_TARGET_DATE,
            date_precision="day",
            recurrence={
                "known": False,
                "basis": "a goal states one target, not a cadence",
                "note": RECURRENCE_POLICY,
            },
            manual_basis=True,
            manual_disclosure=(
                "this target is a manually recorded goal, not observed money; the "
                "goal's saved figure is a manual tracking number and is never "
                "carried into the fund as a recorded allocation, because neither "
                "a goal target nor its saved field proves cash exists"
            ),
        )
        proposals.append(proposal)
    return proposals


def fund_proposals(
    *,
    estimate: dict[str, Any],
    obligations: dict[str, Any],
    goals: dict[str, Any],
    confirmed_source_references: set[str],
) -> list[dict[str, Any]]:
    """Every fund worth offering, from evidence that is already in the ledger.

    A source already carried by a confirmed fund is not proposed again — that
    is what keeps a confirmed fund and its own proposal from being counted as
    two separate future expenses.
    """
    taken = set(confirmed_source_references)
    proposals = [
        *_irregular_proposals(estimate, taken=taken),
        *_obligation_proposals(obligations, taken=taken),
        *_goal_proposals(goals, taken=taken),
    ]
    proposals.sort(key=lambda item: str(item["proposal_id"]))
    return proposals


__all__ = ["PROPOSAL_POLICY", "RECURRENCE_POLICY", "fund_proposals"]
