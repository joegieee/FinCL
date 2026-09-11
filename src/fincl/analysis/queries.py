"""The `GET /api/v1/analysis` panel assembly (v0.2 slice 2).

One generation-coherent read over the committed component projection: the
spending / fees / refunds summaries, the selectable spending-versus-income bars,
the ranked category changes, the ranked merchants, and the handles that page the
exact components behind each of them. Every panel is computed from the same
`analysis_components` rows, so a total and its drill-down cannot disagree.
"""

from __future__ import annotations

import calendar
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from ..db import connect_readonly, database_path
from ..decision_journal import default_journal_path, read_records
from ..errors import FinCLError
from ..generation import build_manifest, current_generation, generation_token
from ..intervals import DateInterval, month_span
from ..money import ExactMoney, exact_sum, format_decimal
from ..report import _coverage_contract
from . import handles
from .scopes import AnalysisScope, normalize_scope, resolve_comparison

_SUMMARY_BUCKETS = (
    "spending", "fees", "refunds", "income", "reimbursements",
    "non_income_inflow", "pending_review",
)


def _money(value: str, currency: str) -> dict[str, str]:
    return {"value": value, "currency": currency}


def _sum_magnitude(rows: list[Any], predicate: Any) -> str:
    values = [
        ExactMoney(abs(int(r["signed_coefficient"])), int(r["scale"]), "")
        for r in rows
        if predicate(r)
    ]
    coefficient, scale = exact_sum(*values)
    return format_decimal(coefficient, scale)


def _sum_signed(rows: list[Any], predicate: Any) -> str:
    values = [
        ExactMoney(int(r["signed_coefficient"]), int(r["scale"]), "")
        for r in rows
        if predicate(r)
    ]
    coefficient, scale = exact_sum(*values)
    return format_decimal(coefficient, scale)


