"""v0.6 planning domain — dated budgets, savings goals and recurring commitments.

Journal kinds live in per-concern modules; this package assembles the closed
``PLANNING_DECISION_KINDS`` set and the single ``apply_planning`` dispatch that
``decisions._apply_record`` calls.
"""

from __future__ import annotations

import sqlite3

from ..decision_journal import JournalRecord
from ..errors import FinCLError
from .budgets import BUDGET_DECISION_KINDS, apply_budget, validate_budget
from .goals import GOAL_DECISION_KINDS, apply_goal, validate_goal
from .recurring import RECURRING_DECISION_KINDS, apply_recurring, validate_recurring

PLANNING_DECISION_KINDS = (
    BUDGET_DECISION_KINDS | GOAL_DECISION_KINDS | RECURRING_DECISION_KINDS
)


_DATED_PLAN_TABLES = (
    "budget_revisions", "budget_archive_periods",
    "goal_details", "goal_observations", "goal_archive",
)


def plan_has_dated_records(connection: sqlite3.Connection, plan_id: str) -> bool:
    """True once any v0.6 dated budget / goal decision has been projected for
    this plan id. A legacy ``PLAN_SET`` for the same entity is then refused —
    old ``PLAN_SET`` records already in the journal still replay
    (DESIGN-personal-finance §§5–6)."""
    for table in _DATED_PLAN_TABLES:
        if connection.execute(f"SELECT 1 FROM {table} WHERE plan = ? LIMIT 1", (plan_id,)).fetchone():
            return True
    return False


def validate_planning(kind: str, anchors: dict[str, str], payload: dict[str, object]) -> None:
    """Structural validation for the command API (authoring boundary). Cross-record
    invariants — currency immutability, one revision per month, unknown-commitment —
    are enforced by ``apply_*`` at replay."""
    if kind in BUDGET_DECISION_KINDS:
        validate_budget(kind, anchors, payload)
    elif kind in GOAL_DECISION_KINDS:
        validate_goal(kind, anchors, payload)
    elif kind in RECURRING_DECISION_KINDS:
        validate_recurring(kind, anchors, payload)
    else:  # pragma: no cover - guarded by the caller's kind check
        raise FinCLError(f"{kind} is not a planning decision kind")


def apply_planning(
    connection: sqlite3.Connection,
    record: JournalRecord,
    *,
    batch_index: int | None = None,
) -> None:
    """Validate and project one planning decision. Fails closed on a malformed
    record so replay never silently drops it."""
    try:
        if record.decision_kind in BUDGET_DECISION_KINDS:
            validate_budget(record.decision_kind, record.anchors, record.payload)
            apply_budget(connection, record, batch_index=batch_index)
            return
        if record.decision_kind in GOAL_DECISION_KINDS:
            validate_goal(record.decision_kind, record.anchors, record.payload)
            apply_goal(connection, record)
            return
        if record.decision_kind in RECURRING_DECISION_KINDS:
            validate_recurring(record.decision_kind, record.anchors, record.payload)
            apply_recurring(connection, record)
            return
    except FinCLError as exc:
        raise FinCLError(f"decision journal record {record.seq}: {exc}") from exc
    raise FinCLError(f"{record.decision_kind} is not a planning decision kind")
