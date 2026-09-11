"""Reporting-currency HTTP surface (plan M1, §7.1).

Read routes only — the write path reuses the shared ``/api/v1/preview`` and
``/api/v1/commands`` flow (``api._validate`` gains the ``REPORTING_CURRENCY_SET``
branch). Kept in its own module so ``create_app`` grows by a single call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .db import connect_readonly
from .generation import current_generation
from .reporting_currency import (
    currency_coverage_preview,
    currency_registry_dto,
    reporting_policy,
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
    def fx_coverage_read(currency: str | None = None) -> Any:
        connection = connect_readonly(database)
        try:
            policy = reporting_policy(connection)
            target = currency or policy.currency
            generation = current_generation(connection)
            return {
                'generation': generation['generation_token'] if generation else None,
                **currency_coverage_preview(connection, target),
            }
        finally:
            connection.close()
