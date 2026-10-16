"""Reported Analysis: FLOW valuation over the shared, generation-pinned
economic component projection (§3.1-3.2, §6; plan M3b.1 step 04).

Every reader here reads the same ``analysis_components`` rows the native
``analysis_page_from``/``contributors_page`` already read (materialized once
per refresh by ``analysis.materialize``, from the same ``build_components``
projection the MVP economic-flow report shares) — never a second component
projection, and never a default currency predicate. Only an explicit
``pocket_currency`` narrows the row set; every other filter (accounts,
category_keys, merchant, date window) is the same native meaning the legacy
route already uses.

Valuation itself is never re-derived here: every reported amount goes through
``valuation.consolidate``/``value_one`` under ``flow-rate-v1``, so a total,
its bucket partitions and every contributor row underneath it are computed by
the same function and therefore cannot silently disagree.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any

from ..coverage import CoverageContract, coverage_contract, latest_covered_complete_months
from ..errors import FinCLError
from ..intervals import DateInterval, month_span
from ..money import ExactMoney, format_decimal
from ..reporting_context import ReportedScope, ReportingContext, build_reporting_context
from ..valuation import (
    FLOW_RATE_V1,
    ConsolidatedValuation,
    ConsolidationStatus,
    EntryPrecision,
    ExactValuation,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    consolidate,
    eligible_month_ceiling,
    quantize,
)
from .handles import decode_reported, encode_reported
from .scopes import AnalysisScope, resolve_comparison

LENS_RECORDED_FLOW = "recorded_flow"

# Legacy `analysis/queries.py`'s exact bucket set (no "other": that bucket
# nets zero and carries no dedicated summary row in the native surface
# either). `exceptional_inflow`/`exceptional_loss` are handled separately
# below, not folded into this list, because they need their own disclosure
# rather than one more anonymous row (§3.1's "exceptional_loss ... is not
# automatically ordinary spending or burn").
SUMMARY_BUCKETS = (
    "spending", "fees", "refunds", "income",
    "reimbursements", "non_income_inflow", "pending_review",
)
# Buckets shown as a positive magnitude, matching
# `analysis.components.ComponentProjection.bucket_total`'s existing
# convention (spending/fees/pending_review are recorded with a negative or
# zero-net native sign but always displayed as a positive amount spent).
_MAGNITUDE_BUCKETS = frozenset({"spending", "fees", "pending_review"})
_EXCEPTIONAL_BUCKETS = frozenset({"exceptional_inflow", "exceptional_loss"})

DEFAULT_BURN_MONTH_LIMIT = 3


def _flow_context(reporting_currency: str, as_of: date) -> ValuationContext:
    """One shared FLOW context (§3.1). ``as_of`` is carried on the wire DTO
    only — FLOW's own rate selection never reads it (period drives it)."""
    return ValuationContext(
        reporting_currency=reporting_currency, purpose=ValuationPurpose.FLOW,
        valuation_as_of=as_of, rate_selection_policy=FLOW_RATE_V1,
    )


_NO_EVIDENCE_COVERAGE = "NO_EVIDENCE_COVERAGE"


def _uncovered_result(context: ValuationContext) -> ConsolidatedValuation:
    """A window outside evidence coverage: unavailable for lack of *evidence*,
    never a fabricated zero and never confused with a missing-FX gap (§4.3)."""
    return ConsolidatedValuation(
        total=None, status=ConsolidationStatus.UNAVAILABLE, partial_total=None,
        included=(), excluded=(), excluded_entry_count=0,
        unavailable_reasons=(_NO_EVIDENCE_COVERAGE,), excluded_detail=(),
        rate_inputs=(), context=context, values=(),
    )


def _delta_and_pct(
    current: ConsolidatedValuation, previous: ConsolidatedValuation | None
) -> tuple[Fraction | None, str | None]:
    """Exact delta and percentage change (§3.1 item 4).

    Both require *complete* totals on both sides — a percentage built from a
    partial total would silently compare an incomplete scope against a
    complete one. ``pct`` reuses ``quantize`` (half-away-from-zero, one
    decimal place) so the rounding rule is the same one every displayed
    money amount already uses, not a second one invented for percentages.
    """
    if (
        previous is None
        or current.status is not ConsolidationStatus.AVAILABLE
        or previous.status is not ConsolidationStatus.AVAILABLE
    ):
        return None, None
    assert current.total is not None and previous.total is not None
    delta = current.total - previous.total
    if previous.total == 0:
        return delta, None
    pct_fraction = delta / previous.total * 100
    return delta, quantize(pct_fraction, scale=1).text


