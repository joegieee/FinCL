"""Stage 1B (part 1) — the per-month cost and income evidence table.

Pure read over the committed ``analysis_components`` projection — the same rows
the economic-flow report and the ``/analysis`` panels fold — so a baseline
figure and its drill-down cannot disagree.

For every month of the recommended baseline window it returns, per currency:

* ``gross_cost``   — gross spending plus fees, before any receipt is applied;
* ``owner_cost_recorded`` — gross cost less the refunds/reimbursements that
  *landed* that month (the recorded receipt-period semantics);
* ``owner_cost_baseline`` — gross cost less only the refunds *allocated* to a
  purchase in that month, so this month's refund of an earlier purchase never
  erases this month's groceries;
* ``income`` / ``other_inflow`` / ``pending_inflow`` kept separate.

The two owner-cost views are reconciled explicitly: their totals plus the
cross-month refund delta and the unmatched receipts must balance. Income is
grouped by payer into *likely* streams — never automatically confirmed.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..generation import current_generation
from ..intervals import month_span
from ..money import ExactMoney, compare, exact_sum, format_decimal
from .readiness import baseline_readiness_from

_COST_BUCKETS = {"spending", "fees"}
_OTHER_INFLOW_BUCKETS = {"non_income_inflow", "exceptional_inflow"}


def _zero() -> list[ExactMoney]:
    return []


def _total(values: list[ExactMoney]) -> str:
    coefficient, scale = exact_sum(*values) if values else (0, 0)
    return format_decimal(coefficient, scale)


def _minus(left: str, *rights: str) -> str:
    terms = [ExactMoney.parse(left)]
    for right in rights:
        parsed = ExactMoney.parse(right)
        terms.append(ExactMoney(-parsed.coefficient, parsed.scale, ""))
    return format_decimal(*exact_sum(*terms))


def _month_list(start_month: str, end_month: str) -> list[str]:
    months = [start_month]
    while months[-1] < end_month:
        year, month = (int(part) for part in months[-1].split("-"))
        month += 1
        if month == 13:
            year, month = year + 1, 1
        months.append(f"{year:04d}-{month:02d}")
    return months


def _refund_allocations(
    connection: sqlite3.Connection, token: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT ra.refund_coefficient AS coefficient, ra.refund_scale AS scale,
               ra.currency AS currency,
               substr(re.effective_at, 1, 7) AS refund_month,
               substr(pe.effective_at, 1, 7) AS purchase_month
        FROM refund_allocations ra
        JOIN ledger_entries re ON re.natural_anchor = ra.refund_anchor
        JOIN ledger_entries pe ON pe.natural_anchor = ra.purchase_anchor
        """
    ).fetchall()
    del token  # allocations are generation-independent; kept for signature parity
    return [
        {
            "amount": ExactMoney(int(row["coefficient"]), int(row["scale"]), ""),
            "currency": row["currency"],
            "refund_month": row["refund_month"],
            "purchase_month": row["purchase_month"],
        }
        for row in rows
    ]


def cost_income_table(database: Path, *, today: date) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return cost_income_table_from(connection, today=today)
    finally:
        connection.close()


