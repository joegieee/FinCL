"""Replay the ``PERSONAL_DEBT_*`` / ``ENTRY_RESIDUAL_*`` kinds into schema 13.

``apply_debt`` projects one record: identity and details into ``personal_debts``,
the dated principal timeline into ``personal_debt_events``, the per-source
component claims into ``personal_debt_allocations`` (anchors resolved here so the
shared registry can read ``bound = 1`` rows), and the one-per-entry owner share
into ``entry_residuals``. ``INBOUND_CLASSIFICATION_RETIRE`` removes a prior
inbound classification so a coupled correction can re-adjudicate the inflow.

``resolve_debts`` runs once after the record loop to revalidate every debt's
full chronology (day-end non-negative principal, event dates inside the open
window), the direction / currency of each bound source allocation, and — through
``analysis.allocations`` — that claims never over-partition a bank component.

The as-of balance is always computed from the event rows; no mutable
authoritative balance is stored.
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
    OFF_LEDGER_EVENTS,
    PERSONAL_DEBT_CLOSE,
    PERSONAL_DEBT_DETAILS_SET,
    PERSONAL_DEBT_EVENT_RETIRE,
    PERSONAL_DEBT_EVENT_SET,
    PERSONAL_DEBT_OPEN,
    PERSONAL_DEBT_REOPEN,
    PERSONAL_DEBT_SCHEDULE_RETIRE,
    PERSONAL_DEBT_SCHEDULE_SET,
    events_for_direction,
)
from .schedule import clear_schedule, write_schedule


def _debt_row(connection: sqlite3.Connection, debt_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM personal_debts WHERE debt_id = ?", (debt_id,)
    ).fetchone()
    if row is None:
        raise FinCLError(f"debt {debt_id} was referenced before it was opened")
    return row


def _entry_row(connection: sqlite3.Connection, entry_anchor: str) -> sqlite3.Row | None:
    """One SELECTED ledger entry for ``entry_anchor``, or ``None`` when unbound."""
    return connection.execute(
        """
        SELECT e.economic_direction, e.currency_pocket,
               e.amount_coefficient, e.amount_scale
        FROM ledger_entries e
        JOIN extraction_runs x
          ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
        WHERE e.natural_anchor = ?
        LIMIT 1
        """,
        (entry_anchor,),
    ).fetchone()


def _component(payload_value: object) -> tuple[int, int]:
    money = ExactMoney.parse(str(payload_value))
    return money.coefficient, money.scale


def _write_allocations(
    connection: sqlite3.Connection,
    *,
    debt_id: str,
    event_id: str,
    currency: str,
    allocations: list[dict[str, Any]],
    resolve_anchor: Any,
) -> tuple[bool, str | None]:
    """Rebuild ``personal_debt_allocations`` for one event; return (bound, reason)."""
    connection.execute(
        "DELETE FROM personal_debt_allocations WHERE debt_id = ? AND event_id = ?",
        (debt_id, event_id),
    )
    unresolved = 0
    for seq, item in enumerate(allocations):
        anchor = str(item["entry_anchor"])
        entry_id = resolve_anchor(anchor)
        bound = 1 if entry_id is not None else 0
        if not bound:
            unresolved += 1
        principal_c, principal_s = _component(item["principal_amount"])
        interest_c, interest_s = _component(item["interest_amount"])
        fee_c, fee_s = _component(item["fee_amount"])
        sep_c, sep_s = _component(item["separate_fee_amount"])
        connection.execute(
            """
            INSERT INTO personal_debt_allocations
                (debt_id, event_id, seq_in_event, entry_anchor, entry_id,
                 principal_coefficient, principal_scale,
                 interest_coefficient, interest_scale,
                 fee_coefficient, fee_scale,
                 separate_fee_coefficient, separate_fee_scale,
                 currency, bound)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                debt_id, event_id, seq, anchor, entry_id,
                principal_c, principal_s, interest_c, interest_s,
                fee_c, fee_s, sep_c, sep_s, currency, bound,
            ),
        )
    if unresolved:
        return False, f"{unresolved} source allocation(s) do not resolve to a selected entry"
    return True, None


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
        allocations = list(payload["entry_allocations"])
        off_ledger = (
            str(payload["event_kind"]) in OFF_LEDGER_EVENTS
            or bool(payload["opening_balance"])
        )
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
                bound = 1,
                dangling_reason = NULL,
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

        if off_ledger:
            connection.execute(
                "DELETE FROM personal_debt_allocations WHERE debt_id = ? AND event_id = ?",
                (debt_id, event_id),
            )
        else:
            bound, dangling = _write_allocations(
                connection,
                debt_id=debt_id,
                event_id=event_id,
                currency=str(debt["currency"]),
                allocations=allocations,
                resolve_anchor=resolve_anchor,
            )
            connection.execute(
                "UPDATE personal_debt_events SET bound = ?, dangling_reason = ? "
                "WHERE debt_id = ? AND event_id = ?",
                (1 if bound else 0, dangling, debt_id, event_id),
            )
        return

    if decision_kind == PERSONAL_DEBT_EVENT_RETIRE:
        connection.execute(
            "DELETE FROM personal_debt_allocations WHERE debt_id = ? AND event_id = ?",
            (anchors["debt"], anchors["event"]),
        )
        connection.execute(
            "DELETE FROM personal_debt_events WHERE debt_id = ? AND event_id = ?",
            (anchors["debt"], anchors["event"]),
        )
        return

    if decision_kind == ENTRY_RESIDUAL_SET:
        entry_anchor = anchors["entry"]
        entry = _entry_row(connection, entry_anchor)
        coefficient, scale = _component(payload["expense_amount"])
        bound = entry is not None
        currency = str(entry["currency_pocket"]) if entry is not None else ""
        dangling = None if bound else "the source entry does not resolve to a selected entry"
        connection.execute(
            """
            INSERT INTO entry_residuals
                (entry_anchor, expense_coefficient, expense_scale, expense_category,
                 currency, journal_seq, batch_index, bound, dangling_reason, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_anchor) DO UPDATE SET
                expense_coefficient = excluded.expense_coefficient,
                expense_scale = excluded.expense_scale,
                expense_category = excluded.expense_category,
                currency = excluded.currency,
                journal_seq = excluded.journal_seq,
                batch_index = excluded.batch_index,
                bound = excluded.bound,
                dangling_reason = excluded.dangling_reason,
                reason = excluded.reason
            """,
            (
                entry_anchor, coefficient, scale, payload["expense_category"],
                currency, journal_seq, batch_index,
                1 if bound else 0, dangling, payload["reason"],
            ),
        )
        return

    if decision_kind == ENTRY_RESIDUAL_RETIRE:
        connection.execute(
            "DELETE FROM entry_residuals WHERE entry_anchor = ?", (anchors["entry"],)
        )
        return

    if decision_kind == INBOUND_CLASSIFICATION_RETIRE:
        # Return the inflow to its source default; a coupled correction batch may
        # then re-classify it. A later INBOUND_CLASSIFICATION re-adds the row.
        connection.execute(
            "DELETE FROM inbound_classifications WHERE anchor = ?",
            (anchors["entry"],),
        )
        return

    if decision_kind == PERSONAL_DEBT_CLOSE:
        debt = _debt_row(connection, anchors["debt"])
        if str(debt["status"]) == "CLOSED":
            raise FinCLError(f"debt {anchors['debt']} is already closed")
        if str(payload["effective_on"]) < str(debt["opened_on"]):
            raise FinCLError("a debt cannot be closed before it was opened")
        connection.execute(
            "UPDATE personal_debts SET status = 'CLOSED', closed_on = ?, "
            "lifecycle_seq = ?, reason = ? WHERE debt_id = ?",
            (payload["effective_on"], journal_seq, payload["reason"], anchors["debt"]),
        )
        return

    if decision_kind == PERSONAL_DEBT_REOPEN:
        debt = _debt_row(connection, anchors["debt"])
        if str(debt["status"]) != "CLOSED":
            raise FinCLError(
                f"debt {anchors['debt']} is not closed, so it cannot be reopened"
            )
        connection.execute(
            "UPDATE personal_debts SET status = 'OPEN', closed_on = NULL, "
            "lifecycle_seq = ?, reason = ? WHERE debt_id = ?",
            (journal_seq, payload["reason"], anchors["debt"]),
        )
        return

    if decision_kind == PERSONAL_DEBT_SCHEDULE_SET:
        _debt_row(connection, anchors["debt"])
        write_schedule(connection, anchors["debt"], payload, journal_seq)
        return

    if decision_kind == PERSONAL_DEBT_SCHEDULE_RETIRE:
        clear_schedule(connection, anchors["debt"])
        return

    raise FinCLError(f"{decision_kind} is not a personal-debt decision")  # pragma: no cover


