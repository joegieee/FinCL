"""Stage 1C (part 1) — the first baseline estimator and published metrics.

Pure read on top of :func:`fincl.baseline.cost_income_table`. It decomposes
each complete month of the recommended window into

    identified_recurring + unresolved_irregular + routine_variable_residual
        + adjustments (= -refunds -reimbursements)   ==   owner_cost_recorded

with ``routine_variable_residual`` as the exact balancing term, so the
partition reconciles to the selected owner-cost total for every month.

The estimate is deliberately simple and labelled as such:

* fixed recurring monthly cost comes only from *confirmed* recurring
  commitments (none ⇒ zero, and every routine cost sits in the residual);
* the variable residual estimate is the arithmetic mean of the monthly
  residuals, keeping genuine zero months; median / min / max are shown as
  comparison statistics, never as a probability;
* a single entry at least ``_OUTLIER_MULTIPLE``× its category's median month
  and above ``_OUTLIER_FLOOR`` is held out as observed irregular risk for
  review — it stays in the historical total, it is only removed from the
  routine residual.

The reserve amount is derived only when the deterministic role read has no
unresolved owner-cost contribution.  This deliberately makes an unclassified
purchase or an unlinked receipt adjustment block a protective claim.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..generation import current_generation
from ..intervals import month_span
from .queries import cost_income_table
from .roles import RESERVE_MONTHS, baseline_roles

_METHOD_VERSION = "baseline-estimator-v1"
_OUTLIER_METHOD = "outlier-v1: entry ≥ 3× category median-month and ≥ 150"
_OUTLIER_MULTIPLE = 3
_OUTLIER_FLOOR = Decimal("150")


def _dec(value: str) -> Decimal:
    return Decimal(value)


def _mean(values: list[Decimal], scale: int) -> str:
    if not values:
        return "0"
    total = sum(values, Decimal(0))
    quant = Decimal(1).scaleb(-max(scale, 2))
    return str((total / Decimal(len(values))).quantize(quant, rounding=ROUND_HALF_UP))


def _median(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _ratio(numerator: str, denominator: str, *, zero_reason: str) -> dict[str, Any]:
    num, den = _dec(numerator), _dec(denominator)
    if den == 0:
        return {"available": False, "reason": zero_reason,
                "numerator": numerator, "denominator": denominator}
    return {
        "available": True,
        "numerator": numerator,
        "denominator": denominator,
        "value": str((num / den).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
    }


def _entry_spending(
    database: Path, token: str, span_start: str, span_end: str
) -> list[dict[str, Any]]:
    connection = connect_readonly(database)
    try:
        rows = connection.execute(
            """
            SELECT period, currency, category_key,
                   signed_coefficient, scale
            FROM analysis_components
            WHERE generation_token = ? AND lens = 'recorded_flow'
              AND bucket IN ('spending', 'fees')
              AND represented_start >= ? AND represented_end <= ?
            """,
            (token, span_start, span_end),
        ).fetchall()
    finally:
        connection.close()
    return [
        {
            "month": row["period"],
            "currency": row["currency"],
            "category_key": row["category_key"],
            "amount": abs(Decimal(row["signed_coefficient"]).scaleb(-int(row["scale"]))),
        }
        for row in rows
    ]


def _irregular_by_month(
    entries: list[dict[str, Any]], months: list[str], currency: str
) -> tuple[dict[str, Decimal], list[dict[str, Any]]]:
    """Held-out irregular-risk amount per month for one currency."""
    scoped = [e for e in entries if e["currency"] == currency]
    category_month: dict[tuple[str, str], Decimal] = {}
    for entry in scoped:
        key = (entry["category_key"], entry["month"])
        category_month[key] = category_month.get(key, Decimal(0)) + entry["amount"]
    medians: dict[str, Decimal] = {}
    categories = {entry["category_key"] for entry in scoped}
    for category in categories:
        monthly = [category_month.get((category, month), Decimal(0)) for month in months]
        medians[category] = _median(monthly)

    irregular: dict[str, Decimal] = {month: Decimal(0) for month in months}
    flagged: list[dict[str, Any]] = []
    for entry in scoped:
        threshold = max(_OUTLIER_FLOOR, medians[entry["category_key"]] * _OUTLIER_MULTIPLE)
        if entry["amount"] >= threshold and entry["amount"] > 0:
            irregular[entry["month"]] += entry["amount"]
            flagged.append(
                {
                    "month": entry["month"],
                    "currency": currency,
                    "category_key": entry["category_key"],
                    "amount": str(entry["amount"]),
                    "threshold": str(threshold),
                }
            )
    return irregular, flagged


def _confirmed_recurring_flat(database: Path) -> dict[str, Decimal]:
    connection = connect_readonly(database)
    try:
        rows = connection.execute(
            "SELECT currency, expected_coefficient, expected_scale "
            "FROM recurring_commitments WHERE status = 'active'"
        ).fetchall()
    finally:
        connection.close()
    flat: dict[str, Decimal] = {}
    for row in rows:
        amount = Decimal(row["expected_coefficient"]).scaleb(-int(row["expected_scale"]))
        flat[row["currency"]] = flat.get(row["currency"], Decimal(0)) + amount
    return flat


def baseline_estimate(database: Path, *, today: date | None = None) -> dict[str, Any]:
    table = cost_income_table(database, today=today)
    if not table["available"]:
        return {
            "available": False,
            "reason": table["reason"],
            "baseline_window": table["baseline_window"],
            "tasks": table["tasks"],
        }

    window = table["baseline_window"]
    months = [row["month"] for row in table["months"]]
    span_start = month_span(months[0]).start
    span_end = month_span(months[-1]).end

    connection = connect_readonly(database)
    try:
        generation = current_generation(connection)
        token = generation["generation_token"] if generation else ""
    finally:
        connection.close()
    entries = _entry_spending(database, token, span_start, span_end)
    recurring_flat = _confirmed_recurring_flat(database)
    roles = baseline_roles(database, today=today)

    by_currency: dict[str, Any] = {}
    for currency in table["currencies"]:
        cells = {row["month"]: row["by_currency"][currency] for row in table["months"]}
        scale = 2
        irregular, flagged = _irregular_by_month(entries, months, currency)
        fixed_flat = recurring_flat.get(currency, Decimal(0))

        month_decomp: list[dict[str, Any]] = []
        residuals: list[Decimal] = []
        irregulars: list[Decimal] = []
        incomes: list[Decimal] = []
        for month in months:
            cell = cells[month]
            owner = _dec(cell["owner_cost_recorded"])
            adjustments = -(_dec(cell["refunds_received"]) + _dec(cell["reimbursements_received"]))
            irregular_m = irregular[month]
            identified_recurring_m = Decimal(0)  # per-month matching deferred
            residual = owner - irregular_m - identified_recurring_m - adjustments
            residuals.append(residual)
            irregulars.append(irregular_m)
            incomes.append(_dec(cell["income"]))
            month_decomp.append(
                {
                    "month": month,
                    "owner_cost_recorded": cell["owner_cost_recorded"],
                    "identified_recurring": str(identified_recurring_m.quantize(Decimal("0.01"))),
                    "unresolved_irregular": str(irregular_m.quantize(Decimal("0.01"))),
                    "adjustments": str(adjustments.quantize(Decimal("0.01"))),
                    "routine_variable_residual": str(residual.quantize(Decimal("0.01"))),
                    "reconciles": (
                        identified_recurring_m + irregular_m + residual + adjustments == owner
                    ),
                    "income": cell["income"],
                }
            )

        variable_residual_monthly = _mean(residuals, scale)
        irregular_allowance_monthly = _mean(irregulars, scale)
        income_monthly = _mean(incomes, scale)
        routine_lifestyle_baseline = str(
            (_dec(variable_residual_monthly) + fixed_flat).quantize(Decimal("0.01"))
        )
        total_observed_monthly = str(
            (_dec(routine_lifestyle_baseline) + _dec(irregular_allowance_monthly)).quantize(
                Decimal("0.01")
            )
        )
        surplus_monthly = str(
            (_dec(income_monthly) - _dec(total_observed_monthly)).quantize(Decimal("0.01"))
        )

        # A composed read can legitimately have a currency in cost/income but
        # no role rows for it (for example while every owner-cost component is
        # withheld). That is unresolved reserve evidence, not a KeyError that
        # should take down forecast and controls.
        role_summary = roles.get("by_currency", {}).get(currency) if roles["available"] else None
        if role_summary is None or Decimal(role_summary["totals"]["unresolved"]) != 0:
            reserve = {
                "available": False,
                "reason": (
                    "essential role coverage is incomplete; unresolved owner-cost "
                    "contributions must be reviewed before a reserve amount is derived"
                ),
            }
        else:
            essential_monthly = _mean(
                [Decimal(row["essential"]) for row in role_summary["months"]], scale
            )
            reserve = {
                "available": True,
                "months": str(RESERVE_MONTHS),
                "essential_monthly": essential_monthly,
                "amount": str((Decimal(essential_monthly) * RESERVE_MONTHS).quantize(Decimal("0.01"))),
                "basis": "default three months of mean essential owner cost",
            }

        by_currency[currency] = {
            "months": month_decomp,
            "fixed_recurring_monthly": str(fixed_flat.quantize(Decimal("0.01"))),
            "variable_residual_monthly": variable_residual_monthly,
            "variable_residual_median": str(_median(residuals).quantize(Decimal("0.01"))),
            "variable_residual_min": str(min(residuals).quantize(Decimal("0.01"))) if residuals else "0",
            "variable_residual_max": str(max(residuals).quantize(Decimal("0.01"))) if residuals else "0",
            "irregular_allowance_monthly": irregular_allowance_monthly,
            "routine_lifestyle_baseline": routine_lifestyle_baseline,
            "total_observed_monthly": total_observed_monthly,
            "net_received_income_monthly": income_monthly,
            "historical_surplus_monthly": surplus_monthly,
            "savings_rate": _ratio(
                surplus_monthly, income_monthly,
                zero_reason="no classified income in the baseline window",
            ),
            "fixed_cost_ratio": _ratio(
                str(fixed_flat.quantize(Decimal("0.01"))), routine_lifestyle_baseline,
                zero_reason="routine lifestyle baseline is zero",
            ),
            "roles": role_summary,
            "role_groups": [group for group in roles.get("groups", []) if group["currency"] == currency],
            "reserve": reserve,
            "irregular_risk_entries": flagged,
        }

    limitations = [
        "confirmed recurring schedules are not matched per month; fixed_recurring_monthly "
        "is the flat sum of active commitments only",
        "automatic category defaults carry their source and review status; unmapped categories "
        "and unlinked receipt adjustments remain unresolved",
        "the upper-spend comparison statistics are observed dispersion, not a calibrated interval",
    ]
    if window["basis"] == "some-provisional":
        limitations.append(
            "the window rests on observed-only months; this is a limited-history estimate"
        )

    return {
        "available": True,
        "generation": token,
        "baseline_window": window,
        "currencies": table["currencies"],
        "sample": {
            "months": months,
            "month_count": len(months),
            "quality": window["quality"],
            "method_version": _METHOD_VERSION,
            "residual_method": "arithmetic-mean-of-monthly-routine-residual",
            "outlier_method": _OUTLIER_METHOD,
        },
        "by_currency": by_currency,
        "income_streams": table["income_streams"],
        "reconciliation": table["reconciliation"],
        "limitations": limitations,
        "tasks": table["tasks"],
    }


__all__ = ["baseline_estimate"]
