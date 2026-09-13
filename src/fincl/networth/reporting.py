"""Reported Net worth workspace (plan M4 §7; step 13).

A thin connection-accepting adapter over the *native* ``net_worth_page_from``
reader. It performs no independent asset/liability inclusion, double-count
avoidance or manual-estimate logic of its own: every native component
(kind, side, currency, amount, as-of date, basis, origin, label, inclusion,
confidence, note) and every native per-currency native subtotal is read
straight through unchanged. This module only attaches a reported equivalent
to each priceable native component -- each valued at *its own* explicit
as-of date under ``position-monthly-closed-v1`` (§3.3) -- and combines those
independently-dated valuations into one reporting-currency total.

Because native ``assemble_components`` already has the single authority for
each contributing figure (cash from the position read, personal debts from
the obligations read, existing card liability from the cash-conversion
contract -- never a second source for the same obligation), this reader
inherits that de-duplication for free by iterating the native component list
exactly once; it does not re-derive inclusion from a second formula.
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
from ..reporting_context import ReportedScope, ReportingContext, build_reporting_context
from ..valuation import (
    POSITION_MONTHLY_CLOSED_V1,
    ConsolidatedValuation,
    ConsolidationStatus,
    ExactValuation,
    ExcludedScopeDetail,
    ValuationContext,
    ValuationPurpose,
    ValuationScope,
    combine_independently_dated,
    value_position_at_own_date,
)
from .queries import net_worth_page_from

__all__ = [
    "ReportedComponent",
    "ReportedNetWorthPage",
    "ReportedNetWorthTrend",
    "ReportedTotals",
    "reported_net_worth_page",
    "reported_net_worth_page_from",
]

# Distinct from every rate-availability reason valuation.py can produce
# (§4.3: "keep evidence gaps distinct from rate gaps") -- this names a
# component with no native amount/date to value at all, never a missing FX row.
_NO_NET_WORTH_EVIDENCE = "NO_NET_WORTH_EVIDENCE"
_EVIDENCE_REMEDY = "Provide the missing native balance/position evidence for this component, then refresh."


@dataclass(frozen=True, slots=True)
class ReportedComponent:
    """One native component (unchanged) plus its own reported equivalent.

    ``reported`` is ``None`` exactly when there is no native amount and/or
    as-of date to value at all -- missing evidence, never a fabricated zero.
    A structurally excluded-but-priced component (e.g. a card-linked debt,
    counted once in card liability instead) still gets a reported value here
    for native supporting detail, even though it never enters the totals.
    """

    native: Mapping[str, Any]
    reported: ExactValuation | None


@dataclass(frozen=True, slots=True)
class ReportedTotals:
    """Reporting-currency totals combined from independently-dated components.

    ``assets``/``liabilities``/``net_worth`` are evidence-basis only (mirrors
    native ``tracked_*``); ``manual_assets``/``manual_liabilities`` are
    manual-basis only; ``net_worth_including_manual`` combines both bases
    (mirrors native ``net_worth_including_manual``). Missing evidence on
    either side degrades ``net_worth``/``net_worth_including_manual`` too,
    since a net figure is only as complete as its least complete side.
    """

    assets: ConsolidatedValuation
    liabilities: ConsolidatedValuation
    net_worth: ConsolidatedValuation
    manual_assets: ConsolidatedValuation
    manual_liabilities: ConsolidatedValuation
    net_worth_including_manual: ConsolidatedValuation


@dataclass(frozen=True, slots=True)
class ReportedNetWorthTrend:
    """A prior/current comparison valued and combined *separately* at each
    side's own dates, then subtracted exactly (§3.3, §7 M4: "value prior and
    current positions separately, then subtract exact reported results.
    Never mark a native difference at today's rate")."""

    prior_as_of: str
    current_as_of: str
    prior_net_worth: ConsolidatedValuation
    current_net_worth: ConsolidatedValuation
    # ``None`` unless both sides produced at least a known (total or partial)
    # figure; never a delta computed from one missing/zero-substituted side.
    delta: Fraction | None
    both_complete: bool
    scope_changed: bool
    scope_changes: tuple[Mapping[str, str], ...]
    comparability: str


@dataclass(frozen=True, slots=True)
class ReportedNetWorthPage:
    context: ReportingContext
    native: Mapping[str, Any]
    components: tuple[ReportedComponent, ...]
    totals: ReportedTotals
    trend: ReportedNetWorthTrend | None


