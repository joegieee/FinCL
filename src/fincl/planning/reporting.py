"""Reported Income (Inflows) workspace (plan M4 §7; step 15).

A thin connection-accepting adapter over the *native* income-candidate
detector (``recurring_page_from``, reused by the existing
``GET /api/v1/inflows/candidates`` route). It performs no independent
candidate-detection, grouping or classification logic of its own: every
native fact (candidate identity, merchant/currency/account grouping,
cadence, timing window, confidence, review question, occurrences,
exclusions) is read straight through unchanged, and this module only
attaches a reported equivalent to each dated candidate receipt.

Each occurrence is a dated, already-recorded receipt -- valued once under
FLOW at its own represented month (§3.1), never under FORECAST and never at
one shared "today" rate the way a POSITION/FORECAST card would be. Future
derived income already has its own reported treatment where it actually
enters a forecast total: the FORECAST-valued "likely" income-candidate
events inside the reported Forecast workspace's base-case event list
(``forecast/reporting.py``, step 14) -- this module does not re-derive that
computation a second time.

A candidate is never promoted to confirmed income, ranked, or counted into
any total merely because its receipts can be converted: this reader groups
and values in the native detector's own deterministic order, and the
owner's ``INBOUND_CLASSIFICATION``/``INBOUND_RULE_SET`` commands remain the
only way a receipt's classification or a reusable rule actually changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..money import ExactMoney
from ..reporting_context import ReportedScope, ReportingContext, build_reporting_context
from ..valuation import (
    FLOW_RATE_V1,
    EntryPrecision,
    ExactValuation,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    value_one,
)
from .queries import recurring_page_from

__all__ = [
    "ReportedIncomeCandidate",
    "ReportedIncomeCandidatesPage",
    "ReportedOccurrence",
    "ReportedVariation",
    "reported_income_candidates_page",
    "reported_income_candidates_page_from",
]


def _occurrence_valuation(
    connection: Any, *, context: ValuationContext, amount: str, currency: str, occurrence_date: str | None,
) -> ExactValuation | None:
    """One dated candidate receipt, valued under FLOW at its own represented
    month. ``None`` means there is no date to value at all (an "unknown
    date" occurrence) -- distinct from an :class:`ExactValuation` whose
    ``unavailable`` is set (a real dated receipt with no importable rate)."""
    if not occurrence_date:
        return None
    item = ValuationItem(
        native=ExactMoney.parse(amount), native_currency=currency, purpose=ValuationPurpose.FLOW,
        precision=EntryPrecision.DAY, period_month=occurrence_date[:7],
    )
    return value_one(connection, item, context=context)


@dataclass(frozen=True, slots=True)
class ReportedOccurrence:
    native: Mapping[str, Any]
    reported: ExactValuation | None


@dataclass(frozen=True, slots=True)
class ReportedVariation:
    """A payer group's reported amount spread, computed *from the
    already-valued occurrences* -- grouping/ranking only ever happens after
    exact valuation (§7 M4), never by comparing native amounts across
    different months' rates. ``priced_count``/``total_count`` keep the
    candidate's occurrence count visible even when some receipts could not
    be valued (missing FX or an unknown date)."""

    latest: ExactValuation | None
    minimum: ExactValuation | None
    maximum: ExactValuation | None
    mean: Fraction | None
    priced_count: int
    total_count: int


@dataclass(frozen=True, slots=True)
class ReportedIncomeCandidate:
    native: Mapping[str, Any]
    occurrences: tuple[ReportedOccurrence, ...]
    variation: ReportedVariation


@dataclass(frozen=True, slots=True)
class ReportedIncomeCandidatesPage:
    context: ReportingContext
    native: Mapping[str, Any]
    candidates: tuple[ReportedIncomeCandidate, ...]


def _reported_candidate(connection: Any, candidate: Mapping[str, Any], *, context: ValuationContext) -> ReportedIncomeCandidate:
    currency = str(candidate["currency"])
    occurrences = tuple(
        ReportedOccurrence(
            native=occurrence,
            reported=_occurrence_valuation(
                connection, context=context, amount=str(occurrence["amount"]), currency=currency,
                occurrence_date=occurrence.get("date"),
            ),
        )
        for occurrence in candidate["occurrences"]
    )
    priced: list[tuple[ExactValuation, Fraction]] = [
        (occurrence.reported, occurrence.reported.reported_exact)
        for occurrence in occurrences
        if occurrence.reported is not None
        and occurrence.reported.unavailable is None
        and occurrence.reported.reported_exact is not None
    ]
    latest = occurrences[-1].reported if occurrences else None
    variation = ReportedVariation(
        latest=latest,
        minimum=min(priced, key=lambda pair: pair[1])[0] if priced else None,
        maximum=max(priced, key=lambda pair: pair[1])[0] if priced else None,
        mean=(
            sum((exact for _value, exact in priced), Fraction(0)) / len(priced)
            if priced else None
        ),
        priced_count=len(priced), total_count=len(occurrences),
    )
    return ReportedIncomeCandidate(native=candidate, occurrences=occurrences, variation=variation)


def reported_income_candidates_page(database: Path, *, today: date) -> ReportedIncomeCandidatesPage:
    connection = connect_readonly(database)
    try:
        return reported_income_candidates_page_from(connection, today=today)
    finally:
        connection.close()


def reported_income_candidates_page_from(connection: Any, *, today: date) -> ReportedIncomeCandidatesPage:
    """The reported Income (Inflows) read (§3.1, §7 M4; step 15).

    Reuses :func:`recurring_page_from` for every native fact; only its
    income-relevant slice is exposed (mirroring the existing native
    ``GET /api/v1/inflows/candidates`` route's own shape, never the whole
    recurring page -- bill candidates are a different workspace). Defaults
    to every currency/pocket the detector finds: there is no record filter
    to narrow, since the native detector itself accepts none. Takes no
    history parameter: the queue's scope is unresolved income work across
    the whole ledger, not the Overview history range (§7 M4).
    """
    scope = ReportedScope(valuation_as_of=today)
    context = build_reporting_context(connection, scope=scope)

    page = recurring_page_from(connection)
    native: Mapping[str, Any] = {
        "generation": page["generation"],
        "candidates": page["income_candidates"],
        "exclusions": page["income_exclusions"],
        "policy": "Candidates are Likely only. Confirming a classification remains an explicit journal decision.",
    }

    flow_context = ValuationContext(
        reporting_currency=context.reporting_currency, purpose=ValuationPurpose.FLOW,
        valuation_as_of=today, rate_selection_policy=FLOW_RATE_V1,
    )
    candidates = tuple(
        _reported_candidate(connection, candidate, context=flow_context)
        for candidate in native["candidates"]
    )
    return ReportedIncomeCandidatesPage(context=context, native=native, candidates=candidates)
