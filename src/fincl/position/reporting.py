"""Reported Cash workspace (plan M4 §3.3, §7 M4; step 12).

A thin connection-accepting adapter over the *native* liquid-position reader
(:func:`position_page_from`). It performs no independent balance/coverage
calculation of its own: every native fact (basis, as-of date, staleness,
owner exclusions, reconciliation status, unsupported intervals) is read
straight through unchanged, and this module only attaches a reported
equivalent to each native amount it can value.

``cash_at_cutoff`` is the exact same *aggregate* calculation
``money_flow.py``'s Overview cash card already uses -- moved here so both
callers share one owner instead of the Overview response independently
rounding a second copy of the same figure (§3.3: "Share the owner
calculation and snapshot protocol, not independently rounded Overview
response values").

Per-pocket reported equivalents are a second, additive thing: each pocket's
own last supported native balance (and, separately, its common-cutoff
*derived* balance) is valued at *its own* as-of date under
``position-monthly-closed-v1`` -- never summed across pockets with different
dates into a purported same-date total. A pocket with no native balance at
all yields ``None`` (missing *balance evidence*); a pocket with a native
balance but no importable rate yields a valued-but-``unavailable`` result
(missing *FX*) -- the two must never collapse into one explanation (§3.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..money import ExactMoney
from ..reporting_context import ReportedScope, ReportingContext, build_reporting_context
from ..valuation import (
    POSITION_MONTHLY_CLOSED_V1,
    ConsolidatedValuation,
    ConsolidationStatus,
    ExactValuation,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    consolidate,
    value_position_at_own_date,
)
from .queries import position_page_from

__all__ = [
    "CashAtCutoffResult",
    "ReportedCashPage",
    "ReportedCashPocket",
    "cash_at_cutoff",
    "reported_cash_page",
    "reported_cash_page_from",
]


@dataclass(frozen=True, slots=True)
class CashAtCutoffResult:
    """§3.3: supported liquid cash at the owner's common cutoff, never each
    account's own latest (differently dated) balance."""

    result: ConsolidatedValuation
    balance_as_of: str | None
    valuation_as_of: date


def cash_at_cutoff(
    connection: Any, *, context: ReportingContext, today: date, pocket_currency: str | None,
) -> tuple[CashAtCutoffResult, str | None]:
    """The shared aggregate cash-at-cutoff calculation (§3.3).

    Reused verbatim by ``money_flow.py``'s Overview card and by
    :func:`reported_cash_page_from`'s own aggregate card -- one owner, not two
    independently rounded copies of the same figure.
    """
    position = position_page_from(connection, today=today)
    cutoff = position.get("supported_cutoff")
    combined: dict[str, str] = dict(position.get("combined_liquid_position") or {})
    if pocket_currency is not None:
        combined = {currency: amount for currency, amount in combined.items() if currency == pocket_currency}

    if cutoff is None or not combined:
        reasons = tuple(position.get("current_position_reasons") or ()) or (
            "no supported liquid account pockets share a common balance cutoff",
        )
        context_at_today = ValuationContext(
            reporting_currency=context.reporting_currency, purpose=ValuationPurpose.POSITION,
            valuation_as_of=today, rate_selection_policy=POSITION_MONTHLY_CLOSED_V1,
        )
        result = ConsolidatedValuation(
            total=None, status=ConsolidationStatus.UNAVAILABLE, partial_total=None,
            included=(), excluded=(), excluded_entry_count=0,
            unavailable_reasons=reasons, excluded_detail=(),
            rate_inputs=(), context=context_at_today, values=(),
        )
        return CashAtCutoffResult(result=result, balance_as_of=cutoff, valuation_as_of=today), cutoff

    cutoff_date = date.fromisoformat(str(cutoff))
    position_context = ValuationContext(
        reporting_currency=context.reporting_currency, purpose=ValuationPurpose.POSITION,
        valuation_as_of=cutoff_date, rate_selection_policy=POSITION_MONTHLY_CLOSED_V1,
    )
    items = [
        ValuationItem(
            native=ExactMoney.parse(str(amount)), native_currency=currency, purpose=ValuationPurpose.POSITION,
        )
        for currency, amount in sorted(combined.items())
    ]
    result = consolidate(connection, items, context=position_context)
    return CashAtCutoffResult(result=result, balance_as_of=cutoff, valuation_as_of=cutoff_date), cutoff