def _item(row: sqlite3.Row, *, magnitude: bool) -> ValuationItem:
    coefficient = int(row["signed_coefficient"])
    if magnitude:
        coefficient = abs(coefficient)
    scale = int(row["scale"])
    native = ExactMoney(coefficient, scale, format_decimal(coefficient, scale))
    return ValuationItem(
        native=native, native_currency=str(row["currency"]), purpose=ValuationPurpose.FLOW,
        precision=EntryPrecision(str(row["date_precision"]).lower()),
        period_month=str(row["period"]),
    )


def _rows(
    connection: sqlite3.Connection,
    *,
    generation_token: str,
    start: str | None = None,
    end: str | None = None,
    periods: Sequence[str] | None = None,
    pocket_currency: str | None,
    accounts: Sequence[str] = (),
    category_keys: Sequence[str] = (),
    merchant: str | None = None,
) -> list[sqlite3.Row]:
    """Every eligible materialized component row matching this scope.

    Exactly one of ``(start, end)`` or ``periods`` is used to bound the
    window: a date range for the ordinary analysis window, or an explicit set
    of ``YYYY-MM`` months for the burn reader, which searches its own month
    scope independently of the requested history window (§3.2, §4.3).
    """
    where = ["generation_token = :gen", "lens = :lens", "eligible = 1"]
    params: dict[str, Any] = {"gen": generation_token, "lens": LENS_RECORDED_FLOW}
    if periods is not None:
        names = ", ".join(f":p{i}" for i in range(len(periods)))
        where.append(f"period IN ({names})")
        params.update({f"p{i}": period for i, period in enumerate(periods)})
    else:
        assert start is not None and end is not None
        where.append("represented_start >= :start")
        where.append("represented_end <= :end")
        params.update(start=start, end=end)
    if pocket_currency is not None:
        where.append("currency = :currency")
        params["currency"] = pocket_currency
    if accounts:
        names = ", ".join(f":a{i}" for i in range(len(accounts)))
        where.append(f"account IN ({names})")
        params.update({f"a{i}": account for i, account in enumerate(accounts)})
    if category_keys:
        names = ", ".join(f":c{i}" for i in range(len(category_keys)))
        where.append(f"category_key IN ({names})")
        params.update({f"c{i}": key for i, key in enumerate(category_keys)})
    if merchant is not None:
        where.append("merchant_display = :merchant")
        params["merchant"] = merchant
    return connection.execute(
        "SELECT * FROM analysis_components WHERE " + " AND ".join(where), params
    ).fetchall()


@dataclass(frozen=True, slots=True)
class SummaryEntry:
    """One bucket's current/prior consolidated result plus its drill handle.

    ``delta``/``pct`` are computed here, once, from the two sides' exact
    Fractions (§3.1 item 4: "value each side... then subtract exact reported
    totals") — never left to a caller to reconstruct from two already
    -rendered decimal strings. Both are ``None`` unless *both* sides are
    fully ``AVAILABLE``: a percentage or delta built from a partial total
    would silently compare incomplete scopes (§3.1: "do not silently compare
    different partial scopes").
    """

    bucket: str
    current: ConsolidatedValuation
    previous: ConsolidatedValuation | None
    comparison_available: bool
    comparison_reasons: tuple[str, ...]
    handle: str
    delta: Fraction | None
    pct: str | None


@dataclass(frozen=True, slots=True)
class RankedGroup:
    name: str
    result: ConsolidatedValuation
    handle: str


@dataclass(frozen=True, slots=True)
class GrossBurnResult:
    """§3.2 gross-burn-v2. ``result`` is ``None`` only when no evidence
    -covered complete month was found at all (never a manufactured zero)."""

    result: ConsolidatedValuation | None
    included_months: tuple[str, ...]
    month_count: int
    reason: str | None


@dataclass(frozen=True, slots=True)
class ReportedAnalysisResult:
    context: ReportingContext
    covered: bool
    coverage_reasons: tuple[str, ...]
    comparison_kind: str
    summary: dict[str, SummaryEntry]
    gross_burn: GrossBurnResult
    category_ranking: tuple[RankedGroup, ...]
    merchant_ranking: tuple[RankedGroup, ...]
    contributors_handle: str
    trend: tuple[TrendMonth, ...]


