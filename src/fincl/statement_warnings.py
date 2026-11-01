"""Warnings that only appear when statements are read against each other.

Each statement reconciles alone, so neither of these can refuse one: statements
arrive in any order, and the fault may be in the other statement or in a period
not yet imported. They are recomputed on every refresh, so a warning goes away
once its cause does.

A partial overlap leaves shared rows counted twice until they are resolved in
the duplicate review. A card statement that does not open on the previous
statement's close has lost or gained money between the two.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta

from .common import canonical_json as _json
from .common import utc_now as _now
from .money import ExactMoney, format_decimal
from .supersession import _period, _rows

__all__ = ["CARD_BALANCE_BREAK", "STATEMENT_PERIOD_OVERLAP", "refresh_statement_warnings"]

STATEMENT_PERIOD_OVERLAP = "STATEMENT_PERIOD_OVERLAP"
CARD_BALANCE_BREAK = "CARD_BALANCE_BREAK"

_Warning = tuple[str, str, str, str]


def _selected_by_account(connection: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    accounts: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT s.id, s.account_id, s.extraction_run_id, s.period_start, s.period_end
        FROM statements s JOIN extraction_runs x ON x.id = s.extraction_run_id
        WHERE s.selection_state = 'SELECTED' AND x.status = 'ACCEPTED'
          AND s.period_start IS NOT NULL AND s.period_end IS NOT NULL
        ORDER BY s.period_start, s.period_end, s.id
        """
    ):
        accounts[row["account_id"]].append(row)
    return accounts


def _overlaps(connection: sqlite3.Connection, statements: list[sqlite3.Row]) -> list[_Warning]:
    warnings: list[_Warning] = []
    for index, earlier in enumerate(statements):
        for later in statements[index + 1:]:
            start, end = later["period_start"], min(earlier["period_end"], later["period_end"])
            if start > end:
                break
            shared = sum((
                Counter(row for row, _ in _rows(connection, earlier["id"], start, end))
                & Counter(row for row, _ in _rows(connection, later["id"], start, end))
            ).values())
            if shared:
                warnings.append((
                    later["extraction_run_id"],
                    STATEMENT_PERIOD_OVERLAP,
                    "this statement shares rows with another one; they count twice "
                    "until confirmed in the duplicate review",
                    _json({"period": _period(earlier), "shared_rows": shared}),
                ))
    return warnings


def _liability(
    connection: sqlite3.Connection, statement_id: str, role: str
) -> dict[str, ExactMoney]:
    return {
        row["currency_pocket"]: ExactMoney(
            row["value_coefficient"], row["value_scale"], row["value_text"]
        )
        for row in connection.execute(
            """
            SELECT currency_pocket, value_coefficient, value_scale, value_text
            FROM card_statement_facts WHERE statement_id = ? AND field_role = ?
            """,
            (statement_id, role),
        )
    }


def _card_breaks(connection: sqlite3.Connection, statements: list[sqlite3.Row]) -> list[_Warning]:
    warnings: list[_Warning] = []
    for previous, current in zip(statements, statements[1:], strict=False):
        next_day = date.fromisoformat(previous["period_end"]) + timedelta(days=1)
        if current["period_start"] != next_day.isoformat():
            continue
        closes = _liability(connection, previous["id"], "closing_liability")
        opens = _liability(connection, current["id"], "opening_liability")
        for pocket in sorted(closes.keys() & opens.keys()):
            close, opening = closes[pocket], opens[pocket]
            scale = max(close.scale, opening.scale)
            if close.rescaled_coefficient(scale) != opening.rescaled_coefficient(scale):
                warnings.append((
                    current["extraction_run_id"],
                    CARD_BALANCE_BREAK,
                    "this card statement does not open on the previous statement's closing balance",
                    _json({
                        "currency": pocket,
                        "period": _period(previous),
                        "previous_close": format_decimal(close.rescaled_coefficient(scale), scale),
                        "opening": format_decimal(opening.rescaled_coefficient(scale), scale),
                    }),
                ))
    return warnings


def refresh_statement_warnings(connection: sqlite3.Connection) -> None:
    """Bring the stored warnings in line with the selected statements.

    Idempotent: a warning that still holds keeps its row.
    """
    wanted: list[_Warning] = []
    for statements in _selected_by_account(connection).values():
        wanted += _overlaps(connection, statements)
        wanted += _card_breaks(connection, statements)
    current = {(run, code, context) for run, code, _, context in wanted}
    stored = set()
    for row in connection.execute(
        "SELECT id, extraction_run_id, code, context_json FROM extraction_issues "
        "WHERE code IN (?, ?)",
        (STATEMENT_PERIOD_OVERLAP, CARD_BALANCE_BREAK),
    ).fetchall():
        key = (row["extraction_run_id"], row["code"], row["context_json"])
        if key in current:
            stored.add(key)
        else:
            connection.execute("DELETE FROM extraction_issues WHERE id = ?", (row["id"],))
    for run, code, message, context in wanted:
        if (run, code, context) not in stored:
            connection.execute(
                """
                INSERT INTO extraction_issues(
                    extraction_run_id, raw_record_id, severity, code, message, context_json, created_at
                ) VALUES (?, NULL, 'WARNING', ?, ?, ?, ?)
                """,
                (run, code, message, context, _now()),
            )
