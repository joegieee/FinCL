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

from . import clock
from .analysis.reporting import (
    ContributorsPageResult,
    ReportedAnalysisResult,
    reported_analysis_page_from,
    reported_contributors_page_from,
)
from .api import Entry
from .coverage import dated_reporting_coverage
from .db import connect_readonly
from .errors import FinCLError
from .forecast.reporting import (
    CombinedMinimum,
    ReportedCurrencyForecast,
    ReportedForecastPage,
    ReportedScenarioCell,
    reported_forecast_page_from,
)
from .generation import current_generation
from .money import format_decimal
from .money_flow import MoneyFlowResult, money_flow_page_from
from .networth.reporting import ReportedNetWorthPage, reported_net_worth_page_from
from .planning.reporting import (
    ReportedIncomeCandidate,
    ReportedIncomeCandidatesPage,
    ReportedVariation,
    reported_income_candidates_page_from,
)
from .position.reporting import ReportedCashPage, reported_cash_page_from
from .reporting_currency import (
    CURRENCY_REGISTRY,
    currency_coverage_preview,
    currency_registry_dto,
    reporting_policy,
)
from .reporting_transactions import (
    ReportedTransactionRow,
    decode_anchor,
    reported_transaction_anchors_from,
    reported_transaction_detail_from,
    reported_transactions_page_from,
)
from .statement_money import StatementMoneyDetailDTO
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
    resolved_as_of = date.fromisoformat(as_of) if as_of else clock.today()
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
    previous_spending: ConsolidatedValuationDTO | None = None
    spending_change: MoneyDTO | None = None
    spending_change_pct: str | None = None
    spending_change_reason: str | None = None


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
                previous_spending=_cv(month.previous_spending) if month.previous_spending is not None else None,
                spending_change=_delta_dto(month.spending_change),
                spending_change_pct=month.spending_change_pct,
                spending_change_reason=month.spending_change_reason,
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


class ExactAmountOut(_StrictModel):
    currency: str
    amount_text: str


class PrintedConversionOut(_StrictModel):
    base_currency: str
    quote_currency: str
    base_amount_text: str
    quote_amount_text: str
    raw_text: str


class EffectiveRateOut(_StrictModel):
    base_currency: str
    quote_currency: str
    numerator: str
    denominator: str
    amount_basis: str


class ValidationOut(_StrictModel):
    status: str
    reason: str | None


class ProvenanceOut(_StrictModel):
    document_record_id: str | None
    source_location: str | None
    detail_basis: str
    normalizer_version: str


class StatementMoneyDetailOut(_StrictModel):
    original: ExactAmountOut | None
    settled: ExactAmountOut
    explicit_fee: ExactAmountOut | None
    statement_cash_movement: ExactAmountOut | None
    printed_conversion: PrintedConversionOut | None
    printed_charge_percent: str | None
    effective_rate: EffectiveRateOut | None
    validation: ValidationOut
    provenance: ProvenanceOut


def _exact_amount_out(amount: Any) -> ExactAmountOut | None:
    if amount is None:
        return None
    return ExactAmountOut(currency=amount.currency, amount_text=amount.amount_text)


def _statement_detail_out(detail: StatementMoneyDetailDTO) -> StatementMoneyDetailOut:
    printed = detail.printed_conversion
    effective = detail.effective_rate
    return StatementMoneyDetailOut(
        original=_exact_amount_out(detail.original),
        settled=ExactAmountOut(currency=detail.settled.currency, amount_text=detail.settled.amount_text),
        explicit_fee=_exact_amount_out(detail.explicit_fee),
        statement_cash_movement=_exact_amount_out(detail.statement_cash_movement),
        printed_conversion=(
            PrintedConversionOut(
                base_currency=printed.base_currency, quote_currency=printed.quote_currency,
                base_amount_text=printed.base_amount_text, quote_amount_text=printed.quote_amount_text,
                raw_text=printed.raw_text,
            )
            if printed is not None else None
        ),
        printed_charge_percent=detail.printed_charge_percent,
        effective_rate=(
            EffectiveRateOut(
                base_currency=effective.base_currency, quote_currency=effective.quote_currency,
                numerator=effective.numerator, denominator=effective.denominator,
                amount_basis=effective.amount_basis,
            )
            if effective is not None else None
        ),
        validation=ValidationOut(status=detail.validation.status, reason=detail.validation.reason),
        provenance=ProvenanceOut(
            document_record_id=detail.provenance.document_record_id,
            source_location=detail.provenance.source_location,
            detail_basis=detail.provenance.detail_basis,
            normalizer_version=detail.provenance.normalizer_version,
        ),
    )


class TransactionRowOut(_StrictModel):
    entry: Entry
    amount: ValuedMoneyDTO
    fee: ValuedMoneyDTO | None
    statement_detail: StatementMoneyDetailOut


def _transaction_row_out(row: ReportedTransactionRow) -> TransactionRowOut:
    return TransactionRowOut(
        entry=Entry(**row.entry),
        amount=to_dto(row.amount_valuation, currency_registry=CURRENCY_REGISTRY),
        fee=(
            to_dto(row.fee_valuation, currency_registry=CURRENCY_REGISTRY)
            if row.fee_valuation is not None else None
        ),
        statement_detail=_statement_detail_out(row.statement_detail),
    )


