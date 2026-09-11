"""Exact valuation seam and wire shape for the reporting-currency view (§3.1, §3.3, §3.6).

``src/fincl/valuation.py`` owns all rate selection, exact multiplication,
consolidation, and quantization. Other modules do not implement FX arithmetic;
they build a :class:`ValuationItem`, hand it to :func:`value_one`,
:func:`value_many` or :func:`consolidate`, and render the result through
:func:`to_dto` / :func:`to_consolidated_dto`.

``Fraction`` is retained end to end. Quantization happens exactly once, at the
DTO boundary (§3.6): once when building a single :class:`ValuedMoneyDTO` and
once when building a :class:`ConsolidatedValuationDTO`'s ``total`` /
``partial_total`` — never per row and then summed.

There is no default purpose and no implicit ``date.today()`` here (§2 invariant
6, §3.1). Every ``ValuationContext`` carries its own explicit
``valuation_as_of``; callers outside the money-flow call graph (§5.1) resolve
that date from their own non-wall-clock source (an analysis window boundary, a
requested ``as_of``, …).

Rate granularity in this milestone (M2) is whatever ``fx.py`` actually stores:
a rate keyed by calendar month, printed by the MAS weekly loader as the last
end-of-period value in that month. That is exactly what §3.4 calls
``MONTHLY_PROXY``. Daily/weekly observation metadata, the widened primary key,
and the full ``flow-rate-v1``/``position-rate-v1`` day-precision ladder are M7
(§3.4, §8 M7); this module's rate selection intentionally has only the two
steps that current data can support: an exact-period lookup for ``FLOW`` and a
latest-on-or-before lookup for ``POSITION``/``FORECAST``. Both already satisfy
the no-look-ahead invariant (§2 invariant 7) because neither ever selects a
period later than the one requested.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from fractions import Fraction

from pydantic import BaseModel, ConfigDict

from . import fx
from .errors import FinCLError
from .money import ExactMoney, format_decimal
from .reporting_currency.models import CurrencyDefinition

__all__ = [
    "ConsolidatedValuation",
    "ConsolidatedValuationDTO",
    "ConsolidationStatus",
    "EntryPrecision",
    "ExactValuation",
    "MoneyDTO",
    "RateBasis",
    "RateInput",
    "RateInputDTO",
    "RateInventory",
    "ScopeDTO",
    "Unavailability",
    "UnavailabilityDTO",
    "ValuationContext",
    "ValuationItem",
    "ValuationPurpose",
    "ValuationScope",
    "ValuedMoneyDTO",
    "consolidate",
    "quantize",
    "to_consolidated_dto",
    "to_dto",
    "value_many",
    "value_one",
]

PERIOD_UNKNOWN = "PERIOD_UNKNOWN"
RATE_NOT_IMPORTED = "RATE_NOT_IMPORTED"


class ValuationPurpose(StrEnum):
    """Which financial question is this (§3.1)?"""

    FLOW = "flow"
    POSITION = "position"
    FORECAST = "forecast"


class RateBasis(StrEnum):
    """Which rate source was actually used (§3.1)?"""

    IDENTITY = "identity"
    REALIZED = "realized"
    EXACT_OBSERVATION = "exact_observation"
    PRIOR_OBSERVATION = "prior_observation"
    MONTHLY_AVERAGE = "monthly_average"
    MONTHLY_PROXY = "monthly_proxy"
    CURRENT_MARK = "current_mark"
    HELD_LATEST = "held_latest"


# Basis variants whose applied rate is a dated observation vs. a whole-month
# aggregate (§3.3: "rate_as_of and rate_period are ... exactly one is
# populated, and which one is populated is already implied by basis"). M7 adds
# real members to the first set; M2 never resolves one, but the mapping is
# total over the enum so a future basis cannot silently fall through both.
_DATED_BASES = frozenset({RateBasis.EXACT_OBSERVATION, RateBasis.PRIOR_OBSERVATION})
_PERIOD_BASES = frozenset(
    {RateBasis.MONTHLY_AVERAGE, RateBasis.MONTHLY_PROXY, RateBasis.CURRENT_MARK, RateBasis.HELD_LATEST}
)


@dataclass(frozen=True, slots=True)
class ValuationContext:
    """Explicit policy and time for one valuation (§3.1)."""

    reporting_currency: str
    purpose: ValuationPurpose
    valuation_as_of: date
    rate_selection_policy: str
    forward_policy: str | None = None


class EntryPrecision(StrEnum):
    """How precisely a ``FLOW`` item's date is known (§3.4, §9.3)."""

    DAY = "day"
    MONTH = "month"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ValuationItem:
    """One native amount to value under a shared :class:`ValuationContext`.

    ``purpose`` is carried on the item, not only on the context, so
    :func:`consolidate` can reject a batch that mixes purposes (a critical
    invariant): every item's ``purpose`` must equal the context's.
    """

    native: ExactMoney
    native_currency: str
    purpose: ValuationPurpose
    precision: EntryPrecision = EntryPrecision.MONTH
    period_month: str | None = None
    effective_on: date | None = None


