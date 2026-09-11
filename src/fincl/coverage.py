"""Evidence coverage and dated reporting-currency requirements (F23; §4.3 of
PLAN-reporting-currency-and-money-flow.md).

``coverage_contract`` is the exact F23 interval-set intersection the MVP
economic-flow report always used (moved here verbatim from ``report.py`` so
both the native report and this module's dated reporting-currency coverage
share one evidence-coverage answer instead of two).

``dated_reporting_coverage`` is step 02's main deliverable: given an explicit
or resolved ``(history_start, history_end, as_of)`` scope, it discovers every
native (purpose, currency, represented period-or-date) combination a reported
surface would need to price, then predicts each one's eligibility by calling
:func:`fincl.valuation.value_one` with a zero probe amount under the exact
named policy a real reported reader uses. That is deliberate: "coverage
prediction matches actual valuation" (M0 acceptance) is not a property to
prove separately, it holds by construction because both paths call the same
function — there is no second rate-selection ladder here.

This module intentionally lives outside the ``reporting_currency`` package.
``valuation.py`` imports ``reporting_currency.queries`` at module load, so a
module *inside* ``reporting_currency`` cannot import ``valuation`` back at
module load without a cycle. A plain top-level module has no such
constraint: it is one more leaf consumer of both, exactly like
``api_reporting.py``.

``latest_covered_complete_months`` is the one burn-month selector §3.2's
``gross-burn-v2`` (step 04) and this module's dated coverage (step 02) both
consume, so a currency/period a coverage preview names as "the burn scope" is
guaranteed to be the same scope burn actually averages.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from .analysis.components import build_components
from .intervals import DateInterval, intersect_all, month_span, normalize
from .money import ExactMoney
from .position.queries import position_page_from
from .reporting_currency.queries import reporting_policy
from .valuation import (
    FLOW_RATE_V1,
    FORWARD_MONTHLY_CLOSED_V1,
    POSITION_MONTHLY_CLOSED_V1,
    EntryPrecision,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    eligible_month_ceiling,
    value_one,
)

__all__ = [
    "GROSS_BURN_DEFINITION_VERSION",
    "BurnMonthSelection",
    "CoverageContract",
    "DatedCoverage",
    "RateRequirement",
    "coverage_contract",
    "dated_reporting_coverage",
    "latest_covered_complete_months",
    "resolve_dated_scope",
]


@dataclass(frozen=True, slots=True)
class CoverageContract:
    """The F23 interval-set coverage contract for one database."""

    required_sources: list[str]
    source_intervals: dict[str, dict[str, Any]]
    covered_intervals: list[DateInterval]
    status: str
    reasons: list[str]


def _required_source_families(connection: sqlite3.Connection) -> list[str]:
    """Every inventoried supported source family, before eligibility filtering.

    Quarantined and stale-only sources still appear here: a family that has been
    imported at all is required, and a required family with no eligible interval
    contributes an empty set that collapses the intersection rather than
    silently dropping out (§2).
    """
    families = {
        str(row[0])
        for row in connection.execute("SELECT DISTINCT source_type FROM statements")
    }
    return sorted(families)


def _family_eligible_intervals(
    connection: sqlite3.Connection, family: str
) -> tuple[list[DateInterval], str, bool]:
    """Accepted, analytically selected intervals for one source family.

    Returns ``(intervals, basis, completeness_established)``. Every current
    source is a statement document with a printed period, so coverage is the
    union of ``statement_period`` intervals (§2).
    """
    rows = connection.execute(
        """
        SELECT s.period_start AS lo, s.period_end AS hi
        FROM statements s JOIN extraction_runs x ON x.id = s.extraction_run_id
        WHERE s.source_type = ? AND x.status = 'ACCEPTED'
          AND s.selection_state = 'SELECTED'
          AND s.period_start IS NOT NULL AND s.period_end IS NOT NULL
        """,
        (family,),
    ).fetchall()
    return [DateInterval(row["lo"], row["hi"]) for row in rows], "statement_period", True


def _family_evidence_refs(connection: sqlite3.Connection, family: str) -> list[str]:
    rows = connection.execute(
        "SELECT DISTINCT s.evidence_sha256 FROM statements s "
        "JOIN extraction_runs x ON x.id = s.extraction_run_id "
        "WHERE s.source_type = ? AND x.status = 'ACCEPTED' "
        "AND s.selection_state = 'SELECTED'",
        (family,),
    )
    return sorted(str(row[0]) for row in rows if row[0])


def coverage_contract(connection: sqlite3.Connection) -> CoverageContract:
    """Normalize each required family's intervals and intersect them (§2)."""
    required = _required_source_families(connection)
    source_intervals: dict[str, dict[str, Any]] = {}
    family_sets: list[list[DateInterval]] = []
    empty_families: list[str] = []
    for family in required:
        raw, basis, established = _family_eligible_intervals(connection, family)
        merged = normalize(raw)
        source_intervals[family] = {
            "basis": basis,
            "completeness_established": established,
            "intervals": [interval.as_dict() for interval in merged],
            "evidence_refs": _family_evidence_refs(connection, family),
            "warnings": (
                []
                if established
                else ["export completeness was not independently established"]
            ),
        }
        family_sets.append(merged)
        if not merged:
            empty_families.append(family)

    covered = intersect_all(family_sets) if required else []
    reasons: list[str] = []
    if not required:
        status = "UNAVAILABLE"
        reasons.append("no inventoried source families")
    elif not covered:
        status = "UNAVAILABLE"
        if empty_families:
            reasons.append(
                "required source families with no eligible interval: "
                + ", ".join(empty_families)
            )
        else:
            reasons.append("required source families do not overlap")
    elif len(covered) == 1:
        status = "COVERED"
    else:
        status = "GAPPED"
        reasons.append(
            "coverage is split across "
            + ", ".join(f"{interval.start}..{interval.end}" for interval in covered)
        )
    return CoverageContract(required, source_intervals, covered, status, reasons)


