"""The four questions the app exists to answer, in order.

1. Are my statements in, and how far do they reach?
2. What did I spend on?  (the reported Analysis read answers this)
3. What do I owe?
4. How short will I be?  (the "Will I have enough" read answers this)

This module answers 1 and 3 in one fast read so the landing page can show
them at once. It adds no financial model of its own: statement reach comes
from the selected statements, and what is owed is the obligations page's own
remaining amounts and lanes, valued in the reporting currency at today's
date. Anything that cannot be valued, or has no known amount, is named and
counted separately — never folded in as zero.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from .db import connect_readonly
from .money import ExactMoney, format_decimal
from .obligations.queries import obligations_page_from
from .read_cache import request_cache
from .reporting_currency.models import CURRENCY_REGISTRY
from .reporting_currency.queries import reporting_policy
from .valuation import (
    POSITION_MONTHLY_CLOSED_V1,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    quantize,
    value_one,
)

# A statement is issued monthly; past this many days the account is behind.
STALE_AFTER_DAYS = 45

_OWED_GROUPS: tuple[tuple[str, str], ...] = (
    ("personal_debt", "Debts you owe"),
    ("card_statement", "Credit card bills"),
    ("installment_plan", "Instalment and pay-later plans"),
    ("recurring_commitment", "Regular commitments"),
)


def _statement_reach(connection: sqlite3.Connection, today: date) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT a.id, a.display_name, a.source_type,
               max(coalesce(s.period_end, s.statement_date)) AS reach,
               count(s.id) AS statements
        FROM accounts a JOIN statements s ON s.account_id = a.id AND s.selection_state = 'SELECTED'
        GROUP BY a.id ORDER BY reach, a.display_name
        """
    ).fetchall()
    accounts = []
    for row in rows:
        reach = str(row["reach"])[:10]
        days = (today - date.fromisoformat(reach)).days
        accounts.append({
            "account_id": str(row["id"]), "name": str(row["display_name"] or row["id"]),
            "statements": int(row["statements"]), "through": reach, "days_behind": days,
            "behind": days > STALE_AFTER_DAYS,
        })
    quarantined = connection.execute(
        "SELECT count(*) FROM statements WHERE selection_state = 'QUARANTINED'"
    ).fetchone()[0]
    return {
        "accounts": accounts,
        # The whole picture is only as recent as its oldest account.
        "complete_through": min((str(a["through"]) for a in accounts), default=None),
        "latest": max((str(a["through"]) for a in accounts), default=None),
        "behind_count": sum(1 for a in accounts if a["behind"]),
        "quarantined_count": int(quarantined),
        "stale_after_days": STALE_AFTER_DAYS,
    }


def _owed(connection: sqlite3.Connection, today: date, reporting: str, scale: int) -> dict[str, Any]:
    page = obligations_page_from(connection, today=today)
    context = ValuationContext(
        reporting_currency=reporting, purpose=ValuationPurpose.POSITION,
        valuation_as_of=today, rate_selection_policy=POSITION_MONTHLY_CLOSED_V1,
    )
    groups: dict[str, dict[str, Any]] = {
        origin: {"origin": origin, "label": label, "count": 0, "total": Fraction(0),
                 "native": {}, "unknown": [], "unconverted": [], "next_due": None}
        for origin, label in _OWED_GROUPS
    }
    owed_to_you: dict[str, Any] = {"count": 0, "total": Fraction(0), "unknown": 0, "unconverted": []}
    for item in page["obligations"]:
        if not item.get("stays_visible", True):
            continue
        amount = item.get("remaining_amount")
        if item["direction"] != "outflow":
            if item["origin"] != "personal_debt":
                continue
            owed_to_you["count"] += 1
            if amount is None:
                owed_to_you["unknown"] += 1
                continue
            valued = value_one(connection, ValuationItem(
                native=ExactMoney.parse(str(amount["value"])), native_currency=str(amount["currency"]),
                purpose=ValuationPurpose.POSITION), context=context)
            if valued.reported_exact is None:
                owed_to_you["unconverted"].append({"label": item["label"], **amount})
            else:
                owed_to_you["total"] += valued.reported_exact
            continue
        group = groups.get(item["origin"])
        if group is None:
            continue
        group["count"] += 1
        if amount is None:
            group["unknown"].append(item["label"])
            continue
        native = group["native"]
        parsed = ExactMoney.parse(str(amount["value"]))
        native[amount["currency"]] = native.get(amount["currency"], Fraction(0)) + Fraction(
            parsed.coefficient, 10**parsed.scale)
        valued = value_one(connection, ValuationItem(
            native=ExactMoney.parse(str(amount["value"])), native_currency=str(amount["currency"]),
            purpose=ValuationPurpose.POSITION), context=context)
        if valued.reported_exact is None:
            group["unconverted"].append({"label": item["label"], **amount})
        else:
            group["total"] += valued.reported_exact
        due = (item.get("date_basis") or {}).get("value")
        if isinstance(due, str) and due >= today.isoformat() and (
            group["next_due"] is None or due < group["next_due"]["on"]
        ):
            group["next_due"] = {"label": item["label"], "on": due}

    def text(value: Fraction) -> str:
        rounded = quantize(value, scale=scale)
        return format_decimal(rounded.coefficient, rounded.scale)

    rows = []
    for group in groups.values():
        rows.append({
            "origin": group["origin"], "label": group["label"], "count": group["count"],
            "total": text(group["total"]),
            "by_currency": {c: text(v) for c, v in sorted(group["native"].items())},
            "unknown_amount": group["unknown"], "unconverted": group["unconverted"],
            "next_due": group["next_due"],
        })
    return {
        "as_of": page["as_of"],
        "groups": rows,
        "total": text(sum((g["total"] for g in groups.values()), start=Fraction(0))),
        # Counted in neither total: no amount yet, or no rate to convert it.
        "unknown_amount_count": sum(len(g["unknown"]) for g in groups.values()),
        "unconverted_count": sum(len(g["unconverted"]) for g in groups.values()),
        "owed_to_you": {**owed_to_you, "total": text(owed_to_you["total"])},
        "policy": (
            "remaining amounts from Obligations: a card statement due and the "
            "instalments still to be billed to the card are separate lanes, so "
            "nothing is counted twice; each is valued at today's month-closed rate"
        ),
    }


def money_journey_from(connection: sqlite3.Connection, *, today: date) -> dict[str, Any]:
    with request_cache(connection):
        return _journey(connection, today=today)


def _journey(connection: sqlite3.Connection, *, today: date) -> dict[str, Any]:
    reporting = reporting_policy(connection).currency
    definition = CURRENCY_REGISTRY.get(reporting)
    scale = definition.minor_unit_scale if definition else 2
    return {
        "generated_for": today.isoformat(),
        "reporting_currency": reporting,
        "statements": _statement_reach(connection, today),
        "owed": _owed(connection, today, reporting, scale),
    }


def money_journey(database: Path, *, today: date) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return money_journey_from(connection, today=today)
    finally:
        connection.close()
