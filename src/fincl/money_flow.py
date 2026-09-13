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

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .analysis.reporting import GrossBurnResult, SummaryEntry, reported_analysis_page_from
from .coverage import GROSS_BURN_DEFINITION_VERSION
from .db import connect_readonly
from .forecast.reporting import ForecastClosingResult, forecast_closings
from .position.reporting import CashAtCutoffResult, cash_at_cutoff
from .reporting_context import ReportedScope, ReportingContext, build_reporting_context
from .valuation import (
    FLOW_RATE_V1,
    FORWARD_MONTHLY_CLOSED_V1,
    POSITION_MONTHLY_CLOSED_V1,
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
    forecast_result, forecast_cutoff, forecast_limitations = forecast_closings(
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