class TransactionsPageOut(_StrictModel):
    generation: str
    reporting_currency: str
    sort: str
    rows: list[TransactionRowOut]
    total: int
    next_cursor: str | None


class TransactionAnchorsOut(_StrictModel):
    generation: str
    anchors: list[str]
    truncated: bool
    cap: int


class EconomicFlowOut(_StrictModel):
    inflow: ConsolidatedValuationDTO
    outflow: ConsolidatedValuationDTO
    net: ConsolidatedValuationDTO
    exceptional_adjustments: ConsolidatedValuationDTO


class GrossBurnCardOut(_StrictModel):
    amount: ConsolidatedValuationDTO | None
    included_months: list[str]
    excluded_months: list[str]
    month_count: int
    reason: str | None
    definition_version: str


class CashAtCutoffOut(_StrictModel):
    amount: ConsolidatedValuationDTO
    balance_as_of: str | None
    valuation_as_of: str


class ForecastClosingOut(_StrictModel):
    base: ConsolidatedValuationDTO
    conservative: ConsolidatedValuationDTO
    opening_balance_as_of: str | None
    valuation_as_of: str
    horizon_end: str | None


class MoneyFlowSummaryOut(_StrictModel):
    economic_flow: EconomicFlowOut
    gross_burn: GrossBurnCardOut
    cash_at_cutoff: CashAtCutoffOut
    forecast_closing: ForecastClosingOut


class MoneyFlowCutoffOut(_StrictModel):
    supported_cash_cutoff: str | None
    first_modelled_day: str | None
    stale_days: int | None


class MoneyFlowProvenanceOut(_StrictModel):
    generation_manifest_digest: str
    rate_inventory_digest: str
    policy_versions: dict[str, str]


class MoneyFlowOut(_StrictModel):
    generation: str
    freshness: str
    reporting_currency: str
    policy_version: str | None
    as_of: str
    history_start: str
    history_end: str
    pocket_currency: str | None
    summary: MoneyFlowSummaryOut
    cutoff: MoneyFlowCutoffOut
    limitations: list[str]
    provenance: MoneyFlowProvenanceOut


def _money_flow_out(result: MoneyFlowResult) -> MoneyFlowOut:
    def _cv(consolidated: Any) -> ConsolidatedValuationDTO:
        return to_consolidated_dto(consolidated, currency_registry=CURRENCY_REGISTRY)

    summary = result.summary
    return MoneyFlowOut(
        generation=result.context.generation_token,
        freshness="current",
        reporting_currency=result.context.reporting_currency,
        policy_version=result.context.policy.policy_version,
        as_of=result.context.scope.valuation_as_of.isoformat(),
        history_start=result.context.scope.history_start or "",
        history_end=result.context.scope.history_end or "",
        pocket_currency=result.context.scope.pocket_currency,
        summary=MoneyFlowSummaryOut(
            economic_flow=EconomicFlowOut(
                inflow=_cv(summary.inflow.current),
                outflow=_cv(summary.outflow.current),
                net=_cv(summary.net_economic_flow.current),
                exceptional_adjustments=_cv(summary.exceptional_adjustments.current),
            ),
            gross_burn=GrossBurnCardOut(
                amount=_cv(summary.gross_burn.result) if summary.gross_burn.result is not None else None,
                included_months=list(summary.gross_burn.included_months),
                excluded_months=list(summary.gross_burn_excluded_months),
                month_count=summary.gross_burn.month_count,
                reason=summary.gross_burn.reason,
                definition_version=result.policy_versions["gross_burn"],
            ),
            cash_at_cutoff=CashAtCutoffOut(
                amount=_cv(summary.cash_at_cutoff.result),
                balance_as_of=summary.cash_at_cutoff.balance_as_of,
                valuation_as_of=summary.cash_at_cutoff.valuation_as_of.isoformat(),
            ),
            forecast_closing=ForecastClosingOut(
                base=_cv(summary.forecast_closing.base),
                conservative=_cv(summary.forecast_closing.conservative),
                opening_balance_as_of=summary.forecast_closing.opening_balance_as_of,
                valuation_as_of=summary.forecast_closing.valuation_as_of.isoformat(),
                horizon_end=summary.forecast_closing.horizon_end,
            ),
        ),
        cutoff=MoneyFlowCutoffOut(
            supported_cash_cutoff=result.supported_cash_cutoff,
            first_modelled_day=result.first_modelled_day,
            stale_days=result.cash_stale_days,
        ),
        limitations=list(result.limitations),
        provenance=MoneyFlowProvenanceOut(
            generation_manifest_digest=result.context.generation_token,
            rate_inventory_digest=result.context.rate_inventory_digest,
            policy_versions=dict(result.policy_versions),
        ),
    )


class MissingIntervalOut(_StrictModel):
    account_id: str
    currency_pocket: str
    start: str
    end: str
    reason: str


class CashBridgePocketOut(_StrictModel):
    account_id: str
    currency_pocket: str
    opening: str
    opening_as_of: str
    external_inflows: str
    external_outflows: str
    owned_transfer_inflows: str
    owned_transfer_outflows: str
    closing: str
    closing_as_of: str
    calculated_closing: str
    reconciled: bool
    movement_count: int
    paired_transfer_link_count: int
    unsupported_reason: str | None