@dataclass(frozen=True, slots=True)
class TrendMonth:
    """One calendar month's reported spending/fees/income (§6.3 "trends").

    ``complete`` mirrors the native trend's own rule: a month counts only
    when it is wholly inside the evidence-coverage contract. An incomplete
    month's cells are the same ``_uncovered_result`` placeholder the main
    summary uses for an uncovered window — a gap, never a manufactured zero.
    """

    month: str
    spending: ConsolidatedValuation
    fees: ConsolidatedValuation
    income: ConsolidatedValuation
    complete: bool
    previous_spending: ConsolidatedValuation | None = None
    spending_change: Fraction | None = None
    spending_change_pct: str | None = None
    spending_change_reason: str | None = None


_TREND_RANGES = ("6", "12", "all")


def _bucket_items(rows: Sequence[sqlite3.Row], bucket: str) -> list[ValuationItem]:
    magnitude = bucket in _MAGNITUDE_BUCKETS
    return [_item(row, magnitude=magnitude) for row in rows if row["bucket"] == bucket]


def _net_partition_items(rows: Sequence[sqlite3.Row]) -> tuple[
    list[ValuationItem], list[ValuationItem], list[ValuationItem], list[ValuationItem]
]:
    """``(net, inflow, outflow, exceptional)`` item lists (§3.1).

    ``net`` is every ``nets=1`` component at its true signed native amount.
    ``inflow``/``outflow`` partition that same set by sign, ``outflow`` as a
    positive magnitude so ``inflow - outflow == net`` holds exactly.
    ``exceptional`` is the ``nets=1`` subset already inside ``net`` whose
    bucket is a debt forgiveness/write-off — disclosed separately, never
    excluded from ``net`` (§3.1's correction: not automatically ordinary flow,
    but still part of it).
    """
    net_rows = [row for row in rows if int(row["nets"])]
    net = [_item(row, magnitude=False) for row in net_rows]
    inflow = [_item(row, magnitude=False) for row in net_rows if int(row["signed_coefficient"]) > 0]
    outflow = [_item(row, magnitude=True) for row in net_rows if int(row["signed_coefficient"]) < 0]
    exceptional = [_item(row, magnitude=False) for row in net_rows if row["bucket"] in _EXCEPTIONAL_BUCKETS]
    return net, inflow, outflow, exceptional


def _comparison_scope(start: str, end: str) -> AnalysisScope:
    # `resolve_comparison`/`is_full_calendar_month` only ever read
    # start/end off this object; currency/accounts/category/merchant are
    # unused by that function and are never consulted, so a placeholder
    # here reuses the native comparison-window rule without duplicating it.
    return AnalysisScope(
        start=start, end=end, currency="", lens=LENS_RECORDED_FLOW,
        accounts=(), category_keys=(), merchant=None,
    )


def _handle_for(
    context: ReportingContext, *, start: str, end: str, buckets: list[str],
    category_key: str | None = None, merchant_display: str | None = None,
) -> str:
    return encode_reported(
        generation=context.generation_token,
        reporting_currency=context.reporting_currency,
        policy_version=context.policy.policy_version,
        rate_inventory=context.rate_inventory_digest,
        as_of=context.scope.valuation_as_of.isoformat(),
        lens=LENS_RECORDED_FLOW,
        start=start, end=end, buckets=buckets, pocket_currency=context.scope.pocket_currency,
        category_key=category_key, merchant_display=merchant_display,
    )