def latest_covered_complete_months(
    contract: CoverageContract,
    *,
    history_end: date,
    limit: int = 3,
    max_lookback_months: int = 240,
) -> list[str]:
    """At most ``limit`` complete calendar months, searching backward from the
    closed-month anchor at or before ``history_end`` (§3.2 gross-burn-v2 month
    selection; reused by step 02's dated coverage and step 04's burn reader).

    The anchor is exactly :func:`fincl.valuation.eligible_month_ceiling`'s
    closed-month bound: a month qualifies only when its own last calendar day
    is on or before ``history_end``, so a still-open current month is never
    treated as complete. Search does not stop at the first uncovered month —
    it keeps walking backward, skipping gaps, so one short later gap cannot
    starve the figure of an earlier fully evidence-covered month; the burn
    lookback may reach earlier than any requested history window (§4.3).

    Returns months oldest-first (``["2026-05", "2026-06", "2026-07"]``);
    an empty result means no qualifying month was found at all.
    """
    anchor = eligible_month_ceiling(history_end, closed_month_only=True)
    year, month = int(anchor[:4]), int(anchor[5:7])
    included_latest_first: list[str] = []
    scanned = 0
    while len(included_latest_first) < limit and scanned < max_lookback_months:
        ym = f"{year:04d}-{month:02d}"
        span = month_span(ym)
        if any(interval.contains_interval(span) for interval in contract.covered_intervals):
            included_latest_first.append(ym)
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        scanned += 1
    return list(reversed(included_latest_first))


# §3.2/§4.1: the shared spending+fees-per-included-month definition step 04
# implements against this module's burn-month selector. Pinned here, ahead of
# that implementation, so the generation manifest already carries it and a
# later change to the definition is a new version string rather than a silent
# reinterpretation under the same reported handles (§4.1: "a changed
# definition must not silently reuse old reporting handles").
GROSS_BURN_DEFINITION_VERSION = "gross-burn-v2"


# --- dated, requirement-based coverage (§4.3) -------------------------------

_PROBE_MONEY = ExactMoney(0, 2, "0.00")

_POLICY_BY_PURPOSE: dict[ValuationPurpose, str] = {
    ValuationPurpose.FLOW: FLOW_RATE_V1,
    ValuationPurpose.POSITION: POSITION_MONTHLY_CLOSED_V1,
    ValuationPurpose.FORECAST: FORWARD_MONTHLY_CLOSED_V1,
}


@dataclass(frozen=True, slots=True)
class RateRequirement:
    """One coverage requirement: would a reported reader need a rate here,
    and does one exist (§4.3)?

    ``period`` (FLOW) and ``as_of_date`` (POSITION/FORECAST) are mutually
    exclusive, exactly like ``valuation.ValuedMoneyDTO``'s ``rate_period`` /
    ``rate_as_of`` split — which one is populated is implied by ``purpose``.
    An identity row (``native_currency == reporting_currency``) is still
    returned, ``status="AVAILABLE"`` with no policy/basis, so a caller can see
    that this scope needs no rate at all rather than inferring it from absence.
    """

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
    affected_metrics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BurnMonthSelection:
    """§3.2 gross-burn-v2's month scope, predicted the way step 04 will select it."""

    included_months: tuple[str, ...]
    requested_count: int
    reason: str | None