@dataclass(frozen=True, slots=True)
class RateInput:
    """A reference-rate leg used by one valuation (§3.3).

    This is the reference-rate variant of the ``RateInputDTO`` discriminated
    union. The realized-conversion variant (§3.5) is M7 and is not modelled
    here: a realized rate prices only its own conversion and is never fed into
    ``consolidate``.
    """

    base_currency: str
    quote_currency: str
    period_month: str | None
    effective_on: date | None
    observation_kind: str
    source: str
    source_version: str
    inverted: bool
    rate_numerator: int
    rate_denominator: int
    rate_text: str
    imported_at: str | None = None

    def legacy_provenance_dict(self) -> dict[str, str]:
        """The exact shape ``fx.selected_rate`` returns.

        Exists only so ``report.py``'s pre-valuation ``fx_inputs`` payload
        shape can be reconstructed byte-for-byte from the seam's own record of
        which rate was applied, instead of re-querying the database.
        """
        return {
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "period": self.period_month or "",
            "rate_numerator": str(self.rate_numerator),
            "rate_denominator": str(self.rate_denominator),
            "rate_text": self.rate_text,
            "source": self.source,
            "source_version": self.source_version,
            "imported_at": self.imported_at or "",
        }


@dataclass(frozen=True, slots=True)
class Unavailability:
    """A typed reason a valuation could not be produced (§2 invariant 8)."""

    reason: str
    message: str


@dataclass(frozen=True, slots=True)
class ExactValuation:
    """The internal, exact result of valuing one item (§3.3)."""

    native: ExactMoney
    native_currency: str
    reported_exact: Fraction | None
    context: ValuationContext
    basis: RateBasis | None
    rate_path: tuple[RateInput, ...]
    unavailable: Unavailability | None


@dataclass(frozen=True, slots=True)
class RateInventory:
    """Every distinct rate input consumed by a :func:`value_many` call."""

    rate_inputs: tuple[RateInput, ...]


