"""Snapshot-only reads for the Cards screens (v0.4 slice 1).

Every figure is an observed statement fact or the printed source equation; there
is no stored balance and no timeless "current balance" label. Reconciliation
status and missing statement periods are surfaced alongside every card so a
partial export never reads as a complete picture.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..money import format_decimal
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


def _money_fraction(shape: dict[str, Any] | None) -> Fraction | None:
    if shape is None or shape.get("kind") != "money":
        return None
    try:
        return Fraction(shape["value"])
    except (ValueError, ZeroDivisionError):
        return None


def _utilization(
    *,
    limit: dict[str, Any] | None,
    liability: dict[str, Any] | None,
    limit_currency: str | None,
    liability_currency: str | None,
    basis_label: str,
) -> dict[str, Any]:
    """Utilization only for a positive limit and a same-currency liability basis.

    An unknown or mismatched limit currency means "unavailable". A credit
    balance (negative liability) is shown distinctly, never as a negative amount
    owed. The ratio may be capped for a bar width by the caller, but the text
    preserves an over-limit amount.
    """
    limit_value = _money_fraction(limit)
    liability_value = _money_fraction(liability)
    if limit_value is None or liability_value is None:
        return {"available": False, "reason": "limit or liability basis not evidenced"}
    if limit_currency is None or limit_currency != liability_currency:
        return {"available": False, "reason": "limit and liability currency do not match"}
    if limit_value <= 0:
        return {"available": False, "reason": "limit is not positive"}
    if liability_value < 0:
        return {
            "available": True,
            "basis": basis_label,
            "currency": limit_currency,
            "credit_balance": True,
            "credit_balance_amount": format_decimal(int(-liability_value * 100), 2),
            "ratio": "0.0000",
            "over_limit": False,
        }
    ratio = liability_value / limit_value
    headroom = limit_value - liability_value
    return {
        "available": True,
        "basis": basis_label,
        "currency": limit_currency,
        "credit_balance": False,
        "ratio": format_decimal(int(ratio * 10000), 4),
        "over_limit": liability_value > limit_value,
        "over_limit_amount": (
            format_decimal(int((liability_value - limit_value) * 100), 2)
            if liability_value > limit_value
            else None
        ),
        "headroom": format_decimal(int(headroom * 100), 2),
    }


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
    """Facility/limit basis, statement activity, due fields and utilization.

    The limit basis is a dated `CARD_LIMIT_OBSERVATION_SET` on/before the as-of
    date when a facility exists; otherwise it falls back to what the latest
    statement printed, and utilization is then labelled "statement utilization".
    A limit is never FX-converted to another currency.
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

        facility_row = projection.facility_for_account(connection, account_id)
        facility: dict[str, Any] | None = None
        limit_observation: dict[str, Any] | None = None
        if facility_row is not None:
            facility = {
                "facility_id": str(facility_row["facility_id"]),
                "label": str(facility_row["label"]),
                "currency": str(facility_row["currency"]),
                "bound": bool(facility_row["bound"]),
                # The member cards, so an edit starts from the current set.
                "account_ids": [str(row[0]) for row in connection.execute(
                    "SELECT account_id FROM card_facility_accounts WHERE facility_id=? ORDER BY account_id",
                    (str(facility_row["facility_id"]),),
                )],
            }
            limit_observation = projection.limit_as_of(
                connection, str(facility_row["facility_id"]), as_of
            )

        printed_limit = summary["credit_limit"] if summary is not None else None
        closing = summary["closing_liability"] if summary is not None else None
        limit_basis: dict[str, Any] | None
        if limit_observation is not None:
            limit_basis = {
                "kind": "money",
                "value": limit_observation["limit"]["value"],
                "currency": limit_observation["limit"]["currency"],
                "observed_on": limit_observation["observed_on"],
                "source_kind": limit_observation["source_kind"],
            }
            util = _utilization(
                limit=limit_basis,
                liability=closing,
                limit_currency=limit_observation["limit"]["currency"],
                liability_currency=(closing or {}).get("currency"),
                basis_label="observed limit + statement-close liability",
            )
        else:
            limit_basis = printed_limit
            util = _utilization(
                limit=printed_limit,
                liability=closing,
                limit_currency=(printed_limit or {}).get("currency"),
                liability_currency=(closing or {}).get("currency"),
                basis_label="statement utilization",
            )

        return {
            "generation": token,
            "as_of": as_of,
            "account": _account_shape(account),
            "facility": facility,
            "limit_basis": limit_basis,
            "limit_observation": limit_observation,
            # How you said this card is repaid; None when nothing is declared.
            "repayment_plan": projection.repayment_plan(connection, account_id),
            # Each statement's amount due beside what was paid toward it, and
            # where that figure comes from (a manual match, or the card's own
            # credits between the period end and the due date).
            "repayments": _repayments(connection, account_id),
            "utilization": util,
            "latest_statement": summary,
            "statement_activity": views,
            "coverage_gaps": projection.coverage_gaps(statements),
            "statements_missing_period": _missing_period_statements(statements),
            "manually_allocated_payments": projection.manual_payment_allocations(
                connection, account_id
            ),
        }
    finally:
        connection.close()


__all__ = ["cards_page", "card_statements", "card_detail"]


def _repayments(connection: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    from .cash_conversion import _pockets, _statement_rows

    rows: list[dict[str, Any]] = []
    for pocket in _pockets(connection, account_id):
        for row in _statement_rows(connection, account_id, pocket):
            if row["due"] is None:
                continue
            rows.append({
                "statement_id": row["statement_id"], "statement_date": row["statement_date"],
                "currency": pocket, "due": row["due"], "paid": row["paid"],
                "payment_due_date": row["payment_due_date"], "paid_basis": row["paid_basis"],
                "paid_in_full": Decimal(row["paid"]) >= Decimal(row["due"]),
            })
    rows.sort(key=lambda item: str(item["statement_date"]), reverse=True)
    return rows