class CashBridgeCombinedOut(_StrictModel):
    opening: str
    external_inflows: str
    external_outflows: str
    closing: str
    reconciled: bool
    owned_transfer_pairs_cancelled: int


class CashBridgeOut(_StrictModel):
    pockets: list[CashBridgePocketOut]
    combined_by_currency: dict[str, CashBridgeCombinedOut]
    owned_transfer_pairs_cancelled: int
    policy: str


class BoundaryDetailOut(_StrictModel):
    balance: str
    as_of: str
    basis: str
    observation: str | None = None


class CashPocketOut(_StrictModel):
    account_id: str
    institution: str | None
    display_name: str | None
    source_type: str | None
    currency_pocket: str
    liquid: bool
    excluded_reason: str | None
    basis: str
    balance: str | None
    as_of: str | None
    date_precision: str | None
    stale: bool
    staleness_days: int | None
    unavailable_reason: str | None
    statement_running_balance: BoundaryDetailOut | None
    manual_observation: BoundaryDetailOut | None
    derived_balance: str | None
    derived_as_of: str | None
    derived_basis: str | None
    coverage_status: str
    # This pocket's own furthest coverage-backed date, independent of what the
    # other pockets can prove. ``derived_*`` above is the *shared* cutoff, so a
    # pocket that is fine through today still reads ``None`` there whenever a
    # sibling pocket is short; these three say what this pocket alone supports.
    best_supported_cutoff: str | None
    best_supported_balance: str | None
    best_supported_basis: str | None
    blocking_reason: str | None
    # §3.3: each pocket's own native balance/derived-balance, valued at its
    # own as-of date -- ``None`` means no native amount to value (missing
    # balance evidence), never a fabricated zero. A present-but-unavailable
    # ``ValuedMoneyDTO`` means a real native amount with no importable rate
    # (missing FX) -- the two must render as different explanations.
    reported_balance: ValuedMoneyDTO | None
    reported_derived_balance: ValuedMoneyDTO | None


class PartialPocketOut(_StrictModel):
    account_id: str
    currency_pocket: str
    display_name: str | None
    balance: str | None = None
    basis: str | None = None
    reason: str | None = None
    best_supported_cutoff: str | None = None


class PartialPositionOut(_StrictModel):
    """The most current cash figure the evidence supports, however partial.

    Complementary to ``combined_liquid_position``, never a replacement for it:
    that total is complete but can only be dated to the latest day every pocket
    proves, while this one is dated as late as any pocket proves and names the
    pockets it had to leave out. ``is_complete`` is true only when they coincide.
    """

    as_of: str | None
    by_currency: dict[str, str]
    contributing_pockets: list[PartialPocketOut]
    short_pockets: list[PartialPocketOut]
    is_complete: bool
    reason: str | None


class CoverageSummaryOut(_StrictModel):
    included_pockets: int
    covered_pockets: int
    short_pockets: int
    ratio: float
    basis: str
    status: str


class CashPageOut(_StrictModel):
    generation: str
    reporting_currency: str
    policy_version: str | None
    as_of: str
    pocket_currency: str | None
    generated_for: str
    stale_after_days: int
    supported_cutoff: str | None
    current_position_supported: bool
    current_position_reasons: list[str]
    # Native, unchanged per-currency totals (§1 invariant 3) shown *beside*
    # the reported aggregate below -- never replaced by it.
    combined_liquid_position: dict[str, str]
    liquid_total: dict[str, str]
    liquid_total_withheld: dict[str, str]
    partial_position: PartialPositionOut
    coverage_summary: CoverageSummaryOut
    combined: CashAtCutoffOut
    pockets: list[CashPocketOut]
    stale: bool
    missing_intervals: list[MissingIntervalOut]
    cash_bridge: CashBridgeOut


def _boundary_detail_out(detail: Any) -> BoundaryDetailOut | None:
    if detail is None:
        return None
    return BoundaryDetailOut(
        balance=str(detail["balance"]), as_of=str(detail["as_of"]), basis=str(detail["basis"]),
        observation=detail.get("observation"),
    )


def _cash_pocket_out(pocket: Any) -> CashPocketOut:
    native = pocket.native
    return CashPocketOut(
        account_id=native["account_id"], institution=native["institution"],
        display_name=native["display_name"], source_type=native["source_type"],
        currency_pocket=native["currency_pocket"], liquid=native["liquid"],
        excluded_reason=native["excluded_reason"], basis=native["basis"],
        balance=native["balance"], as_of=native["as_of"], date_precision=native["date_precision"],
        stale=native["stale"], staleness_days=native["staleness_days"],
        unavailable_reason=native["unavailable_reason"],
        statement_running_balance=_boundary_detail_out(native["statement_running_balance"]),
        manual_observation=_boundary_detail_out(native["manual_observation"]),
        derived_balance=native["derived_balance"], derived_as_of=native["derived_as_of"],
        derived_basis=native["derived_basis"], coverage_status=native["coverage_status"],
        best_supported_cutoff=native.get("best_supported_cutoff"),
        best_supported_balance=native.get("best_supported_balance"),
        best_supported_basis=native.get("best_supported_basis"),
        blocking_reason=native.get("blocking_reason"),
        reported_balance=(
            to_dto(pocket.reported_balance, currency_registry=CURRENCY_REGISTRY)
            if pocket.reported_balance is not None else None
        ),
        reported_derived_balance=(
            to_dto(pocket.reported_derived_balance, currency_registry=CURRENCY_REGISTRY)
            if pocket.reported_derived_balance is not None else None
        ),
    )


