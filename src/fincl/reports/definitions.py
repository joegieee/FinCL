"""Saved report definitions (v0.7 slice 2).

``REPORT_DEFINITION_SET {report} -> {name, scope, lens, group_by, columns, reason}``
replaces a report's definition; ``REPORT_DEFINITION_RETIRE {report} -> {reason}``
removes it. A definition is a configuration, never a stored result: reopening a
report evaluates it against the current generation and says on which date.

Every field is a closed enum or a bounded list. Saved reports take no free-text
search: a typed filter keeps identity (a category key, an account id, a trip
id, a country code), so it survives a label change, and a filter value that no
longer exists is reported as a warning rather than silently dropped.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date
from typing import Any

from ..context.countries import COUNTRY_CODES
from ..errors import FinCLError

REPORT_DEFINITION_SET = "REPORT_DEFINITION_SET"
REPORT_DEFINITION_RETIRE = "REPORT_DEFINITION_RETIRE"
REPORT_DECISION_KINDS = frozenset({REPORT_DEFINITION_SET, REPORT_DEFINITION_RETIRE})

LENSES = ("recorded_flow",)
GROUP_KEYS = ("category", "merchant", "month", "account", "trip", "country", "currency")
COLUMNS = ("gross_cost", "refunds", "net_cost", "entry_count")
WINDOW_KINDS = ("all", "fixed", "recent_months")
MAX_NAME = 80
MAX_GROUP_BY = 2
MAX_RECENT_MONTHS = 36
# Bounded so a definition stays a small, reviewable configuration.
FILTER_LIMITS = {"currencies": 20, "accounts": 50, "categories": 100, "trips": 50, "countries": 50}
_CURRENCY = re.compile(r"[A-Z]{3}")


def _reason(payload: dict[str, Any]) -> None:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise FinCLError("A reason of up to 2,000 characters is required")


def _report_id(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise FinCLError("A report is identified by a lowercase UUID") from exc
    return value


def _day(value: Any, label: str) -> date:
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise FinCLError(f"{label} must be a calendar date (YYYY-MM-DD)")
    return parsed


def _window(window: Any) -> None:
    if not isinstance(window, dict) or window.get("kind") not in WINDOW_KINDS:
        raise FinCLError(f"The window kind is one of {', '.join(WINDOW_KINDS)}")
    kind = window["kind"]
    if kind == "all" and set(window) != {"kind"}:
        raise FinCLError("An all-time window takes no other fields")
    if kind == "fixed":
        if set(window) != {"kind", "start", "end"}:
            raise FinCLError("A fixed window is {kind, start, end}")
        if _day(window["start"], "start") > _day(window["end"], "end"):
            raise FinCLError("A window cannot end before it starts")
    if kind == "recent_months":
        months = window.get("months")
        if set(window) != {"kind", "months"} or not isinstance(months, int) or isinstance(months, bool) \
                or not 1 <= months <= MAX_RECENT_MONTHS:
            raise FinCLError(f"A recent window covers 1-{MAX_RECENT_MONTHS} months")


def _filter(name: str, values: Any) -> list[str]:
    limit = FILTER_LIMITS[name]
    if not isinstance(values, list) or len(values) > limit or not all(isinstance(v, str) and v for v in values):
        raise FinCLError(f"{name} is a list of at most {limit} values")
    if len(set(values)) != len(values):
        raise FinCLError(f"Each value in {name} is listed once")
    for value in values:
        if name == "currencies" and not _CURRENCY.fullmatch(value):
            raise FinCLError(f"{value!r} is not a three-letter currency code")
        if name == "countries" and value not in COUNTRY_CODES:
            raise FinCLError(f"{value!r} is not a supported two-letter country code")
        if name == "trips":
            try:
                if str(uuid.UUID(value)) != value:
                    raise ValueError
            except ValueError as exc:
                raise FinCLError("A trip filter names trips by id") from exc
        if len(value) > 200:
            raise FinCLError(f"A value in {name} is too long")
    return values


def validate_report(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    """Shape checks shared by preview, commands and replay."""
    if kind not in REPORT_DECISION_KINDS:
        raise FinCLError(f"{kind} is not a report decision")
    if set(anchors) != {"report"}:
        raise FinCLError("This decision is anchored to one report")
    _report_id(anchors["report"])
    if kind == REPORT_DEFINITION_RETIRE:
        if set(payload) != {"reason"}:
            raise FinCLError("Retiring a report needs only a reason")
        _reason(payload)
        return
    validate_definition(payload)


def validate_definition(payload: dict[str, Any], *, with_reason: bool = True) -> None:
    """The definition body itself; a preview of an unsaved report omits the reason."""
    keys = {"name", "scope", "lens", "group_by", "columns"} | ({"reason"} if with_reason else set())
    if set(payload) != keys:
        raise FinCLError("A report is {" + ", ".join(sorted(keys)) + "}")
    name = payload["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
        raise FinCLError(f"A report name is 1-{MAX_NAME} characters")
    scope = payload["scope"]
    if not isinstance(scope, dict) or set(scope) != {"window", *FILTER_LIMITS}:
        raise FinCLError("A report scope is {window, " + ", ".join(FILTER_LIMITS) + "}")
    _window(scope["window"])
    for filter_name in FILTER_LIMITS:
        _filter(filter_name, scope[filter_name])
    if payload["lens"] not in LENSES:
        raise FinCLError(f"The lens is one of {', '.join(LENSES)}")
    group_by = payload["group_by"]
    if not isinstance(group_by, list) or len(group_by) > MAX_GROUP_BY \
            or any(key not in GROUP_KEYS for key in group_by) or len(set(group_by)) != len(group_by):
        raise FinCLError(f"Group by up to {MAX_GROUP_BY} of {', '.join(GROUP_KEYS)}, each once")
    columns = payload["columns"]
    if not isinstance(columns, list) or not columns or any(column not in COLUMNS for column in columns) \
            or len(set(columns)) != len(columns):
        raise FinCLError(f"Choose at least one column from {', '.join(COLUMNS)}, each once")
    if with_reason:
        _reason(payload)


def check_report(connection: sqlite3.Connection, kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    validate_report(kind, anchors, payload)
    if kind == REPORT_DEFINITION_RETIRE and not connection.execute(
        "SELECT 1 FROM report_definitions WHERE report_id=?", (anchors["report"],)
    ).fetchone():
        raise FinCLError("That report does not exist")


def apply_report(
    connection: sqlite3.Connection, *, kind: str, anchors: dict[str, str], payload: dict[str, Any], journal_seq: int,
) -> None:
    validate_report(kind, anchors, payload)
    if kind == REPORT_DEFINITION_RETIRE:
        connection.execute("DELETE FROM report_definitions WHERE report_id=?", (anchors["report"],))
        return
    definition = {key: payload[key] for key in ("scope", "lens", "group_by", "columns")}
    connection.execute(
        "INSERT OR REPLACE INTO report_definitions(report_id,name,definition_json,reason,journal_seq) VALUES(?,?,?,?,?)",
        (anchors["report"], payload["name"].strip(), json.dumps(definition, sort_keys=True), payload["reason"], journal_seq),
    )