def reported_analysis_page_from(
    connection: sqlite3.Connection,
    *,
    as_of: date,
    history_start: str,
    history_end: str,
    pocket_currency: str | None = None,
    accounts: Sequence[str] = (),
    category_keys: Sequence[str] = (),
    merchant: str | None = None,
    comparison: str | None = None,
    top_n: int = 10,
    burn_month_limit: int = DEFAULT_BURN_MONTH_LIMIT,
    trend_range: str = "6",
    reporting_currency: str | None = None,
) -> ReportedAnalysisResult:
    """The reported Analysis panel: summary buckets, gross burn, rankings (§3, §6)."""
    if trend_range not in _TREND_RANGES:
        raise FinCLError(f"trend_range must be one of: {', '.join(_TREND_RANGES)}")
    scope = ReportedScope(
        valuation_as_of=as_of, history_start=history_start, history_end=history_end,
        pocket_currency=pocket_currency,
        filters={"accounts": tuple(accounts), "category_keys": tuple(category_keys), "merchant": merchant},
    )
    context = build_reporting_context(connection, scope=scope)
    target_currency = reporting_currency or context.reporting_currency
    flow_context = _flow_context(target_currency, as_of)

    contract = coverage_contract(connection)
    covered = any(
        interval.contains_interval(DateInterval(history_start, history_end))
        for interval in contract.covered_intervals
    )
    comp = resolve_comparison(
        _comparison_scope(history_start, history_end), contract.covered_intervals,
        requested=comparison, main_covered=covered,
    )

    current_rows = _rows(
        connection, generation_token=context.generation_token, start=history_start, end=history_end,
        pocket_currency=pocket_currency, accounts=accounts, category_keys=category_keys, merchant=merchant,
    ) if covered else []
    previous_rows = _rows(
        connection, generation_token=context.generation_token,
        start=comp.start, end=comp.end,
        pocket_currency=pocket_currency, accounts=accounts, category_keys=category_keys, merchant=merchant,
    ) if comp.available else []

    summary: dict[str, SummaryEntry] = {}

    def _entry(name: str, buckets: list[str], cur_items: list[ValuationItem], prev_items: list[ValuationItem]) -> None:
        # An uncovered window is missing *evidence*, not missing FX (§4.3): a
        # fresh `consolidate([])` here would read as a real zero, not as "we
        # do not know". `_uncovered_result` names that gap with its own
        # reason instead, kept out of `consolidate`'s FX-only vocabulary.
        current_result = (
            consolidate(connection, cur_items, context=flow_context)
            if covered else _uncovered_result(flow_context)
        )
        previous_result = (
            consolidate(connection, prev_items, context=flow_context) if comp.available else None
        )
        delta, pct = _delta_and_pct(current_result, previous_result)
        summary[name] = SummaryEntry(
            bucket=name,
            current=current_result,
            previous=previous_result,
            comparison_available=comp.available, comparison_reasons=comp.reasons,
            handle=_handle_for(context, start=history_start, end=history_end, buckets=buckets),
            delta=delta, pct=pct,
        )

    for bucket in SUMMARY_BUCKETS:
        _entry(bucket, [bucket], _bucket_items(current_rows, bucket), _bucket_items(previous_rows, bucket))

    net_items, inflow_items, outflow_items, exceptional_items = _net_partition_items(current_rows)
    prev_net_items, prev_inflow_items, prev_outflow_items, prev_exceptional_items = (
        _net_partition_items(previous_rows) if comp.available else ([], [], [], [])
    )
    # "net"/"inflow"/"outflow" are sign-based partitions of the nets=1 set,
    # not bucket memberships, so their drill-through handles use the three
    # sentinel selectors `decode_reported` maps back onto that exact sign
    # test (§3.1) rather than a `bucket IN (...)` clause.
    _entry("net_economic_flow", ["net"], net_items, prev_net_items)
    _entry("inflow", ["inflow"], inflow_items, prev_inflow_items)
    _entry("outflow", ["outflow"], outflow_items, prev_outflow_items)
    _entry(
        "exceptional_adjustments", ["exceptional_inflow", "exceptional_loss"],
        exceptional_items, prev_exceptional_items,
    )

    gross_burn = _gross_burn(
        connection, context=context, flow_context=flow_context, history_end=history_end,
        pocket_currency=pocket_currency, burn_month_limit=burn_month_limit,
    )

    category_ranking = _ranked(
        current_rows, key="category_key", flow_context=flow_context, connection=connection,
        context=context, start=history_start, end=history_end, top_n=top_n,
    )
    merchant_ranking = _ranked(
        current_rows, key="merchant_display", flow_context=flow_context, connection=connection,
        context=context, start=history_start, end=history_end, top_n=top_n,
    )

    trend = _trend(
        connection, context=context, flow_context=flow_context, contract=contract,
        history_end=history_end, trend_range=trend_range, pocket_currency=pocket_currency,
        accounts=accounts, category_keys=category_keys, merchant=merchant,
    )

    return ReportedAnalysisResult(
        context=context, covered=covered, coverage_reasons=tuple(contract.reasons),
        comparison_kind=comp.kind, summary=summary, gross_burn=gross_burn,
        category_ranking=category_ranking, merchant_ranking=merchant_ranking,
        contributors_handle=_handle_for(context, start=history_start, end=history_end, buckets=["spending"]),
        trend=trend,
    )