def _cash_page_out(page: ReportedCashPage) -> CashPageOut:
    position = page.position
    bridge = position["cash_bridge"]
    combined_bridge = bridge["combined"]
    return CashPageOut(
        generation=page.context.generation_token, reporting_currency=page.context.reporting_currency,
        policy_version=page.context.policy.policy_version,
        as_of=page.context.scope.valuation_as_of.isoformat(),
        pocket_currency=page.context.scope.pocket_currency,
        generated_for=position["generated_for"], stale_after_days=position["stale_after_days"],
        supported_cutoff=position["supported_cutoff"],
        current_position_supported=position["current_position_supported"],
        current_position_reasons=list(position["current_position_reasons"]),
        combined_liquid_position=dict(position["combined_liquid_position"]),
        liquid_total=dict(position["liquid_total"]),
        liquid_total_withheld=dict(position["liquid_total_withheld"]),
        partial_position=PartialPositionOut(
            as_of=position["partial_position"]["as_of"],
            by_currency=dict(position["partial_position"]["by_currency"]),
            contributing_pockets=[
                PartialPocketOut(**entry)
                for entry in position["partial_position"]["contributing_pockets"]
            ],
            short_pockets=[
                PartialPocketOut(**entry)
                for entry in position["partial_position"]["short_pockets"]
            ],
            is_complete=position["partial_position"]["is_complete"],
            reason=position["partial_position"]["reason"],
        ),
        coverage_summary=CoverageSummaryOut(**position["coverage_summary"]),
        combined=CashAtCutoffOut(
            amount=to_consolidated_dto(page.combined.result, currency_registry=CURRENCY_REGISTRY),
            balance_as_of=page.combined.balance_as_of,
            valuation_as_of=page.combined.valuation_as_of.isoformat(),
        ),
        pockets=[_cash_pocket_out(pocket) for pocket in page.pockets],
        stale=position["stale"],
        missing_intervals=[MissingIntervalOut(**gap) for gap in position["missing_intervals"]],
        cash_bridge=CashBridgeOut(
            pockets=[CashBridgePocketOut(**bridge_pocket) for bridge_pocket in bridge["pockets"]],
            combined_by_currency={
                currency: CashBridgeCombinedOut(**cell)
                for currency, cell in combined_bridge["by_currency"].items()
            },
            owned_transfer_pairs_cancelled=combined_bridge["owned_transfer_pairs_cancelled"],
            policy=combined_bridge["policy"],
        ),
    )


class NetWorthMissingComponentOut(_StrictModel):
    component_id: str
    kind: str
    side: str
    reason: str | None


class NetWorthExcludedComponentOut(_StrictModel):
    component_id: str
    kind: str
    side: str
    reason: str | None
    amount: str


class NativeCurrencySummaryOut(_StrictModel):
    """Native, unchanged per-currency totals (§1 invariant 3) shown *beside*
    the reporting-currency totals below -- never replaced by them."""

    tracked_assets: str
    tracked_liabilities: str
    tracked_net_worth: str
    manual_assets: str
    manual_liabilities: str
    net_worth_including_manual: str
    manual_basis_note: str
    subtotal_note: str
    missing_components: list[NetWorthMissingComponentOut]
    excluded_components: list[NetWorthExcludedComponentOut]
    complete: bool
    valuation_dates: list[str]
    as_of: str


class NetWorthComponentOut(_StrictModel):
    component_id: str
    kind: str
    side: str
    currency: str
    amount: str | None
    as_of: str | None
    basis: str
    origin: str
    label: str
    included: bool
    confidence: str | None
    note: str | None
    # §3.3: this component's own reported equivalent, valued at its own
    # as_of date -- ``None`` means no native amount/date to value at all
    # (missing evidence), never a fabricated zero.
    reported: ValuedMoneyDTO | None


class NetWorthTotalsOut(_StrictModel):
    assets: ConsolidatedValuationDTO
    liabilities: ConsolidatedValuationDTO
    net_worth: ConsolidatedValuationDTO
    manual_assets: ConsolidatedValuationDTO
    manual_liabilities: ConsolidatedValuationDTO
    net_worth_including_manual: ConsolidatedValuationDTO


class ScopeChangeOut(_StrictModel):
    component_id: str
    change: str


class NetWorthTrendOut(_StrictModel):
    prior_as_of: str
    current_as_of: str
    prior_net_worth: ConsolidatedValuationDTO
    current_net_worth: ConsolidatedValuationDTO
    delta: MoneyDTO | None
    both_complete: bool
    scope_changed: bool
    scope_changes: list[ScopeChangeOut]
    comparability: str


