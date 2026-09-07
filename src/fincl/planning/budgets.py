"""Dated budget revisions and month-boundary archive state (DESIGN-personal-finance §5).

`PLAN_SET` stays the undated legacy baseline. `BUDGET_REVISION_SET` records a
document effective from a given month; `BUDGET_ARCHIVE_SET` suppresses a budget's
cards from a month onward. Both project into disposable SQLite tables rebuilt by
journal replay; no mutable authority table.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..decision_journal import JournalRecord
from ..errors import FinCLError
from ..money import ExactMoney

BUDGET_REVISION_SET = "BUDGET_REVISION_SET"
BUDGET_ARCHIVE_SET = "BUDGET_ARCHIVE_SET"
BUDGET_DECISION_KINDS = frozenset({BUDGET_REVISION_SET, BUDGET_ARCHIVE_SET})

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_SAFE_NAME = re.compile(r"^[^\x00-\x1f]{1,80}$")


def _reason(payload: dict[str, Any]) -> None:
    value = payload.get("reason")
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise FinCLError("A reason of 1–2,000 characters is required")


def _currency(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise FinCLError("Choose a three-letter uppercase currency")
    return value


def _positive_money(value: Any, currency: str, *, allow_zero: bool = False) -> ExactMoney:
    if not isinstance(value, str):
        raise FinCLError("Amounts must be exact decimal strings")
    try:
        money = ExactMoney.parse(value)
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"{value!r} is not an exact amount") from exc
    if money.scale > 2:
        raise FinCLError("Amounts allow at most two decimal places")
    if money.coefficient < 0 or (not allow_zero and money.coefficient == 0):
        raise FinCLError("A positive amount is required")
    del currency  # currency equality across an entity's Money is a projection-time check
    return money


def validate_budget(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    """Structural validation at authoring and replay. Cross-record invariants
    (currency immutability, one active revision per month, legacy-lockout) are
    enforced when the projection is built."""
    _reason(payload)
    if kind == BUDGET_REVISION_SET:
        if set(anchors) != {"plan", "revision"} or not all(_ID.fullmatch(anchors[k]) for k in ("plan", "revision")):
            raise FinCLError("BUDGET_REVISION_SET anchors must be {plan, revision} identifiers")
        if set(payload) != {"effective_month", "name", "currency", "category_key", "limit", "reason"}:
            raise FinCLError("BUDGET_REVISION_SET payload fields must be exactly "
                             "{effective_month, name, currency, category_key, limit, reason}")
        if not _MONTH.fullmatch(str(payload["effective_month"])):
            raise FinCLError("effective_month must be YYYY-MM")
        if not _SAFE_NAME.fullmatch(str(payload["name"]).strip()) or not str(payload["name"]).strip():
            raise FinCLError("Budget name must contain 1–80 characters without control characters")
        currency = _currency(payload["currency"])
        if payload["category_key"] is not None and not isinstance(payload["category_key"], str):
            raise FinCLError("category_key must be a category string or null")
        _positive_money(payload["limit"], currency)
    elif kind == BUDGET_ARCHIVE_SET:
        if set(anchors) != {"plan"} or not _ID.fullmatch(anchors["plan"]):
            raise FinCLError("BUDGET_ARCHIVE_SET anchors must be {plan}")
        if set(payload) != {"effective_month", "archived", "reason"}:
            raise FinCLError("BUDGET_ARCHIVE_SET payload fields must be exactly "
                             "{effective_month, archived, reason}")
        if not _MONTH.fullmatch(str(payload["effective_month"])):
            raise FinCLError("effective_month must be YYYY-MM")
        if not isinstance(payload["archived"], bool):
            raise FinCLError("archived must be a boolean")
    else:  # pragma: no cover - guarded by the caller
        raise FinCLError(f"{kind} is not a budget decision kind")


def apply_budget(
    connection: sqlite3.Connection,
    record: JournalRecord,
    *,
    batch_index: int | None = None,
) -> None:
    payload = record.payload
    if record.decision_kind == BUDGET_REVISION_SET:
        plan, revision = record.anchors["plan"], record.anchors["revision"]
        existing = connection.execute(
            "SELECT currency FROM budget_revisions WHERE plan = ? LIMIT 1", (plan,)
        ).fetchone()
        if existing is not None and existing["currency"] != payload["currency"]:
            raise FinCLError(
                f"decision journal record {record.seq}: budget {plan} currency is "
                f"{existing['currency']}; a different currency needs a new budget"
            )
        clash = connection.execute(
            "SELECT revision FROM budget_revisions "
            "WHERE plan = ? AND effective_month = ? AND revision != ?",
            (plan, payload["effective_month"], revision),
        ).fetchone()
        if clash is not None:
            raise FinCLError(
                f"decision journal record {record.seq}: budget {plan} already has a "
                f"revision for {payload['effective_month']}; supersede it by its id"
            )
        limit = ExactMoney.parse(str(payload["limit"]))
        connection.execute(
            "INSERT OR REPLACE INTO budget_revisions(plan, revision, effective_month, "
            "name, currency, category_key, limit_coefficient, limit_scale, journal_seq, "
            "batch_index, bound, dangling_reason) VALUES (?,?,?,?,?,?,?,?,?,?,1,NULL)",
            (
                plan, revision, payload["effective_month"], str(payload["name"]).strip(),
                payload["currency"], payload["category_key"],
                limit.coefficient, limit.scale, record.seq, batch_index,
            ),
        )
    elif record.decision_kind == BUDGET_ARCHIVE_SET:
        connection.execute(
            "INSERT OR REPLACE INTO budget_archive_periods(plan, effective_month, "
            "archived, journal_seq) VALUES (?,?,?,?)",
            (
                record.anchors["plan"], payload["effective_month"],
                1 if payload["archived"] else 0, record.seq,
            ),
        )