@dataclass(frozen=True, slots=True)
class DatedCoverage:
    """§4.3: dated, requirement-based coverage for one resolved scope.

    ``evidence_status``/``evidence_reasons`` are the F23 contract's own
    status: a currency can be FX-available and still have no supported
    balance/movement evidence, and the reverse. Keeping this alongside
    ``requirements`` (which is FX-only) is what keeps "missing evidence" and
    "missing FX" visibly distinct, per the M0 acceptance criteria.
    """

    history_start: str
    history_end: str
    as_of: str
    reporting_currency: str
    evidence_status: str
    evidence_reasons: tuple[str, ...]
    burn: BurnMonthSelection
    requirements: tuple[RateRequirement, ...]

    def unavailable_requirements(self) -> tuple[RateRequirement, ...]:
        return tuple(r for r in self.requirements if r.status != "AVAILABLE")


def resolve_dated_scope(
    connection: sqlite3.Connection,
    *,
    as_of: date,
    history_start: date | None,
    history_end: date | None,
) -> tuple[date, date, date]:
    """Explicit or resolved ``(history_start, history_end, as_of)`` (§4.3).

    ``as_of`` is mandatory: like step 01's readers, this function never
    consults the wall clock itself (§2 invariant 7); a caller wanting
    "today" resolves the wall clock at its own outer edge (the API route)
    and passes it in, exactly like every existing ``today: date`` reader in
    ``api.py``. An omitted ``history_end`` resolves to ``as_of``; an omitted
    ``history_start`` resolves to the earliest evidence-covered date, so a
    caller asking for "the ledger's whole covered history" need not scan for
    it separately.
    """
    resolved_end = history_end if history_end is not None else as_of
    if history_start is not None:
        resolved_start = history_start
    else:
        contract = coverage_contract(connection)
        resolved_start = (
            date.fromisoformat(min(interval.start for interval in contract.covered_intervals))
            if contract.covered_intervals
            else resolved_end
        )
    return resolved_start, resolved_end, as_of


def _predict_requirement(
    connection: sqlite3.Connection,
    *,
    purpose: ValuationPurpose,
    native_currency: str,
    reporting_currency: str,
    scope: str,
    fallback_valuation_as_of: date,
    affected_metrics: tuple[str, ...],
) -> RateRequirement:
    """Predict one requirement by calling the real selector, not a copy of it.

    ``scope`` is the represented ``YYYY-MM`` for FLOW or the ISO valuation
    date for POSITION/FORECAST. The probe amount is zero: rate *selection* in
    ``value_one`` never inspects the native magnitude, only the currency,
    purpose, period/date and policy, so a zero amount predicts exactly the
    same basis/unavailability a real amount would.
    """
    policy_name = _POLICY_BY_PURPOSE[purpose]
    if native_currency == reporting_currency:
        period = scope if purpose is ValuationPurpose.FLOW else None
        as_of_date = None if purpose is ValuationPurpose.FLOW else scope
        return RateRequirement(
            purpose=purpose.value, native_currency=native_currency,
            reporting_currency=reporting_currency, period=period, as_of_date=as_of_date,
            policy=None, status="AVAILABLE", basis="identity", inverted=False,
            reason=None, remedy=None, affected_metrics=affected_metrics,
        )
    if purpose is ValuationPurpose.FLOW:
        period = scope
        as_of_date = None
        context_as_of = fallback_valuation_as_of
        item = ValuationItem(
            native=_PROBE_MONEY, native_currency=native_currency, purpose=purpose,
            precision=EntryPrecision.MONTH, period_month=scope,
        )
    else:
        period = None
        as_of_date = scope
        context_as_of = date.fromisoformat(scope)
        item = ValuationItem(native=_PROBE_MONEY, native_currency=native_currency, purpose=purpose)
    context = ValuationContext(
        reporting_currency=reporting_currency, purpose=purpose, valuation_as_of=context_as_of,
        rate_selection_policy=policy_name,
        forward_policy=policy_name if purpose is ValuationPurpose.FORECAST else None,
    )
    result = value_one(connection, item, context=context)
    if result.unavailable is not None:
        return RateRequirement(
            purpose=purpose.value, native_currency=native_currency,
            reporting_currency=reporting_currency, period=period, as_of_date=as_of_date,
            policy=policy_name, status="UNAVAILABLE", basis=None, inverted=None,
            reason=result.unavailable.reason, remedy=result.unavailable.remedy,
            affected_metrics=affected_metrics,
        )
    return RateRequirement(
        purpose=purpose.value, native_currency=native_currency,
        reporting_currency=reporting_currency, period=period, as_of_date=as_of_date,
        policy=policy_name, status="AVAILABLE",
        basis=result.basis.value if result.basis is not None else None,
        inverted=result.rate_path[0].inverted if result.rate_path else None,
        reason=None, remedy=None, affected_metrics=affected_metrics,
    )


