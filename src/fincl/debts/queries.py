"""Snapshot-only reads for the Debts screens (v0.3).

Payables and receivables are never netted. Every balance is computed here from
the dated event rows through an inclusive ``as_of``; there is no stored balance.
Decimal strings are authoritative.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from .. import clock
from ..db import connect_readonly, read_snapshot
from ..errors import FinCLError
from ..generation import current_generation
from ..money import MAX_SCALE, format_decimal
from .models import events_for_direction
from .projection import expected_bank_direction

_DATE_RE_LEN = 10


def entry_residual(database: Path, *, anchor: str, expected: str | None = None) -> dict[str, Any]:
    """Native source evidence for editing the one owner share of an outflow.

    Add back the existing share when finding the replacement limit. All other
    claims remain owned by the shared allocation registry, including refunds
    and installment claims. Nothing is inferred to be the owner's expense.
    """
    from ..analysis.allocations import open_residual

    with read_snapshot(database) as connection:
        token = _generation(connection, expected)
        entry = connection.execute(
            """SELECT e.amount_coefficient, e.amount_scale, e.currency_pocket, e.economic_direction
               FROM ledger_entries e JOIN extraction_runs x ON x.id = e.extraction_run_id
               WHERE e.natural_anchor = ? AND x.selection_state = 'SELECTED' LIMIT 1""",
            (anchor,),
        ).fetchone()
        if entry is None:
            raise FinCLError("source entry does not exist in the selected evidence")
        if entry["economic_direction"] != "OUT":
            raise FinCLError("An owner expense share requires an outgoing transaction")
        residual = connection.execute(
            "SELECT * FROM entry_residuals WHERE entry_anchor = ? AND bound = 1", (anchor,)
        ).fetchone()
        own = (Fraction(int(residual["expense_coefficient"]), 10 ** int(residual["expense_scale"]))
               if residual else Fraction(0))
        unassigned = open_residual(connection, anchor)

        def decimal(value: Fraction) -> str:
            for scale in range(int(entry["amount_scale"]), MAX_SCALE + 1):
                scaled = value * 10**scale
                if scaled.denominator == 1:
                    return format_decimal(int(scaled), scale)
            raise FinCLError("The allocation cannot be represented as exact money")  # pragma: no cover

        return {
            "generation": token, "entry_anchor": anchor, "currency": str(entry["currency_pocket"]),
            "amount": decimal(Fraction(abs(int(entry["amount_coefficient"])), 10 ** int(entry["amount_scale"]))),
            "available_amount": decimal(unassigned + own),
            "unassigned_amount": decimal(unassigned),
            "owner_share": ({"expense_amount": decimal(own), "expense_category": str(residual["expense_category"]),
                             "reason": str(residual["reason"])} if residual else None),
        }


def overlapping_commitments(connection: Any, debt_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Active recurring commitments whose payments are this debt's movements.

    A loan repaid monthly is easily confirmed as a recurring bill before its
    payments are recorded against the debt. Both then project the same money:
    the commitment every month, the debt as its remaining principal. A payment
    counts here when it seeds the commitment or is bound to one of its
    occurrences, and a debt allocation claims it. Nothing is retired; this only
    names the overlap so it can be resolved.
    """
    rows = connection.execute(
        """
        WITH commitment_payments AS (
          SELECT commitment, entry_anchor FROM recurring_occurrences
          WHERE bound = 1 AND entry_anchor IS NOT NULL
          UNION
          SELECT rc.commitment, j.value FROM recurring_commitments rc, json_each(rc.seed_anchors_json) j
        )
        SELECT a.debt_id, rc.commitment, rc.name, count(DISTINCT a.entry_anchor) AS payments
        FROM personal_debt_allocations a
        JOIN commitment_payments cp ON cp.entry_anchor = a.entry_anchor
        JOIN recurring_commitments rc ON rc.commitment = cp.commitment
        WHERE a.bound = 1 AND rc.status = 'active' AND (? IS NULL OR a.debt_id = ?)
        GROUP BY a.debt_id, rc.commitment
        ORDER BY rc.name, rc.commitment
        """,
        (debt_id, debt_id),
    ).fetchall()
    found: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        found.setdefault(str(row["debt_id"]), []).append(
            {"commitment": str(row["commitment"]), "name": str(row["name"]), "payments": int(row["payments"])}
        )
    return found