def _trend(
    connection: sqlite3.Connection,
    *,
    context: ReportingContext,
    flow_context: ValuationContext,
    contract: CoverageContract,
    history_end: str,
    trend_range: str,
    pocket_currency: str | None,
    accounts: Sequence[str],
    category_keys: Sequence[str],
    merchant: str | None,
) -> tuple[TrendMonth, ...]:
    """Complete calendar months ending at the closed-month anchor at or before
    ``history_end`` (§6.3). Reuses step 01's ``eligible_month_ceiling`` for
    that anchor instead of re-deriving the same closed-month arithmetic."""
    anchor = eligible_month_ceiling(date.fromisoformat(history_end), closed_month_only=True)
    anchor_index = int(anchor[:4]) * 12 + int(anchor[5:7]) - 1
    if trend_range == "all" and contract.covered_intervals:
        first = min(interval.start for interval in contract.covered_intervals)
        first_date = date.fromisoformat(first)
        first_index = first_date.year * 12 + first_date.month - 1
        months = max(1, anchor_index - first_index + 1)
    else:
        months = int(trend_range)

    result: list[TrendMonth] = []
    previous: ConsolidatedValuation | None = None
    # Read one extra month so the first visible month can still be compared
    # with its calendar predecessor, under exactly the same filters and rates.
    for offset in range(months, -1, -1):
        year, month = divmod(anchor_index - offset, 12)
        month += 1
        ym = f"{year:04d}-{month:02d}"
        full = any(interval.contains_interval(month_span(ym)) for interval in contract.covered_intervals)
        if full:
            rows = _rows(
                connection, generation_token=context.generation_token, periods=[ym],
                pocket_currency=pocket_currency, accounts=accounts, category_keys=category_keys,
                merchant=merchant,
            )
            spending = consolidate(connection, _bucket_items(rows, "spending"), context=flow_context)
            fees = consolidate(connection, _bucket_items(rows, "fees"), context=flow_context)
            income = consolidate(connection, _bucket_items(rows, "income"), context=flow_context)
        else:
            spending = fees = income = _uncovered_result(flow_context)
        delta, pct = _delta_and_pct(spending, previous)
        reason = None
        if delta is None:
            reason = "Comparison unavailable: both calendar months need full statement coverage and complete currency valuation."
        elif pct is None:
            reason = "Previous month spending was zero; percentage change is undefined."
        if offset < months:
            result.append(TrendMonth(
                month=ym, spending=spending, fees=fees, income=income, complete=full,
                previous_spending=previous, spending_change=delta,
                spending_change_pct=pct, spending_change_reason=reason,
            ))
        previous = spending
    return tuple(result)