class NetWorthPageOut(_StrictModel):
    generation: str
    reporting_currency: str
    policy_version: str | None
    as_of: str
    prior_as_of: str | None
    label: str
    is_total_personal_wealth: bool
    disclaimer: str
    notes: list[str]
    components: list[NetWorthComponentOut]
    totals: NetWorthTotalsOut
    trend: NetWorthTrendOut | None
    per_currency: dict[str, NativeCurrencySummaryOut]


def _net_worth_component_out(component: Any) -> NetWorthComponentOut:
    native = component.native
    return NetWorthComponentOut(
        component_id=native["component_id"], kind=native["kind"], side=native["side"],
        currency=native["currency"], amount=native["amount"], as_of=native["as_of"],
        basis=native["basis"], origin=native["origin"], label=native["label"],
        included=native["included"], confidence=native["confidence"], note=native["note"],
        reported=(
            to_dto(component.reported, currency_registry=CURRENCY_REGISTRY)
            if component.reported is not None else None
        ),
    )


def _money_from_fraction(value: Any, *, currency: str) -> MoneyDTO | None:
    if value is None:
        return None
    definition = CURRENCY_REGISTRY.get(currency)
    if definition is None:
        raise FinCLError(f"reporting currency {currency!r} is not in the checked currency registry")
    money = quantize(value, scale=definition.minor_unit_scale)
    return MoneyDTO(value=format_decimal(money.coefficient, money.scale), currency=currency)


def _net_worth_page_out(page: ReportedNetWorthPage) -> NetWorthPageOut:
    def _cv(consolidated: Any) -> ConsolidatedValuationDTO:
        return to_consolidated_dto(consolidated, currency_registry=CURRENCY_REGISTRY)

    native = page.native
    trend_out = None
    if page.trend is not None:
        trend_out = NetWorthTrendOut(
            prior_as_of=page.trend.prior_as_of, current_as_of=page.trend.current_as_of,
            prior_net_worth=_cv(page.trend.prior_net_worth), current_net_worth=_cv(page.trend.current_net_worth),
            delta=_money_from_fraction(page.trend.delta, currency=page.context.reporting_currency),
            both_complete=page.trend.both_complete, scope_changed=page.trend.scope_changed,
            scope_changes=[ScopeChangeOut(**sc) for sc in page.trend.scope_changes],
            comparability=page.trend.comparability,
        )
    return NetWorthPageOut(
        generation=page.context.generation_token, reporting_currency=page.context.reporting_currency,
        policy_version=page.context.policy.policy_version, as_of=str(native["as_of"]),
        prior_as_of=(page.trend.prior_as_of if page.trend is not None else None),
        label=native["label"], is_total_personal_wealth=native["is_total_personal_wealth"],
        disclaimer=native["disclaimer"], notes=list(native["notes"]),
        components=[_net_worth_component_out(component) for component in page.components],
        totals=NetWorthTotalsOut(
            assets=_cv(page.totals.assets), liabilities=_cv(page.totals.liabilities),
            net_worth=_cv(page.totals.net_worth), manual_assets=_cv(page.totals.manual_assets),
            manual_liabilities=_cv(page.totals.manual_liabilities),
            net_worth_including_manual=_cv(page.totals.net_worth_including_manual),
        ),
        trend=trend_out,
        per_currency={
            currency: NativeCurrencySummaryOut(
                tracked_assets=summary["tracked_assets"], tracked_liabilities=summary["tracked_liabilities"],
                tracked_net_worth=summary["tracked_net_worth"], manual_assets=summary["manual_assets"],
                manual_liabilities=summary["manual_liabilities"],
                net_worth_including_manual=summary["net_worth_including_manual"],
                manual_basis_note=summary["manual_basis_note"], subtotal_note=summary["subtotal_note"],
                missing_components=[NetWorthMissingComponentOut(**m) for m in summary["missing_components"]],
                excluded_components=[NetWorthExcludedComponentOut(**x) for x in summary["excluded_components"]],
                complete=summary["complete"], valuation_dates=list(summary["valuation_dates"]),
                as_of=summary["as_of"],
            )
            for currency, summary in native["per_currency"].items()
        },
    )


class ReportedMonthRowOut(_StrictModel):
    month: str
    opening: ValuedMoneyDTO
    closing: ValuedMoneyDTO
    income: ValuedMoneyDTO
    expense: ValuedMoneyDTO
    lowest_balance: ValuedMoneyDTO
    # Observed dispersion around the closing balance: every unscheduled category
    # repeating its own cheapest or dearest observed month, accumulated across
    # the horizon. It is a range with precedent, not a confidence interval, and
    # both ends are absent together when the month has no observed spread.
    closing_low: ValuedMoneyDTO | None = None
    closing_high: ValuedMoneyDTO | None = None


class ReportedForecastEventOut(_StrictModel):
    event_id: str
    source_id: str
    amount: ValuedMoneyDTO


class ReportedForecastScenarioOut(_StrictModel):
    closing_balance: ValuedMoneyDTO
    lowest_balance: ValuedMoneyDTO
    months: list[ReportedMonthRowOut]
    events: list[ReportedForecastEventOut]


