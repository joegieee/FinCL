"""Replay the ``SINKING_FUND_*`` kinds into schema 21.

Each record projects one row and nothing else.  In particular a contribution
writes only to ``sinking_fund_allocations`` — no balance, no total, no derived
"saved" figure is stored anywhere, because every one of those would be a second
place for cash to live and a chance for the three quantities to disagree.  What
is earmarked is read back as the sum of the non-retired allocations minus the
release, computed on read in :mod:`fincl.funds.queries`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    SINKING_FUND_ALLOCATION_RETIRE,
    SINKING_FUND_ALLOCATION_SET,
    SINKING_FUND_RELEASE_SET,
    SINKING_FUND_SET,
)


def apply_fund_decision(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
) -> None:
    fund = anchors["fund"]
    if decision_kind == SINKING_FUND_SET:
        existing = connection.execute(
            "SELECT currency FROM sinking_funds WHERE fund = ?", (fund,)
        ).fetchone()
        if existing is not None and existing["currency"] != payload["currency"]:
            raise FinCLError(
                f"sinking fund {fund} is denominated in {existing['currency']} and "
                "cannot change currency; unlike currencies are never added"
            )
        amount = (
            ExactMoney.parse(str(payload["amount_due"]))
            if payload["amount_due"] is not None
            else None
        )
        connection.execute(
            """
            INSERT INTO sinking_funds
                (fund, name, currency, amount_due_coefficient, amount_due_scale,
                 due_on, amount_basis, date_basis, source_reference,
                 contribution_dates_json, linked_event_id, journal_seq)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fund) DO UPDATE SET
                name = excluded.name,
                amount_due_coefficient = excluded.amount_due_coefficient,
                amount_due_scale = excluded.amount_due_scale,
                due_on = excluded.due_on,
                amount_basis = excluded.amount_basis,
                date_basis = excluded.date_basis,
                source_reference = excluded.source_reference,
                contribution_dates_json = excluded.contribution_dates_json,
                linked_event_id = excluded.linked_event_id,
                journal_seq = excluded.journal_seq
            """,
            (
                fund,
                str(payload["name"]).strip(),
                str(payload["currency"]),
                amount.coefficient if amount else None,
                amount.scale if amount else None,
                payload["due_on"],
                str(payload["amount_basis"]),
                str(payload["date_basis"]),
                str(payload["source_reference"]),
                json.dumps(list(payload["contribution_dates"])),
                payload["linked_event_id"],
                journal_seq,
            ),
        )
        return

    if decision_kind == SINKING_FUND_ALLOCATION_SET:
        if connection.execute(
            "SELECT 1 FROM sinking_funds WHERE fund = ?", (fund,)
        ).fetchone() is None:
            raise FinCLError(f"contribution references unknown sinking fund {fund}")
        amount = ExactMoney.parse(str(payload["amount"]))
        connection.execute(
            """
            INSERT INTO sinking_fund_allocations
                (fund, allocation, allocated_on, amount_coefficient, amount_scale,
                 journal_seq, retired)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(fund, allocation) DO UPDATE SET
                allocated_on = excluded.allocated_on,
                amount_coefficient = excluded.amount_coefficient,
                amount_scale = excluded.amount_scale,
                journal_seq = excluded.journal_seq,
                retired = 0
            """,
            (
                fund,
                anchors["allocation"],
                str(payload["allocated_on"]),
                amount.coefficient,
                amount.scale,
                journal_seq,
            ),
        )
        return

    if decision_kind == SINKING_FUND_ALLOCATION_RETIRE:
        updated = connection.execute(
            "UPDATE sinking_fund_allocations SET retired = 1, journal_seq = ? "
            "WHERE fund = ? AND allocation = ?",
            (journal_seq, fund, anchors["allocation"]),
        ).rowcount
        if updated == 0:
            raise FinCLError(
                f"contribution {anchors['allocation']} was retired before it was recorded"
            )
        return

    if decision_kind == SINKING_FUND_RELEASE_SET:
        if connection.execute(
            "SELECT 1 FROM sinking_funds WHERE fund = ?", (fund,)
        ).fetchone() is None:
            raise FinCLError(f"release references unknown sinking fund {fund}")
        amount = ExactMoney.parse(str(payload["amount"]))
        # The primary key is what enforces "released exactly once": a second
        # release for the same fund replaces the first rather than adding to it.
        connection.execute(
            """
            INSERT INTO sinking_fund_releases
                (fund, released_on, amount_coefficient, amount_scale, journal_seq)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fund) DO UPDATE SET
                released_on = excluded.released_on,
                amount_coefficient = excluded.amount_coefficient,
                amount_scale = excluded.amount_scale,
                journal_seq = excluded.journal_seq
            """,
            (
                fund,
                str(payload["released_on"]),
                amount.coefficient,
                amount.scale,
                journal_seq,
            ),
        )
        return

    raise FinCLError(  # pragma: no cover - guarded by the dispatcher
        f"{decision_kind} is not a sinking-fund decision"
    )


__all__ = ["apply_fund_decision"]
