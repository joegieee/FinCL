"""Reported Forecast workspace (plan M4 §7; step 14).

A thin connection-accepting adapter over the *native* ``cash_timeline_from``
reader. It performs no independent scenario/timeline calculation of its own:
every native fact (starting balance, cash-cutoff bridge status, monthly and
daily walks, milestones, source events, limitations) is read straight
through unchanged, and this module only attaches reported equivalents and
two new cross-currency aggregates.

``forecast_closings`` is the exact same *aggregate closing* calculation
``money_flow.py``'s Overview forecast card already uses -- moved here so
both callers share one owner instead of the Overview response independently
rounding a second copy of the same figure (§3.3: "Share the owner
calculation and snapshot protocol, not independently rounded Overview
response values").

Per-currency reported equivalents (starting balance, monthly rows, source
events) reuse *one* resolved rate per currency -- both scenarios and every
amount in that currency are held at the same issue-date rate (§3.3: "value
both base and conservative under one issue-date held-rate policy and
inventory"), so a single ``value_one`` call per currency is repriced onto
every other same-currency amount rather than re-querying the rate table for
each of the hundreds of day/month/event figures a six-month horizon holds.

The combined minimum is the one genuinely new cross-currency metric this
step adds (§7 M4 extrema rule): the exact reporting-currency total of every
usable currency's own *same calendar day* low point, minimised over the
whole aligned horizon -- never a sum of each currency's own independently
timed lowest day. It is available only when every currency this ledger
holds cash in is both natively forecastable and priceable; otherwise it is
withheld as unavailable while the (possibly partial) closing totals remain.
No combined breach date, percentage or flag is added, per the plan's own
scope line for this step.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..money import ExactMoney
from ..reporting_context import ReportedScope, ReportingContext, build_reporting_context
from ..valuation import (
    FORWARD_MONTHLY_CLOSED_V1,
    ConsolidatedValuation,
    ConsolidationStatus,
    ExactValuation,
    ExcludedScopeDetail,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    ValuationScope,
    consolidate,
    value_one,
)
from .engine import SCENARIO_BASE, SCENARIO_CONSERVATIVE, cash_timeline_from, month_of

__all__ = [
    "CombinedMinimum",
    "ForecastClosingResult",
    "ReportedCurrencyForecast",
    "ReportedEvent",
    "ReportedForecastPage",
    "ReportedForecastTotals",
    "ReportedMonthRow",
    "ReportedScenarioCell",
    "forecast_closings",
    "reported_forecast_page",
    "reported_forecast_page_from",
]

# Distinct from every rate-availability reason ``valuation.py`` can produce
# (§4.3: "keep evidence gaps distinct from rate gaps") -- names a *pocket*
# with no supported balance/forecast at all, never a missing FX row.
_NO_FORECAST_EVIDENCE = "NO_SUPPORTED_FORECAST_EVIDENCE"
# The extrema rule's own withholding reason (§7 M4): distinct from a single
# pocket's own missing evidence/FX -- this names the *combined* metric.
_NO_COMBINED_MINIMUM = "NO_COMBINED_MINIMUM_EVIDENCE"
_COMBINED_MINIMUM_REMEDY = (
    "Every pocket needs both a supported forecast and an importable rate "
    "before a combined minimum can be shown; otherwise summing would risk "
    "hiding the true worst day behind a currency that could not be priced."
)


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


def forecast_closings(
    connection: Any, *, context: ReportingContext, today: date, pocket_currency: str | None,
) -> tuple[ForecastClosingResult, str | None, tuple[str, ...]]:
    """The shared aggregate forecast-closing calculation (§3.3).

    Reused verbatim by ``money_flow.py``'s Overview card and by
    :func:`reported_forecast_page_from`'s own totals -- one owner, not two
    independently rounded copies of the same figure.
    """
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


def _as_fraction(value: str) -> Fraction:
    money = ExactMoney.parse(value)
    return Fraction(money.coefficient, 10 ** money.scale)


def _multiplier(basis: ExactValuation) -> Fraction:
    """The exact held rate ``basis`` resolved, for reuse on other same
    -currency amounts without a second rate lookup (§3.3: one issue-date
    held-rate policy applies uniformly to every amount in that currency,
    not only the one this ``basis`` happened to price)."""
    if not basis.rate_path:
        return Fraction(1)
    rate_input = basis.rate_path[0]
    return Fraction(rate_input.rate_numerator, rate_input.rate_denominator)


def _reprice(basis: ExactValuation, *, amount: str) -> ExactValuation:
    """Apply ``basis``'s already-resolved held rate to a different native
    amount in the same currency -- exact, since the FORECAST held rate does
    not vary by amount or date within one issue-date context."""
    native = ExactMoney.parse(amount)
    if basis.unavailable is not None:
        return dataclasses.replace(basis, native=native, reported_exact=None)
    native_fraction = Fraction(native.coefficient, 10 ** native.scale)
    return dataclasses.replace(basis, native=native, reported_exact=native_fraction * _multiplier(basis))


@dataclass(frozen=True, slots=True)
class ReportedMonthRow:
    month: str
    opening: ExactValuation
    closing: ExactValuation
    income: ExactValuation
    expense: ExactValuation
    lowest_balance: ExactValuation
    # The observed-dispersion band around this month's closing balance, repriced
    # at the same basis as the balance itself so the three figures are directly
    # comparable. ``None`` when the native row has no band -- a month whose
    # unscheduled categories have no observed spread, or a scenario already
    # running at its own observed high.
    closing_low: ExactValuation | None = None
    closing_high: ExactValuation | None = None


@dataclass(frozen=True, slots=True)
class ReportedEvent:
    event_id: str
    source_id: str
    amount: ExactValuation


@dataclass(frozen=True, slots=True)
class ReportedScenarioCell:
    closing_balance: ExactValuation
    lowest_balance: ExactValuation
    months: tuple[ReportedMonthRow, ...]
    events: tuple[ReportedEvent, ...]


@dataclass(frozen=True, slots=True)
class ReportedCurrencyForecast:
    """One native currency cell (unchanged, via ``native``) plus its own
    reported equivalents. Only present for currencies whose native cell is
    ``available`` (both scenarios succeeded) -- an unavailable currency has
    nothing to convert and stays native-only, disclosed through ``native``."""

    currency: str
    starting_balance: ExactValuation
    base: ReportedScenarioCell
    conservative: ReportedScenarioCell


def _reported_scenario_cell(cell: Mapping[str, Any], *, basis: ExactValuation) -> ReportedScenarioCell:
    months = tuple(
        ReportedMonthRow(
            month=row["month"],
            opening=_reprice(basis, amount=row["opening"]),
            closing=_reprice(basis, amount=row["closing"]),
            income=_reprice(basis, amount=row["income"]),
            expense=_reprice(basis, amount=row["expense"]),
            lowest_balance=_reprice(basis, amount=row["lowest_balance"]["amount"]),
            closing_low=(
                _reprice(basis, amount=row["observed_range"]["closing"]["low"])
                if row.get("observed_range") else None
            ),
            closing_high=(
                _reprice(basis, amount=row["observed_range"]["closing"]["high"])
                if row.get("observed_range") else None
            ),
        )
        for row in cell["months"]
    )
    events = tuple(
        ReportedEvent(
            event_id=event["event_id"], source_id=event["source_id"],
            amount=_reprice(basis, amount=event["amount"]),
        )
        for event in cell["events"]["items"]
    )
    return ReportedScenarioCell(
        closing_balance=_reprice(basis, amount=cell["closing_balance"]),
        lowest_balance=_reprice(basis, amount=cell["lowest_balance"]["amount"]),
        months=months, events=events,
    )


@dataclass(frozen=True, slots=True)
class CombinedMinimum:
    """§7 M4 extrema rule: the exact combined low, aligned by calendar day
    across every usable currency, then minimised -- never a sum of each
    currency's own independently timed low. ``month`` names the calendar
    month the combined low day falls in (the owner-defined checkpoint this
    is aligned to); no combined breach date, percentage or flag is added."""

    result: ConsolidatedValuation
    month: str | None


def _combined_minimum(
    by_currency: Mapping[str, Any], *, name: str, all_currencies: Sequence[str],
    bases: Mapping[str, ExactValuation], display_context: ValuationContext,
) -> CombinedMinimum:
    usable = [currency for currency in all_currencies if currency in bases and bases[currency].unavailable is None]
    if not all_currencies or set(usable) != set(all_currencies):
        missing = sorted(set(all_currencies) - set(usable)) or ["*"]
        excluded = tuple(ValuationScope(currency=currency, period=None) for currency in missing)
        excluded_detail = tuple(
            ExcludedScopeDetail(scope=scope, entry_count=1, reasons=(_NO_COMBINED_MINIMUM,), remedies=(_COMBINED_MINIMUM_REMEDY,))
            for scope in excluded
        )
        result = ConsolidatedValuation(
            total=None, status=ConsolidationStatus.UNAVAILABLE, partial_total=None,
            included=(), excluded=excluded, excluded_entry_count=len(excluded),
            unavailable_reasons=(_NO_COMBINED_MINIMUM,), excluded_detail=excluded_detail,
            rate_inputs=(), context=display_context, values=(),
        )
        return CombinedMinimum(result=result, month=None)

    day_lists = {currency: by_currency[currency][name]["days"] for currency in usable}
    day_count = len(next(iter(day_lists.values())))
    lowest_total: Fraction | None = None
    lowest_day: str | None = None
    for index in range(day_count):
        total = sum(
            (_as_fraction(day_lists[currency][index]["low_point"]) * _multiplier(bases[currency]) for currency in usable),
            start=Fraction(0),
        )
        day_date = next(iter(day_lists.values()))[index]["date"]
        if lowest_total is None or total < lowest_total:
            lowest_total, lowest_day = total, day_date

    included = tuple(sorted((ValuationScope(currency=currency, period=lowest_day) for currency in usable), key=_scope_key))
    rate_inputs = tuple(dict.fromkeys(rate for currency in usable for rate in bases[currency].rate_path))
    result = ConsolidatedValuation(
        total=lowest_total, status=ConsolidationStatus.AVAILABLE, partial_total=None,
        included=included, excluded=(), excluded_entry_count=0, unavailable_reasons=(),
        excluded_detail=(), rate_inputs=rate_inputs, context=display_context, values=(),
    )
    return CombinedMinimum(result=result, month=month_of(lowest_day) if lowest_day else None)


@dataclass(frozen=True, slots=True)
class ReportedForecastTotals:
    base_closing: ConsolidatedValuation
    conservative_closing: ConsolidatedValuation
    base_combined_minimum: CombinedMinimum
    conservative_combined_minimum: CombinedMinimum


@dataclass(frozen=True, slots=True)
class ReportedForecastPage:
    context: ReportingContext
    native: Mapping[str, Any]
    currencies: tuple[ReportedCurrencyForecast, ...]
    totals: ReportedForecastTotals
    opening_balance_as_of: str | None
    valuation_as_of: date
    horizon_end: str | None
    limitations: tuple[str, ...]


def reported_forecast_page(database: Path, *, today: date) -> ReportedForecastPage:
    connection = connect_readonly(database)
    try:
        return reported_forecast_page_from(connection, today=today)
    finally:
        connection.close()


def reported_forecast_page_from(connection: Any, *, today: date) -> ReportedForecastPage:
    """The reported Forecast workspace read (§3.3, §7 M4; step 14).

    Reuses :func:`cash_timeline_from` for every native fact and
    :func:`forecast_closings` for the shared aggregate closing totals (the
    same function ``money_flow.py``'s Overview card calls). Takes no history
    parameter at all: Forecast's horizon is owner-defined from the supported
    cash cutoff, never a caller-supplied window (§7 M4: "A history filter
    must not change horizon").
    """
    scope = ReportedScope(valuation_as_of=today)
    context = build_reporting_context(connection, scope=scope)

    native = cash_timeline_from(connection, today=today)
    by_currency: Mapping[str, Any] = native.get("by_currency") or {}
    all_currencies = sorted(by_currency)

    reported: dict[str, ReportedCurrencyForecast] = {}
    for currency in all_currencies:
        cell = by_currency[currency]
        if not isinstance(cell, dict) or not cell.get("available"):
            continue
        basis = value_one(
            connection,
            ValuationItem(
                native=ExactMoney.parse(str(cell["starting_balance"])), native_currency=currency,
                purpose=ValuationPurpose.FORECAST,
            ),
            context=ValuationContext(
                reporting_currency=context.reporting_currency, purpose=ValuationPurpose.FORECAST,
                valuation_as_of=today, rate_selection_policy=FORWARD_MONTHLY_CLOSED_V1,
            ),
        )
        reported[currency] = ReportedCurrencyForecast(
            currency=currency, starting_balance=basis,
            base=_reported_scenario_cell(cell[SCENARIO_BASE], basis=basis),
            conservative=_reported_scenario_cell(cell[SCENARIO_CONSERVATIVE], basis=basis),
        )

    display_context = ValuationContext(
        reporting_currency=context.reporting_currency, purpose=ValuationPurpose.FORECAST,
        valuation_as_of=today, rate_selection_policy=FORWARD_MONTHLY_CLOSED_V1,
    )
    bases = {currency: cell.starting_balance for currency, cell in reported.items()}
    base_minimum = _combined_minimum(
        by_currency, name=SCENARIO_BASE, all_currencies=all_currencies, bases=bases, display_context=display_context,
    )
    conservative_minimum = _combined_minimum(
        by_currency, name=SCENARIO_CONSERVATIVE, all_currencies=all_currencies, bases=bases, display_context=display_context,
    )

    closing_result, _cutoff, closing_limitations = forecast_closings(
        connection, context=context, today=today, pocket_currency=None,
    )

    totals = ReportedForecastTotals(
        base_closing=closing_result.base, conservative_closing=closing_result.conservative,
        base_combined_minimum=base_minimum, conservative_combined_minimum=conservative_minimum,
    )
    return ReportedForecastPage(
        context=context, native=native, currencies=tuple(reported[currency] for currency in sorted(reported)),
        totals=totals, opening_balance_as_of=closing_result.opening_balance_as_of,
        valuation_as_of=today, horizon_end=closing_result.horizon_end,
        limitations=closing_limitations,
    )