class ReportedForecastCurrencyOut(_StrictModel):
    currency: str
    starting_balance: ValuedMoneyDTO
    base: ReportedForecastScenarioOut
    conservative: ReportedForecastScenarioOut


class CombinedMinimumOut(_StrictModel):
    amount: ConsolidatedValuationDTO
    # §7 M4 extrema rule: the calendar month the combined low day falls in
    # -- the owner-defined checkpoint this metric is aligned to. No combined
    # breach date, percentage or flag is added.
    month: str | None


class ForecastTotalsOut(_StrictModel):
    base_closing: ConsolidatedValuationDTO
    conservative_closing: ConsolidatedValuationDTO
    base_combined_minimum: CombinedMinimumOut
    conservative_combined_minimum: CombinedMinimumOut


class ForecastPageOut(_StrictModel):
    generation: str
    reporting_currency: str
    policy_version: str | None
    valuation_as_of: str
    opening_balance_as_of: str | None
    horizon_end: str | None
    currencies: list[ReportedForecastCurrencyOut]
    totals: ForecastTotalsOut
    limitations: list[str]
    # The complete, unchanged native `cash_timeline_from` result (starting
    # balances, cash-cutoff bridge status, monthly/daily walks, milestones,
    # source events, action items, limitations, provenance) -- deliberately
    # untyped here rather than duplicating its already-established frontend
    # shape (`features/forecast/api.ts`'s `ForecastPage`) a second time in
    # this backend model; the frontend casts this field to that existing
    # type rather than re-declaring it as generated schema.
    native: dict[str, Any]


def _reported_money_out(value: Any) -> ValuedMoneyDTO:
    return to_dto(value, currency_registry=CURRENCY_REGISTRY)


def _reported_month_row_out(row: Any) -> ReportedMonthRowOut:
    return ReportedMonthRowOut(
        month=row.month, opening=_reported_money_out(row.opening), closing=_reported_money_out(row.closing),
        income=_reported_money_out(row.income), expense=_reported_money_out(row.expense),
        lowest_balance=_reported_money_out(row.lowest_balance),
        closing_low=_reported_money_out(row.closing_low) if row.closing_low else None,
        closing_high=_reported_money_out(row.closing_high) if row.closing_high else None,
    )


def _reported_scenario_out(cell: ReportedScenarioCell) -> ReportedForecastScenarioOut:
    return ReportedForecastScenarioOut(
        closing_balance=_reported_money_out(cell.closing_balance), lowest_balance=_reported_money_out(cell.lowest_balance),
        months=[_reported_month_row_out(row) for row in cell.months],
        events=[
            ReportedForecastEventOut(event_id=e.event_id, source_id=e.source_id, amount=_reported_money_out(e.amount))
            for e in cell.events
        ],
    )


def _reported_currency_out(currency: ReportedCurrencyForecast) -> ReportedForecastCurrencyOut:
    return ReportedForecastCurrencyOut(
        currency=currency.currency, starting_balance=_reported_money_out(currency.starting_balance),
        base=_reported_scenario_out(currency.base), conservative=_reported_scenario_out(currency.conservative),
    )


def _combined_minimum_out(minimum: CombinedMinimum) -> CombinedMinimumOut:
    return CombinedMinimumOut(
        amount=to_consolidated_dto(minimum.result, currency_registry=CURRENCY_REGISTRY), month=minimum.month,
    )


def _forecast_page_out(page: ReportedForecastPage) -> ForecastPageOut:
    return ForecastPageOut(
        generation=page.context.generation_token, reporting_currency=page.context.reporting_currency,
        policy_version=page.context.policy.policy_version, valuation_as_of=page.valuation_as_of.isoformat(),
        opening_balance_as_of=page.opening_balance_as_of, horizon_end=page.horizon_end,
        currencies=[_reported_currency_out(currency) for currency in page.currencies],
        totals=ForecastTotalsOut(
            base_closing=to_consolidated_dto(page.totals.base_closing, currency_registry=CURRENCY_REGISTRY),
            conservative_closing=to_consolidated_dto(page.totals.conservative_closing, currency_registry=CURRENCY_REGISTRY),
            base_combined_minimum=_combined_minimum_out(page.totals.base_combined_minimum),
            conservative_combined_minimum=_combined_minimum_out(page.totals.conservative_combined_minimum),
        ),
        limitations=list(page.limitations),
        native=dict(page.native),
    )


class ReportedOccurrenceOut(_StrictModel):
    anchor: str
    reported: ValuedMoneyDTO | None


class ReportedVariationOut(_StrictModel):
    latest: ValuedMoneyDTO | None
    minimum: ValuedMoneyDTO | None
    maximum: ValuedMoneyDTO | None
    mean: MoneyDTO | None
    priced_count: int
    total_count: int


class ReportedIncomeCandidateOut(_StrictModel):
    # Only the identity and reported detail live here; every other native
    # fact (merchant, currency, cadence, timing window, confidence, review
    # question, native occurrence amount/date) stays on `IncomeCandidatesPageOut
    # .native` unchanged -- the same documented passthrough choice as the
    # reported Forecast page's own `native` field (step 14).
    candidate: str
    occurrences: list[ReportedOccurrenceOut]
    variation: ReportedVariationOut


