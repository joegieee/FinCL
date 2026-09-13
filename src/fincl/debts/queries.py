"""Snapshot-only reads for the Debts screens (v0.3).

Payables and receivables are never netted. Every balance is computed here from
the dated event rows through an inclusive ``as_of``; there is no stored balance.
Decimal strings are authoritative.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..money import format_decimal
from .models import events_for_direction
from .projection import expected_bank_direction

_DATE_RE_LEN = 10


def _check_as_of(as_of: str | None) -> str:
    if as_of is None:
        return date.today().isoformat()
    try:
        return date.fromisoformat(as_of).isoformat()
    except ValueError as exc:
        raise FinCLError("as_of must be an ISO calendar date (YYYY-MM-DD)") from exc


def _generation(connection: Any, expected: str | None) -> str:
    generation = current_generation(connection)
    if generation is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    token = str(generation["generation_token"])
    if expected and expected != token:
        raise FinCLError("STALE_GENERATION: reload before continuing")
    return token


def _balance_minor(rows: list[Any], as_of: str) -> tuple[int, int]:
    scale = 0
    for row in rows:
        scale = max(scale, int(row["principal_scale"]))
    total = 0
    for row in rows:
        if str(row["effective_on"]) > as_of:
            continue
        coefficient = int(row["principal_coefficient"]) * (
            10 ** (scale - int(row["principal_scale"]))
        )
        total += int(row["signed"]) * coefficient
    return total, scale


def _reconciliation_status(rows: list[Any]) -> str:
    if not rows:
        return "no_events"
    if any(row["dangling_reason"] for row in rows):
        return "unverified"
    if all(int(row["opening_balance"]) == 1 for row in rows):
        return "opening_balance_only"
    return "reconciled"


def _debt_shape(debt: Any, rows: list[Any], as_of: str) -> dict[str, Any]:
    total, scale = _balance_minor(rows, as_of)
    last = max(rows, key=lambda r: (str(r["effective_on"]), str(r["event_id"]))) if rows else None
    return {
        "debt_id": str(debt["debt_id"]),
        "direction": str(debt["direction"]),
        "currency": str(debt["currency"]),
        "counterparty_label": str(debt["counterparty_label"]),
        "opened_on": str(debt["opened_on"]),
        "due_on": (str(debt["due_on"]) if debt["due_on"] is not None else None),
        "status": str(debt["status"]),
        "closed_on": (str(debt["closed_on"]) if debt["closed_on"] is not None else None),
        "remaining_principal": {
            "value": format_decimal(total, scale),
            "currency": str(debt["currency"]),
        },
        "last_event": (
            None
            if last is None
            else {"kind": str(last["event_kind"]), "effective_on": str(last["effective_on"])}
        ),
        "reconciliation_status": _reconciliation_status(rows),
        "event_count": len(rows),
    }


def debts_page(
    database: Path,
    *,
    as_of: str | None = None,
    currency: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    expected: str | None = None,
) -> dict[str, Any]:
    """The Debts landing page: separate ``i_owe`` / ``owed_to_me`` lists."""
    as_of = _check_as_of(as_of)
    limit = max(1, min(int(limit), 200))
    if status is not None and status not in {"OPEN", "CLOSED"}:
        raise FinCLError("status must be OPEN or CLOSED")
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        clauses: list[str] = []
        params: list[Any] = []
        if currency is not None:
            clauses.append("currency = ?")
            params.append(currency)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if cursor is not None:
            clauses.append("debt_id > ?")
            params.append(cursor)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        debts = connection.execute(
            f"SELECT * FROM personal_debts{where} ORDER BY debt_id LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        next_cursor = str(debts[limit]["debt_id"]) if len(debts) > limit else None
        debts = debts[:limit]
        i_owe: list[dict[str, Any]] = []
        owed_to_me: list[dict[str, Any]] = []
        for debt in debts:
            rows = connection.execute(
                "SELECT * FROM personal_debt_events WHERE debt_id = ?",
                (debt["debt_id"],),
            ).fetchall()
            shape = _debt_shape(debt, rows, as_of)
            (i_owe if debt["direction"] == "PAYABLE" else owed_to_me).append(shape)
        return {
            "generation": token,
            "as_of": as_of,
            "i_owe": i_owe,
            "owed_to_me": owed_to_me,
            "next_cursor": next_cursor,
        }
    finally:
        connection.close()


def debt_detail(
    database: Path, debt_id: str, *, as_of: str | None = None, expected: str | None = None
) -> dict[str, Any]:
    as_of = _check_as_of(as_of)
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        debt = connection.execute(
            "SELECT * FROM personal_debts WHERE debt_id = ?", (debt_id,)
        ).fetchone()
        if debt is None:
            raise FinCLError("that debt does not exist")
        rows = connection.execute(
            "SELECT * FROM personal_debt_events WHERE debt_id = ? "
            "ORDER BY effective_on, signed DESC, event_id",
            (debt_id,),
        ).fetchall()
        shape = _debt_shape(debt, rows, as_of)
        _, scale = _balance_minor(rows, as_of)
        history: list[dict[str, Any]] = []
        for row in rows:
            history.append(
                {
                    "event_id": str(row["event_id"]),
                    "event_kind": str(row["event_kind"]),
                    "effective_on": str(row["effective_on"]),
                    "opening_balance": bool(row["opening_balance"]),
                    "signed": int(row["signed"]),
                    "principal": {
                        "value": format_decimal(
                            int(row["principal_coefficient"]), int(row["principal_scale"])
                        ),
                        "currency": str(debt["currency"]),
                    },
                    "expense_category": (
                        str(row["expense_category"])
                        if row["expense_category"] is not None
                        else None
                    ),
                    "bound": bool(row["bound"]),
                    "dangling_reason": (
                        str(row["dangling_reason"])
                        if row["dangling_reason"] is not None
                        else None
                    ),
                    "reason": str(row["reason"]),
                    "journal_seq": int(row["journal_seq"]),
                }
            )
        shape["principal_history"] = history
        shape["reason"] = str(debt["reason"])
        return {"generation": token, "as_of": as_of, "debt": shape}
    finally:
        connection.close()


def debt_events(
    database: Path,
    debt_id: str,
    *,
    cursor: str | None = None,
    limit: int = 100,
    expected: str | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        if connection.execute(
            "SELECT 1 FROM personal_debts WHERE debt_id = ?", (debt_id,)
        ).fetchone() is None:
            raise FinCLError("that debt does not exist")
        params: list[Any] = [debt_id]
        cursor_clause = ""
        if cursor is not None:
            cursor_clause = " AND event_id > ?"
            params.append(cursor)
        rows = connection.execute(
            "SELECT * FROM personal_debt_events WHERE debt_id = ?"
            + cursor_clause
            + " ORDER BY event_id LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        next_cursor = str(rows[limit]["event_id"]) if len(rows) > limit else None
        rows = rows[:limit]
        return {
            "generation": token,
            "events": [
                {
                    "event_id": str(row["event_id"]),
                    "event_kind": str(row["event_kind"]),
                    "effective_on": str(row["effective_on"]),
                    "opening_balance": bool(row["opening_balance"]),
                    "principal": {
                        "value": format_decimal(
                            int(row["principal_coefficient"]), int(row["principal_scale"])
                        ),
                        "currency": None,
                    },
                    "bound": bool(row["bound"]),
                    "reason": str(row["reason"]),
                }
                for row in rows
            ],
            "next_cursor": next_cursor,
        }
    finally:
        connection.close()


def allocation_candidates(
    database: Path,
    *,
    debt_id: str,
    event_kind: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    expected: str | None = None,
) -> dict[str, Any]:
    """Selected entries that could back a source allocation for one debt event.

    Scoped to the debt's currency and to the bank direction the chosen event kind
    requires (increase kind by default). Each row carries the component magnitude
    and the amount still unclaimed by any domain. No fuzzy ranking: newest first,
    keyset by ``natural_anchor``.
    """
    from ..analysis.allocations import open_residual

    limit = max(1, min(int(limit), 200))
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        debt = connection.execute(
            "SELECT direction, currency FROM personal_debts WHERE debt_id = ?",
            (debt_id,),
        ).fetchone()
        if debt is None:
            raise FinCLError("that debt does not exist")
        kinds = events_for_direction(str(debt["direction"]))
        if event_kind is None:
            event_kind = next(k for k, sign in kinds.items() if sign > 0)
        if event_kind not in kinds:
            raise FinCLError(f"{event_kind!r} is not a {debt['direction']} event kind")
        want = expected_bank_direction(str(debt["direction"]), kinds[event_kind])

        params: list[Any] = [str(debt["currency"]), want]
        cursor_clause = ""
        if cursor is not None:
            cursor_clause = " AND e.natural_anchor > ?"
            params.append(cursor)
        rows = connection.execute(
            f"""
            SELECT e.natural_anchor, e.effective_at, e.economic_direction,
                   e.amount_coefficient, e.amount_scale, e.currency_pocket,
                   COALESCE(r.description_raw, d.description_raw) AS description_raw
            FROM ledger_entries e
            JOIN extraction_runs x
              ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
            LEFT JOIN raw_records r ON r.id = e.raw_record_id
            LEFT JOIN document_records d ON d.id = e.document_record_id
            WHERE e.currency_pocket = ? AND e.economic_direction = ?{cursor_clause}
            ORDER BY e.natural_anchor
            LIMIT ?
            """,
            (*params, limit + 1),
        ).fetchall()
        next_cursor = str(rows[limit]["natural_anchor"]) if len(rows) > limit else None
        rows = rows[:limit]

        def _decimal(fraction: Fraction, scale: int) -> str:
            scaled = fraction * (10**scale)
            return format_decimal(int(scaled), scale) if scaled.denominator == 1 else str(fraction)

        candidates = []
        for row in rows:
            scale = int(row["amount_scale"])
            magnitude = Fraction(abs(int(row["amount_coefficient"])), 10**scale)
            remaining = open_residual(connection, str(row["natural_anchor"]), role="amount")
            candidates.append(
                {
                    "entry_anchor": str(row["natural_anchor"]),
                    "effective_at": str(row["effective_at"]),
                    "economic_direction": str(row["economic_direction"]),
                    "currency": str(row["currency_pocket"]),
                    "amount": _decimal(magnitude, scale),
                    "open_amount": _decimal(remaining, scale),
                    "description": row["description_raw"],
                }
            )
        return {
            "generation": token,
            "debt_id": debt_id,
            "event_kind": event_kind,
            "required_direction": want,
            "currency": str(debt["currency"]),
            "candidates": candidates,
            "next_cursor": next_cursor,
        }
    finally:
        connection.close()