def _pocket_valuation(
    connection: Any, *, context: ReportingContext, amount: str | None, currency: str, as_of: str | None,
) -> ExactValuation | None:
    """One pocket's own native amount, valued at its own ``as_of`` date.

    A thin wrapper over the shared :func:`value_position_at_own_date` (§3.3,
    §7 M4) -- Net worth (``networth/reporting.py``) values its own dated
    native components through that same shared function, so this and that
    module cannot silently diverge on what "position at its own date" means.
    """
    return value_position_at_own_date(
        connection, reporting_currency=context.reporting_currency, amount=amount, currency=currency, as_of=as_of,
    )


@dataclass(frozen=True, slots=True)
class ReportedCashPocket:
    """One native pocket row (unchanged) plus its own reported equivalents."""

    native: Mapping[str, Any]
    reported_balance: ExactValuation | None
    reported_derived_balance: ExactValuation | None


@dataclass(frozen=True, slots=True)
class ReportedCashPage:
    context: ReportingContext
    position: Mapping[str, Any]
    combined: CashAtCutoffResult
    pockets: tuple[ReportedCashPocket, ...]


def reported_cash_page(
    database: Path, *, today: date, pocket_currency: str | None = None,
) -> ReportedCashPage:
    connection = connect_readonly(database)
    try:
        return reported_cash_page_from(connection, today=today, pocket_currency=pocket_currency)
    finally:
        connection.close()


def reported_cash_page_from(
    connection: Any, *, today: date, pocket_currency: str | None = None,
) -> ReportedCashPage:
    """The reported Cash workspace read (§3.3; plan M4 step 12).

    Reuses :func:`position_page_from` for every native fact -- owner
    exclusions, reconciliation status, unsupported intervals and the cash
    bridge stay untouched -- and attaches only a reported equivalent to each
    priceable native amount. ``pocket_currency`` is an optional, purely local
    record filter (§6.3's Account-currency filter), independent of the
    ledger's resolved reporting currency, applied to which native pockets are
    considered/returned; it never changes which reporting currency is used.
    """
    scope = ReportedScope(valuation_as_of=today, pocket_currency=pocket_currency)
    context = build_reporting_context(connection, scope=scope)

    position = position_page_from(connection, today=today)
    native_pockets = list(position.get("pockets") or [])
    if pocket_currency is not None:
        native_pockets = [p for p in native_pockets if p["currency_pocket"] == pocket_currency]

    pockets = tuple(
        ReportedCashPocket(
            native=pocket,
            reported_balance=_pocket_valuation(
                connection, context=context, amount=pocket.get("balance"),
                currency=pocket["currency_pocket"], as_of=pocket.get("as_of"),
            ),
            reported_derived_balance=_pocket_valuation(
                connection, context=context, amount=pocket.get("derived_balance"),
                currency=pocket["currency_pocket"], as_of=pocket.get("derived_as_of"),
            ),
        )
        for pocket in native_pockets
    )

    combined_result, _cutoff = cash_at_cutoff(
        connection, context=context, today=today, pocket_currency=pocket_currency,
    )
    if pocket_currency is not None:
        position = dict(position)
        position["pockets"] = native_pockets
        position["included_pockets"] = [p for p in native_pockets if p["liquid"]]
        position["missing_intervals"] = [
            gap for gap in position.get("missing_intervals") or ()
            if gap["currency_pocket"] == pocket_currency
        ]
        for field in ("combined_liquid_position", "liquid_total", "liquid_total_withheld"):
            position[field] = {
                currency: amount for currency, amount in (position.get(field) or {}).items()
                if currency == pocket_currency
            }

    return ReportedCashPage(context=context, position=position, combined=combined_result, pockets=pockets)