def _decode_allocation_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if {"at", "anchor"} <= set(payload) and all(isinstance(payload[k], str) for k in ("at", "anchor")):
            return payload["at"], payload["anchor"]
    except (binascii.Error, ValueError, TypeError):
        pass
    raise FinCLError("The page cursor is not valid; reload the list")


def _encode_allocation_cursor(effective_at: str, entry_anchor: str) -> str:
    payload = {"at": effective_at, "anchor": entry_anchor}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _check_as_of(as_of: str | None) -> str:
    if as_of is None:
        return clock.today().isoformat()
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
        shape["overlapping_commitments"] = overlapping_commitments(connection, debt_id).get(debt_id, [])
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


def principal_timeline(connection: Any, debt_id: str) -> dict[str, Any] | None:
    """Every event of one debt with the principal standing after it.

    Events run in the order the chronology check walks them (date, increases
    before decreases on a day, event id), so each ``balance`` is the one the
    projection itself would reach. ``remaining`` counts every event, including
    any dated after today.
    """
    debt = connection.execute(
        "SELECT * FROM personal_debts WHERE debt_id = ?", (debt_id,)
    ).fetchone()
    if debt is None:
        return None
    rows = connection.execute(
        "SELECT * FROM personal_debt_events WHERE debt_id = ? "
        "ORDER BY effective_on, signed DESC, event_id",
        (debt_id,),
    ).fetchall()
    scale = max((int(row["principal_scale"]) for row in rows), default=0)
    running = 0
    events = []
    for row in rows:
        running += int(row["signed"]) * int(row["principal_coefficient"]) * 10 ** (
            scale - int(row["principal_scale"])
        )
        events.append({
            "event_id": str(row["event_id"]),
            "event_kind": str(row["event_kind"]),
            "effective_on": str(row["effective_on"]),
            "signed": int(row["signed"]),
            "principal": format_decimal(int(row["principal_coefficient"]), int(row["principal_scale"])),
            "balance": format_decimal(running, scale),
        })
    return {
        "debt_id": debt_id,
        "counterparty_label": str(debt["counterparty_label"]),
        "direction": str(debt["direction"]),
        "currency": str(debt["currency"]),
        "status": str(debt["status"]),
        "closed_on": str(debt["closed_on"]) if debt["closed_on"] is not None else None,
        "remaining": format_decimal(running, scale),
        "events": events,
    }


def _own_claims(connection: Any, debt_id: str, event_id: str | None) -> dict[str, Fraction]:
    """What one event already claims of each source, to offer back when it is being replaced."""
    claims: dict[str, Fraction] = {}
    if event_id is None:
        return claims
    for row in connection.execute(
        "SELECT entry_anchor, principal_coefficient, principal_scale, interest_coefficient, "
        "interest_scale, fee_coefficient, fee_scale FROM personal_debt_allocations "
        "WHERE debt_id = ? AND event_id = ? AND bound = 1",
        (debt_id, event_id),
    ):
        claims[str(row["entry_anchor"])] = sum(
            (Fraction(int(row[f"{part}_coefficient"]), 10 ** int(row[f"{part}_scale"]))
             for part in ("principal", "interest", "fee")),
            Fraction(0),
        )
    return claims