def _negate(value: ExactValuation) -> ExactValuation:
    if value.reported_exact is None:
        return value
    return dataclasses.replace(value, reported_exact=-value.reported_exact)


def _scope_key(scope: ValuationScope) -> tuple[str, str]:
    return (scope.currency, scope.period or "")


def _fold_missing_evidence(
    result: ConsolidatedValuation, *, missing: Sequence[tuple[str, str | None]],
) -> ConsolidatedValuation:
    """Fold in components excluded for lack of a native amount/date to value
    at all -- distinct from a component that *has* a native amount but no
    importable rate, which :func:`combine_independently_dated` already marks
    ``unavailable`` with reason ``RATE_NOT_IMPORTED`` on its own. Mirrors
    ``money_flow.py``'s ``_with_evidence_gaps`` for the same reason: "the
    consolidated result cannot be complete simply because all included
    components have FX."
    """
    if not missing:
        return result
    extra_scopes = tuple(
        sorted({ValuationScope(currency=currency, period=as_of) for currency, as_of in missing}, key=_scope_key)
    )
    extra_detail = tuple(
        ExcludedScopeDetail(scope=scope, entry_count=1, reasons=(_NO_NET_WORTH_EVIDENCE,), remedies=(_EVIDENCE_REMEDY,))
        for scope in extra_scopes
    )
    if result.status is ConsolidationStatus.AVAILABLE:
        # A vacuous ``combine_independently_dated([])`` reports AVAILABLE/$0
        # with nothing ``included`` at all -- that $0 is not a real known
        # subtotal to carry forward once a genuine gap is folded in; only an
        # AVAILABLE result that actually included something becomes a
        # "Known subtotal" partial below.
        total, partial_total = None, (result.total if result.included else None)
    else:
        total, partial_total = None, result.partial_total
    new_status = (
        ConsolidationStatus.PARTIAL if (result.included or partial_total is not None)
        else ConsolidationStatus.UNAVAILABLE
    )
    return dataclasses.replace(
        result,
        status=new_status, total=total, partial_total=partial_total,
        excluded=tuple(sorted((*result.excluded, *extra_scopes), key=_scope_key)),
        excluded_detail=tuple(sorted((*result.excluded_detail, *extra_detail), key=lambda d: _scope_key(d.scope))),
        excluded_entry_count=result.excluded_entry_count + len(extra_scopes),
        unavailable_reasons=tuple(sorted({*result.unavailable_reasons, _NO_NET_WORTH_EVIDENCE})),
    )


def _value_components(
    connection: Any, native: Mapping[str, Any], *, context: ReportingContext,
) -> tuple[tuple[ReportedComponent, ...], ReportedTotals]:
    components: list[ReportedComponent] = []
    tracked_assets: list[ExactValuation] = []
    tracked_liabilities: list[ExactValuation] = []
    tracked_net_signed: list[ExactValuation] = []
    manual_assets: list[ExactValuation] = []
    manual_liabilities: list[ExactValuation] = []
    manual_net_signed: list[ExactValuation] = []
    missing_asset_scopes: list[tuple[str, str | None]] = []
    missing_liability_scopes: list[tuple[str, str | None]] = []

    for component in native["components"]:
        valued = value_position_at_own_date(
            connection, reporting_currency=context.reporting_currency,
            amount=component["amount"], currency=component["currency"], as_of=component.get("as_of"),
        )
        components.append(ReportedComponent(native=component, reported=valued))

        if valued is None:
            # A component native deliberately excludes for a structural
            # reason (card-linked debt, a not-stays-visible obligation) while
            # still knowing its amount is not a reported evidence gap: it
            # never enters native's own totals either, so folding it here
            # would flag "missing evidence" for something intentionally
            # uncounted. Anything else with no valuation -- a genuinely
            # missing amount, or a known amount with no date to value it at
            # -- is a real gap even if native still counted it in its own
            # (undated) subtotal.
            structural_exclusion = (not component["included"]) and component["amount"] is not None
            if not structural_exclusion:
                scope = (component["currency"], component.get("as_of"))
                (missing_asset_scopes if component["side"] == "asset" else missing_liability_scopes).append(scope)
            continue

        if not component["included"]:
            continue  # structurally excluded but priced; shown in the components table only.

        is_liability = component["side"] == "liability"
        signed = _negate(valued) if is_liability else valued
        if component["basis"] == "manual":
            (manual_liabilities if is_liability else manual_assets).append(valued)
            manual_net_signed.append(signed)
        else:
            (tracked_liabilities if is_liability else tracked_assets).append(valued)
            tracked_net_signed.append(signed)

    display_context = ValuationContext(
        reporting_currency=context.reporting_currency, purpose=ValuationPurpose.POSITION,
        valuation_as_of=date.fromisoformat(str(native["as_of"])), rate_selection_policy=POSITION_MONTHLY_CLOSED_V1,
    )

    def _combine(values: list[ExactValuation]) -> ConsolidatedValuation:
        return combine_independently_dated(values, context=display_context)

    assets_result = _fold_missing_evidence(_combine(tracked_assets), missing=missing_asset_scopes)
    liabilities_result = _fold_missing_evidence(_combine(tracked_liabilities), missing=missing_liability_scopes)
    net_result = _fold_missing_evidence(
        _combine(tracked_net_signed), missing=[*missing_asset_scopes, *missing_liability_scopes],
    )
    manual_assets_result = _combine(manual_assets)
    manual_liabilities_result = _combine(manual_liabilities)
    net_including_manual_result = _fold_missing_evidence(
        _combine([*tracked_net_signed, *manual_net_signed]),
        missing=[*missing_asset_scopes, *missing_liability_scopes],
    )
    totals = ReportedTotals(
        assets=assets_result, liabilities=liabilities_result, net_worth=net_result,
        manual_assets=manual_assets_result, manual_liabilities=manual_liabilities_result,
        net_worth_including_manual=net_including_manual_result,
    )
    return tuple(components), totals