def cost_income_table_from(connection: sqlite3.Connection, *, today: date) -> dict[str, Any]:
    readiness = baseline_readiness_from(connection, today=today)
    window = readiness["baseline_window"]
    if window["quality"] == "none" or not window["start_month"]:
        return {
            "available": False,
            "reason": window["reason"],
            "baseline_window": window,
            "tasks": readiness["tasks"],
            "months": [],
            "income_streams": [],
        }

    months = _month_list(window["start_month"], window["end_month"])
    span_start = month_span(months[0]).start
    span_end = month_span(months[-1]).end
    month_set = set(months)

    if not connection.in_transaction:
        connection.execute("BEGIN")
    generation = current_generation(connection)
    if generation is None:
        raise RuntimeError("ledger is not refreshed")
    token = generation["generation_token"]

    rows = connection.execute(
        """
        SELECT period, currency, bucket, signed_coefficient, scale,
               category_key, merchant_display, date_precision
        FROM analysis_components
        WHERE generation_token = ? AND lens = 'recorded_flow'
          AND represented_start >= ? AND represented_end <= ?
        """,
        (token, span_start, span_end),
    ).fetchall()
    allocations = _refund_allocations(connection, token)

    # (month, currency) -> bucketed magnitudes
    gross: dict[tuple[str, str], list[ExactMoney]] = {}
    refunds_received: dict[tuple[str, str], list[ExactMoney]] = {}
    reimb_received: dict[tuple[str, str], list[ExactMoney]] = {}
    income: dict[tuple[str, str], list[ExactMoney]] = {}
    other_inflow: dict[tuple[str, str], list[ExactMoney]] = {}
    pending_inflow: dict[tuple[str, str], list[ExactMoney]] = {}
    income_rows: list[dict[str, Any]] = []
    currencies: set[str] = set()

    for row in rows:
        month = row["period"]
        if month not in month_set:
            continue
        currency = row["currency"]
        currencies.add(currency)
        key = (month, currency)
        magnitude = ExactMoney(abs(int(row["signed_coefficient"])), int(row["scale"]), "")
        bucket = row["bucket"]
        if bucket in _COST_BUCKETS:
            gross.setdefault(key, _zero()).append(magnitude)
        elif bucket == "refunds":
            refunds_received.setdefault(key, _zero()).append(magnitude)
        elif bucket == "reimbursements":
            reimb_received.setdefault(key, _zero()).append(magnitude)
        elif bucket == "income":
            income.setdefault(key, _zero()).append(magnitude)
            income_rows.append(
                {
                    "month": month,
                    "currency": currency,
                    "payer": row["merchant_display"],
                    "amount": magnitude,
                    "date_precision": row["date_precision"],
                }
            )
        elif bucket in _OTHER_INFLOW_BUCKETS:
            other_inflow.setdefault(key, _zero()).append(magnitude)
        elif bucket == "pending_review":
            pending_inflow.setdefault(key, _zero()).append(magnitude)

    # Refund attribution for the baseline-adjusted view.
    allocated_to_purchase_month: dict[tuple[str, str], list[ExactMoney]] = {}
    matched_refund_months: dict[tuple[str, str], list[ExactMoney]] = {}
    # per currency: refund received outside window but allocated to an in-window
    # purchase, and the mirror case.
    cross_month_in: dict[str, list[ExactMoney]] = {}
    cross_month_out: dict[str, list[ExactMoney]] = {}
    for alloc in allocations:
        currency = alloc["currency"]
        purchase_key = (alloc["purchase_month"], currency)
        refund_key = (alloc["refund_month"], currency)
        purchase_in = alloc["purchase_month"] in month_set
        refund_in = alloc["refund_month"] in month_set
        if purchase_in:
            allocated_to_purchase_month.setdefault(purchase_key, _zero()).append(alloc["amount"])
            currencies.add(currency)
        if refund_in:
            matched_refund_months.setdefault(refund_key, _zero()).append(alloc["amount"])
        if purchase_in and not refund_in:
            cross_month_in.setdefault(currency, _zero()).append(alloc["amount"])
        if refund_in and not purchase_in:
            cross_month_out.setdefault(currency, _zero()).append(alloc["amount"])

    ordered_currencies = sorted(currencies)
    unmatched_total: dict[str, list[ExactMoney]] = {}
    month_rows: list[dict[str, Any]] = []
    for month in months:
        entry: dict[str, Any] = {"month": month, "by_currency": {}}
        for currency in ordered_currencies:
            key = (month, currency)
            gross_cost = _total(gross.get(key, []))
            refunds_month = _total(refunds_received.get(key, []))
            reimb_month = _total(reimb_received.get(key, []))
            matched_here = _total(matched_refund_months.get(key, []))
            unmatched_here = _minus(refunds_month, matched_here)
            unmatched_total.setdefault(currency, _zero()).append(
                ExactMoney.parse(unmatched_here)
            )
            allocated_here = _total(allocated_to_purchase_month.get(key, []))
            entry["by_currency"][currency] = {
                "gross_cost": gross_cost,
                "refunds_received": refunds_month,
                "reimbursements_received": reimb_month,
                "unmatched_receipts": unmatched_here,
                "owner_cost_recorded": _minus(gross_cost, refunds_month, reimb_month),
                "owner_cost_baseline": _minus(gross_cost, allocated_here, reimb_month),
                "income": _total(income.get(key, [])),
                "other_inflow": _total(other_inflow.get(key, [])),
                "pending_inflow": _total(pending_inflow.get(key, [])),
            }
        month_rows.append(entry)

    reconciliation = {}
    for currency in ordered_currencies:
        recorded_total = _total(
            [
                ExactMoney.parse(m["by_currency"][currency]["owner_cost_recorded"])
                for m in month_rows
            ]
        )
        baseline_total = _total(
            [
                ExactMoney.parse(m["by_currency"][currency]["owner_cost_baseline"])
                for m in month_rows
            ]
        )
        cross_in = _total(cross_month_in.get(currency, []))
        cross_out = _total(cross_month_out.get(currency, []))
        unmatched = _total(unmatched_total.get(currency, []))
        # recorded_total - baseline_total = A_in - R_in
        #   = cross_in - cross_out - unmatched_received_in_window
        delta = _minus(recorded_total, baseline_total)
        expected_delta = _minus(cross_in, cross_out, unmatched)
        reconciliation[currency] = {
            "owner_cost_recorded_total": recorded_total,
            "owner_cost_baseline_total": baseline_total,
            "unmatched_receipts_total": unmatched,
            "cross_month_refund_in": cross_in,
            "cross_month_refund_out": cross_out,
            "recorded_minus_baseline": delta,
            "expected_from_allocations": expected_delta,
            "balances": compare(ExactMoney.parse(delta), ExactMoney.parse(expected_delta)) == 0,
        }

    return {
        "available": True,
        "generation": token,
        "baseline_window": window,
        "currencies": ordered_currencies,
        "months": month_rows,
        "reconciliation": reconciliation,
        "income_streams": _income_streams(income_rows),
        "tasks": readiness["tasks"],
    }


