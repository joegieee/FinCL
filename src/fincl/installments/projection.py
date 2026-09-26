"""Replay the ``INSTALLMENT_*`` kinds into schema 16.

``apply_installment`` projects one record: plan identity and known/unknown
principal / term into ``installment_plans``, the evidence-anchored source
occurrences into ``installment_occurrences`` (anchors resolved here), the
projected schedule into ``installment_schedule`` with a disclosed rounding
remainder on the final row, the original-purchase component claims into
``installment_purchase_allocations`` (so the shared registry can read
``bound = 1`` rows), the conversion credit the plan opened from into
``installment_conversions``, and early settlements partitioned into principal / fees /
waivers into ``installment_settlements``.

``resolve_installments`` runs once after the record loop: it revalidates every
occurrence / allocation / settlement binding, enforces at most one active
installment plan per source occurrence, and — through ``analysis.allocations`` —
that a source component is never over-claimed across debts, residuals, refunds
and installments. No mutable remaining liability is stored.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    INSTALLMENT_ALLOCATION_RETIRE,
    INSTALLMENT_ALLOCATION_SET,
    INSTALLMENT_PLAN_RETIRE,
    INSTALLMENT_PLAN_SET,
    INSTALLMENT_SETTLEMENT_RETIRE,
    INSTALLMENT_SETTLEMENT_SET,
)


def _plan_row(connection: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM installment_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        raise FinCLError(f"installment plan {plan_id} was referenced before it was set")
    return row


def _money(value: object) -> tuple[int, int]:
    money = ExactMoney.parse(str(value))
    return money.coefficient, money.scale


def _opt_money(value: object) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    return _money(value)


def _write_occurrences(
    connection: sqlite3.Connection,
    plan_id: str,
    occurrences: list[dict[str, Any]],
    resolve_anchor: Any,
) -> None:
    connection.execute(
        "DELETE FROM installment_occurrences WHERE plan_id = ?", (plan_id,)
    )
    for item in occurrences:
        anchor = str(item["entry_anchor"])
        entry_id = resolve_anchor(anchor)
        coefficient, scale = _money(item["amount"])
        connection.execute(
            """
            INSERT INTO installment_occurrences
                (plan_id, occurrence_id, occurrence_index, occurrence_count,
                 entry_anchor, entry_id, effective_on, amount_coefficient,
                 amount_scale, date_precision, bound)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                str(item["occurrence_id"]),
                item["occurrence_index"],
                item["occurrence_count"],
                anchor,
                entry_id,
                item["effective_on"],
                coefficient,
                scale,
                item["date_precision"],
                1 if entry_id is not None else 0,
            ),
        )


def _write_schedule(
    connection: sqlite3.Connection, plan_id: str, schedule: list[dict[str, Any]]
) -> None:
    connection.execute(
        "DELETE FROM installment_schedule WHERE plan_id = ?", (plan_id,)
    )
    for index, row in enumerate(schedule, start=1):
        principal_c, principal_s = _money(row["principal"])
        fee_c, fee_s = _money(row["fee"])
        connection.execute(
            """
            INSERT INTO installment_schedule
                (plan_id, schedule_index, due_on, principal_coefficient,
                 principal_scale, fee_coefficient, fee_scale,
                 rounding_remainder_coefficient, rounding_remainder_scale,
                 is_projected)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 1)
            """,
            (plan_id, index, row["due_on"], principal_c, principal_s, fee_c, fee_s),
        )


def _apply_schedule_remainder(
    connection: sqlite3.Connection, plan_id: str, known_principal: object
) -> None:
    """Stamp the last schedule row with the disclosed rounding remainder."""
    if known_principal is None:
        return
    rows = connection.execute(
        "SELECT schedule_index, principal_coefficient, principal_scale "
        "FROM installment_schedule WHERE plan_id = ? ORDER BY schedule_index",
        (plan_id,),
    ).fetchall()
    if not rows:
        return
    scale = max(int(r["principal_scale"]) for r in rows)
    scale = max(scale, ExactMoney.parse(str(known_principal)).scale)
    scheduled = sum(
        int(r["principal_coefficient"]) * 10 ** (scale - int(r["principal_scale"]))
        for r in rows
    )
    want = ExactMoney.parse(str(known_principal)).rescaled_coefficient(scale)
    remainder = want - scheduled
    connection.execute(
        "UPDATE installment_schedule SET rounding_remainder_coefficient = ?, "
        "rounding_remainder_scale = ? WHERE plan_id = ? AND schedule_index = ?",
        (remainder, scale, plan_id, int(rows[-1]["schedule_index"])),
    )


