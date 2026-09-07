"""Full-state pre-append check for the ``PERSONAL_DEBT_*`` / residual kinds.

``validate_debt_payload`` covers shape; this module covers everything that needs
the current projection: the debt exists (or, for OPEN, does not), every source
anchor resolves, principal direction / currency is right, the debt chronology
still holds, and no bank component ends up over-claimed. It works by applying the
pending decision inside a SAVEPOINT and running the real projection pass, so the
preview can never diverge from replay.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import FinCLError
from .models import (
    ENTRY_RESIDUAL_SET,
    PERSONAL_DEBT_DETAILS_SET,
    PERSONAL_DEBT_EVENT_RETIRE,
    PERSONAL_DEBT_EVENT_SET,
    PERSONAL_DEBT_OPEN,
)
from .projection import apply_debt, resolve_debts
from .validation import validate_debt_payload

_PREVIEW_SEQ = 1 << 40


def preview_debt(
    connection: sqlite3.Connection,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    *,
    resolve_anchor: Any,
) -> None:
    """Raise :class:`FinCLError` if appending this decision would not fully replay."""
    validate_debt_payload(decision_kind, anchors, payload)

    _needs_debt = {
        PERSONAL_DEBT_DETAILS_SET,
        PERSONAL_DEBT_EVENT_SET,
        PERSONAL_DEBT_EVENT_RETIRE,
    }
    debt_exists = bool(
        "debt" in anchors
        and connection.execute(
            "SELECT 1 FROM personal_debts WHERE debt_id = ?", (anchors["debt"],)
        ).fetchone()
    )
    if decision_kind == PERSONAL_DEBT_OPEN and debt_exists:
        raise FinCLError("that debt id is already open")
    if decision_kind in _needs_debt and not debt_exists:
        raise FinCLError("that debt does not exist")

    if decision_kind == ENTRY_RESIDUAL_SET and resolve_anchor(anchors["entry"]) is None:
        raise FinCLError("the source transaction does not resolve to one selected entry")

    connection.execute("SAVEPOINT debt_preview")
    try:
        apply_debt(
            connection,
            decision_kind=decision_kind,
            anchors=anchors,
            payload=payload,
            journal_seq=_PREVIEW_SEQ,
            batch_index=None,
            resolve_anchor=resolve_anchor,
        )
        resolve_debts(connection)
    finally:
        connection.execute("ROLLBACK TO debt_preview")
        connection.execute("RELEASE debt_preview")
