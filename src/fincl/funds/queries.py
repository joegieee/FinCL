"""Stage 4A — the sinking-funds read.

One read composes what already exists: the Stage 2A liquid position for total
cash, the Stage 2B obligations adapter and the Stage 1 estimator for proposals
and for the minimum reserve, the v0.6 goal records for manually recorded
targets, and the schema-21 projections for confirmed funds.

Every fund keeps four things apart, and the shape says so:

* ``target``                — the sourced amount due and its basis;
* ``planned_contributions`` — what *would* be contributed on the remaining
  dates.  A plan, not money;
* ``recorded_allocation``   — the contributions that were actually journalled.
  This alone is the earmark; and
* ``earmark``               — what remains earmarked after any release.

Nothing promotes one into another as time passes.  A contribution date going by
without a journalled contribution leaves the recorded allocation exactly where
it was and the shortfall exactly as large, which is the whole point.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from .. import clock
from ..baseline.estimators import baseline_estimate
from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..money import ExactMoney, format_decimal
from ..obligations.queries import obligations_page
from ..planning.queries import goals_page
from ..position.queries import position_page
from .models import (
    ALLOCATION_IS_NOT_CASH_RULE,
    CENTRAL_INVARIANT,
    CONTRIBUTION_ROUNDING_RULE,
    MANUAL_BASES,
    CashSeparation,
    contribution_plan,
)
from .proposals import PROPOSAL_POLICY, RECURRENCE_POLICY, fund_proposals

STATUS_RELEASED = "released"
STATUS_OVERDUE = "overdue"
STATUS_DUE_NOW = "due_now"
STATUS_SCHEDULED = "scheduled"
STATUS_INCOMPLETE = "incomplete"


def _as_of(value: str | None) -> str:
    if value is None:
        return clock.today().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise FinCLError("as_of must be an ISO calendar date (YYYY-MM-DD)") from exc


def _generation(connection: sqlite3.Connection) -> str:
    row = current_generation(connection)
    if row is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    return str(row["generation_token"])


def _fund_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    funds: list[dict[str, Any]] = []
    for row in connection.execute("SELECT * FROM sinking_funds ORDER BY fund"):
        allocations = connection.execute(
            "SELECT allocation, allocated_on, amount_coefficient, amount_scale "
            "FROM sinking_fund_allocations WHERE fund = ? AND retired = 0 "
            "ORDER BY allocated_on, allocation",
            (row["fund"],),
        ).fetchall()
        release = connection.execute(
            "SELECT released_on, amount_coefficient, amount_scale "
            "FROM sinking_fund_releases WHERE fund = ?",
            (row["fund"],),
        ).fetchone()
        funds.append(
            {
                "fund": str(row["fund"]),
                "name": str(row["name"]),
                "currency": str(row["currency"]),
                "amount_due": (
                    None
                    if row["amount_due_coefficient"] is None
                    else format_decimal(
                        int(row["amount_due_coefficient"]), int(row["amount_due_scale"])
                    )
                ),
                "due_on": row["due_on"],
                "amount_basis": str(row["amount_basis"]),
                "date_basis": str(row["date_basis"]),
                "source_reference": str(row["source_reference"]),
                "contribution_dates": list(json.loads(row["contribution_dates_json"])),
                "linked_event_id": row["linked_event_id"],
                "allocations": [
                    {
                        "allocation": str(item["allocation"]),
                        "allocated_on": str(item["allocated_on"]),
                        "amount": format_decimal(
                            int(item["amount_coefficient"]), int(item["amount_scale"])
                        ),
                    }
                    for item in allocations
                ],
                "release": (
                    None
                    if release is None
                    else {
                        "released_on": str(release["released_on"]),
                        "amount": format_decimal(
                            int(release["amount_coefficient"]),
                            int(release["amount_scale"]),
                        ),
                    }
                ),
            }
        )
    return funds


def _scale_for(values: list[str]) -> int:
    return max([2, *(ExactMoney.parse(value).scale for value in values)])


def _minor(value: str, scale: int) -> int:
    return ExactMoney.parse(value).rescaled_coefficient(scale)


def _fund_cell(raw: dict[str, Any], *, scale: int, as_of: str) -> dict[str, Any]:
    """One fund with its target, plan, recorded allocation and earmark apart."""
    currency = str(raw["currency"])
    allocated = sum(_minor(item["amount"], scale) for item in raw["allocations"])
    released = (
        _minor(raw["release"]["amount"], scale) if raw["release"] is not None else 0
    )
    outstanding_earmark = max(0, allocated - released)
    amount_due_minor = (
        None if raw["amount_due"] is None else _minor(str(raw["amount_due"]), scale)
    )
    due_on = raw["due_on"]
    # A remaining contribution date is one that has not passed and does not sit
    # after the due date. A date already gone is not remaining, and nothing in
    # its passing says money was contributed on it.
    remaining_dates = sorted(
        day
        for day in raw["contribution_dates"]
        if day > as_of and (due_on is None or day <= due_on)
    )
    plan = contribution_plan(
        amount_due_minor=amount_due_minor,
        allocated_now_minor=allocated,
        remaining_dates=remaining_dates,
    )
    shortfall = plan["shortfall_minor"]

    if raw["release"] is not None:
        status = STATUS_RELEASED
    elif amount_due_minor is None or due_on is None:
        status = STATUS_INCOMPLETE
    elif due_on < as_of:
        status = STATUS_OVERDUE
    elif due_on == as_of:
        status = STATUS_DUE_NOW
    else:
        status = STATUS_SCHEDULED

    # Due now and overdue are still obligations: the fund is not refused, it
    # reports what has to be found today.
    immediate = None
    if status in (STATUS_OVERDUE, STATUS_DUE_NOW) and shortfall is not None:
        immediate = {
            "amount": format_decimal(shortfall, scale),
            "reason": (
                "the due date has passed and the expense is still owed"
                if status == STATUS_OVERDUE
                else "the expense is due today"
            ),
            "remains_an_obligation": True,
            "note": (
                "no contribution schedule can be offered for a date that has "
                "arrived; this amount is required now, and the fund stays visible"
            ),
        }

    manual = raw["amount_basis"] in MANUAL_BASES or raw["date_basis"] in MANUAL_BASES
    return {
        "fund": raw["fund"],
        "name": raw["name"],
        "currency": currency,
        "status": status,
        "target": {
            "available": amount_due_minor is not None,
            "amount_due": raw["amount_due"],
            "amount_basis": raw["amount_basis"],
            "due_on": due_on,
            "date_basis": raw["date_basis"],
            "source_reference": raw["source_reference"],
            "reason": (
                None
                if amount_due_minor is not None and due_on is not None
                else "the fund keeps an unknown amount or due date rather than a "
                "guessed one; its matching irregular allowance is retained"
            ),
            "manual_basis": manual,
            "manual_basis_disclosure": (
                "this target rests on a manually recorded goal; neither a goal "
                "target nor its saved field is evidence that cash exists"
                if manual
                else None
            ),
        },
        "planned_contributions": {
            "available": plan["available"],
            "reason": plan["reason"],
            "all_contribution_dates": list(raw["contribution_dates"]),
            "remaining_dates": remaining_dates,
            "required_contribution": (
                None
                if plan["required_contribution_minor"] is None
                else format_decimal(plan["required_contribution_minor"], scale)
            ),
            "final_contribution": (
                None
                if plan["final_contribution_minor"] is None
                else format_decimal(plan["final_contribution_minor"], scale)
            ),
            "schedule": [
                {"on": item["on"], "amount": format_decimal(item["amount_minor"], scale)}
                for item in plan["schedule"]
            ],
            "sums_exactly": plan["sums_exactly"],
            "rounding_rule": CONTRIBUTION_ROUNDING_RULE,
            "is_a_plan_not_money": True,
        },
        "recorded_allocation": {
            "allocated_now": format_decimal(allocated, scale),
            "entries": raw["allocations"],
            "basis": "journalled contributions actually recorded against this fund",
            "rule": ALLOCATION_IS_NOT_CASH_RULE,
        },
        "earmark": {
            "outstanding": format_decimal(outstanding_earmark, scale),
            "released": format_decimal(released, scale),
            "release": raw["release"],
            "sits_within_liquid_cash": True,
        },
        "shortfall": None if shortfall is None else format_decimal(shortfall, scale),
        "fully_allocated": shortfall == 0 if shortfall is not None else None,
        "insufficient_allocation": (
            None
            if shortfall is None
            else {
                "amount": format_decimal(shortfall, scale),
                "visible": True,
                "note": (
                    "the recorded allocation does not cover the amount due; the "
                    "gap is shown rather than smoothed away"
                ),
            }
            if shortfall > 0
            else None
        ),
        "immediate_funding_requirement": immediate,
        "linked_event_id": raw["linked_event_id"],
        "outstanding_earmark_minor": outstanding_earmark,
    }


def sinking_funds_page(
    database: Path, *, as_of: str | None = None, today: date | None = None
) -> dict[str, Any]:
    """Proposals, confirmed funds, and cash held apart from its earmarks."""
    as_of = _as_of(as_of)
    today = today or date.fromisoformat(as_of)
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection)
        raw_funds = _fund_rows(connection)
    finally:
        connection.close()

    estimate = baseline_estimate(database, today=today)
    obligations = obligations_page(database, as_of=as_of, today=today)
    goals = goals_page(database, as_of=as_of)
    position = position_page(database, today=today)

    limitations: list[str] = []
    reported = {
        "obligations_page": obligations.get("generation"),
        "goals_page": goals.get("generation"),
        "baseline_estimate": estimate.get("generation") if estimate.get("available") else None,
    }
    mismatched = {
        name: value
        for name, value in reported.items()
        if value is not None and value != token
    }
    if mismatched:
        limitations.append(
            "mixed-generation source adapters are not combined: "
            + ", ".join(f"{name}={value}" for name, value in sorted(mismatched.items()))
        )

    values: list[str] = []
    for fund in raw_funds:
        if fund["amount_due"] is not None:
            values.append(str(fund["amount_due"]))
        values.extend(item["amount"] for item in fund["allocations"])
        if fund["release"] is not None:
            values.append(str(fund["release"]["amount"]))
    liquid_total = dict(position.get("liquid_total", {}))
    values.extend(str(amount) for amount in liquid_total.values())
    reserves: dict[str, str] = {}
    if estimate.get("available"):
        for currency, cell in estimate.get("by_currency", {}).items():
            reserve = cell.get("reserve", {})
            if reserve.get("available") and reserve.get("amount") is not None:
                reserves[str(currency)] = str(reserve["amount"])
    values.extend(reserves.values())
    scale = _scale_for(values)

    funds = [_fund_cell(raw, scale=scale, as_of=as_of) for raw in raw_funds]

    by_currency: dict[str, Any] = {}
    currencies = sorted(
        {fund["currency"] for fund in funds}
        | set(liquid_total)
        | set(reserves)
    )
    for currency in currencies:
        scoped = [fund for fund in funds if fund["currency"] == currency]
        earmarked = sum(fund["outstanding_earmark_minor"] for fund in scoped)
        cash = liquid_total.get(currency)
        reserve_minor = _minor(reserves[currency], scale) if currency in reserves else 0
        if cash is None:
            by_currency[currency] = {
                "available": False,
                "reason": (
                    position.get("liquid_total_withheld", {}).get(currency)
                    or "no supported liquid cash total for this currency"
                ),
                "earmarked_funds": format_decimal(earmarked, scale),
                "total_cash": None,
                "unallocated_cash": None,
                "note": (
                    "the earmarks are reported without a cash separation rather "
                    "than against an assumed balance"
                ),
                "funds": [fund["fund"] for fund in scoped],
            }
            continue
        separation = CashSeparation(
            currency=currency,
            total_cash_minor=_minor(str(cash), scale),
            earmarked_minor=earmarked,
            reserve_minor=reserve_minor,
        )
        by_currency[currency] = {
            "available": True,
            "reason": None,
            **separation.as_dict(scale),
            "minimum_reserve_basis": (
                "Stage 1 reserve target" if currency in reserves else "no supported reserve"
            ),
            "funds": [
                {
                    "fund": fund["fund"],
                    "earmarked": fund["earmark"]["outstanding"],
                }
                for fund in scoped
            ],
        }

    for fund in funds:
        cell = by_currency.get(fund["currency"], {})
        fund["unfunded_warning"] = (
            cell.get("unfunded_warning") if cell.get("unfunded") else None
        )
        del fund["outstanding_earmark_minor"]

    proposals = fund_proposals(
        estimate=estimate,
        obligations=obligations,
        goals=goals,
        confirmed_source_references={fund["source_reference"] for fund in raw_funds},
    )

    return {
        "generation": token,
        "as_of": as_of,
        "scale": scale,
        "proposals": proposals,
        "funds": funds,
        "cash_separation": {
            "by_currency": by_currency,
            "invariant": CENTRAL_INVARIANT,
            "currency_policy": "unlike currencies are never added",
            "cash_basis": {
                "source": "Stage 2A liquid position",
                "supported_cutoff": position.get("supported_cutoff"),
                "current_position_supported": position.get(
                    "current_position_supported"
                ),
                "reasons": position.get("current_position_reasons", []),
            },
        },
        "rules": [
            CENTRAL_INVARIANT,
            ALLOCATION_IS_NOT_CASH_RULE,
            CONTRIBUTION_ROUNDING_RULE,
            PROPOSAL_POLICY,
            RECURRENCE_POLICY,
            "a due-now or overdue expense stays an obligation: the fund reports "
            "an immediate funding requirement instead of being refused",
            "an allocation sits within liquid cash; one that exceeds the "
            "available unallocated cash is kept and warned about, never trimmed "
            "to fit",
            "scheduling contributions moves no cash on any day, so a schedule "
            "can never make an insolvent cash path solvent",
        ],
        "limitations": limitations,
    }


__all__ = ["sinking_funds_page"]
