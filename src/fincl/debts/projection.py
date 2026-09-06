"""Replay the ``PERSONAL_DEBT_*`` / ``ENTRY_RESIDUAL_*`` kinds into schema 13.

``apply_debt`` projects one record; ``resolve_debts`` runs once after the record
loop to rebuild ``personal_debt_allocations`` from the event payloads, fold
``entry_residuals``, and revalidate each debt's full chronology (day-end
non-negative principal, event dates within the debt's open window).

Slice 1 accepts only identity, details, and explicit opening-balance events, so
the allocation rebuild is a no-op here; it becomes load-bearing in slice 2 when
source-linked events land. The as-of balance is always computed from the event
rows — no mutable authoritative balance is stored.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    ENTRY_RESIDUAL_RETIRE,
    ENTRY_RESIDUAL_SET,
    INBOUND_CLASSIFICATION_RETIRE,
    PERSONAL_DEBT_CLOSE,
    PERSONAL_DEBT_DETAILS_SET,
    PERSONAL_DEBT_EVENT_RETIRE,
    PERSONAL_DEBT_EVENT_SET,
    PERSONAL_DEBT_OPEN,
    PERSONAL_DEBT_REOPEN,
    events_for_direction,
)


def _debt_row(connection: sqlite3.Connection, debt_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM personal_debts WHERE debt_id = ?", (debt_id,)
    ).fetchone()
    if row is None:
        raise FinCLError(f"debt {debt_id} was referenced before it was opened")
    return row


def apply_debt(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
    batch_index: int | None,
    resolve_anchor: Any,
) -> None:
    if decision_kind == PERSONAL_DEBT_OPEN:
        debt_id = anchors["debt"]
        if connection.execute(
            "SELECT 1 FROM personal_debts WHERE debt_id = ?", (debt_id,)
        ).fetchone():
            raise FinCLError(f"debt {debt_id} was opened twice")
        connection.execute(
            """
            INSERT INTO personal_debts
                (debt_id, direction, currency, counterparty_label, opened_on,
                 due_on, status, closed_on, opened_seq, details_seq,
                 lifecycle_seq, reason)
            VALUES (?, ?, ?, ?, ?, ?, 'OPEN', NULL, ?, NULL, NULL, ?)
            """,
            (
                debt_id,
                payload["direction"],
                payload["currency"],
                payload["counterparty_label"],
                payload["opened_on"],
                payload["due_on"],
                journal_seq,
                payload["reason"],
            ),
        )
        return

    if decision_kind == PERSONAL_DEBT_DETAILS_SET:
        debt_id = anchors["debt"]
        _debt_row(connection, debt_id)
        connection.execute(
            "UPDATE personal_debts SET counterparty_label = ?, due_on = ?, "
            "details_seq = ?, reason = ? WHERE debt_id = ?",
            (
                payload["counterparty_label"],
                payload["due_on"],
                journal_seq,
                payload["reason"],
                debt_id,
            ),
        )
        return

    if decision_kind == PERSONAL_DEBT_EVENT_SET:
        debt_id, event_id = anchors["debt"], anchors["event"]
        debt = _debt_row(connection, debt_id)
        if payload["direction"] != debt["direction"]:
            raise FinCLError(
                f"event {event_id} direction does not match debt {debt_id}"
            )
        if payload["effective_on"] < debt["opened_on"]:
            raise FinCLError(
                f"event {event_id} is dated before the debt was opened"
            )
        signed = events_for_direction(str(debt["direction"]))[str(payload["event_kind"])]
        principal = ExactMoney.parse(str(payload["principal_amount"]))
        connection.execute(
            """
            INSERT INTO personal_debt_events
                (debt_id, event_id, event_kind, effective_on,
                 principal_coefficient, principal_scale, signed, opening_balance,
                 expense_category, journal_seq, batch_index, bound,
                 dangling_reason, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
            ON CONFLICT(debt_id, event_id) DO UPDATE SET
                event_kind = excluded.event_kind,
                effective_on = excluded.effective_on,
                principal_coefficient = excluded.principal_coefficient,
                principal_scale = excluded.principal_scale,
                signed = excluded.signed,
                opening_balance = excluded.opening_balance,
                expense_category = excluded.expense_category,
                journal_seq = excluded.journal_seq,
                batch_index = excluded.batch_index,
                bound = excluded.bound,
                dangling_reason = excluded.dangling_reason,
                reason = excluded.reason
            """,
            (
                debt_id,
                event_id,
                payload["event_kind"],
                payload["effective_on"],
                principal.coefficient,
                principal.scale,
                signed,
                1 if payload["opening_balance"] else 0,
                payload["expense_category"],
                journal_seq,
                batch_index,
                payload["reason"],
            ),
        )
        return

    if decision_kind == PERSONAL_DEBT_EVENT_RETIRE:
        connection.execute(
            "DELETE FROM personal_debt_events WHERE debt_id = ? AND event_id = ?",
            (anchors["debt"], anchors["event"]),
        )
        return

    if decision_kind == ENTRY_RESIDUAL_RETIRE:
        connection.execute(
            "DELETE FROM entry_residuals WHERE entry_anchor = ?", (anchors["entry"],)
        )
        return

    if decision_kind in {
        PERSONAL_DEBT_CLOSE,
        PERSONAL_DEBT_REOPEN,
        ENTRY_RESIDUAL_SET,
        INBOUND_CLASSIFICATION_RETIRE,
    }:  # pragma: no cover - validation refuses these in slice 1
        raise FinCLError(f"{decision_kind} projection arrives in a later v0.3 slice")

    raise FinCLError(f"{decision_kind} is not a personal-debt decision")  # pragma: no cover


def resolve_debts(connection: sqlite3.Connection) -> None:
    """Rebuild derived debt tables and revalidate every debt's chronology.

    Slice 1: the allocation rebuild has nothing to do (opening events carry no
    allocations); the day-end non-negative check still runs so a retroactive
    edit that would drive principal negative is caught at replay time.
    """
    connection.execute("DELETE FROM personal_debt_allocations")

    for debt in connection.execute("SELECT * FROM personal_debts").fetchall():
        events = connection.execute(
            "SELECT effective_on, principal_coefficient, principal_scale, signed "
            "FROM personal_debt_events WHERE debt_id = ? "
            "ORDER BY effective_on, signed DESC, event_id",
            (debt["debt_id"],),
        ).fetchall()
        running_by_day: dict[str, int] = {}
        scale = 0
        for event in events:
            scale = max(scale, int(event["principal_scale"]))
        running = 0
        current_day = None
        for event in events:
            day = str(event["effective_on"])
            if current_day is not None and day != current_day:
                running_by_day[current_day] = running
            current_day = day
            coefficient = int(event["principal_coefficient"]) * (
                10 ** (scale - int(event["principal_scale"]))
            )
            running += int(event["signed"]) * coefficient
            if int(event["signed"]) < 0 and running < 0:
                raise FinCLError(
                    f"debt {debt['debt_id']} would fall below zero principal on {day}"
                )
        if current_day is not None:
            running_by_day[current_day] = running
        for day, day_end in running_by_day.items():
            if day_end < 0:
                raise FinCLError(
                    f"debt {debt['debt_id']} ends {day} with negative principal"
                )