def dated_reporting_coverage(
    connection: sqlite3.Connection,
    *,
    as_of: date,
    history_start: date | None = None,
    history_end: date | None = None,
    reporting_currency: str | None = None,
    burn_month_limit: int = 3,
) -> DatedCoverage:
    """§4.3: requirement-based coverage over one resolved dated scope.

    ``as_of`` is mandatory (§2 invariant 7; see :func:`resolve_dated_scope`) —
    the caller resolves "today" once, outside this connection-accepting
    reader, exactly as step 01 made ``valuation_as_of`` mandatory inside
    ``value_one``.

    Discovers, without triangulating original purchase currencies as extra
    pockets (native settlement currency — ``ledger_entries.currency_pocket``
    — is the only thing :func:`fincl.analysis.components.build_components`
    ever groups by; a foreign ``source_currency`` never enters this scan):

    1. native settlement currencies/represented months of economic components
       inside ``[history_start, history_end]``;
    2. the shared burn-month selector's own up-to-``burn_month_limit`` latest
       evidence-covered complete months, which may reach earlier than
       ``history_start``, and their spending/fee currencies;
    3. native liquid (POSITION) currencies at ``as_of``, reused unchanged for
       FORECAST's issue-date requirement — a forecast opens from the same
       supported balance cash does, so it needs no separate discovery.

    Every requirement is a read: this function issues no writes, and neither
    do any of the readers it calls.
    """
    if not connection.in_transaction:
        connection.execute("BEGIN")
    resolved_start, resolved_end, resolved_as_of = resolve_dated_scope(
        connection, as_of=as_of, history_start=history_start, history_end=history_end
    )
    policy = reporting_policy(connection)
    target_currency = reporting_currency or policy.currency

    contract = coverage_contract(connection)
    burn_months = latest_covered_complete_months(
        contract, history_end=resolved_end, limit=burn_month_limit
    )
    burn = BurnMonthSelection(
        included_months=tuple(burn_months),
        requested_count=burn_month_limit,
        reason=(
            None if burn_months
            else "no evidence-covered complete calendar month was found on or before "
                 f"{resolved_end.isoformat()}"
        ),
    )

    requirement_scopes: dict[tuple[ValuationPurpose, str, str], set[str]] = {}

    def _need(purpose: ValuationPurpose, currency: str, scope: str, metric: str) -> None:
        requirement_scopes.setdefault((purpose, currency, scope), set()).add(metric)

    history_projection = build_components(
        connection,
        covered_intervals=contract.covered_intervals,
        analysis_intervals=[DateInterval(resolved_start.isoformat(), resolved_end.isoformat())],
    )
    for component in history_projection.components:
        _need(ValuationPurpose.FLOW, component.currency, component.period, "analysis_flow")

    if burn_months:
        burn_projection = build_components(
            connection,
            covered_intervals=contract.covered_intervals,
            analysis_intervals=[month_span(month) for month in burn_months],
        )
        for component in burn_projection.components:
            if component.bucket in ("spending", "fees"):
                _need(ValuationPurpose.FLOW, component.currency, component.period, "gross_burn")

    position = position_page_from(connection, today=resolved_as_of)
    liquid_currencies = sorted({
        pocket["currency_pocket"] for pocket in position["included_pockets"]
    })
    for currency in liquid_currencies:
        _need(ValuationPurpose.POSITION, currency, resolved_as_of.isoformat(), "cash_position")
        _need(ValuationPurpose.FORECAST, currency, resolved_as_of.isoformat(), "forecast_opening")

    requirements = [
        _predict_requirement(
            connection, purpose=purpose, native_currency=currency, scope=scope,
            reporting_currency=target_currency, fallback_valuation_as_of=resolved_as_of,
            affected_metrics=tuple(sorted(metrics)),
        )
        for (purpose, currency, scope), metrics in requirement_scopes.items()
    ]
    requirements.sort(
        key=lambda r: (r.purpose, r.native_currency, r.period or "", r.as_of_date or "")
    )

    return DatedCoverage(
        history_start=resolved_start.isoformat(),
        history_end=resolved_end.isoformat(),
        as_of=resolved_as_of.isoformat(),
        reporting_currency=target_currency,
        evidence_status=contract.status,
        evidence_reasons=tuple(contract.reasons),
        burn=burn,
        requirements=tuple(requirements),
    )