def expected_bank_direction(debt_direction: str, signed: int) -> str:
    """The bank direction a bound principal allocation must carry (§2)."""
    if (debt_direction == "PAYABLE" and signed > 0) or (
        debt_direction == "RECEIVABLE" and signed < 0
    ):
        return "IN"
    return "OUT"


def _check_allocation_binding(connection: sqlite3.Connection) -> None:
    """Direction and currency of every bound source allocation (§1, §2)."""
    rows = connection.execute(
        """
        SELECT a.entry_anchor, a.currency AS debt_currency,
               a.principal_coefficient, e.signed, d.direction
        FROM personal_debt_allocations a
        JOIN personal_debt_events e
          ON e.debt_id = a.debt_id AND e.event_id = a.event_id
        JOIN personal_debts d ON d.debt_id = a.debt_id
        WHERE a.bound = 1
        """
    ).fetchall()
    for row in rows:
        entry = _entry_row(connection, str(row["entry_anchor"]))
        if entry is None:
            # A row marked bound whose entry vanished this replay: treat as a
            # hard inconsistency rather than silently trusting the flag.
            raise FinCLError(
                f"debt allocation against {row['entry_anchor']} lost its entry on replay"
            )
        if str(entry["currency_pocket"]) != str(row["debt_currency"]):
            raise FinCLError(
                f"debt allocation against {row['entry_anchor']} is "
                f"{entry['currency_pocket']} but the debt is {row['debt_currency']}; "
                "cross-currency debt allocation is not supported"
            )
        if int(row["principal_coefficient"]) == 0:
            continue
        expected = expected_bank_direction(str(row["direction"]), int(row["signed"]))
        if str(entry["economic_direction"]) != expected:
            raise FinCLError(
                f"debt allocation against {row['entry_anchor']} needs a bank "
                f"{expected} movement but the entry is {entry['economic_direction']}"
            )