def debt_event(
    database: Path, debt_id: str, event_id: str, *, expected: str | None = None
) -> dict[str, Any]:
    """One event as its decision payload, ready to be corrected.

    ``sources`` describes each allocated transaction the way the allocation
    picker does, with this event's own claim counted as still open, since a
    correction replaces it.
    """
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        debt = connection.execute(
            "SELECT direction, currency FROM personal_debts WHERE debt_id = ?", (debt_id,)
        ).fetchone()
        row = connection.execute(
            "SELECT * FROM personal_debt_events WHERE debt_id = ? AND event_id = ?",
            (debt_id, event_id),
        ).fetchone()
        if debt is None or row is None:
            raise FinCLError("that debt event does not exist")
        allocations = connection.execute(
            "SELECT * FROM personal_debt_allocations WHERE debt_id = ? AND event_id = ? "
            "ORDER BY seq_in_event",
            (debt_id, event_id),
        ).fetchall()
        own = _own_claims(connection, debt_id, event_id)
        direction = expected_bank_direction(str(debt["direction"]), int(row["signed"]))
        sources: list[dict[str, Any]] = []
        for allocation in allocations:
            found, _ = _entry_candidates(
                connection, currency=None, direction=direction, search=None,
                anchor=str(allocation["entry_anchor"]), cursor=None, limit=1, add_back=own,
            )
            sources.extend(found)

        def amount(part: str, item: Any) -> str:
            return format_decimal(int(item[f"{part}_coefficient"]), int(item[f"{part}_scale"]))

        return {
            "generation": token,
            "debt_id": debt_id,
            "event": {
                "event_id": event_id,
                "event_kind": str(row["event_kind"]),
                "direction": str(debt["direction"]),
                "principal_amount": amount("principal", row),
                "effective_on": str(row["effective_on"]),
                "entry_allocations": [
                    {
                        "entry_anchor": str(item["entry_anchor"]),
                        "principal_amount": amount("principal", item),
                        "interest_amount": amount("interest", item),
                        "fee_amount": amount("fee", item),
                        "separate_fee_amount": amount("separate_fee", item),
                    }
                    for item in allocations
                ],
                "opening_balance": bool(row["opening_balance"]),
                "expense_category": row["expense_category"],
                "reason": str(row["reason"]),
            },
            "sources": sources,
        }
    finally:
        connection.close()


