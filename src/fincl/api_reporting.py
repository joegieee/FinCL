"""Reporting-currency HTTP surface (plan M1 §7.1; M0 §4.3 dated coverage).

Read routes only — the write path reuses the shared ``/api/v1/preview`` and
``/api/v1/commands`` flow (``api._validate`` gains the ``REPORTING_CURRENCY_SET``
branch). Kept in its own module so ``create_app`` grows by a single call.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from .analysis.reporting import (
    ContributorsPageResult,
    ReportedAnalysisResult,
    reported_analysis_page_from,
    reported_contributors_page_from,
)
from .coverage import dated_reporting_coverage
from .db import connect_readonly
from .errors import FinCLError
from .generation import current_generation
from .reporting_currency import (
    CURRENCY_REGISTRY,
    currency_coverage_preview,
    currency_registry_dto,
    reporting_policy,
)
from .valuation import (
    ConsolidatedValuationDTO,
    MoneyDTO,
    ValuedMoneyDTO,
    quantize,
    to_consolidated_dto,
    to_dto,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RateRequirementOut(_StrictModel):
    purpose: str
    native_currency: str
    reporting_currency: str
    period: str | None
    as_of_date: str | None
    policy: str | None
    status: str
    basis: str | None
    inverted: bool | None
    reason: str | None
    remedy: str | None
    affected_metrics: list[str]


class BurnMonthSelectionOut(_StrictModel):
    included_months: list[str]
    requested_count: int
    reason: str | None


class DatedCoverageOut(_StrictModel):
    history_start: str
    history_end: str
    as_of: str
    reporting_currency: str
    evidence_status: str
    evidence_reasons: list[str]
    burn: BurnMonthSelectionOut
    requirements: list[RateRequirementOut]


def _dated_coverage_out(
    connection: Any, *, history_start: str | None, history_end: str | None, as_of: str | None,
    reporting_currency: str | None,
) -> DatedCoverageOut:
    # The route is the outer edge that resolves "today" (matching every other
    # ``today: date`` route in api.py); ``dated_reporting_coverage`` itself
    # never consults the wall clock (§2 invariant 7).
    resolved_as_of = date.fromisoformat(as_of) if as_of else date.today()
    dated = dated_reporting_coverage(
        connection,
        as_of=resolved_as_of,
        history_start=date.fromisoformat(history_start) if history_start else None,
        history_end=date.fromisoformat(history_end) if history_end else None,
        reporting_currency=reporting_currency,
    )
    return DatedCoverageOut(
        history_start=dated.history_start,
        history_end=dated.history_end,
        as_of=dated.as_of,
        reporting_currency=dated.reporting_currency,
        evidence_status=dated.evidence_status,
        evidence_reasons=list(dated.evidence_reasons),
        burn=BurnMonthSelectionOut(
            included_months=list(dated.burn.included_months),
            requested_count=dated.burn.requested_count,
            reason=dated.burn.reason,
        ),
        requirements=[
            RateRequirementOut(
                purpose=r.purpose, native_currency=r.native_currency,
                reporting_currency=r.reporting_currency, period=r.period,
                as_of_date=r.as_of_date, policy=r.policy, status=r.status,
                basis=r.basis, inverted=r.inverted, reason=r.reason, remedy=r.remedy,
                affected_metrics=list(r.affected_metrics),
            )
            for r in dated.requirements
        ],
    )


class SummaryEntryOut(_StrictModel):
    current: ConsolidatedValuationDTO
    previous: ConsolidatedValuationDTO | None
    comparison_available: bool
    comparison_reasons: list[str]
    handle: str
    delta: MoneyDTO | None
    pct: str | None


class RankedGroupOut(_StrictModel):
    name: str
    result: ConsolidatedValuationDTO
    handle: str


class GrossBurnOut(_StrictModel):
    result: ConsolidatedValuationDTO | None
    included_months: list[str]
    month_count: int
    reason: str | None


class TrendMonthOut(_StrictModel):
    month: str
    spending: ConsolidatedValuationDTO
    fees: ConsolidatedValuationDTO
    income: ConsolidatedValuationDTO
    complete: bool


class ReportedAnalysisOut(_StrictModel):
    generation: str
    reporting_currency: str
    as_of: str
    history_start: str
    history_end: str
    pocket_currency: str | None
    covered: bool
    coverage_reasons: list[str]
    comparison_kind: str
    summary: dict[str, SummaryEntryOut]
    gross_burn: GrossBurnOut
    category_ranking: list[RankedGroupOut]
    merchant_ranking: list[RankedGroupOut]
    contributors_handle: str
    trend: list[TrendMonthOut]


def _reported_analysis_out(result: ReportedAnalysisResult) -> ReportedAnalysisOut:
    reporting_currency = result.context.reporting_currency
    definition = CURRENCY_REGISTRY[reporting_currency]

    def _cv(consolidated: Any) -> ConsolidatedValuationDTO:
        return to_consolidated_dto(consolidated, currency_registry=CURRENCY_REGISTRY)

    def _delta_dto(delta: Any) -> MoneyDTO | None:
        if delta is None:
            return None
        money = quantize(delta, scale=definition.minor_unit_scale)
        return MoneyDTO(value=money.text, currency=reporting_currency)

    return ReportedAnalysisOut(
        generation=result.context.generation_token,
        reporting_currency=result.context.reporting_currency,
        as_of=result.context.scope.valuation_as_of.isoformat(),
        history_start=result.context.scope.history_start or "",
        history_end=result.context.scope.history_end or "",
        pocket_currency=result.context.scope.pocket_currency,
        covered=result.covered,
        coverage_reasons=list(result.coverage_reasons),
        comparison_kind=result.comparison_kind,
        summary={
            name: SummaryEntryOut(
                current=_cv(entry.current),
                previous=_cv(entry.previous) if entry.previous is not None else None,
                comparison_available=entry.comparison_available,
                comparison_reasons=list(entry.comparison_reasons),
                handle=entry.handle,
                delta=_delta_dto(entry.delta), pct=entry.pct,
            )
            for name, entry in result.summary.items()
        },
        gross_burn=GrossBurnOut(
            result=_cv(result.gross_burn.result) if result.gross_burn.result is not None else None,
            included_months=list(result.gross_burn.included_months),
            month_count=result.gross_burn.month_count,
            reason=result.gross_burn.reason,
        ),
        category_ranking=[
            RankedGroupOut(name=group.name, result=_cv(group.result), handle=group.handle)
            for group in result.category_ranking
        ],
        merchant_ranking=[
            RankedGroupOut(name=group.name, result=_cv(group.result), handle=group.handle)
            for group in result.merchant_ranking
        ],
        contributors_handle=result.contributors_handle,
        trend=[
            TrendMonthOut(
                month=month.month, spending=_cv(month.spending), fees=_cv(month.fees),
                income=_cv(month.income), complete=month.complete,
            )
            for month in result.trend
        ],
    )


class OwningSourceOut(_StrictModel):
    kind: str
    id: str


class ContributorRowOut(_StrictModel):
    component_id: str
    origin_kind: str
    owning_source: OwningSourceOut
    role: str
    bucket: str
    valuation: ValuedMoneyDTO
    category: str
    merchant: str | None
    account: str
    date: str
    precision: str
    source_location: str
    nets: bool


class ContributorsPageOut(_StrictModel):
    generation: str
    reporting_currency: str
    scope_start: str
    scope_end: str
    buckets: list[str]
    pocket_currency: str | None
    category_key: str | None
    merchant_display: str | None
    sort: str
    rows: list[ContributorRowOut]
    total: int
    next_cursor: str | None
    parent: ConsolidatedValuationDTO


def _contributors_page_out(page: ContributorsPageResult) -> ContributorsPageOut:
    return ContributorsPageOut(
        generation=page.context.generation_token,
        reporting_currency=page.context.reporting_currency,
        scope_start=page.scope_start, scope_end=page.scope_end,
        buckets=list(page.buckets), pocket_currency=page.pocket_currency,
        category_key=page.category_key, merchant_display=page.merchant_display,
        sort=page.sort,
        rows=[
            ContributorRowOut(
                component_id=row.component_id, origin_kind=row.origin_kind,
                owning_source=OwningSourceOut(kind=row.owning_source.kind, id=row.owning_source.id),
                role=row.role, bucket=row.bucket,
                valuation=to_dto(row.valuation, currency_registry=CURRENCY_REGISTRY),
                category=row.category, merchant=row.merchant, account=row.account,
                date=row.date, precision=row.precision, source_location=row.source_location,
                nets=row.nets,
            )
            for row in page.rows
        ],
        total=page.total, next_cursor=page.next_cursor,
        parent=to_consolidated_dto(page.parent, currency_registry=CURRENCY_REGISTRY),
    )


def register_reporting_routes(app: FastAPI, database: Path) -> None:
    @app.get('/api/v1/reporting-currency')
    def reporting_currency_read() -> Any:
        connection = connect_readonly(database)
        try:
            policy = reporting_policy(connection)
            generation = current_generation(connection)
            coverage = currency_coverage_preview(connection, policy.currency)
            return {
                'generation': generation['generation_token'] if generation else None,
                'reporting_policy': {
                    'currency': policy.currency,
                    'minor_unit_scale': policy.minor_unit_scale,
                    'journal_seq': policy.journal_seq,
                    'decided_at': policy.decided_at,
                    'policy_version': policy.policy_version,
                    'is_default': policy.is_default,
                },
                'currency_registry': currency_registry_dto(),
                'coverage': coverage,
            }
        finally:
            connection.close()

    @app.get('/api/v1/fx/coverage')
    def fx_coverage_read(
        currency: str | None = None,
        history_start: str | None = None,
        history_end: str | None = None,
        as_of: str | None = None,
    ) -> Any:
        """Structural pair preview (unchanged) plus §4.3 dated requirements.

        ``dated`` is additive: it never replaces ``by_currency``'s pair-only
        structural check, and reuses M0 step 01's real valuation selector, so
        this endpoint cannot predict a rate its own valuation would not select.
        """
        connection = connect_readonly(database)
        try:
            policy = reporting_policy(connection)
            target = currency or policy.currency
            generation = current_generation(connection)
            dated = _dated_coverage_out(
                connection, history_start=history_start, history_end=history_end,
                as_of=as_of, reporting_currency=target,
            )
            return {
                'generation': generation['generation_token'] if generation else None,
                **currency_coverage_preview(connection, target),
                'dated': dated.model_dump(),
            }
        finally:
            connection.close()

    @app.get('/api/v1/reporting/analysis', response_model=ReportedAnalysisOut)
    def reported_analysis_read(
        history_start: str,
        history_end: str,
        as_of: str | None = None,
        pocket_currency: str | None = None,
        accounts: list[str] | None = Query(default=None),
        category_keys: list[str] | None = Query(default=None),
        merchant: str | None = None,
        comparison: str | None = None,
        top_n: int = 10,
        trend_range: str = '6',
    ) -> Any:
        """M3b.1 step 04-05: reported Analysis over all pockets by default.

        Additive to the legacy ``/api/v1/analysis``; that route's schema,
        default currency filter and outputs are unchanged.
        """
        connection = connect_readonly(database)
        try:
            resolved_as_of = date.fromisoformat(as_of) if as_of else date.today()
            result = reported_analysis_page_from(
                connection, as_of=resolved_as_of, history_start=history_start, history_end=history_end,
                pocket_currency=pocket_currency, accounts=accounts or (), category_keys=category_keys or (),
                merchant=merchant, comparison=comparison, top_n=top_n, trend_range=trend_range,
            )
            return _reported_analysis_out(result)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()

    @app.get('/api/v1/reporting/contributors/{handle}', response_model=ContributorsPageOut)
    def reported_contributors_read(handle: str, cursor: str | None = None, limit: int = 50) -> Any:
        """M3b.1 step 04: the exact reported components behind one bucket/rank
        row. Additive to the legacy ``/api/v1/contributors/{handle}``, which
        only ever accepts a legacy handle and is otherwise unchanged."""
        connection = connect_readonly(database)
        try:
            page = reported_contributors_page_from(connection, handle=handle, cursor=cursor, limit=limit)
            return _contributors_page_out(page)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION') or str(exc).startswith('STALE_REPORTING_CONTEXT'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()