def _ranked(
    rows: Sequence[sqlite3.Row], *, key: str, flow_context: ValuationContext,
    connection: sqlite3.Connection, context: ReportingContext, start: str, end: str, top_n: int,
) -> tuple[RankedGroup, ...]:
    """Consolidate every group first, then rank — never a per-pocket top N
    concatenated afterward (§3.1: "do not concatenate each pocket's top N")."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["bucket"] != "spending":
            continue
        name = row[key]
        if name is None:
            continue
        groups.setdefault(str(name), []).append(row)

    results = {
        name: consolidate(connection, [_item(row, magnitude=True) for row in group], context=flow_context)
        for name, group in groups.items()
    }

    def sort_key(item: tuple[str, ConsolidatedValuation]) -> tuple[bool, Fraction]:
        name, result = item
        magnitude = result.total if result.total is not None else result.partial_total
        return (magnitude is None, -(magnitude if magnitude is not None else Fraction(0)))

    ranked = sorted(results.items(), key=sort_key)[:top_n]
    handle_kwarg = "category_key" if key == "category_key" else "merchant_display"
    return tuple(
        RankedGroup(
            name=name, result=result,
            handle=_handle_for(
                context, start=start, end=end, buckets=["spending"], **{handle_kwarg: name}
            ),
        )
        for name, result in ranked
    )


def _gross_burn(
    connection: sqlite3.Connection,
    *,
    context: ReportingContext,
    flow_context: ValuationContext,
    history_end: str,
    pocket_currency: str | None,
    burn_month_limit: int,
) -> GrossBurnResult:
    """§3.2 gross-burn-v2: reuse step 02's shared burn-month selector, never
    a second rule, and average by that fixed month count regardless of FX
    availability (a missing rate makes the *amount* partial, not the count)."""
    contract = coverage_contract(connection)
    months = latest_covered_complete_months(
        contract, history_end=date.fromisoformat(history_end), limit=burn_month_limit,
    )
    if not months:
        return GrossBurnResult(
            result=None, included_months=(), month_count=0,
            reason=(
                "no evidence-covered complete calendar month was found on or before "
                f"{history_end}"
            ),
        )
    rows = _rows(
        connection, generation_token=context.generation_token, periods=months,
        pocket_currency=pocket_currency,
    )
    items = [
        _item(row, magnitude=True) for row in rows if row["bucket"] in ("spending", "fees")
    ]
    summed = consolidate(connection, items, context=flow_context)
    month_count = len(months)
    averaged = dataclasses.replace(
        summed,
        total=(summed.total / month_count) if summed.total is not None else None,
        partial_total=(summed.partial_total / month_count) if summed.partial_total is not None else None,
    )
    return GrossBurnResult(
        result=averaged, included_months=tuple(months), month_count=month_count, reason=None,
    )


# --- reported contributors (§6, step 04 items 5-6) --------------------------

_SENTINEL_BUCKETS = frozenset({"net", "inflow", "outflow"})


@dataclass(frozen=True, slots=True)
class OwningSource:
    """Where this component actually comes from (§4.3.6: never a fabricated
    bank transaction id for a component with no ledger row of its own)."""

    kind: str  # "transaction" | "debt_event"
    id: str


@dataclass(frozen=True, slots=True)
class ContributorRow:
    component_id: str
    origin_kind: str
    owning_source: OwningSource
    role: str
    bucket: str
    valuation: ExactValuation
    category: str
    merchant: str | None
    account: str
    date: str
    precision: str
    source_location: str
    nets: bool


@dataclass(frozen=True, slots=True)
class ContributorsPageResult:
    context: ReportingContext
    scope_start: str
    scope_end: str
    buckets: tuple[str, ...]
    pocket_currency: str | None
    category_key: str | None
    merchant_display: str | None
    sort: str
    rows: tuple[ContributorRow, ...]
    total: int
    next_cursor: str | None
    parent: ConsolidatedValuation


def _magnitude_for_buckets(buckets: Sequence[str]) -> bool:
    return bool(set(buckets) & (_MAGNITUDE_BUCKETS | {"outflow"}))


def _rows_for_buckets(
    connection: sqlite3.Connection, *, generation_token: str, buckets: Sequence[str],
    start: str, end: str, pocket_currency: str | None,
    category_key: str | None, merchant_display: str | None,
) -> list[sqlite3.Row]:
    rows = _rows(
        connection, generation_token=generation_token, start=start, end=end,
        pocket_currency=pocket_currency,
        category_keys=(category_key,) if category_key else (),
        merchant=merchant_display,
    )
    sentinel = set(buckets) & _SENTINEL_BUCKETS
    if sentinel:
        selector = next(iter(sentinel))
        rows = [row for row in rows if int(row["nets"])]
        if selector == "inflow":
            rows = [row for row in rows if int(row["signed_coefficient"]) > 0]
        elif selector == "outflow":
            rows = [row for row in rows if int(row["signed_coefficient"]) < 0]
        return rows
    return [row for row in rows if row["bucket"] in buckets]


def _owning_source(row: sqlite3.Row) -> OwningSource:
    if row["origin_kind"] == "DEBT_EVENT":
        return OwningSource(kind="debt_event", id=str(row["origin_anchor"]))
    return OwningSource(kind="transaction", id=str(row["origin_anchor"]))


def _sort_key_amount(pair: tuple[sqlite3.Row, ExactValuation]) -> tuple[bool, Fraction, str, str]:
    row, value = pair
    if value.unavailable is not None:
        return (True, Fraction(0), str(row["origin_anchor"]), str(row["component_id"]))
    assert value.reported_exact is not None
    return (False, -abs(value.reported_exact), str(row["origin_anchor"]), str(row["component_id"]))


def _sort_key_date(pair: tuple[sqlite3.Row, ExactValuation]) -> tuple[int, str, str]:
    row, _value = pair
    ordinal = date.fromisoformat(str(row["represented_start"])).toordinal()
    return (-ordinal, str(row["origin_anchor"]), str(row["component_id"]))


def _encode_cursor(*, handle: str, offset: int) -> str:
    payload = {"handle_bind": _sign_bind(handle), "offset": offset}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _sign_bind(handle: str) -> str:
    # A cursor is meaningless outside the exact recomputed ordering its own
    # handle produced; binding it to a short digest of the handle (not the
    # handle's own HMAC — that is `handles.py`'s concern) stops a cursor
    # minted under one scope from being replayed against a different one.
    return hashlib.sha256(handle.encode()).hexdigest()[:16]


def _decode_offset(cursor: str | None, *, handle: str) -> int:
    if cursor is None:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        offset = int(payload["offset"])
        bind = str(payload["handle_bind"])
    except (ValueError, TypeError, KeyError) as exc:
        raise FinCLError("This page link is not valid; reopen Analysis.") from exc
    if bind != _sign_bind(handle) or offset < 0:
        raise FinCLError("This page link is not valid; reopen Analysis.")
    return offset


def reported_contributors_page_from(
    connection: sqlite3.Connection, *, handle: str, cursor: str | None = None, limit: int = 50,
) -> ContributorsPageResult:
    """One page of the exact components behind a reported total (§6, item 6).

    The handle binds generation, reporting currency, policy version, rate
    -inventory digest and as-of (§6.2); any mismatch against the live context
    is refused rather than answered against a different snapshot. Every row
    is valued and the *complete* candidate set is sorted before paginating
    (item 6: "use exact sorting before pagination when amount-ranked") — a
    per-page re-sort of a SQL ``LIMIT`` window could not reproduce the exact
    cross-currency reported order a caller relies on for rank stability.
    """
    body = decode_reported(handle)
    as_of = date.fromisoformat(str(body["as_of"]))
    pocket_currency = body.get("pocket_currency")
    context = build_reporting_context(
        connection, scope=ReportedScope(valuation_as_of=as_of, pocket_currency=pocket_currency)
    )
    if (
        body["generation"] != context.generation_token
        or body["reporting_currency"] != context.reporting_currency
        or body.get("policy_version") != context.policy.policy_version
        or body["rate_inventory"] != context.rate_inventory_digest
    ):
        raise FinCLError(
            "STALE_REPORTING_CONTEXT: the reporting policy, rate inventory or "
            "generation has changed since this link was minted; reopen Analysis."
        )

    limit = max(1, min(int(limit), 200))
    offset = _decode_offset(cursor, handle=handle)
    buckets = tuple(body["buckets"])
    category_key = body.get("category_key")
    merchant_display = body.get("merchant_display")

    rows = _rows_for_buckets(
        connection, generation_token=context.generation_token, buckets=buckets,
        start=str(body["start"]), end=str(body["end"]), pocket_currency=pocket_currency,
        category_key=category_key, merchant_display=merchant_display,
    )
    flow_context = _flow_context(context.reporting_currency, as_of)
    items = [_item(row, magnitude=_magnitude_for_buckets(buckets)) for row in rows]
    parent = consolidate(connection, items, context=flow_context)

    pairs = list(zip(rows, parent.values, strict=True))
    sort = str(body["sort"])
    pairs.sort(key=_sort_key_amount if sort == "amount" else _sort_key_date)

    total = len(pairs)
    page = pairs[offset:offset + limit]
    next_cursor = _encode_cursor(handle=handle, offset=offset + limit) if offset + limit < total else None

    contributor_rows = tuple(
        ContributorRow(
            component_id=str(row["component_id"]), origin_kind=str(row["origin_kind"]),
            owning_source=_owning_source(row), role=str(row["role"]), bucket=str(row["bucket"]),
            valuation=value, category=str(row["category_key"]),
            merchant=(str(row["merchant_display"]) if row["merchant_display"] is not None else None),
            account=str(row["account"]), date=str(row["represented_start"]),
            precision=str(row["date_precision"]), source_location=str(row["source_location"]),
            nets=bool(row["nets"]),
        )
        for row, value in page
    )
    return ContributorsPageResult(
        context=context, scope_start=str(body["start"]), scope_end=str(body["end"]),
        buckets=buckets, pocket_currency=pocket_currency, category_key=category_key,
        merchant_display=merchant_display, sort=sort, rows=contributor_rows,
        total=total, next_cursor=next_cursor, parent=parent,
    )