def value_one(
    connection: sqlite3.Connection, item: ValuationItem, *, context: ValuationContext
) -> ExactValuation:
    """Value one native amount under ``context`` (§3.3)."""
    if item.purpose != context.purpose:
        raise FinCLError(
            f"valuation item purpose {item.purpose!r} does not match context purpose {context.purpose!r}"
        )
    native_fraction = Fraction(item.native.coefficient, 10**item.native.scale)

    if item.native_currency == context.reporting_currency:
        return ExactValuation(
            native=item.native,
            native_currency=item.native_currency,
            reported_exact=native_fraction,
            context=context,
            basis=RateBasis.IDENTITY,
            rate_path=(),
            unavailable=None,
        )

    if context.purpose is ValuationPurpose.FLOW:
        period = item.period_month
        if period is None and item.precision is EntryPrecision.DAY and item.effective_on is not None:
            period = item.effective_on.strftime("%Y-%m")
        if item.precision is EntryPrecision.UNKNOWN or period is None:
            return _unavailable(
                item, context, PERIOD_UNKNOWN, "entry has no dated or monthly precision to value"
            )
        rate_input = _select_monthly_rate(connection, item.native_currency, context.reporting_currency, period)
        basis = RateBasis.MONTHLY_PROXY
    else:
        target_period = context.valuation_as_of.strftime("%Y-%m")
        rate_input = _select_latest_rate_on_or_before(
            connection, item.native_currency, context.reporting_currency, target_period
        )
        basis = RateBasis.CURRENT_MARK if context.purpose is ValuationPurpose.POSITION else RateBasis.HELD_LATEST

    if rate_input is None:
        return _unavailable(
            item,
            context,
            RATE_NOT_IMPORTED,
            f"no fx rate path configured for {item.native_currency} -> {context.reporting_currency}",
        )
    rate = Fraction(rate_input.rate_numerator, rate_input.rate_denominator)
    return ExactValuation(
        native=item.native,
        native_currency=item.native_currency,
        reported_exact=native_fraction * rate,
        context=context,
        basis=basis,
        rate_path=(rate_input,),
        unavailable=None,
    )


def _unavailable(
    item: ValuationItem, context: ValuationContext, reason: str, message: str
) -> ExactValuation:
    return ExactValuation(
        native=item.native,
        native_currency=item.native_currency,
        reported_exact=None,
        context=context,
        basis=None,
        rate_path=(),
        unavailable=Unavailability(reason, message),
    )


def value_many(
    connection: sqlite3.Connection, items: Sequence[ValuationItem], *, context: ValuationContext
) -> tuple[list[ExactValuation], RateInventory]:
    """Value every item under one shared context; also return the distinct rates used."""
    values = [value_one(connection, item, context=context) for item in items]
    seen: dict[tuple[str, str, str | None, bool, str], RateInput] = {}
    for value in values:
        for rate_input in value.rate_path:
            key = (
                rate_input.base_currency,
                rate_input.quote_currency,
                rate_input.period_month,
                rate_input.inverted,
                rate_input.source_version,
            )
            seen[key] = rate_input
    ordered = tuple(seen[key] for key in sorted(seen))
    return values, RateInventory(rate_inputs=ordered)


@dataclass(frozen=True, slots=True)
class ValuationScope:
    """One exact currency/period scope named in a consolidated result (§3.6)."""

    currency: str
    period: str | None


