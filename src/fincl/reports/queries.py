"""Evaluate saved report definitions (v0.7 slice 2).

A report is evaluated on read against the current generation, from the same
recorded-flow rows the Trips page and Analysis use, so its figures reconcile
with both. Nothing about a result is stored.

Filters keep identity and never broaden: a category renamed by a taxonomy
migration follows its new name with a warning; a category, account or trip
that no longer exists is named in a warning and still filters (so a report
whose only trip was retired shows nothing, not everything).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..common import resolve_category
from ..context.queries import LENS_NOTE, _load
from ..db import connect_readonly
from ..errors import FinCLError
from .definitions import validate_definition

_GROUP_LABEL = {"category": "Category", "merchant": "Merchant", "month": "Month", "account": "Account",
                "trip": "Trip", "country": "Country", "currency": "Currency"}


def _text(value: Decimal, scale: int) -> str:
    return f"{value:.{max(scale, 2)}f}"


def _window(window: dict[str, Any], today: date) -> tuple[str | None, str | None]:
    if window["kind"] == "fixed":
        return str(window["start"]), str(window["end"])
    if window["kind"] == "recent_months":
        index = today.year * 12 + today.month - 1 - (int(window["months"]) - 1)
        return date(index // 12, index % 12 + 1, 1).isoformat(), today.isoformat()
    return None, None


def _definitions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {"report_id": str(row["report_id"]), "name": str(row["name"]), "reason": str(row["reason"]),
         **json.loads(row["definition_json"])}
        for row in connection.execute("SELECT * FROM report_definitions ORDER BY name, report_id")
    ]


def evaluate(
    connection: sqlite3.Connection, definition: dict[str, Any], *, today: date,
    custom_categories: frozenset[str] = frozenset(), expected: str | None = None,
) -> dict[str, Any]:
    """One report's result at the current generation."""
    state = _load(connection, expected)
    scope = definition["scope"]
    start, end = _window(scope["window"], today)
    warnings: list[str] = []

    categories: set[str] = set()
    for name in scope["categories"]:
        try:
            group, category, migrated = resolve_category(name, custom_categories)
        except FinCLError:
            warnings.append(f"Category “{name}” no longer exists, so nothing matches it.")
            continue
        current = f"{group} / {category}"
        if migrated is not None:
            warnings.append(f"Category “{name}” is now “{current}”; the report follows it.")
        categories.add(current)
    account_names = {str(r[0]): str(r[1] or r[0]) for r in connection.execute("SELECT id, display_name FROM accounts")}
    for account in scope["accounts"]:
        if account not in account_names:
            warnings.append(f"Account “{account}” no longer exists, so nothing matches it.")
    trips = state["trips"]
    for trip in scope["trips"]:
        if trip not in trips:
            warnings.append("A trip in this report has been removed, so nothing matches it.")

    def keep(entry: dict[str, Any]) -> bool:
        if start is not None and not (start <= entry["on"] and entry["end"] <= str(end)):
            return False
        checks = (("currencies", entry["currency"]), ("accounts", entry["account"]), ("trips", entry["trip"]),
                  ("countries", entry["country"]))
        if any(scope[name] and value not in scope[name] for name, value in checks):
            return False
        return not (scope["categories"] and entry["category_key"] not in categories)

    def key_of(entry: dict[str, Any], key: str) -> tuple[str | None, str]:
        if key == "month":
            return entry["on"][:7], entry["on"][:7]
        if key == "account":
            return entry["account"], account_names.get(entry["account"], entry["account"])
        if key == "trip":
            trip = entry["trip"]
            return trip, trips[trip]["name"] if trip in trips else "No trip"
        if key == "country":
            return entry["country"], entry["country"] or "Unknown"
        if key == "merchant":
            return entry["merchant"], entry["merchant"]
        if key == "category":
            return entry["category_key"], entry["category_key"]
        return entry["currency"], entry["currency"]

    group_by: list[str] = list(definition["group_by"])
    cells: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in state["entries"].values():
        if not keep(entry):
            continue
        keys = tuple(key_of(entry, key) for key in group_by)
        cell = cells.setdefault((*keys, entry["currency"]), {
            "group": [{"key": key, "value": value, "label": label} for key, (value, label) in zip(group_by, keys, strict=True)],
            "currency": entry["currency"], "gross": Decimal(0), "refunds": Decimal(0), "count": 0, "scale": 2,
            "parts": {},
        })
        cell["gross"] += entry["cost"]
        cell["refunds"] += entry["refund"]
        cell["count"] += 1
        cell["scale"] = max(cell["scale"], entry["scale"])
        for kind, value in (("cost", entry["cost"]), ("refund", entry["refund"])):
            if value:
                part = cell["parts"].setdefault((entry["end"], kind), [Decimal(0), 2])
                part[0] += value
                part[1] = max(part[1], entry["scale"])

    rows = [
        {
            "group": cell["group"], "currency": cell["currency"],
            "gross_cost": _text(cell["gross"], cell["scale"]), "refunds": _text(cell["refunds"], cell["scale"]),
            "net_cost": _text(cell["gross"] - cell["refunds"], cell["scale"]), "entry_count": cell["count"],
            # Daily parts behind each total, so each is valued at its own date.
            "cost_parts": [{"on": on, "currency": cell["currency"], "kind": kind, "amount": _text(value, scale)}
                           for (on, kind), (value, scale) in sorted(cell["parts"].items())],
            "_sort": cell["gross"], "_scale": cell["scale"],
        }
        for cell in cells.values()
    ]
    if group_by[:1] == ["month"]:
        rows.sort(key=lambda row: (row["currency"], [str(g["value"]) for g in row["group"]]))
    else:
        rows.sort(key=lambda row: (row["currency"], -row["_sort"], [str(g["value"] or "") for g in row["group"]]))
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        total = totals.setdefault(row["currency"], {"gross": Decimal(0), "refunds": Decimal(0), "count": 0, "scale": 2})
        total["gross"] += Decimal(row["gross_cost"])
        total["refunds"] += Decimal(row["refunds"])
        total["count"] += row["entry_count"]
        total["scale"] = max(total["scale"], row.pop("_scale"))
        del row["_sort"]
    return {
        "generation": state["token"], "evaluated_on": today.isoformat(), "lens": definition["lens"],
        "lens_note": LENS_NOTE,
        "report": {key: definition[key] for key in ("report_id", "name", "scope", "lens", "group_by", "columns")
                   if key in definition},
        "window": {"start": start, "end": end},
        # The latest date any recorded transaction covers: a window reaching
        # past it is not evidence that nothing was spent there.
        "data_through": max((entry["end"] for entry in state["entries"].values()), default=None),
        "group_labels": {key: _GROUP_LABEL[key] for key in group_by},
        "warnings": warnings,
        "rows": rows,
        "totals_by_currency": {
            currency: {"gross_cost": _text(t["gross"], t["scale"]), "refunds": _text(t["refunds"], t["scale"]),
                       "net_cost": _text(t["gross"] - t["refunds"], t["scale"]), "entry_count": t["count"]}
            for currency, t in sorted(totals.items())
        },
    }