class IncomeCandidatesPageOut(_StrictModel):
    generation: str
    reporting_currency: str
    policy_version: str | None
    valuation_as_of: str
    candidates: list[ReportedIncomeCandidateOut]
    # The complete, unchanged native `{generation, candidates, exclusions,
    # policy}` slice `recurring_page_from` already exposes through the
    # existing `GET /api/v1/inflows/candidates` route -- deliberately
    # untyped here rather than duplicating that already-established
    # frontend shape a second time as generated schema.
    native: dict[str, Any]


def _reported_occurrence_out(occurrence: Any) -> ReportedOccurrenceOut:
    return ReportedOccurrenceOut(
        anchor=str(occurrence.native["anchor"]),
        reported=(
            to_dto(occurrence.reported, currency_registry=CURRENCY_REGISTRY)
            if occurrence.reported is not None else None
        ),
    )


def _reported_variation_out(variation: ReportedVariation, *, currency: str) -> ReportedVariationOut:
    return ReportedVariationOut(
        latest=(to_dto(variation.latest, currency_registry=CURRENCY_REGISTRY) if variation.latest is not None else None),
        minimum=(to_dto(variation.minimum, currency_registry=CURRENCY_REGISTRY) if variation.minimum is not None else None),
        maximum=(to_dto(variation.maximum, currency_registry=CURRENCY_REGISTRY) if variation.maximum is not None else None),
        mean=_money_from_fraction(variation.mean, currency=currency),
        priced_count=variation.priced_count, total_count=variation.total_count,
    )


def _reported_income_candidate_out(candidate: ReportedIncomeCandidate, *, currency: str) -> ReportedIncomeCandidateOut:
    return ReportedIncomeCandidateOut(
        candidate=str(candidate.native["candidate"]),
        occurrences=[_reported_occurrence_out(occurrence) for occurrence in candidate.occurrences],
        variation=_reported_variation_out(candidate.variation, currency=currency),
    )