def _income_streams(income_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in income_rows:
        grouped.setdefault((row["payer"], row["currency"]), []).append(row)
    streams: list[dict[str, Any]] = []
    for (payer, currency), occurrences in sorted(grouped.items()):
        amounts = [occ["amount"] for occ in occurrences]
        months = sorted({occ["month"] for occ in occurrences})
        lo = hi = amounts[0]
        for amount in amounts[1:]:
            if compare(amount, lo) < 0:
                lo = amount
            if compare(amount, hi) > 0:
                hi = amount
        streams.append(
            {
                "payer": payer,
                "currency": currency,
                "certainty": "likely",
                "occurrence_count": len(occurrences),
                "months_seen": months,
                "first_month": months[0],
                "last_month": months[-1],
                "total_received": _total(amounts),
                "min_amount": format_decimal(lo.coefficient, lo.scale),
                "max_amount": format_decimal(hi.coefficient, hi.scale),
                # Additive: each dated receipt, so a reporting view can value the
                # total and range at their own months instead of at one rate.
                "occurrences": [
                    {"month": occ["month"], "amount": format_decimal(occ["amount"].coefficient, occ["amount"].scale)}
                    for occ in occurrences
                ],
                "note": (
                    "historical receipts only; a future receipt is not evidenced, "
                    "so this stream is Likely, never Confirmed"
                ),
            }
        )
    return streams