class ConsolidationStatus(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ConsolidatedValuation:
    """Result of grouping and valuing many items under one context (§3.6)."""

    total: Fraction | None
    status: ConsolidationStatus
    partial_total: Fraction | None
    included: tuple[ValuationScope, ...]
    excluded: tuple[ValuationScope, ...]
    excluded_entry_count: int
    unavailable_reasons: tuple[str, ...]
    rate_inputs: tuple[RateInput, ...]
    context: ValuationContext


def _scope_for(item: ValuationItem, context: ValuationContext) -> ValuationScope:
    if context.purpose is ValuationPurpose.FLOW:
        period = item.period_month or (
            item.effective_on.strftime("%Y-%m") if item.effective_on is not None else None
        )
    else:
        period = context.valuation_as_of.strftime("%Y-%m")
    return ValuationScope(currency=item.native_currency, period=period)


def _scope_key(scope: ValuationScope) -> tuple[str, str]:
    return (scope.currency, scope.period or "")


def consolidate(
    connection: sqlite3.Connection, items: Sequence[ValuationItem], *, context: ValuationContext
) -> ConsolidatedValuation:
    """Group ``items`` by ``(native currency, represented period)`` and value each subtotal (§3.6).

    Realized conversion detail is never passed here; it is priced through
    ``RealizedConversion`` (§3.5, M7), never folded into a flow total.

    Rejects a batch whose items do not all share ``context.purpose`` — a
    critical invariant, not a convenience check: purpose is the question being
    asked, and a consolidated total must answer exactly one question.
    """
    if any(item.purpose != context.purpose for item in items):
        raise FinCLError("consolidate: mixed-purpose batch")

    values, inventory = value_many(connection, items, context=context)

    groups: dict[ValuationScope, list[ExactValuation]] = defaultdict(list)
    for item, value in zip(items, values, strict=True):
        groups[_scope_for(item, context)].append(value)

    included: list[ValuationScope] = []
    excluded: list[ValuationScope] = []
    reasons: set[str] = set()
    excluded_entry_count = 0
    available_total = Fraction(0)
    any_unavailable = False

    for scope, group_values in groups.items():
        unavailable_values = [v for v in group_values if v.unavailable is not None]
        available_values = [v for v in group_values if v.unavailable is None]
        if unavailable_values:
            any_unavailable = True
            excluded.append(scope)
            excluded_entry_count += len(unavailable_values)
            reasons.update(v.unavailable.reason for v in unavailable_values if v.unavailable is not None)
        if available_values:
            included.append(scope)
            for v in available_values:
                assert v.reported_exact is not None
                available_total += v.reported_exact

    if any_unavailable and not included:
        status = ConsolidationStatus.UNAVAILABLE
        total, partial_total = None, None
    elif any_unavailable:
        status = ConsolidationStatus.PARTIAL
        total, partial_total = None, available_total
    else:
        status = ConsolidationStatus.AVAILABLE
        total, partial_total = available_total, None

    return ConsolidatedValuation(
        total=total,
        status=status,
        partial_total=partial_total,
        included=tuple(sorted(included, key=_scope_key)),
        excluded=tuple(sorted(excluded, key=_scope_key)),
        excluded_entry_count=excluded_entry_count,
        unavailable_reasons=tuple(sorted(reasons)),
        rate_inputs=inventory.rate_inputs,
        context=context,
    )


def quantize(value: Fraction, *, scale: int) -> ExactMoney:
    """Round ``value`` to ``scale`` decimal places, half away from zero (§3.6).

    The single quantization point for the whole module: every DTO builder
    below calls this exactly once per displayed amount, never per row before a
    parent sum.
    """
    unit = Fraction(1, 10**scale)
    steps = value / unit
    rounded = int(steps + Fraction(1, 2)) if steps >= 0 else -int(-steps + Fraction(1, 2))
    return ExactMoney(rounded, scale, format_decimal(rounded, scale))


# --- Wire DTOs (§3.3) -------------------------------------------------------


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoneyDTO(_StrictModel):
    value: str
    currency: str


class RateInputDTO(_StrictModel):
    base_currency: str
    quote_currency: str
    period_month: str | None
    effective_on: date | None
    observation_kind: str
    source: str
    source_version: str
    inverted: bool


class UnavailabilityDTO(_StrictModel):
    reason: str
    message: str


class ValuedMoneyDTO(_StrictModel):
    native: MoneyDTO
    reported: MoneyDTO | None
    purpose: ValuationPurpose
    basis: RateBasis | None
    valuation_as_of: date
    rate_as_of: date | None
    rate_period: str | None
    rate_path: list[RateInputDTO]
    forward_policy: str | None
    unavailable: UnavailabilityDTO | None


class ScopeDTO(_StrictModel):
    currency: str
    period: str | None


class ConsolidatedValuationDTO(_StrictModel):
    total: MoneyDTO | None
    status: ConsolidationStatus
    partial_total: MoneyDTO | None
    included: list[ScopeDTO]
    excluded: list[ScopeDTO]
    excluded_entry_count: int
    unavailable_reasons: list[str]
    rate_path: list[RateInputDTO]


def _rate_input_dto(rate_input: RateInput) -> RateInputDTO:
    return RateInputDTO(
        base_currency=rate_input.base_currency,
        quote_currency=rate_input.quote_currency,
        period_month=rate_input.period_month,
        effective_on=rate_input.effective_on,
        observation_kind=rate_input.observation_kind,
        source=rate_input.source,
        source_version=rate_input.source_version,
        inverted=rate_input.inverted,
    )


def to_dto(
    value: ExactValuation, *, currency_registry: dict[str, CurrencyDefinition]
) -> ValuedMoneyDTO:
    """Render one :class:`ExactValuation` as its JSON-safe wire shape (§3.3).

    Every basis and unavailability variant renders: an unavailable valuation
    carries ``native`` (the entry is real and priced in its own currency) plus
    ``unavailable`` with a typed reason, never a silent ``reported=None`` with
    no explanation.
    """
    native_dto = MoneyDTO(
        value=format_decimal(value.native.coefficient, value.native.scale),
        currency=value.native_currency,
    )
    if value.unavailable is not None:
        return ValuedMoneyDTO(
            native=native_dto,
            reported=None,
            purpose=value.context.purpose,
            basis=None,
            valuation_as_of=value.context.valuation_as_of,
            rate_as_of=None,
            rate_period=None,
            rate_path=[],
            forward_policy=value.context.forward_policy,
            unavailable=UnavailabilityDTO(
                reason=value.unavailable.reason, message=value.unavailable.message
            ),
        )
    assert value.reported_exact is not None and value.basis is not None
    definition = currency_registry[value.context.reporting_currency]
    reported_money = quantize(value.reported_exact, scale=definition.minor_unit_scale)
    reported_dto = MoneyDTO(
        value=format_decimal(reported_money.coefficient, reported_money.scale),
        currency=value.context.reporting_currency,
    )
    rate_as_of: date | None = None
    rate_period: str | None = None
    if value.basis in _DATED_BASES and value.rate_path:
        rate_as_of = value.rate_path[0].effective_on
    elif value.basis in _PERIOD_BASES and value.rate_path:
        rate_period = value.rate_path[0].period_month
    return ValuedMoneyDTO(
        native=native_dto,
        reported=reported_dto,
        purpose=value.context.purpose,
        basis=value.basis,
        valuation_as_of=value.context.valuation_as_of,
        rate_as_of=rate_as_of,
        rate_period=rate_period,
        rate_path=[_rate_input_dto(r) for r in value.rate_path],
        forward_policy=value.context.forward_policy,
        unavailable=None,
    )


def to_consolidated_dto(
    consolidated: ConsolidatedValuation, *, currency_registry: dict[str, CurrencyDefinition]
) -> ConsolidatedValuationDTO:
    """Render a :class:`ConsolidatedValuation` as its JSON-safe wire shape (§3.6).

    ``total`` and ``partial_total`` are mutually exclusive by construction
    (:func:`consolidate` never sets both), and each is quantized exactly once
    from the exact rational fold — never from a sum of already-rounded rows.
    """
    definition = currency_registry[consolidated.context.reporting_currency]
    scale = definition.minor_unit_scale
    total_dto = None
    if consolidated.total is not None:
        money = quantize(consolidated.total, scale=scale)
        total_dto = MoneyDTO(value=format_decimal(money.coefficient, money.scale), currency=consolidated.context.reporting_currency)
    partial_dto = None
    if consolidated.partial_total is not None:
        money = quantize(consolidated.partial_total, scale=scale)
        partial_dto = MoneyDTO(value=format_decimal(money.coefficient, money.scale), currency=consolidated.context.reporting_currency)
    return ConsolidatedValuationDTO(
        total=total_dto,
        status=consolidated.status,
        partial_total=partial_dto,
        included=[ScopeDTO(currency=s.currency, period=s.period) for s in consolidated.included],
        excluded=[ScopeDTO(currency=s.currency, period=s.period) for s in consolidated.excluded],
        excluded_entry_count=consolidated.excluded_entry_count,
        unavailable_reasons=list(consolidated.unavailable_reasons),
        rate_path=[_rate_input_dto(r) for r in consolidated.rate_inputs],
    )


# --- Rate selection (fx.py-backed; M2 has month-precision data only) -------


def _select_monthly_rate(
    connection: sqlite3.Connection, native_currency: str, reporting_currency: str, period: str
) -> RateInput | None:
    """Exact-period lookup, direct then reciprocal (§3.2 ``direct-or-inverse-v1``).

    This is the whole of M2's ``flow-rate-v1``: with only ``MONTHLY_PROXY``
    data in ``fx_rates``, steps 1-2 (exact/prior dated observation) and step 3
    (explicit monthly average) of §3.4 have nothing to select from yet. Never
    consults a different period, so it cannot look ahead.
    """
    row = fx.selected_rate(connection, native_currency, reporting_currency, period)
    if row is not None:
        return RateInput(
            base_currency=row["base_currency"],
            quote_currency=row["quote_currency"],
            period_month=row["period"],
            effective_on=None,
            observation_kind="MONTHLY_PROXY",
            source=row["source"],
            source_version=row["source_version"],
            inverted=False,
            rate_numerator=int(row["rate_numerator"]),
            rate_denominator=int(row["rate_denominator"]),
            rate_text=row["rate_text"],
            imported_at=row["imported_at"],
        )
    inverse_row = fx.selected_rate(connection, reporting_currency, native_currency, period)
    if inverse_row is not None:
        numerator = int(inverse_row["rate_denominator"])
        denominator = int(inverse_row["rate_numerator"])
        return RateInput(
            base_currency=native_currency,
            quote_currency=reporting_currency,
            period_month=inverse_row["period"],
            effective_on=None,
            observation_kind="MONTHLY_PROXY",
            source=inverse_row["source"],
            source_version=inverse_row["source_version"],
            inverted=True,
            rate_numerator=numerator,
            rate_denominator=denominator,
            rate_text=f"{numerator}/{denominator}",
            imported_at=inverse_row["imported_at"],
        )
    return None


def _select_latest_rate_on_or_before(
    connection: sqlite3.Connection, native_currency: str, reporting_currency: str, period_ceiling: str
) -> RateInput | None:
    """``position-rate-v1``/``forward-hold-latest-v1``: latest period <= ceiling, never later (§3.4).

    ``period`` is stored ``YYYY-MM`` text, so lexicographic ordering is
    chronological ordering; the ``<=`` bound is what makes no-look-ahead hold
    for a historical ``valuation_as_of``.
    """
    row = _latest_row_on_or_before(connection, native_currency, reporting_currency, period_ceiling)
    if row is not None:
        return RateInput(
            base_currency=row["base_currency"],
            quote_currency=row["quote_currency"],
            period_month=row["period"],
            effective_on=None,
            observation_kind="MONTHLY_PROXY",
            source=row["source"],
            source_version=row["source_version"],
            inverted=False,
            rate_numerator=int(row["rate_numerator"]),
            rate_denominator=int(row["rate_denominator"]),
            rate_text=row["rate_text"],
            imported_at=row["imported_at"],
        )
    inverse_row = _latest_row_on_or_before(connection, reporting_currency, native_currency, period_ceiling)
    if inverse_row is not None:
        numerator = int(inverse_row["rate_denominator"])
        denominator = int(inverse_row["rate_numerator"])
        return RateInput(
            base_currency=native_currency,
            quote_currency=reporting_currency,
            period_month=inverse_row["period"],
            effective_on=None,
            observation_kind="MONTHLY_PROXY",
            source=inverse_row["source"],
            source_version=inverse_row["source_version"],
            inverted=True,
            rate_numerator=numerator,
            rate_denominator=denominator,
            rate_text=f"{numerator}/{denominator}",
            imported_at=inverse_row["imported_at"],
        )
    return None


def _latest_row_on_or_before(
    connection: sqlite3.Connection, base_currency: str, quote_currency: str, period_ceiling: str
) -> dict[str, str] | None:
    cursor = connection.execute(
        """
        SELECT base_currency, quote_currency, period, rate_numerator,
               rate_denominator, rate_text, source, source_version, imported_at
        FROM fx_rates
        WHERE base_currency = ? AND quote_currency = ? AND period <= ?
        ORDER BY period DESC, imported_at DESC, source_version DESC LIMIT 1
        """,
        (base_currency, quote_currency, period_ceiling),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {column[0]: str(value) for column, value in zip(cursor.description, row, strict=True)}