def apply_installment(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
    batch_index: int | None,
    resolve_anchor: Any,
) -> None:
    if decision_kind == INSTALLMENT_PLAN_SET:
        plan_id = anchors["plan"]
        principal_c, principal_s = _opt_money(payload["known_principal"])
        fee_c, fee_s = _opt_money(payload["financing_fee"])
        connection.execute(
            """
            INSERT INTO installment_plans
                (plan_id, card_account_id, currency, plan_reference, merchant_label,
                 term_months, principal_coefficient, principal_scale,
                 financing_fee_coefficient, financing_fee_scale, opened_on, status,
                 journal_seq, batch_index, bound, dangling_reason, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
                card_account_id = excluded.card_account_id,
                currency = excluded.currency,
                plan_reference = excluded.plan_reference,
                merchant_label = excluded.merchant_label,
                term_months = excluded.term_months,
                principal_coefficient = excluded.principal_coefficient,
                principal_scale = excluded.principal_scale,
                financing_fee_coefficient = excluded.financing_fee_coefficient,
                financing_fee_scale = excluded.financing_fee_scale,
                opened_on = excluded.opened_on,
                status = excluded.status,
                journal_seq = excluded.journal_seq,
                batch_index = excluded.batch_index,
                bound = 1,
                dangling_reason = NULL,
                reason = excluded.reason
            """,
            (
                plan_id,
                payload["card_account_id"],
                payload["currency"],
                payload["plan_reference"],
                payload["merchant_label"],
                payload["term_months"],
                principal_c,
                principal_s,
                fee_c,
                fee_s,
                payload["opened_on"],
                payload["status"],
                journal_seq,
                batch_index,
                payload["reason"],
            ),
        )
        _write_occurrences(connection, plan_id, list(payload["occurrences"]), resolve_anchor)
        connection.execute("DELETE FROM installment_conversions WHERE plan_id = ?", (plan_id,))
        conversion = payload.get("conversion_anchor")
        if conversion is not None:
            entry_id = resolve_anchor(str(conversion))
            connection.execute(
                "INSERT INTO installment_conversions (plan_id, entry_anchor, entry_id, bound) "
                "VALUES (?, ?, ?, ?)",
                (plan_id, str(conversion), entry_id, 1 if entry_id is not None else 0),
            )
        _write_schedule(connection, plan_id, list(payload["schedule"]))
        _apply_schedule_remainder(connection, plan_id, payload["known_principal"])
        return

    if decision_kind == INSTALLMENT_PLAN_RETIRE:
        plan_id = anchors["plan"]
        for table in (
            "installment_settlements",
            "installment_purchase_allocations",
            "installment_schedule",
            "installment_occurrences",
            "installment_conversions",
        ):
            connection.execute(f"DELETE FROM {table} WHERE plan_id = ?", (plan_id,))
        connection.execute(
            "DELETE FROM installment_plans WHERE plan_id = ?", (plan_id,)
        )
        return

    if decision_kind == INSTALLMENT_ALLOCATION_SET:
        plan_id = anchors["plan"]
        plan = _plan_row(connection, plan_id)
        connection.execute(
            "DELETE FROM installment_purchase_allocations WHERE plan_id = ?", (plan_id,)
        )
        for seq, item in enumerate(payload["allocations"]):
            anchor = str(item["entry_anchor"])
            entry_id = resolve_anchor(anchor)
            coefficient, scale = _money(item["amount"])
            connection.execute(
                """
                INSERT INTO installment_purchase_allocations
                    (plan_id, seq_in_plan, entry_anchor, entry_id, component_role,
                     amount_coefficient, amount_scale, currency, bound)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    seq,
                    anchor,
                    entry_id,
                    item["component_role"],
                    coefficient,
                    scale,
                    str(plan["currency"]),
                    1 if entry_id is not None else 0,
                ),
            )
        return

    if decision_kind == INSTALLMENT_ALLOCATION_RETIRE:
        connection.execute(
            "DELETE FROM installment_purchase_allocations WHERE plan_id = ?",
            (anchors["plan"],),
        )
        return

    if decision_kind == INSTALLMENT_SETTLEMENT_SET:
        plan_id, settlement_id = anchors["plan"], anchors["settlement"]
        plan = _plan_row(connection, plan_id)
        anchor = str(payload["source_entry_anchor"])
        entry_id = resolve_anchor(anchor)
        principal_c, principal_s = _money(payload["principal_amount"])
        fee_c, fee_s = _money(payload["fee_amount"])
        waiver_c, waiver_s = _money(payload["waiver_amount"])
        connection.execute(
            """
            INSERT INTO installment_settlements
                (plan_id, settlement_id, settled_on, source_entry_anchor,
                 source_entry_id, principal_coefficient, principal_scale,
                 fee_coefficient, fee_scale, waiver_coefficient, waiver_scale,
                 currency, bound)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id, settlement_id) DO UPDATE SET
                settled_on = excluded.settled_on,
                source_entry_anchor = excluded.source_entry_anchor,
                source_entry_id = excluded.source_entry_id,
                principal_coefficient = excluded.principal_coefficient,
                principal_scale = excluded.principal_scale,
                fee_coefficient = excluded.fee_coefficient,
                fee_scale = excluded.fee_scale,
                waiver_coefficient = excluded.waiver_coefficient,
                waiver_scale = excluded.waiver_scale,
                currency = excluded.currency,
                bound = excluded.bound
            """,
            (
                plan_id,
                settlement_id,
                payload["settled_on"],
                anchor,
                entry_id,
                principal_c,
                principal_s,
                fee_c,
                fee_s,
                waiver_c,
                waiver_s,
                str(plan["currency"]),
                1 if entry_id is not None else 0,
            ),
        )
        return

    if decision_kind == INSTALLMENT_SETTLEMENT_RETIRE:
        connection.execute(
            "DELETE FROM installment_settlements WHERE plan_id = ? AND settlement_id = ?",
            (anchors["plan"], anchors["settlement"]),
        )
        return

    raise FinCLError(  # pragma: no cover - guarded by the dispatcher
        f"{decision_kind} is not an installment decision"
    )


def _check_single_active_plan_per_occurrence(connection: sqlite3.Connection) -> None:
    seen: dict[str, str] = {}
    for row in connection.execute(
        "SELECT entry_anchor, plan_id FROM installment_occurrences ORDER BY plan_id, occurrence_id"
    ).fetchall():
        anchor = str(row["entry_anchor"])
        plan_id = str(row["plan_id"])
        if anchor in seen and seen[anchor] != plan_id:
            raise FinCLError(
                f"source occurrence {anchor} is claimed by more than one active "
                f"installment plan ({seen[anchor]}, {plan_id})"
            )
        seen[anchor] = plan_id


def _check_single_plan_per_conversion(connection: sqlite3.Connection) -> None:
    """A conversion credit opens one plan, and is never also an instalment."""
    for row in connection.execute(
        "SELECT entry_anchor, group_concat(plan_id, ', ') AS plans FROM installment_conversions "
        "GROUP BY entry_anchor HAVING count(*) > 1"
    ).fetchall():
        raise FinCLError(
            f"conversion credit {row['entry_anchor']} is attached to more than one "
            f"installment plan ({row['plans']})"
        )
    row = connection.execute(
        "SELECT c.entry_anchor FROM installment_conversions c "
        "JOIN installment_occurrences o ON o.entry_anchor = c.entry_anchor LIMIT 1"
    ).fetchone()
    if row is not None:
        raise FinCLError(
            f"entry {row['entry_anchor']} is both a conversion credit and an instalment"
        )


def _check_partitions(connection: sqlite3.Connection) -> None:
    from ..analysis.allocations import assert_partition

    anchors: set[str] = set()
    for (anchor,) in connection.execute(
        "SELECT DISTINCT entry_anchor FROM installment_purchase_allocations WHERE bound = 1"
    ):
        anchors.add(str(anchor))
    for (anchor,) in connection.execute(
        "SELECT DISTINCT source_entry_anchor FROM installment_settlements WHERE bound = 1"
    ):
        anchors.add(str(anchor))
    for (anchor,) in connection.execute(
        "SELECT entry_anchor FROM installment_conversions WHERE bound = 1"
    ):
        anchors.add(str(anchor))
    for anchor in anchors:
        assert_partition(connection, anchor, role="amount")
        assert_partition(connection, anchor, role="fee")


def resolve_installments(connection: sqlite3.Connection) -> None:
    _check_single_active_plan_per_occurrence(connection)
    _check_single_plan_per_conversion(connection)
    _check_partitions(connection)


__all__ = ["apply_installment", "resolve_installments"]