def _pct(current: str, previous: str) -> str | None:
    prev = Decimal(previous)
    if prev == 0:
        return None
    change = (Decimal(current) - prev) / prev * 100
    return str(change.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _delta(current: str, previous: str) -> str:
    prev = ExactMoney.parse(previous)
    return format_decimal(
        *exact_sum(
            ExactMoney.parse(current),
            ExactMoney(-prev.coefficient, prev.scale, ""),
        )
    )


def _window_rows(
    connection: Any, token: str, scope: AnalysisScope, start: str, end: str
) -> list[Any]:
    where = [
        "generation_token = :gen", "lens = :lens", "currency = :currency",
        "represented_start >= :s", "represented_end <= :e",
    ]
    params: dict[str, Any] = {
        "gen": token, "lens": scope.lens, "currency": scope.currency,
        "s": start, "e": end,
    }
    if scope.accounts:
        names = ", ".join(f":a{i}" for i in range(len(scope.accounts)))
        where.append(f"account IN ({names})")
        params.update({f"a{i}": name for i, name in enumerate(scope.accounts)})
    if scope.category_keys:
        names = ", ".join(f":c{i}" for i in range(len(scope.category_keys)))
        where.append(f"category_key IN ({names})")
        params.update({f"c{i}": key for i, key in enumerate(scope.category_keys)})
    if scope.merchant is not None:
        where.append("merchant_display = :merchant")
        params["merchant"] = scope.merchant
    return connection.execute(
        "SELECT bucket, role, nets, signed_coefficient, scale, category_key, "
        "merchant_display FROM analysis_components WHERE " + " AND ".join(where),
        params,
    ).fetchall()


def _grouped_spending(rows: list[Any], key: str) -> dict[str, tuple[str, int]]:
    out: dict[str, list[ExactMoney]] = {}
    for row in rows:
        if row["bucket"] != "spending":
            continue
        name = row[key]
        out.setdefault(name, []).append(
            ExactMoney(abs(int(row["signed_coefficient"])), int(row["scale"]), "")
        )
    return {
        name: (format_decimal(*exact_sum(*values)), len(values))
        for name, values in out.items()
    }


def analysis_page(
    database: Path,
    *,
    start: str,
    end: str,
    currency: str = "SGD",
    accounts: list[str] | None = None,
    category_keys: list[str] | None = None,
    merchant: str | None = None,
    comparison: str | None = None,
    trend_range: str = "6",
    expected: str | None = None,
) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        return analysis_page_from(
            connection,
            start=start, end=end, currency=currency, accounts=accounts,
            category_keys=category_keys, merchant=merchant, comparison=comparison,
            trend_range=trend_range, expected=expected,
        )
    finally:
        connection.close()


def analysis_page_from(
    connection: Any,
    *,
    start: str,
    end: str,
    currency: str = "SGD",
    accounts: list[str] | None = None,
    category_keys: list[str] | None = None,
    merchant: str | None = None,
    comparison: str | None = None,
    trend_range: str = "6",
    expected: str | None = None,
) -> dict[str, Any]:
    if trend_range not in {"6", "12", "all"}:
        raise FinCLError("trend_range must be one of: 6, 12, all")
    scope = normalize_scope(
        start=start, end=end, currency=currency, accounts=accounts or (),
        category_keys=category_keys or (), merchant=merchant,
    )
    generation = current_generation(connection)
    if generation is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    token = generation["generation_token"]
    if expected and expected != token:
        raise FinCLError("STALE_GENERATION: reopen Analysis")
    manifest = json.loads(generation["manifest_json"])
    records = read_records(default_journal_path(database_path(connection)))
    stale = generation_token(build_manifest(connection)) != token or (
        records[-1].record_hash if records else "0" * 64
    ) != manifest["journal_head"]

    contract = _coverage_contract(connection)
    covered = any(
        interval.contains_interval(DateInterval(scope.start, scope.end))
        for interval in contract.covered_intervals
    )
    comp = resolve_comparison(
        scope, contract.covered_intervals, requested=comparison,
        main_covered=covered,
    )

    current_rows = (
        _window_rows(connection, token, scope, scope.start, scope.end)
        if covered else []
    )
    previous_rows = (
        _window_rows(connection, token, scope, comp.start, comp.end)  # type: ignore[arg-type]
        if comp.available else []
    )

    def _handle(buckets: list[str], **narrow: str) -> str | None:
        if not covered:
            return None
        return handles.encode(
            generation=token, lens=scope.lens, currency=scope.currency,
            start=scope.start, end=scope.end, buckets=buckets, sort="amount",
            **narrow,
        )

    summary: dict[str, Any] = {}
    summary_handles: dict[str, str | None] = {}
    for bucket in _SUMMARY_BUCKETS:
        value = _sum_magnitude(current_rows, lambda r, b=bucket: r["bucket"] == b) if covered else None
        entry: dict[str, Any] = {"value": _money(value, currency) if value is not None else None}
        if comp.available:
            prev = _sum_magnitude(previous_rows, lambda r, b=bucket: r["bucket"] == b)
            entry["previous"] = _money(prev, currency)
            entry["delta"] = _money(_delta(value or "0", prev), currency)
            entry["pct"] = _pct(value or "0", prev)
        else:
            entry["previous"] = entry["delta"] = entry["pct"] = None
        summary[bucket] = entry
        summary_handles[bucket] = _handle([bucket])
    net_value = _sum_signed(current_rows, lambda r: bool(r["nets"])) if covered else None
    net_entry: dict[str, Any] = {"value": _money(net_value, currency) if net_value is not None else None}
    if comp.available:
        net_prev = _sum_signed(previous_rows, lambda r: bool(r["nets"]))
        net_entry.update(
            previous=_money(net_prev, currency),
            delta=_money(_delta(net_value or "0", net_prev), currency),
            pct=_pct(net_value or "0", net_prev),
        )
    else:
        net_entry.update(previous=None, delta=None, pct=None)
    summary["net_economic_flow"] = net_entry
    summary_handles["net_economic_flow"] = _handle(["net"])

    # What changed? — per-category gross spending, current vs comparison.
    increases: list[dict[str, Any]] = []
    decreases: list[dict[str, Any]] = []
    if comp.available:
        cur_by_cat = _grouped_spending(current_rows, "category_key")
        prev_by_cat = _grouped_spending(previous_rows, "category_key")
        for name in sorted(set(cur_by_cat) | set(prev_by_cat)):
            cur_value = cur_by_cat.get(name, ("0", 0))[0]
            prev_value = prev_by_cat.get(name, ("0", 0))[0]
            delta = _delta(cur_value, prev_value)
            if Decimal(delta) == 0:
                continue
            row = {
                "category": name,
                "current": _money(cur_value, currency),
                "previous": _money(prev_value, currency),
                "delta": _money(delta, currency),
                "pct": _pct(cur_value, prev_value),
                "new_in_period": Decimal(prev_value) == 0,
                "handle": _handle(["spending"], category_key=name),
            }
            (increases if Decimal(delta) > 0 else decreases).append(row)
        increases.sort(key=lambda r: Decimal(r["delta"]["value"]), reverse=True)
        decreases.sort(key=lambda r: Decimal(r["delta"]["value"]))

    # Ranked merchants for the current period.
    merchant_ranking = [
        {
            "merchant": name,
            "value": _money(value, currency),
            "count": count,
            "handle": _handle(["spending"], merchant_display=name),
        }
        for name, (value, count) in sorted(
            _grouped_spending(current_rows, "merchant_display").items(),
            key=lambda kv: (-Decimal(kv[1][0]), kv[0]),
        )
    ]

    # Receipt-period category cost: gross spending per category minus the
    # refund allocations whose refund was received inside the window,
    # attributed to the refunded purchase's current category
    # (DESIGN-relationships-and-cost.md §3). This can go negative for a
    # category with refunds but no purchases in the window.
    gross_by_cat = _grouped_spending(current_rows, "category_key") if covered else {}
    refunds_by_cat: dict[str, list[ExactMoney]] = {}
    if covered:
        for row in connection.execute(
            "SELECT ra.refund_coefficient, ra.refund_scale, ra.currency, "
            "substr(re.effective_at, 1, 10) AS refund_date, "
            "COALESCE(ec.group_name || ' / ' || ec.category, "
            "         'Unclassified / Unclassified') AS category_key "
            "FROM refund_allocations ra "
            "JOIN ledger_entries re ON re.natural_anchor = ra.refund_anchor "
            "JOIN extraction_runs xr ON xr.id = re.extraction_run_id "
            "  AND xr.selection_state = 'SELECTED' "
            "JOIN ledger_entries pe ON pe.natural_anchor = ra.purchase_anchor "
            "LEFT JOIN entry_categories ec ON ec.entry_id = pe.id"
        ):
            if row["currency"] != scope.currency:
                continue
            if not (scope.start <= row["refund_date"] <= scope.end):
                continue
            if scope.category_keys and row["category_key"] not in scope.category_keys:
                continue
            refunds_by_cat.setdefault(row["category_key"], []).append(
                ExactMoney(int(row["refund_coefficient"]), int(row["refund_scale"]), "")
            )
    receipt_period_cost = []
    for name in sorted(set(gross_by_cat) | set(refunds_by_cat)):
        gross_str = gross_by_cat.get(name, ("0", 0))[0]
        refund_terms = refunds_by_cat.get(name, [])
        refund_str = format_decimal(*exact_sum(*refund_terms)) if refund_terms else "0"
        gross = ExactMoney.parse(gross_str)
        refund = ExactMoney.parse(refund_str)
        net_str = format_decimal(
            *exact_sum(gross, ExactMoney(-refund.coefficient, refund.scale, ""))
        )
        receipt_period_cost.append({
            "category": name,
            "gross": _money(gross_str, currency),
            "refunds_received": _money(refund_str, currency),
            "net_cost": _money(net_str, currency),
        })

    # Complete calendar months ending at the most recent complete month whose
    # end does not exceed the selected end. "all" begins at the first month
    # in the ledger's coverage contract, retaining any gaps as unavailable.
    end_date = date.fromisoformat(scope.end)
    if end_date.day != calendar.monthrange(end_date.year, end_date.month)[1]:
        anchor_index = end_date.year * 12 + end_date.month - 2
    else:
        anchor_index = end_date.year * 12 + end_date.month - 1
    if trend_range == "all" and contract.covered_intervals:
        first = min(interval.start for interval in contract.covered_intervals)
        first_date = date.fromisoformat(first)
        first_index = first_date.year * 12 + first_date.month - 1
        months = max(1, anchor_index - first_index + 1)
    else:
        months = int(trend_range)

    trend: list[dict[str, Any]] = []
    for offset in range(months - 1, -1, -1):
        year, month = divmod(anchor_index - offset, 12)
        month += 1
        ym = f"{year:04d}-{month:02d}"
        span = month_span(ym)
        full = any(
            interval.contains_interval(span)
            for interval in contract.covered_intervals
        )
        if full:
            rows = _window_rows(connection, token, scope, span.start, span.end)
            trend.append({
                "month": ym,
                "spending": _money(_sum_magnitude(rows, lambda r: r["bucket"] == "spending"), currency),
                "fees": _money(_sum_magnitude(rows, lambda r: r["bucket"] == "fees"), currency),
                "income": _money(_sum_magnitude(rows, lambda r: r["bucket"] == "income"), currency),
                "complete": True,
            })
        else:
            trend.append({
                "month": ym, "spending": None, "fees": None, "income": None,
                "complete": False,
            })

    return {
        "generation": token,
        "freshness": "stale" if stale else "current",
        "updated_at": generation["completed_at"],
        "scope": scope.as_dict(),
        "covered": covered,
        "coverage_reasons": contract.reasons,
        "comparison": comp.as_dict(),
        "summary": summary,
        "summary_handles": summary_handles,
        "trend": trend,
        "category_changes": {
            "increases": increases,
            "decreases": decreases,
            "restated_using_current_categories": True,
        },
        "merchant_ranking": merchant_ranking,
        "receipt_period_cost": receipt_period_cost,
        "contributors_handle": _handle(["spending"]),
    }
