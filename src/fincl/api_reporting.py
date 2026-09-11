"""Reporting-currency HTTP surface (plan M1 §7.1; M0 §4.3 dated coverage).

Read routes only — the write path reuses the shared ``/api/v1/preview`` and
``/api/v1/commands`` flow (``api._validate`` gains the ``REPORTING_CURRENCY_SET``
branch). Kept in its own module so ``create_app`` grows by a single call.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from .coverage import dated_reporting_coverage
from .db import connect_readonly
from .generation import current_generation
from .reporting_currency import (
    currency_coverage_preview,
    currency_registry_dto,
    reporting_policy,
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
