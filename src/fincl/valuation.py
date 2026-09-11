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

Which ladder runs is named by ``context.rate_selection_policy`` and resolved
through ``_RATE_SELECTION_POLICIES`` — never inferred from the purpose alone.
That is what lets the pre-reporting-currency economic-flow report keep its
exact historical selection (``LATEST_FIRST_IMPORT_V1``, direct rows only)
while new surfaces opt into §3.2's reciprocal leg.
"""

from __future__ import annotations

import calendar
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import fx
from .errors import FinCLError
from .money import ExactMoney, format_decimal
from .reporting_currency.models import CurrencyDefinition
from .reporting_currency.queries import reporting_policy

__all__ = [
    "FORWARD_HOLD_LATEST_V1",
    "FORWARD_MONTHLY_CLOSED_V1",
    "FLOW_RATE_V1",
    "LATEST_FIRST_IMPORT_V1",
    "POSITION_MONTHLY_CLOSED_V1",
    "POSITION_RATE_V1",
    "ConsolidatedValuation",
    "ConsolidatedValuationDTO",
    "ConsolidationStatus",
    "EntryPrecision",
    "ExactValuation",
    "ExcludedScopeDetail",
    "ExcludedScopeDetailDTO",
    "MoneyDTO",
    "RateBasis",
    "RateInput",
    "RateInputDTO",
    "RateInventory",
    "RateSelectionPolicy",
    "ScopeDTO",
    "Unavailability",
    "UnavailabilityDTO",
    "ValuationContext",
    "ValuationItem",
    "ValuationPurpose",
    "ValuationScope",
    "ValuedMoneyDTO",
    "consolidate",
    "eligible_month_ceiling",
    "quantize",
    "reporting_policy",
    "resolve_rate_selection_policy",
    "select_latest_rate_on_or_before",
    "select_monthly_rate",
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
# The third arm, so the classification is total: neither field applies.
# ``IDENTITY`` applied no rate at all, and a ``REALIZED`` rate prices only its
# own conversion — its date belongs to that ``RealizedConversion`` detail
# (§3.5, M7), never to the shared reference-rate fields.
_RATELESS_BASES = frozenset({RateBasis.IDENTITY, RateBasis.REALIZED})


@dataclass(frozen=True, slots=True)
class ValuationContext:
    """Explicit policy and time for one valuation (§3.1)."""

    reporting_currency: str
    purpose: ValuationPurpose
    valuation_as_of: date
    rate_selection_policy: str
    forward_policy: str | None = None


FLOW_RATE_V1 = "flow-rate-v1"
POSITION_RATE_V1 = "position-rate-v1"
FORWARD_HOLD_LATEST_V1 = "forward-hold-latest-v1"
LATEST_FIRST_IMPORT_V1 = "latest-first-import-v1"

# §4.2 M0: the closed-month policies. Registering them alone would not be
# enough to change behavior, because ``value_one`` used to compute its
# on-or-before ceiling purely from ``purpose`` — every POSITION/FORECAST
# context, named or not, got the same permissive "any row with period <=
# as_of's own month" ceiling. ``RateSelectionPolicy.closed_month_only`` below
# is what the named policy actually dispatches on, so these two names differ
# in more than label from ``POSITION_RATE_V1``/``FORWARD_HOLD_LATEST_V1``
# while those legacy names keep resolving exactly what they always did.
POSITION_MONTHLY_CLOSED_V1 = "position-monthly-closed-v1"
FORWARD_MONTHLY_CLOSED_V1 = "forward-monthly-closed-v1"


@dataclass(frozen=True, slots=True)
class RateSelectionPolicy:
    """A named, versioned rate-selection ladder (§3.2, §3.4).

    The ladder is named on the context rather than inferred from the purpose so
    that a caller which must keep its historical selection semantics can say so
    explicitly, and so that changing a ladder is a new version string rather
    than a silent change of meaning under an existing one.
    """

    name: str
    purpose: ValuationPurpose
    allow_inverse: bool
    # §4.2: a POSITION/FORECAST proxy for month M is eligible only when M's
    # last calendar day is on or before ``valuation_as_of``. False preserves
    # every existing named policy's permissive current-month ceiling.
    closed_month_only: bool = False


_RATE_SELECTION_POLICIES: dict[str, RateSelectionPolicy] = {
    policy.name: policy
    for policy in (
        RateSelectionPolicy(FLOW_RATE_V1, ValuationPurpose.FLOW, allow_inverse=True),
        RateSelectionPolicy(POSITION_RATE_V1, ValuationPurpose.POSITION, allow_inverse=True),
        RateSelectionPolicy(FORWARD_HOLD_LATEST_V1, ValuationPurpose.FORECAST, allow_inverse=True),
        RateSelectionPolicy(
            POSITION_MONTHLY_CLOSED_V1, ValuationPurpose.POSITION, allow_inverse=True, closed_month_only=True
        ),
        RateSelectionPolicy(
            FORWARD_MONTHLY_CLOSED_V1, ValuationPurpose.FORECAST, allow_inverse=True, closed_month_only=True
        ),
        # The economic-flow report's pre-seam selection (``report.py``'s
        # ``fx_selection_policy``). It predates ``direct-or-inverse-v1`` and
        # only ever read a directly imported native -> reporting row, so it
        # takes no reciprocal: moving that report behind this seam must not
        # change which rows it resolves, nor light up a total it previously
        # withheld (§8 M2, "without changing its existing SGD/monthly result").
        RateSelectionPolicy(LATEST_FIRST_IMPORT_V1, ValuationPurpose.FLOW, allow_inverse=False),
    )
}


def resolve_rate_selection_policy(context: ValuationContext) -> RateSelectionPolicy:
    """The ladder named by ``context``, rejecting an unknown or mismatched one.

    Failing loudly matters more than defaulting: silently falling back to a
    ladder the caller did not name would let a misspelled policy change which
    rate a figure was valued at without any signal. Exposed (not just used
    internally) so the step 02 coverage reader resolves the identical policy
    object actual valuation does, rather than re-deriving its own copy.
    """
    policy = _RATE_SELECTION_POLICIES.get(context.rate_selection_policy)
    if policy is None:
        raise FinCLError(f"unknown rate selection policy {context.rate_selection_policy!r}")
    if policy.purpose is not context.purpose:
        raise FinCLError(
            f"rate selection policy {policy.name!r} answers {policy.purpose!r}, "
            f"not context purpose {context.purpose!r}"
        )
    return policy


def eligible_month_ceiling(valuation_as_of: date, *, closed_month_only: bool) -> str:
    """The latest ``YYYY-MM`` period a POSITION/FORECAST proxy may use (§4.2).

    A permissive ceiling (``closed_month_only=False``, every pre-M0 named
    policy) is simply ``valuation_as_of``'s own month: any row with
    ``period <= that month`` may be selected, including one dated inside the
    still-open current month.

    A closed-month ceiling admits month M only when M's last calendar day is
    on or before ``valuation_as_of`` — August 15 may use July or earlier;
    August 31 may use August. Exposed for the step 02 coverage reader so it
    predicts eligibility through this exact function, not a second copy of it.
    """
    if not closed_month_only:
        return valuation_as_of.strftime("%Y-%m")
    _, last_day_of_month = calendar.monthrange(valuation_as_of.year, valuation_as_of.month)
    if valuation_as_of.day == last_day_of_month:
        return valuation_as_of.strftime("%Y-%m")
    last_day_of_prior_month = valuation_as_of.replace(day=1) - timedelta(days=1)
    return last_day_of_prior_month.strftime("%Y-%m")


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
    # The genuinely *imported* printed decimal, when one exists (§4.1: "preserve
    # imported text separately when available"). For a direct row this equals
    # ``rate_text``. For an inverted row, ``rate_text`` is a synthesized
    # ``"numerator/denominator"`` display fallback — never something anyone
    # actually typed — so this field instead carries the *other* direction's
    # own printed text (e.g. the imported "0.7500" for a reporting -> native
    # row that this leg reciprocates), or ``None`` when no printed text is on
    # file at all.
    imported_rate_text: str | None = None

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
    """A typed reason a valuation could not be produced (§2 invariant 8).

    ``remedy`` is a required field, not a nicety: §9.2 discharges "unknown is
    never zero" with "every unavailable path yields a typed reason *and a
    remedy*", and §7.2 requires ``<Money>`` to render both. Making it part of
    the dataclass means a new unavailable path cannot ship without one.
    """

    reason: str
    message: str
    remedy: str


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
    policy = resolve_rate_selection_policy(context)
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
                item,
                context,
                PERIOD_UNKNOWN,
                "entry has no dated or monthly precision to value",
                "Record a transaction date or statement period for this entry, then refresh.",
            )
        rate_input = select_monthly_rate(
            connection,
            item.native_currency,
            context.reporting_currency,
            period,
            allow_inverse=policy.allow_inverse,
        )
        basis = RateBasis.MONTHLY_PROXY
        missing_scope = f"covering {period}"
    else:
        period = eligible_month_ceiling(context.valuation_as_of, closed_month_only=policy.closed_month_only)
        rate_input = select_latest_rate_on_or_before(
            connection,
            item.native_currency,
            context.reporting_currency,
            period,
            allow_inverse=policy.allow_inverse,
        )
        basis = RateBasis.CURRENT_MARK if context.purpose is ValuationPurpose.POSITION else RateBasis.HELD_LATEST
        missing_scope = f"effective on or before {context.valuation_as_of.isoformat()}"

    if rate_input is None:
        return _unavailable(
            item,
            context,
            RATE_NOT_IMPORTED,
            f"no fx rate path configured for {item.native_currency} -> {context.reporting_currency}",
            f"Import an fx rate for {item.native_currency} -> {context.reporting_currency} {missing_scope}.",
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
    item: ValuationItem, context: ValuationContext, reason: str, message: str, remedy: str
) -> ExactValuation:
    return ExactValuation(
        native=item.native,
        native_currency=item.native_currency,
        reported_exact=None,
        context=context,
        basis=None,
        rate_path=(),
        unavailable=Unavailability(reason, message, remedy),
    )


def value_many(
    connection: sqlite3.Connection, items: Sequence[ValuationItem], *, context: ValuationContext
) -> tuple[list[ExactValuation], RateInventory]:
    """Value every item under one shared context; also return the distinct rates used."""
    values = [value_one(connection, item, context=context) for item in items]
    return values, _rate_inventory(values)


def _rate_inventory(values: Sequence[ExactValuation]) -> RateInventory:
    """The distinct rate inputs consumed by ``values``, in a stable order.

    Split out from :func:`value_many` so the key's ordering and dedup
    behaviour can be exercised directly against rate metadata shapes that no
    M2 selector produces yet.
    """
    # Every component is a string or bool so the key is totally ordered:
    # ``period_month`` and ``effective_on`` are both nullable, and sorting a
    # mix of ``None`` and ``str`` raises. ``observation_kind`` and
    # ``effective_on`` are part of the key because M7 admits a daily
    # observation and a monthly proxy for the same month (§3.4); keying only
    # by month would silently collapse them into one inventory row.
    seen: dict[tuple[str, str, str, str, str, bool, str], RateInput] = {}
    for value in values:
        for rate_input in value.rate_path:
            key = (
                rate_input.base_currency,
                rate_input.quote_currency,
                rate_input.observation_kind,
                rate_input.effective_on.isoformat() if rate_input.effective_on is not None else "",
                rate_input.period_month or "",
                rate_input.inverted,
                rate_input.source_version,
            )
            seen[key] = rate_input
    return RateInventory(rate_inputs=tuple(seen[key] for key in sorted(seen)))


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
class ExcludedScopeDetail:
    """Why exactly *this* scope was excluded (§4.1, §6.2).

    Distinct from ``ConsolidatedValuation.unavailable_reasons``, which is a
    flat, deduplicated set answering "was reason X present anywhere in this
    result". A caller rendering one row per excluded currency/period needs its
    own reason(s) and remedy(ies) beside it, not a global list it must guess
    the mapping for. Every reason here is a *rate*-availability gap by
    construction — this module never knows about missing balance/movement
    evidence, which is a distinct kind of gap surfaced elsewhere (e.g.
    ``fincl.position.queries``) and must not be merged into this list by a
    caller that combines both (§4.3: "keep evidence gaps distinct from rate
    gaps").
    """

    scope: ValuationScope
    entry_count: int
    reasons: tuple[str, ...]
    remedies: tuple[str, ...]


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
    excluded_detail: tuple[ExcludedScopeDetail, ...]
    rate_inputs: tuple[RateInput, ...]
    context: ValuationContext
    # Per-item results in the caller's input order. Not part of the wire DTO:
    # a consolidated total is quantized once from ``total``/``partial_total``
    # and never from these rows (§3.6). They exist so a caller that must show
    # rows beside the parent, or reproduce a pre-seam per-row provenance
    # payload, can do so without valuing anything a second time.
    values: tuple[ExactValuation, ...]


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
    excluded_detail: list[ExcludedScopeDetail] = []
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
            scope_reasons = {v.unavailable.reason for v in unavailable_values if v.unavailable is not None}
            scope_remedies = {v.unavailable.remedy for v in unavailable_values if v.unavailable is not None}
            reasons.update(scope_reasons)
            excluded_detail.append(
                ExcludedScopeDetail(
                    scope=scope, entry_count=len(unavailable_values),
                    reasons=tuple(sorted(scope_reasons)), remedies=tuple(sorted(scope_remedies)),
                )
            )
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
        excluded_detail=tuple(sorted(excluded_detail, key=lambda d: _scope_key(d.scope))),
        rate_inputs=inventory.rate_inputs,
        context=context,
        values=tuple(values),
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
    # §4.1: the exact ratio actually applied, as decimal-string integers —
    # never a JS ``number``, which silently loses precision above 2**53.
    # ``direction`` restates ``inverted`` as a display-ready label rather than
    # asking every consumer to re-derive one from a bare bool.
    applied_numerator: str
    applied_denominator: str
    direction: Literal["direct", "inverse"]
    # The genuinely imported printed decimal, when one is on file (see
    # ``RateInput.imported_rate_text``); ``None`` only when no printed text
    # exists for either direction. A UI must never present a reciprocal like
    # 1/0.75 by rounding it to a false-precision finite decimal (§4.1) — the
    # exact ``applied_numerator/applied_denominator`` pair is what to render,
    # with this field shown alongside as "as originally imported".
    imported_rate_text: str | None


class UnavailabilityDTO(_StrictModel):
    reason: str
    message: str
    remedy: str


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


class ExcludedScopeDetailDTO(_StrictModel):
    currency: str
    period: str | None
    entry_count: int
    reasons: list[str]
    remedies: list[str]


class ConsolidatedValuationDTO(_StrictModel):
    total: MoneyDTO | None
    status: ConsolidationStatus
    partial_total: MoneyDTO | None
    included: list[ScopeDTO]
    excluded: list[ScopeDTO]
    excluded_entry_count: int
    unavailable_reasons: list[str]
    # §4.1/§4.3: per-scope reason(s)/remedy(ies) alongside the flat, backward
    # -compatible ``excluded``/``unavailable_reasons`` pair above.
    excluded_detail: list[ExcludedScopeDetailDTO]
    rate_path: list[RateInputDTO]


def _definition(
    currency_registry: dict[str, CurrencyDefinition], code: str
) -> CurrencyDefinition:
    """The registry entry for ``code``, as a typed error rather than a ``KeyError``.

    Quantization needs a checked ``minor_unit_scale`` (§3.2); there is no
    defensible fallback scale to invent when the reporting currency is not in
    the registry at all.
    """
    definition = currency_registry.get(code)
    if definition is None:
        raise FinCLError(f"reporting currency {code!r} is not in the checked currency registry")
    return definition


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
        applied_numerator=str(rate_input.rate_numerator),
        applied_denominator=str(rate_input.rate_denominator),
        direction="inverse" if rate_input.inverted else "direct",
        imported_rate_text=rate_input.imported_rate_text,
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
                reason=value.unavailable.reason,
                message=value.unavailable.message,
                remedy=value.unavailable.remedy,
            ),
        )
    assert value.reported_exact is not None and value.basis is not None
    definition = _definition(currency_registry, value.context.reporting_currency)
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
    definition = _definition(currency_registry, consolidated.context.reporting_currency)
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
        excluded_detail=[
            ExcludedScopeDetailDTO(
                currency=d.scope.currency, period=d.scope.period, entry_count=d.entry_count,
                reasons=list(d.reasons), remedies=list(d.remedies),
            )
            for d in consolidated.excluded_detail
        ],
        rate_path=[_rate_input_dto(r) for r in consolidated.rate_inputs],
    )


# --- Rate selection (fx.py-backed; M2 has month-precision data only) -------


def select_monthly_rate(
    connection: sqlite3.Connection,
    native_currency: str,
    reporting_currency: str,
    period: str,
    *,
    allow_inverse: bool,
) -> RateInput | None:
    """Exact-period lookup: direct, then reciprocal when the policy allows one.

    ``allow_inverse`` is §3.2's ``direct-or-inverse-v1`` reciprocal leg, made
    conditional so a named older ladder can keep resolving exactly the rows it
    always did (see ``LATEST_FIRST_IMPORT_V1``). No triangulation either way.

    This is the whole of M2's ``flow-rate-v1``: with only ``MONTHLY_PROXY``
    data in ``fx_rates``, steps 1-2 (exact/prior dated observation) and step 3
    (explicit monthly average) of §3.4 have nothing to select from yet. Never
    consults a different period, so it cannot look ahead. Public so the step
    02 coverage reader predicts FLOW rates through this exact ladder.
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
            imported_rate_text=row["rate_text"],
        )
    if not allow_inverse:
        return None
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
            imported_rate_text=inverse_row["rate_text"],
        )
    return None


def select_latest_rate_on_or_before(
    connection: sqlite3.Connection,
    native_currency: str,
    reporting_currency: str,
    period_ceiling: str,
    *,
    allow_inverse: bool,
) -> RateInput | None:
    """Latest period <= ``period_ceiling``, never later (§3.4, §4.2).

    ``period`` is stored ``YYYY-MM`` text, so lexicographic ordering is
    chronological ordering; the ``<=`` bound is what makes no-look-ahead hold
    for a historical ``valuation_as_of``. The reciprocal leg is gated on the
    named policy exactly as in :func:`select_monthly_rate`. ``period_ceiling``
    is the caller's already-resolved eligible-month bound — permissive
    (``valuation_as_of``'s own month) for a legacy policy, or
    :func:`eligible_month_ceiling`'s closed-month bound for
    ``position-monthly-closed-v1``/``forward-monthly-closed-v1``. Public so the
    step 02 coverage reader predicts through this exact ladder.
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
            imported_rate_text=row["rate_text"],
        )
    if not allow_inverse:
        return None
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
            imported_rate_text=inverse_row["rate_text"],
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
