"""All-currency reported Transactions (§5, §6; plan M3b.2 step 07).

Reuses the exact native candidate selector `queries._ENTRY_VIEW` — the same
eligibility, review/category/account filtering, superseded-extraction
exclusion (`selection_state = 'SELECTED'`) and shaping every legacy
transaction surface already reads — so a reported row can never disagree
with its native counterpart about *which* rows are eligible. The only new
predicate this module adds is an *optional* `pocket_currency` narrowing;
default scope is every currency pocket (§1 invariant 2, §6.3).

One connection-accepting candidate selector (`_candidate_rows`) backs list,
count, anchors, and single-entry lookup alike, so all four routes select
from the same set under the same filters. Each row is additionally valued
under FLOW (native settlement amount, never a re-converted foreign
original — §5.3) and enriched with step 06's read-only statement-money
detail; missing FX never removes a candidate, only leaves its `reported`
field `None` with a typed reason (`valuation.Unavailability`).

For `sort="amount"`, the complete filtered candidate set is valued and
sorted in Python before any pagination (§6, item 3): priced rows ascending
by exact `Fraction` value (largest outgoing first, matching the legacy
"amount" sort's own convention), then unpriceable rows, then an
anchor/id tie-break. `sort="date"`/`"merchant"` order by the native field
directly (no valuation needed for order) and only value the returned page
window. Every cursor is a plain offset bound to a digest of the full
scope — filters, sort, generation, reporting policy version, rate
-inventory digest and `valuation_as_of` — so a changed context or a
malformed cursor is rejected outright rather than silently reused.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any

from .baseline.outliers import outlier_anchor_set
from .common import canonical_json
from .errors import FinCLError
from .money import ExactMoney, format_decimal
from .queries import (
    _ENTRY_VIEW,
    BULK_SELECT_CAP,
    _role_filtered_anchors,
    _rule_tables,
    _shape,
    _window_sql,
)
from .reporting_context import ReportedScope, ReportingContext, build_reporting_context
from .statement_money import StatementMoneyDetailDTO, read_statement_money_detail
from .valuation import (
    FLOW_RATE_V1,
    EntryPrecision,
    ExactValuation,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    value_one,
)

__all__ = [
    "ReportedTransactionRow",
    "TransactionAnchorsResult",
    "TransactionsPageResult",
    "decode_anchor",
    "encode_anchor",
    "reported_transaction_anchors_from",
    "reported_transaction_detail_from",
    "reported_transactions_page_from",
]

_SORTS = ("date", "merchant", "amount")


def _flow_context(reporting_currency: str, as_of: date) -> ValuationContext:
    return ValuationContext(
        reporting_currency=reporting_currency,
        purpose=ValuationPurpose.FLOW,
        valuation_as_of=as_of,
        rate_selection_policy=FLOW_RATE_V1,
    )


@dataclass(frozen=True, slots=True)
class ReportedTransactionRow:
    entry: dict[str, Any]
    amount_valuation: ExactValuation
    fee_valuation: ExactValuation | None
    statement_detail: StatementMoneyDetailDTO


@dataclass(frozen=True, slots=True)
class TransactionsPageResult:
    context: ReportingContext
    sort: str
    rows: tuple[ReportedTransactionRow, ...]
    total: int
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TransactionAnchorsResult:
    context: ReportingContext
    anchors: tuple[str, ...]
    truncated: bool
    cap: int


def encode_anchor(natural_anchor: str) -> str:
    """URL-safe transport encoding for a path-segment anchor (§6.1)."""
    return base64.urlsafe_b64encode(natural_anchor.encode()).decode()


def decode_anchor(encoded: str) -> str:
    try:
        return base64.urlsafe_b64decode(encoded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise FinCLError("This transaction link is not valid; reopen the list.") from exc


def _reported_tx_filter(
    *,
    pocket_currency: str | None,
    history_start: str,
    history_end: str,
    include_excluded: bool,
    account: str,
    category: str,
    search: str,
    kind: str,
    direction: str = "",
    role_anchors: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """`queries._tx_filter`, minus the mandatory single-pocket predicate.

    Kept as its own function rather than an edit to `_tx_filter` itself: the
    legacy filter's `currency` clause is required precisely because every
    legacy caller narrows to one pocket by construction (§1 invariant 2 does
    not apply there), and changing that shared function's signature would
    touch the legacy route's own behavior. This mirrors its every other
    clause exactly so both filters can never quietly diverge on eligibility.
    """
    where = [_window_sql(":start", ":end")]
    params: dict[str, Any] = {"start": history_start, "end": history_end}
    if pocket_currency:
        where.append("currency_pocket = :currency")
        params["currency"] = pocket_currency
    if not include_excluded:
        where.append("eligible")
    if account:
        where.append("account = :account")
        params["account"] = account
    if category:
        where.append("category = :category")
        params["category"] = category
    if search.strip():
        where.append("instr(lower(merchant || ' ' || description), lower(:search)) > 0")
        params["search"] = search.strip()
    if direction in ("in", "out"):
        where.append("economic_direction = :direction")
        params["direction"] = direction.upper()
    if kind == "fees":
        where.append("fee_nonzero")
    elif kind == "review":
        where.append("review")
    elif kind:
        where.append("bucket = :kind")
        params["kind"] = kind
    if role_anchors is not None:
        if role_anchors:
            names = ",".join(f":rf{i}" for i in range(len(role_anchors)))
            where.append(f"natural_anchor IN ({names})")
            params.update({f"rf{i}": anchor for i, anchor in enumerate(role_anchors)})
        else:
            where.append("0")
    return " AND ".join(where), params


def _candidate_rows(
    connection: sqlite3.Connection,
    *,
    history_start: str,
    history_end: str,
    pocket_currency: str | None,
    account: str = "",
    category: str = "",
    search: str = "",
    kind: str = "",
    direction: str = "",
    behavior: str = "",
    need: str = "",
    basis: str = "",
    unresolved_only: bool = False,
    include_excluded: bool = False,
) -> list[sqlite3.Row]:
    """The one shared candidate set list, count, anchors and single-entry
    lookup all select from — same WHERE clause, same eligibility."""
    base: dict[str, Any] = {
        "pocket_currency": pocket_currency, "history_start": history_start, "history_end": history_end,
        "include_excluded": include_excluded, "account": account, "category": category, "search": search,
        "kind": kind, "direction": direction,
    }
    role_anchors = None
    if behavior or need or basis or unresolved_only:
        base_clause, base_params = _reported_tx_filter(**base)
        role_anchors = _role_filtered_anchors(
            connection, base_clause, base_params,
            behavior=behavior, need=need, basis=basis, unresolved_only=unresolved_only,
        )
    clause, params = _reported_tx_filter(**base, role_anchors=role_anchors)
    return connection.execute(f"SELECT * FROM ({_ENTRY_VIEW}) WHERE {clause}", params).fetchall()


def _precision_fields(row: sqlite3.Row) -> tuple[EntryPrecision, str | None, date | None]:
    precision = str(row["effective_precision"])
    if precision == "DAY":
        return EntryPrecision.DAY, None, date.fromisoformat(str(row["effective_at"])[:10])
    if precision == "MONTH":
        return EntryPrecision.MONTH, str(row["effective_month"]), None
    return EntryPrecision.UNKNOWN, None, None


def _amount_item(row: sqlite3.Row) -> ValuationItem:
    magnitude = abs(int(row["amount_coefficient"]))
    scale = int(row["amount_scale"])
    signed = -magnitude if row["economic_direction"] == "OUT" else magnitude
    native = ExactMoney(signed, scale, format_decimal(signed, scale))
    precision, period_month, effective_on = _precision_fields(row)
    return ValuationItem(
        native=native, native_currency=str(row["currency_pocket"]), purpose=ValuationPurpose.FLOW,
        precision=precision, period_month=period_month, effective_on=effective_on,
    )


def _fee_item(row: sqlite3.Row) -> ValuationItem:
    coefficient = int(row["fee_coefficient"])
    scale = int(row["fee_scale"])
    native = ExactMoney(coefficient, scale, format_decimal(coefficient, scale))
    precision, period_month, effective_on = _precision_fields(row)
    return ValuationItem(
        native=native, native_currency=str(row["currency_pocket"]), purpose=ValuationPurpose.FLOW,
        precision=precision, period_month=period_month, effective_on=effective_on,
    )


def _sort_key_amount(row: sqlite3.Row, value: ExactValuation | None) -> tuple[bool, Fraction, str, str]:
    assert value is not None
    if value.unavailable is not None:
        return (True, Fraction(0), str(row["natural_anchor"]), str(row["id"]))
    assert value.reported_exact is not None
    return (False, value.reported_exact, str(row["natural_anchor"]), str(row["id"]))


def _sort_key_date(row: sqlite3.Row) -> tuple[int, str, str]:
    ordinal = date.fromisoformat(str(row["effective_at"])[:10]).toordinal()
    return (-ordinal, str(row["natural_anchor"]), str(row["id"]))


def _sort_key_merchant(row: sqlite3.Row) -> tuple[str, str, str]:
    return (str(row["merchant"]), str(row["natural_anchor"]), str(row["id"]))


def _context_digest(context: ReportingContext, *, extra: Mapping[str, object]) -> str:
    payload = {
        "generation": context.generation_token,
        "policy_version": context.policy.policy_version,
        "rate_inventory": context.rate_inventory_digest,
        "as_of": context.scope.valuation_as_of.isoformat(),
        **extra,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()[:32]


def _encode_cursor(*, digest: str, offset: int) -> str:
    payload = {"digest": digest, "offset": offset}
    return base64.urlsafe_b64encode(canonical_json(payload).encode()).decode()


def _decode_offset(cursor: str | None, *, digest: str) -> int:
    if cursor is None:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        offset = int(payload["offset"])
        cursor_digest = str(payload["digest"])
    except (binascii.Error, ValueError, TypeError, KeyError) as exc:
        raise FinCLError("This page cursor is not valid; reload the list.") from exc
    if cursor_digest != digest or offset < 0:
        raise FinCLError("This page cursor is not valid; reload the list.")
    return offset


def _require_iso_dates(*values: str) -> None:
    for value in values:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise FinCLError("Dates must use YYYY-MM-DD") from exc


def _check_generation(context: ReportingContext, generation: str | None) -> None:
    if generation is not None and generation != context.generation_token:
        raise FinCLError("STALE_GENERATION: reload the list")


def _build_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    amount_value: ExactValuation | None,
    *,
    amount_context: ValuationContext,
    role_rules: list[dict[str, Any]],
    inbound_rules: list[dict[str, Any]],
    outliers: set[str],
) -> ReportedTransactionRow:
    if amount_value is None:
        amount_value = value_one(connection, _amount_item(row), context=amount_context)
    fee_value = None
    if row["fee_coefficient"] is not None:
        fee_value = value_one(connection, _fee_item(row), context=amount_context)
    detail = read_statement_money_detail(connection, str(row["id"]))
    entry = _shape(row, role_rules, inbound_rules, outliers)
    return ReportedTransactionRow(
        entry=entry, amount_valuation=amount_value, fee_valuation=fee_value, statement_detail=detail
    )


def reported_transactions_page_from(
    connection: sqlite3.Connection,
    *,
    as_of: date,
    history_start: str,
    history_end: str,
    pocket_currency: str | None = None,
    account: str = "",
    category: str = "",
    search: str = "",
    kind: str = "",
    direction: str = "",
    behavior: str = "",
    need: str = "",
    basis: str = "",
    unresolved_only: bool = False,
    include_excluded: bool = False,
    sort: str = "date",
    cursor: str | None = None,
    limit: int = 50,
    generation: str | None = None,
) -> TransactionsPageResult:
    if sort not in _SORTS:
        raise FinCLError("Unknown sort order")
    limit = max(1, min(int(limit), 200))
    _require_iso_dates(history_start, history_end)

    context = build_reporting_context(
        connection,
        scope=ReportedScope(
            valuation_as_of=as_of, history_start=history_start, history_end=history_end,
            pocket_currency=pocket_currency,
        ),
    )
    _check_generation(context, generation)

    filters = {
        "pocket_currency": pocket_currency, "history_start": history_start, "history_end": history_end,
        "account": account, "category": category, "search": search, "kind": kind, "direction": direction,
        "behavior": behavior, "need": need, "basis": basis, "unresolved_only": unresolved_only,
        "include_excluded": include_excluded, "sort": sort,
    }
    digest = _context_digest(context, extra=filters)
    offset = _decode_offset(cursor, digest=digest)

    rows = _candidate_rows(
        connection, history_start=history_start, history_end=history_end, pocket_currency=pocket_currency,
        account=account, category=category, search=search, kind=kind, direction=direction,
        behavior=behavior, need=need, basis=basis, unresolved_only=unresolved_only,
        include_excluded=include_excluded,
    )

    amount_context = _flow_context(context.reporting_currency, as_of)
    pairs: list[tuple[sqlite3.Row, ExactValuation | None]]
    if sort == "amount":
        # §6 item 3: value the *entire* filtered candidate set exactly before
        # sorting or paginating — a per-page re-sort of a SQL LIMIT window
        # could not reproduce the exact cross-currency reported order.
        pairs = [(row, value_one(connection, _amount_item(row), context=amount_context)) for row in rows]
        pairs.sort(key=lambda pair: _sort_key_amount(pair[0], pair[1]))
    elif sort == "date":
        pairs = [(row, None) for row in rows]
        pairs.sort(key=lambda pair: _sort_key_date(pair[0]))
    else:
        pairs = [(row, None) for row in rows]
        pairs.sort(key=lambda pair: _sort_key_merchant(pair[0]))

    total = len(pairs)
    window = pairs[offset : offset + limit]
    next_cursor = _encode_cursor(digest=digest, offset=offset + limit) if offset + limit < total else None

    role_rules, inbound_rules = _rule_tables(connection)
    outliers = outlier_anchor_set(connection, context.generation_token)
    result_rows = tuple(
        _build_row(
            connection, row, value, amount_context=amount_context,
            role_rules=role_rules, inbound_rules=inbound_rules, outliers=outliers,
        )
        for row, value in window
    )
    return TransactionsPageResult(context=context, sort=sort, rows=result_rows, total=total, next_cursor=next_cursor)


def reported_transaction_anchors_from(
    connection: sqlite3.Connection,
    *,
    as_of: date,
    history_start: str,
    history_end: str,
    pocket_currency: str | None = None,
    account: str = "",
    category: str = "",
    search: str = "",
    kind: str = "",
    direction: str = "",
    behavior: str = "",
    need: str = "",
    basis: str = "",
    unresolved_only: bool = False,
    include_excluded: bool = False,
    generation: str | None = None,
) -> TransactionAnchorsResult:
    """Every natural anchor matching the *same* candidate set the list/count
    use (§6.1: "Same candidate set for bulk selection"), pinned to this
    context's generation and capped, exactly like the legacy bulk selector."""
    _require_iso_dates(history_start, history_end)
    context = build_reporting_context(
        connection,
        scope=ReportedScope(
            valuation_as_of=as_of, history_start=history_start, history_end=history_end,
            pocket_currency=pocket_currency,
        ),
    )
    _check_generation(context, generation)
    rows = _candidate_rows(
        connection, history_start=history_start, history_end=history_end, pocket_currency=pocket_currency,
        account=account, category=category, search=search, kind=kind, direction=direction,
        behavior=behavior, need=need, basis=basis, unresolved_only=unresolved_only,
        include_excluded=include_excluded,
    )
    ordered = sorted(rows, key=_sort_key_date)
    anchors = tuple(str(row["natural_anchor"]) for row in ordered[:BULK_SELECT_CAP])
    return TransactionAnchorsResult(
        context=context, anchors=anchors, truncated=len(ordered) > BULK_SELECT_CAP, cap=BULK_SELECT_CAP
    )


def reported_transaction_detail_from(
    connection: sqlite3.Connection, *, as_of: date, anchor: str, generation: str | None = None
) -> ReportedTransactionRow:
    """One entry, selected through the same `_ENTRY_VIEW` shape as the list
    (§6.1: "consistent single-entry selection") but with no other narrowing —
    a detail view must remain reachable regardless of the list's own filters."""
    context = build_reporting_context(connection, scope=ReportedScope(valuation_as_of=as_of))
    _check_generation(context, generation)
    row = connection.execute(
        f"SELECT * FROM ({_ENTRY_VIEW}) WHERE natural_anchor = :anchor", {"anchor": anchor}
    ).fetchone()
    if row is None:
        raise FinCLError(f"No transaction found for anchor {anchor!r}")
    role_rules, inbound_rules = _rule_tables(connection)
    outliers = outlier_anchor_set(connection, context.generation_token)
    amount_context = _flow_context(context.reporting_currency, as_of)
    return _build_row(
        connection, row, None, amount_context=amount_context,
        role_rules=role_rules, inbound_rules=inbound_rules, outliers=outliers,
    )