def _effective(result: ConsolidatedValuation) -> Fraction | None:
    return result.total if result.total is not None else result.partial_total


def _build_trend(
    native_trend: Mapping[str, Any], *, prior_totals: ReportedTotals, current_totals: ReportedTotals,
) -> ReportedNetWorthTrend:
    prior_value = _effective(prior_totals.net_worth)
    current_value = _effective(current_totals.net_worth)
    delta = current_value - prior_value if prior_value is not None and current_value is not None else None
    return ReportedNetWorthTrend(
        prior_as_of=str(native_trend["prior_as_of"]), current_as_of=str(native_trend["current_as_of"]),
        prior_net_worth=prior_totals.net_worth, current_net_worth=current_totals.net_worth,
        delta=delta,
        both_complete=(
            prior_totals.net_worth.status is ConsolidationStatus.AVAILABLE
            and current_totals.net_worth.status is ConsolidationStatus.AVAILABLE
        ),
        scope_changed=bool(native_trend["scope_changed"]), scope_changes=tuple(native_trend["scope_changes"]),
        comparability=str(native_trend["comparability"]),
    )


def reported_net_worth_page(
    database: Path, *, as_of: str | None = None, prior_as_of: str | None = None, today: date,
) -> ReportedNetWorthPage:
    connection = connect_readonly(database)
    try:
        return reported_net_worth_page_from(connection, as_of=as_of, prior_as_of=prior_as_of, today=today)
    finally:
        connection.close()


def reported_net_worth_page_from(
    connection: Any, *, as_of: str | None = None, prior_as_of: str | None = None, today: date,
) -> ReportedNetWorthPage:
    """The reported Net worth read (§3.3, §7 M4; step 13).

    Reuses :func:`net_worth_page_from` for every native fact -- owner
    asset/liability inclusion, double-count avoidance, manual-estimate labels
    and exclusions stay untouched -- and attaches only a reported equivalent
    to each priceable native component, each valued at its own explicit
    as-of date. ``prior_as_of`` adds a second, independently valued
    observation and an exact reported delta; native scope-change bookkeeping
    (``scope_changed``/``scope_changes``/``comparability``) is reused
    verbatim from the native trend, never recomputed by a second formula.
    """
    scope = ReportedScope(valuation_as_of=today)
    context = build_reporting_context(connection, scope=scope)

    current_native = net_worth_page_from(connection, as_of=as_of, prior_as_of=prior_as_of, today=today)
    current_components, current_totals = _value_components(connection, current_native, context=context)

    trend: ReportedNetWorthTrend | None = None
    if prior_as_of is not None:
        prior_native = net_worth_page_from(connection, as_of=prior_as_of, today=today)
        _prior_components, prior_totals = _value_components(connection, prior_native, context=context)
        trend = _build_trend(current_native["trend"], prior_totals=prior_totals, current_totals=current_totals)

    return ReportedNetWorthPage(
        context=context, native=current_native, components=current_components,
        totals=current_totals, trend=trend,
    )
