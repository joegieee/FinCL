"""v0.6 planning domain — dated budgets, savings goals and recurring commitments.

Journal kinds live in per-concern modules; this package assembles the closed
``PLANNING_DECISION_KINDS`` set and the single ``apply_planning`` dispatch that
``decisions._apply_record`` calls. Payloads are owned by
``DESIGN-personal-finance.md`` §§5–7.
"""

from __future__ import annotations

import sqlite3

from ..decision_journal import JournalRecord
from ..errors import FinCLError
from .budgets import BUDGET_DECISION_KINDS, apply_budget, validate_budget
from .goals import GOAL_DECISION_KINDS, apply_goal, validate_goal

PLANNING_DECISION_KINDS = BUDGET_DECISION_KINDS | GOAL_DECISION_KINDS
# Extended by the recurring slice: | RECURRING_DECISION_KINDS


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
    except FinCLError as exc:
        raise FinCLError(f"decision journal record {record.seq}: {exc}") from exc
    raise FinCLError(f"{record.decision_kind} is not a planning decision kind")
