"""Composite Overview: ``money_flow_page_from`` (§3.3-3.4, §6.2; plan M3b.3 step 09).

Assembles the four reported Overview cards from *existing* owners under one
pinned generation/connection/read transaction:

* economic flow (in/out/net/exceptional) and gross burn reuse the shared
  reported-Analysis reader (``analysis/reporting.py``) verbatim -- this module
  performs no second economic-flow calculation;
* cash at cutoff reuses ``position_page_from``'s owner-derived common cutoff
  and combined liquid balances, valued once under ``POSITION`` at that cutoff;
* both forecast closings reuse ``cash_timeline_from``'s native six-month
  scenarios, valued once under ``FORECAST`` held at the request/issue date.

No screen reads this module for its own economic arithmetic: every number
here is a native total this module only revalues and consolidates, per §6.1
("reuse their core calculations, not their formatted response totals").
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .analysis.reporting import GrossBurnResult, SummaryEntry, reported_analysis_page_from
from .coverage import GROSS_BURN_DEFINITION_VERSION
from .db import connect_readonly
from .forecast.engine import SCENARIO_BASE, SCENARIO_CONSERVATIVE, cash_timeline_from
from .money import ExactMoney
from .position.reporting import CashAtCutoffResult, cash_at_cutoff
from .reporting_context import ReportedScope, ReportingContext, build_reporting_context
from .valuation import (
    FLOW_RATE_V1,
    FORWARD_MONTHLY_CLOSED_V1,
    POSITION_MONTHLY_CLOSED_V1,
    ConsolidatedValuation,
    ConsolidationStatus,
    ExcludedScopeDetail,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    ValuationScope,
    consolidate,
    eligible_month_ceiling,
)

__all__ = [
    "CashAtCutoffResult",
    "ForecastClosingResult",
    "MoneyFlowResult",
    "MoneyFlowSummary",
    "money_flow_page",
    "money_flow_page_from",
]

# Distinct from every rate-availability reason ``valuation.py`` can produce
# (§4.3: "keep evidence gaps distinct from rate gaps") -- these two name a
# *pocket* with no supported balance/forecast at all, never a missing FX row.
_NO_CASH_EVIDENCE = "NO_SUPPORTED_CASH_EVIDENCE"
_NO_FORECAST_EVIDENCE = "NO_SUPPORTED_FORECAST_EVIDENCE"


def _uncovered(context: ValuationContext, *, reason: str) -> ConsolidatedValuation:
    """A card with no priceable evidence at all -- never a fabricated zero."""
    return ConsolidatedValuation(
        total=None, status=ConsolidationStatus.UNAVAILABLE, partial_total=None,
        included=(), excluded=(), excluded_entry_count=0,
        unavailable_reasons=(reason,), excluded_detail=(),
        rate_inputs=(), context=context, values=(),
    )


def _scope_key(scope: ValuationScope) -> tuple[str, str]:
    return (scope.currency, scope.period or "")


def _with_evidence_gaps(
    result: ConsolidatedValuation, *, context: ValuationContext,
    missing_currencies: Sequence[str], reason: str, remedy: str,
) -> ConsolidatedValuation:
    """Fold in pockets excluded for lack of *evidence*, not FX (§3.3 item on
    per-pocket readiness: "the consolidated result cannot be complete simply
    because all included pockets have FX").

    A pocket named here never went through :func:`consolidate` at all -- it
    had no native balance/forecast to hand it in the first place -- so its
    exclusion is merged in afterward rather than being representable as an
    ``ExactValuation.unavailable`` leg (which always prices a *known* native
    amount that merely lacks a rate).
    """
    if not missing_currencies:
        return result
    period = context.valuation_as_of.strftime("%Y-%m")
    extra_scopes = tuple(
        ValuationScope(currency=currency, period=period) for currency in sorted(set(missing_currencies))
    )
    extra_detail = tuple(
        ExcludedScopeDetail(scope=scope, entry_count=1, reasons=(reason,), remedies=(remedy,))
        for scope in extra_scopes
    )
    if result.status is ConsolidationStatus.AVAILABLE:
        total, partial_total = None, result.total
    else:
        total, partial_total = None, result.partial_total
    new_status = ConsolidationStatus.PARTIAL if (result.included or partial_total is not None) else ConsolidationStatus.UNAVAILABLE
    return dataclasses.replace(
        result,
        status=new_status, total=total, partial_total=partial_total,
        excluded=tuple(sorted((*result.excluded, *extra_scopes), key=_scope_key)),
        excluded_detail=tuple(sorted((*result.excluded_detail, *extra_detail), key=lambda d: _scope_key(d.scope))),
        excluded_entry_count=result.excluded_entry_count + len(extra_scopes),
        unavailable_reasons=tuple(sorted({*result.unavailable_reasons, reason})),
    )


def _month_range_desc(anchor: str, earliest: str) -> list[str]:
    """Every ``YYYY-MM`` from ``anchor`` down through ``earliest`` inclusive."""
    year, month = int(anchor[:4]), int(anchor[5:7])
    stop_year, stop_month = int(earliest[:4]), int(earliest[5:7])
    months: list[str] = []
    while True:
        months.append(f"{year:04d}-{month:02d}")
        if (year, month) == (stop_year, stop_month):
            break
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return months


def _burn_excluded_months(burn: GrossBurnResult, *, history_end: str) -> tuple[str, ...]:
    """The gap months skipped while scanning backward for ``burn.included_months``
    (§3.2 item 3): every month strictly between the earliest included month and
    the scan's own starting anchor that did not qualify. Reuses the exact same
    closed-month anchor the burn selector itself starts from; no second
    coverage query is made here."""
    if not burn.included_months:
        return ()
    anchor = eligible_month_ceiling(date.fromisoformat(history_end), closed_month_only=True)
    earliest = min(burn.included_months)
    scanned = _month_range_desc(anchor, earliest)
    included = set(burn.included_months)
    return tuple(month for month in scanned if month not in included)


@dataclass(frozen=True, slots=True)
class ForecastClosingResult:
    """§3.3: both scenarios valued under one issue-date held-rate context.

    ``opening_balance_as_of`` is the owner's cutoff (the same date the cash
    card's balance is drawn from); ``valuation_as_of`` is the request/issue
    date the FORECAST rates are held at. They may legitimately differ from
    the cash card's own ``valuation_as_of`` (§3.3: "reported forecast opening
    cash may differ from the historical cash card solely through FX valuation
    date").
    """

    base: ConsolidatedValuation
    conservative: ConsolidatedValuation
    opening_balance_as_of: str | None
    valuation_as_of: date
    horizon_end: str | None


@dataclass(frozen=True, slots=True)
class MoneyFlowSummary:
    inflow: SummaryEntry
    outflow: SummaryEntry
    net_economic_flow: SummaryEntry
    exceptional_adjustments: SummaryEntry
    gross_burn: GrossBurnResult
    gross_burn_excluded_months: tuple[str, ...]
    cash_at_cutoff: CashAtCutoffResult
    forecast_closing: ForecastClosingResult


@dataclass(frozen=True, slots=True)
class MoneyFlowResult:
    context: ReportingContext
    summary: MoneyFlowSummary
    supported_cash_cutoff: str | None
    first_modelled_day: str | None
    horizon_end: str | None
    cash_stale_days: int | None
    limitations: tuple[str, ...]
    policy_versions: dict[str, str]


def _forecast_closings(
    connection: Any, *, context: ReportingContext, today: date, pocket_currency: str | None,
) -> tuple[ForecastClosingResult, str | None, tuple[str, ...]]:
    timeline = cash_timeline_from(connection, today=today)
    by_currency: dict[str, Any] = dict(timeline.get("by_currency") or {})
    if pocket_currency is not None:
        by_currency = {currency: cell for currency, cell in by_currency.items() if currency == pocket_currency}
    horizon = timeline.get("horizon") or {}
    horizon_end = horizon.get("end")
    limitations = tuple(timeline.get("limitations") or ())

    forecast_context = ValuationContext(
        reporting_currency=context.reporting_currency, purpose=ValuationPurpose.FORECAST,
        valuation_as_of=today, rate_selection_policy=FORWARD_MONTHLY_CLOSED_V1,
    )

    def _scenario(name: str) -> ConsolidatedValuation:
        items: list[ValuationItem] = []
        missing: list[str] = []
        for currency, cell in sorted(by_currency.items()):
            scenario = cell.get(name) if isinstance(cell, dict) else None
            if not cell.get("available") or not isinstance(scenario, dict) or not scenario.get("available"):
                missing.append(currency)
                continue
            items.append(
                ValuationItem(
                    native=ExactMoney.parse(str(scenario["closing_balance"])), native_currency=currency,
                    purpose=ValuationPurpose.FORECAST,
                )
            )
        base_result = (
            consolidate(connection, items, context=forecast_context)
            if items else _uncovered(forecast_context, reason=_NO_FORECAST_EVIDENCE)
        )
        return _with_evidence_gaps(
            base_result, context=forecast_context, missing_currencies=missing, reason=_NO_FORECAST_EVIDENCE,
            remedy="Provide the missing residual-spending or balance evidence for this pocket, then refresh.",
        )

    base = _scenario(SCENARIO_BASE)
    conservative = _scenario(SCENARIO_CONSERVATIVE)
    opening_as_of = next(
        (
            str(cell[SCENARIO_BASE]["starting_balance_as_of"])
            for cell in by_currency.values()
            if isinstance(cell, dict) and cell.get("available") and isinstance(cell.get(SCENARIO_BASE), dict)
        ),
        horizon.get("supported_cash_cutoff"),
    )
    return (
        ForecastClosingResult(
            base=base, conservative=conservative, opening_balance_as_of=opening_as_of,
            valuation_as_of=today, horizon_end=horizon_end,
        ),
        horizon.get("supported_cash_cutoff"),
        limitations,
    )


def money_flow_page(
    database: Path, *, today: date, history_start: str, history_end: str, pocket_currency: str | None = None,
) -> MoneyFlowResult:
    connection = connect_readonly(database)
    try:
        return money_flow_page_from(
            connection, today=today, history_start=history_start, history_end=history_end,
            pocket_currency=pocket_currency,
        )
    finally:
        connection.close()


def money_flow_page_from(
    connection: Any, *, today: date, history_start: str, history_end: str, pocket_currency: str | None = None,
) -> MoneyFlowResult:
    """The composite Overview read (§3.3-3.4, §6.2; step 09).

    One connection, one read transaction (§6.2 composite protocol): the
    caller's own ``connection`` is threaded, unopened and unclosed by this
    function, through every owner below. ``build_reporting_context`` here
    pins the generation *before* any owner is read and raises the typed
    ``STALE_GENERATION_REFRESH_REQUIRED`` refusal with nothing composed if the
    snapshot cannot reproduce the published manifest -- exactly the same
    refusal the reported-Analysis call below independently re-derives from
    the same still-open snapshot, so the two can never disagree.
    """
    scope = ReportedScope(
        valuation_as_of=today, history_start=history_start, history_end=history_end,
        pocket_currency=pocket_currency,
    )
    context = build_reporting_context(connection, scope=scope)

    analysis = reported_analysis_page_from(
        connection, as_of=today, history_start=history_start, history_end=history_end,
        pocket_currency=pocket_currency,
    )
    gross_burn_excluded = _burn_excluded_months(analysis.gross_burn, history_end=history_end)

    cash_result, cash_cutoff = cash_at_cutoff(
        connection, context=context, today=today, pocket_currency=pocket_currency,
    )
    cash_limitations: tuple[str, ...] = ()
    forecast_result, forecast_cutoff, forecast_limitations = _forecast_closings(
        connection, context=context, today=today, pocket_currency=pocket_currency,
    )

    # Both owners derive their cutoff from the same `position_page_from`
    # read of this same connection/today, so they agree by construction; a
    # mismatch would mean one of them observed a different snapshot.
    supported_cutoff = cash_cutoff if cash_cutoff is not None else forecast_cutoff
    first_modelled_day = (
        (date.fromisoformat(supported_cutoff) + timedelta(days=1)).isoformat()
        if supported_cutoff is not None else None
    )
    cash_stale_days = (today - date.fromisoformat(supported_cutoff)).days if supported_cutoff is not None else None

    summary = MoneyFlowSummary(
        inflow=analysis.summary["inflow"],
        outflow=analysis.summary["outflow"],
        net_economic_flow=analysis.summary["net_economic_flow"],
        exceptional_adjustments=analysis.summary["exceptional_adjustments"],
        gross_burn=analysis.gross_burn,
        gross_burn_excluded_months=gross_burn_excluded,
        cash_at_cutoff=cash_result,
        forecast_closing=forecast_result,
    )
    return MoneyFlowResult(
        context=context, summary=summary,
        supported_cash_cutoff=supported_cutoff, first_modelled_day=first_modelled_day,
        horizon_end=forecast_result.horizon_end, cash_stale_days=cash_stale_days,
        limitations=(*cash_limitations, *forecast_limitations),
        policy_versions={
            "reporting_policy": context.policy.policy_version or "",
            "flow": FLOW_RATE_V1,
            "position": POSITION_MONTHLY_CLOSED_V1,
            "forecast": FORWARD_MONTHLY_CLOSED_V1,
            "gross_burn": GROSS_BURN_DEFINITION_VERSION,
        },
    )
