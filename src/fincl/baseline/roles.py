"""Deterministic Stage 1 financial roles derived from existing evidence.

This is deliberately a read model, not a second categorisation system.  Built-in
categories receive a versioned, reviewable default; a bound recurring occurrence
is the only source that establishes ``fixed_recurring``.  Categories outside the
built-in map, unclassified spending, and receipt adjustments without a purchase
link remain unresolved rather than being silently treated as discretionary.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..generation import current_generation
from ..intervals import month_span
from ..money import ExactMoney, exact_sum, format_decimal
from .queries import _month_list, cost_income_table

ROLE_RULE_VERSION = "baseline-roles-2026-v1"
RESERVE_MONTHS = Decimal("3")
_OUTLIER_MULTIPLE = Decimal("3")
_OUTLIER_FLOOR = Decimal("150")

# The map is intentionally category-only: it is reproducible from the existing
# taxonomy, does not infer a need from a merchant name, and leaves custom /
# unclassified categories unresolved for review.
_DEFAULTS: dict[str, tuple[str, int]] = {
    "Food & Dining / Groceries": ("essential", 1),
    "Food & Dining / Hawker & Canteen": ("essential", 1),
    "Food & Dining / Dining Out": ("discretionary", 4),
    "Food & Dining / Cafes & Drinks": ("discretionary", 4),
    "Transport / Public Transit": ("essential", 1),
    "Transport / Fuel": ("essential", 2),
    "Transport / Parking & Tolls": ("essential", 2),
    "Transport / Taxi & Ride-hail": ("discretionary", 4),
    "Housing / Rent": ("essential", 1),
    "Housing / Utilities": ("essential", 1),
    "Housing / Internet & Mobile": ("essential", 1),
    "Housing / Home Supplies & Maintenance": ("essential", 2),
    "Shopping / Clothing & Accessories": ("discretionary", 3),
    "Shopping / Electronics": ("discretionary", 3),
    "Shopping / Books & Stationery": ("discretionary", 3),
    "Shopping / General Shopping": ("discretionary", 3),
    "Health / Medical & Dental": ("essential", 1),
    "Health / Pharmacy": ("essential", 1),
    "Health / Fitness": ("discretionary", 4),
    "Personal & Household / Personal Care": ("essential", 2),
    "Personal & Household / Laundry": ("essential", 2),
    "Personal & Household / Household Services": ("essential", 2),
    "Education / Tuition & Exam Fees": ("essential", 2),
    "Education / Course Materials": ("essential", 2),
    "Education / Education Services": ("essential", 2),
    "Leisure / Entertainment": ("discretionary", 4),
    "Leisure / Hobbies & Sports": ("discretionary", 4),
    "Travel / Transport": ("discretionary", 4),
    "Travel / Accommodation": ("discretionary", 4),
    "Travel / Activities": ("discretionary", 4),
    "Travel / Travel Fees": ("discretionary", 4),
    "Financial & Government / Bank & Card Fees": ("essential", 2),
    "Financial & Government / Interest & Financing": ("essential", 2),
    "Financial & Government / Taxes & Government": ("essential", 1),
    "Giving / Charity": ("discretionary", 4),
    "Giving / Gifts Given": ("discretionary", 4),
}


def _sum(values: list[ExactMoney]) -> str:
    coefficient, scale = exact_sum(*values) if values else (0, 0)
    return format_decimal(coefficient, scale)


def _role(category: str, *, recurring: bool) -> dict[str, Any]:
    default = _DEFAULTS.get(category)
    if default is None:
        return {
            "cost_pattern": "unresolved", "essential_status": "unresolved", "tier": None,
            "basis": "unmapped or unclassified category", "review_status": "unresolved",
        }
    status, tier = default
    return {
        "cost_pattern": "fixed_recurring" if recurring else "variable_routine",
        "essential_status": status,
        "tier": tier,
        "basis": "confirmed recurring occurrence" if recurring else ROLE_RULE_VERSION,
        "review_status": "confirmed" if recurring else "automatic",
    }


def baseline_roles(database: Path, *, today: date | None = None) -> dict[str, Any]:
    """Return role totals and source-backed groups for the selected baseline window."""
    table = cost_income_table(database, today=today)
    if not table["available"]:
        return {"available": False, "reason": table["reason"], "baseline_window": table["baseline_window"], "tasks": table["tasks"]}

    months = _month_list(table["baseline_window"]["start_month"], table["baseline_window"]["end_month"])
    connection = connect_readonly(database)
    try:
        generation = current_generation(connection)
        token = generation["generation_token"] if generation else ""
        rows = connection.execute(
            """SELECT component_id, period, currency, origin_anchor, category_key, bucket,
                      signed_coefficient, scale
                 FROM analysis_components
                 WHERE generation_token=? AND lens='recorded_flow'
                   AND represented_start>=? AND represented_end<=?
                   AND bucket IN ('spending', 'fees', 'refunds', 'reimbursements')""",
            (token, month_span(months[0]).start, month_span(months[-1]).end),
        ).fetchall()
        recurring = {
            row["entry_anchor"] for row in connection.execute(
                """SELECT o.entry_anchor FROM recurring_occurrences o
                   JOIN recurring_commitments c ON c.commitment=o.commitment
                   WHERE o.bound=1 AND o.entry_anchor IS NOT NULL AND c.status='active'"""
            )
        }
        purchase_categories = {
            row["purchase_anchor"]: row["category_key"]
            for row in connection.execute(
                """SELECT DISTINCT ra.purchase_anchor, ac.category_key
                   FROM refund_allocations ra JOIN analysis_components ac
                     ON ac.generation_token=? AND ac.origin_anchor=ra.purchase_anchor
                    AND ac.lens='recorded_flow' AND ac.bucket IN ('spending', 'fees')""",
                (token,),
            )
        }
        refund_links = defaultdict(list)
        for row in connection.execute("SELECT refund_anchor, purchase_anchor FROM refund_allocations"):
            refund_links[row["refund_anchor"]].append(row["purchase_anchor"])
    finally:
        connection.close()

    # The estimator's existing outlier rule is also a source-backed role
    # suggestion.  It is deliberately review-required, not a confirmed one-off.
    category_month: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    spending_rows = [row for row in rows if row["bucket"] in {"spending", "fees"}]
    for row in spending_rows:
        category_month[(row["currency"], row["category_key"], row["period"])] += abs(
            Decimal(row["signed_coefficient"]).scaleb(-int(row["scale"]))
        )
    irregular_components: set[str] = set()
    for row in spending_rows:
        monthly_totals = [
            category_month[(row["currency"], row["category_key"], month)]
            for month in months
        ]
        ordered = sorted(monthly_totals)
        middle = len(ordered) // 2
        median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        threshold = max(_OUTLIER_FLOOR, median * _OUTLIER_MULTIPLE)
        entry_amount = abs(Decimal(row["signed_coefficient"]).scaleb(-int(row["scale"])))
        if entry_amount >= threshold and entry_amount > 0:
            irregular_components.add(row["component_id"])

    # (month, currency, status) exact signed owner-cost contributions.
    totals: dict[tuple[str, str, str], list[ExactMoney]] = defaultdict(list)
    groups: dict[tuple[str, str, str, str, str | None, str, str], list[ExactMoney]] = defaultdict(list)
    for row in rows:
        month, currency, bucket = row["period"], row["currency"], row["bucket"]
        if month not in months:
            continue
        amount = ExactMoney(abs(int(row["signed_coefficient"])), int(row["scale"]), "")
        category = row["category_key"]
        recurring_source = row["origin_anchor"] in recurring
        if bucket == "refunds":
            links = refund_links.get(row["origin_anchor"], [])
            # A refund can have several allocations.  The component remains
            # unresolved if the relation cannot name exactly one purchase role.
            linked_categories = {purchase_categories.get(anchor) for anchor in links}
            if len(linked_categories) == 1 and None not in linked_categories:
                category = next(iter(linked_categories))
            else:
                category = "Unclassified / Unclassified"
            amount = ExactMoney(-amount.coefficient, amount.scale, "")
            recurring_source = False
        elif bucket == "reimbursements":
            category = "Unclassified / Unclassified"
            amount = ExactMoney(-amount.coefficient, amount.scale, "")
            recurring_source = False
        role = _role(category, recurring=recurring_source)
        if row["component_id"] in irregular_components and not recurring_source:
            role = {
                **role,
                "cost_pattern": "irregular",
                "basis": "outlier-v1 source-backed review",
                "review_status": "review_required",
            }
        status = role["essential_status"]
        totals[(month, currency, status)].append(amount)
        key = (currency, category, role["cost_pattern"], status, role["tier"], role["basis"], role["review_status"])
        groups[key].append(amount)

    by_currency: dict[str, Any] = {}
    for currency in table["currencies"]:
        monthly: list[dict[str, Any]] = []
        essential_values: list[ExactMoney] = []
        discretionary_values: list[ExactMoney] = []
        unresolved_values: list[ExactMoney] = []
        for month in months:
            values = {status: _sum(totals[(month, currency, status)]) for status in ("essential", "discretionary", "unresolved")}
            expected = table["months"][months.index(month)]["by_currency"][currency]["owner_cost_recorded"]
            total = _sum([ExactMoney.parse(value) for value in values.values()])
            monthly.append({"month": month, **values, "total": total, "reconciles": Decimal(total) == Decimal(expected)})
            essential_values.append(ExactMoney.parse(values["essential"]))
            discretionary_values.append(ExactMoney.parse(values["discretionary"]))
            unresolved_values.append(ExactMoney.parse(values["unresolved"]))
        total_values = {status: _sum([ExactMoney.parse(item[status]) for item in monthly]) for status in ("essential", "discretionary", "unresolved")}
        total = _sum([ExactMoney.parse(value) for value in total_values.values()])
        by_currency[currency] = {
            "months": monthly,
            "totals": {**total_values, "total": total,
                       "reconciles": total == table["reconciliation"][currency]["owner_cost_recorded_total"]},
        }

    role_groups = [
        {"currency": key[0], "category_key": key[1], "cost_pattern": key[2],
         "essential_status": key[3], "tier": key[4], "basis": key[5],
         "review_status": key[6], "amount": _sum(values), "source_count": len(values)}
        for key, values in sorted(groups.items())
    ]
    return {"available": True, "generation": token, "baseline_window": table["baseline_window"],
            "currencies": table["currencies"], "by_currency": by_currency, "groups": role_groups,
            "rule_version": ROLE_RULE_VERSION, "tasks": table["tasks"]}


__all__ = ["ROLE_RULE_VERSION", "RESERVE_MONTHS", "baseline_roles"]
