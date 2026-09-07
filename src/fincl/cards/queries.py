"""Snapshot-only reads for the Cards screens (v0.4 slice 1).

Every figure is an observed statement fact or the printed source equation; there
is no stored balance and no timeless "current balance" label. Reconciliation
status and missing statement periods are surfaced alongside every card so a
partial export never reads as a complete picture.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from . import projection


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


def _account_shape(row: Any) -> dict[str, Any]:
    return {
        "account_id": str(row["id"]),
        "institution": str(row["institution"]),
        "source_type": str(row["source_type"]),
        "display_name": str(row["display_name"]),
        "masked_identifier": str(row["masked_identifier"]),
    }


def _missing_period_statements(statements: list[Any]) -> list[str]:
    return [
        str(s["id"])
        for s in statements
        if s["period_start"] is None or s["period_end"] is None
    ]


def cards_page(
    database: Path,
    *,
    as_of: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    expected: str | None = None,
) -> dict[str, Any]:
    """The Cards landing page: one row per card account with its latest
    observed statement, coverage gaps and reconciliation status."""
    as_of = _check_as_of(as_of)
    limit = max(1, min(int(limit), 200))
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        accounts = projection.selected_card_accounts(connection)
        if cursor is not None:
            accounts = [a for a in accounts if str(a["id"]) > cursor]
        next_cursor = (
            str(accounts[limit]["id"]) if len(accounts) > limit else None
        )
        accounts = accounts[:limit]
        cards: list[dict[str, Any]] = []
        for account in accounts:
            statements = [
                s
                for s in projection.selected_statements(connection, str(account["id"]))
                if str(s["period_start"] or s["statement_date"]) <= as_of
                or s["period_start"] is None
            ]
            summary = projection.latest_statement_summary(connection, statements)
            cards.append(
                {
                    "account": _account_shape(account),
                    "latest_statement": summary,
                    "statement_count": len(statements),
                    "coverage_gaps": projection.coverage_gaps(statements),
                    "statements_missing_period": _missing_period_statements(statements),
                    "installment_obligations": [],  # slice 3
                }
            )
        return {
            "generation": token,
            "as_of": as_of,
            "cards": cards,
            "next_cursor": next_cursor,
        }
    finally:
        connection.close()


def card_statements(
    database: Path,
    account_id: str,
    *,
    cursor: str | None = None,
    limit: int = 50,
    expected: str | None = None,
) -> dict[str, Any]:
    """Selected statements for one card with coverage and source equations."""
    limit = max(1, min(int(limit), 200))
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        account = connection.execute(
            "SELECT id, institution, source_type, display_name, masked_identifier "
            "FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if account is None:
            raise FinCLError("that card account does not exist")
        statements = projection.selected_statements(connection, account_id)
        page = statements
        if cursor is not None:
            page = [s for s in statements if str(s["id"]) > cursor]
        page = sorted(page, key=lambda s: str(s["id"]))
        next_cursor = str(page[limit]["id"]) if len(page) > limit else None
        page = page[:limit]
        return {
            "generation": token,
            "account": _account_shape(account),
            "statements": [
                projection.statement_view(connection, account_id, s) for s in page
            ],
            "coverage_gaps": projection.coverage_gaps(statements),
            "statements_missing_period": _missing_period_statements(statements),
            "next_cursor": next_cursor,
        }
    finally:
        connection.close()


def card_detail(
    database: Path,
    account_id: str,
    *,
    as_of: str | None = None,
    expected: str | None = None,
) -> dict[str, Any]:
    """Facility/limit basis, statement activity and due fields for one card.

    Slice 1 has no facility or limit-observation journal kinds, so the limit
    basis is whatever the latest statement printed, stamped with its period end.
    """
    as_of = _check_as_of(as_of)
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        account = connection.execute(
            "SELECT id, institution, source_type, display_name, masked_identifier "
            "FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if account is None:
            raise FinCLError("that card account does not exist")
        statements = [
            s
            for s in projection.selected_statements(connection, account_id)
            if s["period_start"] is None or str(s["period_start"]) <= as_of
        ]
        views = [
            projection.statement_view(connection, account_id, s) for s in statements
        ]
        summary = projection.latest_statement_summary(connection, statements)
        return {
            "generation": token,
            "as_of": as_of,
            "account": _account_shape(account),
            "limit_basis": (
                summary["credit_limit"] if summary is not None else None
            ),
            "latest_statement": summary,
            "statement_activity": views,
            "coverage_gaps": projection.coverage_gaps(statements),
            "statements_missing_period": _missing_period_statements(statements),
        }
    finally:
        connection.close()


__all__ = ["cards_page", "card_statements", "card_detail"]