def _entry_candidates(
    connection: Any,
    *,
    currency: str | None,
    direction: str,
    search: str | None,
    anchor: str | None,
    cursor: str | None,
    limit: int,
    without_owner_share: bool = False,
    add_back: dict[str, Fraction] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Selected, non-internal entries in one bank direction with an unclaimed
    amount left, newest first, keyset-paged by ``(effective_at, natural_anchor)``.

    An inflow the user called an own-account transfer is internal only by that
    say-so, so it is still offered: a debt may explain it, once that incoming
    type is cleared in the same change (``incoming_type`` says it is there).

    ``without_owner_share`` leaves out entries whose owner share is already
    recorded (an ``ENTRY_RESIDUAL_SET``): an entry carries one owner share, so a
    second one would replace it rather than add to it.

    ``add_back`` counts claims about to be replaced as still open.
    """
    from ..analysis.allocations import open_residual

    params: list[Any] = [direction]
    clauses = ""
    if currency is not None:
        clauses += " AND e.currency_pocket = ?"
        params.append(currency)
    if search:
        clauses += " AND instr(lower(COALESCE(r.description_raw, d.description_raw, '')), lower(?)) > 0"
        params.append(search)
    if anchor:
        clauses += " AND e.natural_anchor = ?"
        params.append(anchor)
    if without_owner_share:
        clauses += (
            " AND NOT EXISTS (SELECT 1 FROM entry_residuals er"
            " WHERE er.entry_anchor = e.natural_anchor AND er.bound = 1)"
        )
    keyset = _decode_allocation_cursor(cursor)
    if keyset is not None:
        clauses += " AND (e.effective_at < ? OR (e.effective_at = ? AND e.natural_anchor > ?))"
        params.extend([keyset[0], keyset[0], keyset[1]])
    rows = connection.execute(
        f"""
        SELECT e.natural_anchor, e.effective_at, e.economic_direction,
               e.amount_coefficient, e.amount_scale, e.currency_pocket,
               COALESCE(r.description_raw, d.description_raw) AS description_raw,
               (SELECT ic.classification FROM inbound_classifications ic
                WHERE ic.anchor = e.natural_anchor AND ic.bound = 1 LIMIT 1) AS incoming_type
        FROM ledger_entries e
        JOIN extraction_runs x
          ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
        LEFT JOIN raw_records r ON r.id = e.raw_record_id
        LEFT JOIN document_records d ON d.id = e.document_record_id
        LEFT JOIN entry_categories c ON c.entry_id = e.id
        WHERE e.economic_direction = ?
          AND (c.group_name IS NULL OR c.group_name != 'Internal' OR (
            EXISTS (SELECT 1 FROM inbound_classifications ic WHERE ic.anchor = e.natural_anchor
                    AND ic.bound = 1 AND ic.classification = 'self_transfer')
            AND NOT EXISTS (SELECT 1 FROM transfer_links t WHERE t.status = 'CONFIRMED'
                            AND (t.left_entry_id = e.id OR t.right_entry_id = e.id))
          )){clauses}
        ORDER BY e.effective_at DESC, e.natural_anchor ASC
        LIMIT ?
        """,
        (*params, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_allocation_cursor(str(rows[-1]["effective_at"]), str(rows[-1]["natural_anchor"]))
        if has_more and rows
        else None
    )

    def _decimal(fraction: Fraction, scale: int) -> str:
        scaled = fraction * (10**scale)
        return format_decimal(int(scaled), scale) if scaled.denominator == 1 else str(fraction)

    candidates = []
    for row in rows:
        scale = int(row["amount_scale"])
        magnitude = Fraction(abs(int(row["amount_coefficient"])), 10**scale)
        remaining = open_residual(connection, str(row["natural_anchor"]), role="amount") + (
            add_back or {}
        ).get(str(row["natural_anchor"]), Fraction(0))
        if remaining <= 0:
            continue  # fully reconciled elsewhere; not a usable source
        candidates.append(
            {
                "entry_anchor": str(row["natural_anchor"]),
                "effective_at": str(row["effective_at"]),
                "economic_direction": str(row["economic_direction"]),
                "currency": str(row["currency_pocket"]),
                "amount": _decimal(magnitude, scale),
                "open_amount": _decimal(remaining, scale),
                "description": row["description_raw"],
                "incoming_type": row["incoming_type"],
            }
        )
    return candidates, next_cursor


def allocation_candidates(
    database: Path,
    *,
    debt_id: str,
    event_kind: str | None = None,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    expected: str | None = None,
    anchor: str | None = None,
    replacing: str | None = None,
) -> dict[str, Any]:
    """Selected entries that could back a source allocation for one debt event.

    Scoped to the debt's currency and to the bank direction the chosen event kind
    requires (increase kind by default). Each row carries the component magnitude
    and the amount still unclaimed by any domain. Newest first, keyset by
    ``(effective_at, natural_anchor)``. ``search`` narrows by a case-insensitive
    substring match against the transaction's description, unbounded by the page
    window (mirrors the transaction list's own search).

    ``anchor`` narrows to one transaction: the classification editor hands a
    transaction to a debt event this way and needs the same answer the picker
    would give for it -- whether it can back this kind at all, and how much of
    it is still unclaimed -- rather than guessing from its face amount.
    """
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
        candidates, next_cursor = _entry_candidates(
            connection, currency=str(debt["currency"]), direction=want, search=search,
            anchor=anchor, cursor=cursor, limit=limit,
            add_back=_own_claims(connection, debt_id, replacing),
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


def split_candidates(
    database: Path,
    *,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    expected: str | None = None,
) -> dict[str, Any]:
    """Bills that can be split with other people: outgoing entries, in any
    currency, with an unclaimed amount and no owner share recorded yet.

    A split records each other person's share as ``EXPENSE_PAID_FOR_OTHER`` on a
    receivable and the owner's own share as the entry's residual, so it is
    offered only where both can still be written.
    """
    limit = max(1, min(int(limit), 200))
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        candidates, next_cursor = _entry_candidates(
            connection, currency=None, direction="OUT", search=search, anchor=None,
            cursor=cursor, limit=limit, without_owner_share=True,
        )
        return {"generation": token, "candidates": candidates, "next_cursor": next_cursor}
    finally:
        connection.close()