def _income_candidates_page_out(page: ReportedIncomeCandidatesPage) -> IncomeCandidatesPageOut:
    currency = page.context.reporting_currency
    return IncomeCandidatesPageOut(
        generation=page.context.generation_token, reporting_currency=currency,
        policy_version=page.context.policy.policy_version,
        valuation_as_of=page.context.scope.valuation_as_of.isoformat(),
        candidates=[_reported_income_candidate_out(candidate, currency=currency) for candidate in page.candidates],
        native=dict(page.native),
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
            resolved_as_of = date.fromisoformat(as_of) if as_of else clock.today()
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

    @app.get('/api/v1/money-flow', response_model=MoneyFlowOut)
    def money_flow_read(
        history_start: str, history_end: str, as_of: str | None = None, pocket_currency: str | None = None,
    ) -> Any:
        """M3b.3 step 09: the composite Overview read (§3.3-3.4, §6.2).

        One connection/one read transaction composing reported Analysis,
        supported cash-at-cutoff and both forecast closings; additive, and
        introduces no per-request reporting-currency override (§6.1).
        """
        connection = connect_readonly(database)
        try:
            resolved_as_of = date.fromisoformat(as_of) if as_of else clock.today()
            result = money_flow_page_from(
                connection, today=resolved_as_of, history_start=history_start, history_end=history_end,
                pocket_currency=pocket_currency,
            )
            return _money_flow_out(result)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()

    @app.get('/api/v1/reporting/cash', response_model=CashPageOut)
    def reported_cash_read(as_of: str | None = None, pocket_currency: str | None = None) -> Any:
        """M4 step 12: the reported Cash workspace (§3.3).

        Additive to the legacy ``/api/v1/position``, which is unchanged.
        Every native fact (basis, as-of date, staleness, owner exclusions,
        reconciliation status, unsupported intervals) is read straight
        through; only a reported equivalent is attached, at each amount's own
        as-of date, never summed across different dates into a same-date total.
        """
        connection = connect_readonly(database)
        try:
            resolved_as_of = date.fromisoformat(as_of) if as_of else clock.today()
            page = reported_cash_page_from(
                connection, today=resolved_as_of, pocket_currency=pocket_currency,
            )
            return _cash_page_out(page)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()

    @app.get('/api/v1/reporting/net-worth', response_model=NetWorthPageOut)
    def reported_net_worth_read(as_of: str | None = None, prior_as_of: str | None = None) -> Any:
        """M4 step 13: the reported Net worth workspace (§3.3, §7 M4).

        Additive to the legacy ``/api/v1/net-worth``, which is unchanged.
        Every native component/inclusion/exclusion fact is read straight
        through; only a reported equivalent is attached, each valued at that
        component's own as-of date, then combined into one reporting-currency
        total -- never a native delta re-priced at today's rate for the trend.
        """
        connection = connect_readonly(database)
        try:
            # ``today`` is the actual wall-clock date (native ``obligations_
            # page_from``'s own separate aging input) -- never overridden by
            # the requested ``as_of`` valuation date, exactly like the native
            # ``/api/v1/net-worth`` route this one is additive to.
            page = reported_net_worth_page_from(
                connection, as_of=as_of, prior_as_of=prior_as_of, today=clock.today(),
            )
            return _net_worth_page_out(page)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()

    @app.get('/api/v1/reporting/forecast', response_model=ForecastPageOut)
    def reported_forecast_read() -> Any:
        """M4 step 14: the reported Forecast workspace (§3.3, §7 M4).

        Additive to the legacy ``/api/v1/forecast``, which is unchanged.
        Takes no query parameter at all: the horizon is owner-defined from
        the supported cash cutoff, never a caller-supplied history window
        (§7 M4: "A history filter must not change horizon").
        """
        connection = connect_readonly(database)
        try:
            page = reported_forecast_page_from(connection, today=clock.today())
            return _forecast_page_out(page)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()

    @app.get('/api/v1/reporting/inflows/candidates', response_model=IncomeCandidatesPageOut)
    def reported_income_candidates_read() -> Any:
        """M4 step 15: the reported Income (Inflows) workspace (§3.1, §7 M4).

        Additive to the legacy ``/api/v1/inflows/candidates``, which is
        unchanged. Takes no query parameter: the queue's scope is unresolved
        income work across the whole ledger, not the Overview history range,
        and there is no record filter to narrow since the native detector
        accepts none (§7 M4: "defaults to all pockets").
        """
        connection = connect_readonly(database)
        try:
            page = reported_income_candidates_page_from(connection, today=clock.today())
            return _income_candidates_page_out(page)
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

    @app.get('/api/v1/reporting/transactions', response_model=TransactionsPageOut)
    def reported_transactions_read(
        history_start: str,
        history_end: str,
        as_of: str | None = None,
        pocket_currency: str | None = None,
        account: str = '',
        category: str = '',
        search: str = '',
        kind: str = '',
        direction: str = '',
        behavior: str = '',
        need: str = '',
        basis: str = '',
        unresolved_only: bool = False,
        include_excluded: bool = False,
        sort: str = 'date',
        cursor: str | None = None,
        limit: int = 50,
        generation: str | None = None,
    ) -> Any:
        """M3b.2 step 07: all-currency transaction list, default all pockets.

        Additive to the legacy ``/api/v1/transactions``, whose single-pocket
        default and native amount ordering are unchanged.
        """
        connection = connect_readonly(database)
        try:
            resolved_as_of = date.fromisoformat(as_of) if as_of else clock.today()
            page = reported_transactions_page_from(
                connection, as_of=resolved_as_of, history_start=history_start, history_end=history_end,
                pocket_currency=pocket_currency, account=account, category=category, search=search,
                kind=kind, direction=direction, behavior=behavior, need=need, basis=basis,
                unresolved_only=unresolved_only, include_excluded=include_excluded, sort=sort,
                cursor=cursor, limit=limit, generation=generation,
            )
            return TransactionsPageOut(
                generation=page.context.generation_token, reporting_currency=page.context.reporting_currency,
                sort=page.sort, rows=[_transaction_row_out(row) for row in page.rows],
                total=page.total, next_cursor=page.next_cursor,
            )
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()

    @app.get('/api/v1/reporting/transaction-anchors', response_model=TransactionAnchorsOut)
    def reported_transaction_anchors_read(
        history_start: str,
        history_end: str,
        as_of: str | None = None,
        pocket_currency: str | None = None,
        account: str = '',
        category: str = '',
        search: str = '',
        kind: str = '',
        direction: str = '',
        behavior: str = '',
        need: str = '',
        basis: str = '',
        unresolved_only: bool = False,
        include_excluded: bool = False,
        generation: str | None = None,
    ) -> Any:
        """M3b.2 step 07: the same candidate set as the list above, for bulk
        selection. Additive to the legacy ``/api/v1/transaction-anchors``."""
        connection = connect_readonly(database)
        try:
            resolved_as_of = date.fromisoformat(as_of) if as_of else clock.today()
            result = reported_transaction_anchors_from(
                connection, as_of=resolved_as_of, history_start=history_start, history_end=history_end,
                pocket_currency=pocket_currency, account=account, category=category, search=search,
                kind=kind, direction=direction, behavior=behavior, need=need, basis=basis,
                unresolved_only=unresolved_only, include_excluded=include_excluded, generation=generation,
            )
            return TransactionAnchorsOut(
                generation=result.context.generation_token, anchors=list(result.anchors),
                truncated=result.truncated, cap=result.cap,
            )
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()

    @app.get('/api/v1/reporting/transactions/{anchor}', response_model=TransactionRowOut)
    def reported_transaction_detail_read(
        anchor: str, as_of: str | None = None, generation: str | None = None
    ) -> Any:
        """M3b.2 step 07: one entry's native detail, reported amount/fee and
        normalized statement-money detail (§5.3), selected through the same
        candidate view as the list -- no other narrowing applies."""
        connection = connect_readonly(database)
        try:
            resolved_as_of = date.fromisoformat(as_of) if as_of else clock.today()
            row = reported_transaction_detail_from(
                connection, as_of=resolved_as_of, anchor=decode_anchor(anchor), generation=generation,
            )
            return _transaction_row_out(row)
        except FinCLError as exc:
            if str(exc).startswith('STALE_GENERATION'):
                raise HTTPException(409, str(exc)) from exc
            raise
        finally:
            connection.close()
