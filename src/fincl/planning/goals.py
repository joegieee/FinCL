"""Savings goals: latest recorded details plus absolute dated observations
(DESIGN-personal-finance §6).

Goals are tracking records, not cash accounts. Observations are absolute saved
amounts, never deltas. `PLAN_SET` stays the undated legacy baseline. Everything
projects into disposable SQLite tables rebuilt by journal replay.

Design note: §6 lists the `GOAL_DETAILS_SET` payload without a `currency` field
while also requiring an immutable goal currency. This module resolves that gap
the same way §5 does for budgets — `currency` is an explicit payload field on
`GOAL_DETAILS_SET`, and every observation's amount currency must equal it.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import Any

from ..decision_journal import JournalRecord
from ..errors import FinCLError
from ..money import ExactMoney

GOAL_DETAILS_SET = "GOAL_DETAILS_SET"
GOAL_OBSERVATION_SET = "GOAL_OBSERVATION_SET"
GOAL_OBSERVATION_RETIRE = "GOAL_OBSERVATION_RETIRE"
GOAL_ARCHIVE_SET = "GOAL_ARCHIVE_SET"
GOAL_DECISION_KINDS = frozenset(
    {GOAL_DETAILS_SET, GOAL_OBSERVATION_SET, GOAL_OBSERVATION_RETIRE, GOAL_ARCHIVE_SET}
)

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_NAME = re.compile(r"^[^\x00-\x1f]{1,80}$")


def _reason(payload: dict[str, Any]) -> None:
    value = payload.get("reason")
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise FinCLError("A reason of 1–2,000 characters is required")


def _currency(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise FinCLError("Choose a three-letter uppercase currency")
    return value


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise FinCLError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError(f"{field} is not a real date") from exc


def _money(value: Any, *, allow_zero: bool) -> ExactMoney:
    if not isinstance(value, str):
        raise FinCLError("Amounts must be exact decimal strings")
    try:
        money = ExactMoney.parse(value)
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"{value!r} is not an exact amount") from exc
    if money.scale > 2:
        raise FinCLError("Amounts allow at most two decimal places")
    if money.coefficient < 0 or (not allow_zero and money.coefficient == 0):
        raise FinCLError("A positive amount is required" if not allow_zero else "A non-negative amount is required")
    return money


def validate_goal(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    _reason(payload)
    if kind == GOAL_DETAILS_SET:
        if set(anchors) != {"plan"} or not _ID.fullmatch(anchors["plan"]):
            raise FinCLError("GOAL_DETAILS_SET anchors must be {plan}")
        if set(payload) != {"name", "currency", "target", "target_on", "reason"}:
            raise FinCLError("GOAL_DETAILS_SET payload fields must be exactly "
                             "{name, currency, target, target_on, reason}")
        if not str(payload["name"]).strip() or not _SAFE_NAME.fullmatch(str(payload["name"]).strip()):
            raise FinCLError("Goal name must contain 1–80 characters without control characters")
        _currency(payload["currency"])
        _money(payload["target"], allow_zero=False)
        if payload["target_on"] is not None:
            _iso_date(payload["target_on"], "target_on")
    elif kind == GOAL_OBSERVATION_SET:
        if set(anchors) != {"plan", "observation"} or not all(_ID.fullmatch(anchors[k]) for k in ("plan", "observation")):
            raise FinCLError("GOAL_OBSERVATION_SET anchors must be {plan, observation} identifiers")
        if set(payload) != {"observed_on", "saved", "reason"}:
            raise FinCLError("GOAL_OBSERVATION_SET payload fields must be exactly {observed_on, saved, reason}")
        observed = _iso_date(payload["observed_on"], "observed_on")
        if observed > date.today():
            raise FinCLError("observed_on cannot be in the future")
        _money(payload["saved"], allow_zero=True)
    elif kind == GOAL_OBSERVATION_RETIRE:
        if set(anchors) != {"plan", "observation"} or not all(_ID.fullmatch(anchors[k]) for k in ("plan", "observation")):
            raise FinCLError("GOAL_OBSERVATION_RETIRE anchors must be {plan, observation} identifiers")
        if set(payload) != {"reason"}:
            raise FinCLError("GOAL_OBSERVATION_RETIRE payload must be exactly {reason}")
    elif kind == GOAL_ARCHIVE_SET:
        if set(anchors) != {"plan"} or not _ID.fullmatch(anchors["plan"]):
            raise FinCLError("GOAL_ARCHIVE_SET anchors must be {plan}")
        if set(payload) != {"archived", "reason"}:
            raise FinCLError("GOAL_ARCHIVE_SET payload fields must be exactly {archived, reason}")
        if not isinstance(payload["archived"], bool):
            raise FinCLError("archived must be a boolean")
    else:  # pragma: no cover - guarded by the caller
        raise FinCLError(f"{kind} is not a goal decision kind")


def apply_goal(connection: sqlite3.Connection, record: JournalRecord) -> None:
    payload, plan = record.payload, record.anchors["plan"]
    if record.decision_kind == GOAL_DETAILS_SET:
        existing = connection.execute(
            "SELECT currency FROM goal_details WHERE plan = ?", (plan,)
        ).fetchone()
        if existing is not None and existing["currency"] != payload["currency"]:
            raise FinCLError(
                f"decision journal record {record.seq}: goal {plan} currency is "
                f"{existing['currency']} and cannot change"
            )
        target = ExactMoney.parse(str(payload["target"]))
        connection.execute(
            "INSERT OR REPLACE INTO goal_details(plan, name, target_coefficient, "
            "target_scale, currency, target_on, journal_seq) VALUES (?,?,?,?,?,?,?)",
            (plan, str(payload["name"]).strip(), target.coefficient, target.scale,
             payload["currency"], payload["target_on"], record.seq),
        )
    elif record.decision_kind == GOAL_OBSERVATION_SET:
        if connection.execute("SELECT 1 FROM goal_details WHERE plan = ?", (plan,)).fetchone() is None:
            raise FinCLError(f"decision journal record {record.seq}: observation references unknown goal {plan}")
        saved = ExactMoney.parse(str(payload["saved"]))
        connection.execute(
            "INSERT OR REPLACE INTO goal_observations(plan, observation, observed_on, "
            "saved_coefficient, saved_scale, journal_seq, bound, dangling_reason) "
            "VALUES (?,?,?,?,?,?,1,NULL)",
            (plan, record.anchors["observation"], payload["observed_on"],
             saved.coefficient, saved.scale, record.seq),
        )
    elif record.decision_kind == GOAL_OBSERVATION_RETIRE:
        if connection.execute("SELECT 1 FROM goal_observations WHERE plan=? AND observation=?",
                              (plan, record.anchors["observation"])).fetchone() is None:
            raise FinCLError(f"decision journal record {record.seq}: observation to retire was not found")
        connection.execute(
            "DELETE FROM goal_observations WHERE plan = ? AND observation = ?",
            (plan, record.anchors["observation"]),
        )
    elif record.decision_kind == GOAL_ARCHIVE_SET:
        if connection.execute("SELECT 1 FROM goal_details WHERE plan = ?", (plan,)).fetchone() is None:
            raise FinCLError(f"decision journal record {record.seq}: archive references unknown goal {plan}")
        connection.execute(
            "INSERT OR REPLACE INTO goal_archive(plan, archived, journal_seq) VALUES (?,?,?)",
            (plan, 1 if payload["archived"] else 0, record.seq),
        )