def reports_page_from(connection: sqlite3.Connection) -> dict[str, Any]:
    return {"reports": _definitions(connection)}


def report_result_from(
    connection: sqlite3.Connection, report_id: str, *, today: date,
    custom_categories: frozenset[str] = frozenset(), expected: str | None = None,
) -> dict[str, Any]:
    definition = next((d for d in _definitions(connection) if d["report_id"] == report_id), None)
    if definition is None:
        raise FinCLError("that report does not exist")
    return evaluate(connection, definition, today=today, custom_categories=custom_categories, expected=expected)


def preview_result_from(
    connection: sqlite3.Connection, body: dict[str, Any], *, today: date,
    custom_categories: frozenset[str] = frozenset(), expected: str | None = None,
) -> dict[str, Any]:
    """An unsaved definition's result, so a report can be seen before it is kept."""
    validate_definition(body, with_reason=False)
    return evaluate(connection, body, today=today, custom_categories=custom_categories, expected=expected)


def _with(database: Path, fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return dict(fn(connection, *args, **kwargs))
    finally:
        connection.close()


def reports_page(database: Path) -> dict[str, Any]:
    return _with(database, reports_page_from)


def report_result(database: Path, report_id: str, *, today: date, expected: str | None = None) -> dict[str, Any]:
    from ..custom_categories import custom_categories
    return _with(database, report_result_from, report_id, today=today,
                 custom_categories=custom_categories(database), expected=expected)


def preview_result(database: Path, body: dict[str, Any], *, today: date, expected: str | None = None) -> dict[str, Any]:
    from ..custom_categories import custom_categories
    return _with(database, preview_result_from, body, today=today,
                 custom_categories=custom_categories(database), expected=expected)