def _check_partitions(connection: sqlite3.Connection) -> None:
    """No bank component is over-claimed across debts, residuals and refunds."""
    from ..analysis.allocations import assert_partition

    anchors: set[str] = set()
    for (anchor,) in connection.execute(
        "SELECT DISTINCT entry_anchor FROM personal_debt_allocations WHERE bound = 1"
    ):
        anchors.add(str(anchor))
    for (anchor,) in connection.execute(
        "SELECT DISTINCT entry_anchor FROM entry_residuals WHERE bound = 1"
    ):
        anchors.add(str(anchor))
    for anchor in anchors:
        assert_partition(connection, anchor, role="amount")
        assert_partition(connection, anchor, role="fee")


def resolve_debts(connection: sqlite3.Connection) -> None:
    """Revalidate every debt's chronology, allocation binding, partitions and closure.

    Allocation rows are written by :func:`apply_debt`; this pass does not rebuild
    them. It runs the day-end non-negative check, the direction / currency check
    for bound source allocations, the shared component-partition check, and — for
    a CLOSED debt — that the principal is zero as of the closure date and no
    active event is dated after it (§2), so a retroactive edit that breaks any
    invariant is caught at replay time.
    """
    for debt in connection.execute("SELECT * FROM personal_debts").fetchall():
        events = connection.execute(
            "SELECT event_id, effective_on, principal_coefficient, principal_scale, signed "
            "FROM personal_debt_events WHERE debt_id = ? "
            "ORDER BY effective_on, signed DESC, event_id",
            (debt["debt_id"],),
        ).fetchall()
        scale = 0
        for event in events:
            scale = max(scale, int(event["principal_scale"]))
        running_by_day: dict[str, int] = {}
        running = 0
        current_day = None
        closed_on = str(debt["closed_on"]) if debt["closed_on"] is not None else None
        balance_at_close = 0
        later_than_close: list[str] = []
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
            if closed_on is not None:
                if day <= closed_on:
                    balance_at_close = running
                else:
                    later_than_close.append(str(event["event_id"]))
        if current_day is not None:
            running_by_day[current_day] = running
        for day, day_end in running_by_day.items():
            if day_end < 0:
                raise FinCLError(
                    f"debt {debt['debt_id']} ends {day} with negative principal"
                )
        if closed_on is not None:
            if later_than_close:
                raise FinCLError(
                    f"debt {debt['debt_id']} is closed as of {closed_on} but has later "
                    f"active events: {sorted(later_than_close)}"
                )
            if balance_at_close != 0:
                raise FinCLError(
                    f"debt {debt['debt_id']} cannot be closed as of {closed_on}: "
                    f"principal is not zero at that date"
                )

    _check_allocation_binding(connection)
    _check_partitions(connection)
