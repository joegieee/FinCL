"""Reads over the reporting policy and the direct-or-inverse rate path (§3.2, §4).

Two things live here: ``reporting_policy`` (the resolved singleton, defaulting
to SGD for a ledger that has never recorded a decision) and
``resolve_rate_path``, the ``direct-or-inverse-v1`` policy that every reporting
surface in M1 calls rather than re-implementing path selection.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from ..fx import rate_fraction
from .models import (
    CURRENCY_REGISTRY,
    DEFAULT_REPORTING_CURRENCY,
    RATE_PATH_POLICY_VERSION,
    currency_definition,
)

RATE_PATH_NOT_CONFIGURED = "RATE_PATH_NOT_CONFIGURED"

RateBasisLiteral = Literal["identity", "direct", "inverse"]


@dataclass(frozen=True, slots=True)
class ReportingPolicy:
    currency: str
    minor_unit_scale: int
    journal_seq: int | None
    decided_at: str | None
    policy_version: str | None
    is_default: bool


def reporting_policy(connection: sqlite3.Connection) -> ReportingPolicy:
    """The resolved reporting-policy singleton, defaulting to SGD (§4).

    An absent decision resolves to SGD rather than an error: SGD is the no-op
    default and must produce byte-identical output to a ledger with no
    reporting-currency concept at all.
    """
    row = connection.execute(
        "SELECT currency, minor_unit_scale, journal_seq, decided_at, policy_version "
        "FROM reporting_policy WHERE id = 1"
    ).fetchone()
    if row is None:
        definition = currency_definition(DEFAULT_REPORTING_CURRENCY)
        assert definition is not None
        return ReportingPolicy(
            currency=DEFAULT_REPORTING_CURRENCY,
            minor_unit_scale=definition.minor_unit_scale,
            journal_seq=None,
            decided_at=None,
            policy_version=None,
            is_default=True,
        )
    return ReportingPolicy(
        currency=str(row["currency"]),
        minor_unit_scale=int(row["minor_unit_scale"]),
        journal_seq=int(row["journal_seq"]),
        decided_at=str(row["decided_at"]),
        policy_version=str(row["policy_version"]),
        is_default=False,
    )


@dataclass(frozen=True, slots=True)
class RatePathResult:
    basis: RateBasisLiteral | None
    inverted: bool
    available: bool
    reason: str | None


def structural_rate_path(
    connection: sqlite3.Connection, native_currency: str, reporting_currency: str
) -> RatePathResult:
    """Whether *any* direct or inverse pair is configured, ignoring dates.

    This is the structural check the ``REPORTING_CURRENCY_SET`` preview uses to
    reject a currency with no configured path at all (§3.2); a currency with a
    configured pair but a dated gap is repairable and is not rejected here.
    """
    if native_currency == reporting_currency:
        return RatePathResult("identity", False, True, None)
    direct = connection.execute(
        "SELECT 1 FROM fx_rates WHERE base_currency = ? AND quote_currency = ? LIMIT 1",
        (native_currency, reporting_currency),
    ).fetchone()
    if direct is not None:
        return RatePathResult("direct", False, True, None)
    inverse = connection.execute(
        "SELECT 1 FROM fx_rates WHERE base_currency = ? AND quote_currency = ? LIMIT 1",
        (reporting_currency, native_currency),
    ).fetchone()
    if inverse is not None:
        return RatePathResult("inverse", True, True, None)
    return RatePathResult(None, False, False, RATE_PATH_NOT_CONFIGURED)


@dataclass(frozen=True, slots=True)
class ResolvedRate:
    basis: RateBasisLiteral | None
    numerator: int | None
    denominator: int | None
    inverted: bool
    available: bool
    reason: str | None


def resolve_rate_path(
    connection: sqlite3.Connection,
    *,
    native_currency: str,
    reporting_currency: str,
    period: str,
) -> ResolvedRate:
    """The ``direct-or-inverse-v1`` policy (§3.2) for one calendar month.

    1. identity when native equals reporting;
    2. a directly imported native -> reporting rate for ``period``;
    3. otherwise the exact reciprocal of a directly imported reporting ->
       native rate for ``period``;
    4. otherwise unavailable with ``RATE_PATH_NOT_CONFIGURED``.

    No triangulation is performed. A structurally configured pair with no rate
    imported for this exact ``period`` is still unavailable here — filling a
    dated gap is a separate, repairable concern from the path existing at all.
    """
    if native_currency == reporting_currency:
        return ResolvedRate("identity", 1, 1, False, True, None)
    direct = rate_fraction(connection, native_currency, reporting_currency, period)
    if direct is not None:
        return ResolvedRate("direct", direct.numerator, direct.denominator, False, True, None)
    inverse = rate_fraction(connection, reporting_currency, native_currency, period)
    if inverse is not None:
        reciprocal = 1 / inverse
        return ResolvedRate(
            "inverse", reciprocal.numerator, reciprocal.denominator, True, True, None
        )
    return ResolvedRate(None, None, None, False, False, RATE_PATH_NOT_CONFIGURED)


def tracked_native_currencies(connection: sqlite3.Connection) -> list[str]:
    """Every currency this ledger has evidence in, sorted."""
    return sorted(
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT currency_pocket FROM ledger_entries"
        )
    )


def currency_registry_dto() -> list[dict[str, object]]:
    return [
        {
            "code": definition.code,
            "display_name": definition.display_name,
            "minor_unit_scale": definition.minor_unit_scale,
        }
        for definition in sorted(CURRENCY_REGISTRY.values(), key=lambda d: d.code)
    ]


def currency_coverage_preview(
    connection: sqlite3.Connection, reporting_currency: str
) -> dict[str, object]:
    """§3.2's ``REPORTING_CURRENCY_SET`` preview: structural path per tracked currency.

    Rejected (raises) only when the reporting currency is unknown to the
    registry or a tracked currency has no configured direct/inverse pair at
    all — missing *dated* observations are reported but never block the
    command, because they are repairable.
    """
    tracked = tracked_native_currencies(connection)
    by_currency: dict[str, object] = {}
    unconfigured: list[str] = []
    for currency in tracked:
        path = structural_rate_path(connection, currency, reporting_currency)
        by_currency[currency] = {
            "basis": path.basis,
            "inverted": path.inverted,
            "available": path.available,
            "reason": path.reason,
        }
        if not path.available:
            unconfigured.append(currency)
    return {
        "reporting_currency": reporting_currency,
        "rate_path_policy": RATE_PATH_POLICY_VERSION,
        "tracked_currencies": tracked,
        "by_currency": by_currency,
        "structurally_unconfigured": unconfigured,
        "would_be_partial_or_unavailable": unconfigured,
    }


__all__ = [
    "RATE_PATH_NOT_CONFIGURED",
    "RatePathResult",
    "ReportingPolicy",
    "ResolvedRate",
    "currency_coverage_preview",
    "currency_registry_dto",
    "reporting_policy",
    "resolve_rate_path",
    "structural_rate_path",
    "tracked_native_currencies",
]
