"""Coverage gaps expressed as the exact actions that would close them.

``position_page_from`` already names *why* a pocket has no coverage-backed
balance. That is a diagnosis, not a next step: an operator reading "complete
actual-movement coverage is not established through this date" still has to
work out which account, which days, and what to do about it.

This module turns each gap into a named remedy carrying the parameters a UI
needs to act — the pocket, the date range, and the decision kind it would
record — ranked by how much coverage the action unlocks. It computes nothing
financial: every figure it quotes is copied from the position page it was given,
and every remedy points at either an import or one of the two journalled
evidence kinds (``BALANCE_OBSERVATION_SET``, ``MOVEMENT_COVERAGE_CERTIFY``) that
supply completeness or an observed balance without ever inventing an amount.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from .queries import position_page_from

__all__ = [
    "coverage_remedies",
    "coverage_remedies_from",
    "movement_certifications",
    "movement_certifications_from",
]

# A remedy is ranked by what it unlocks, not by how easy it is: closing the one
# pocket that blocks the shared cutoff restores the complete position, while
# closing a pocket that is already contributing only makes it more current.
_RANK = {
    "unblocks-shared-cutoff": 0,
    "adds-a-pocket": 1,
    "brings-forward": 2,
}


def _remedy(
    *,
    action: str,
    decision_kind: str | None,
    pocket: dict[str, Any],
    unlocks: str,
    detail: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "action": action,
        "decision_kind": decision_kind,
        "account_id": pocket["account_id"],
        "currency_pocket": pocket["currency_pocket"],
        "display_name": pocket.get("display_name"),
        "institution": pocket.get("institution"),
        "unlocks": unlocks,
        "rank": _RANK[unlocks],
        "detail": detail,
        "parameters": parameters,
    }


def _gap_remedies(
    pocket: dict[str, Any], *, today: str, unlocks: str
) -> list[dict[str, Any]]:
    """The remedies for one pocket that cannot be proven through ``today``.

    Both offered actions are evidence, and they answer different questions. A
    balance observation answers "what was actually in the account on this day",
    which re-anchors the pocket and makes every day after the gap irrelevant. A
    coverage certification answers "nothing happened in this range", which
    bridges the gap while keeping the earlier observed balance as the anchor.
    Neither one lets an operator type a balance the account never had: the first
    is a figure they read off the account, the second carries no figure at all.
    """
    start = pocket.get("best_supported_cutoff") or pocket.get("as_of")
    reason = pocket.get("blocking_reason") or "this pocket has no coverage-backed balance today"
    remedies = [
        _remedy(
            action="record-balance-observation",
            decision_kind="BALANCE_OBSERVATION_SET",
            pocket=pocket,
            unlocks=unlocks,
            detail=(
                "Read today's balance off the account and record it as a dated "
                "observation. It re-anchors this pocket, so the unproven days "
                f"before it stop mattering. Blocked because: {reason}"
            ),
            parameters={"suggested_observed_on": today},
        )
    ]
    if start and start < today:
        remedies.append(
            _remedy(
                action="certify-quiet-interval",
                decision_kind="MOVEMENT_COVERAGE_CERTIFY",
                pocket=pocket,
                unlocks=unlocks,
                detail=(
                    f"Check the account's own transaction list from {start} to "
                    f"{today}. If it shows no activity, certify the range and the "
                    "existing balance carries forward unchanged."
                ),
                parameters={"period_start": start, "period_end": today},
            )
        )
    if pocket.get("balance") is None:
        remedies.append(
            _remedy(
                action="import-statement",
                decision_kind=None,
                pocket=pocket,
                unlocks=unlocks,
                detail=(
                    "This pocket has no balance boundary at all. Import a "
                    "statement for it, or record an observed balance."
                ),
                parameters={},
            )
        )
    return remedies


def coverage_remedies_from(
    connection: sqlite3.Connection, *, today: date
) -> dict[str, Any]:
    page = position_page_from(connection, today=today)
    iso = today.isoformat()
    partial = page["partial_position"]
    short_keys = {
        (entry["account_id"], entry["currency_pocket"]) for entry in partial["short_pockets"]
    }
    remedies: list[dict[str, Any]] = []
    for pocket in page["included_pockets"]:
        key = (pocket["account_id"], pocket["currency_pocket"])
        if key in short_keys:
            # This pocket is missing from the partial total too: closing it both
            # restores the shared cutoff and adds money to the figure.
            remedies.extend(_gap_remedies(pocket, today=iso, unlocks="unblocks-shared-cutoff"))
        elif pocket.get("best_supported_cutoff") != iso:
            # Already counted, but at an older date than today.
            remedies.extend(_gap_remedies(pocket, today=iso, unlocks="brings-forward"))
    remedies.sort(key=lambda item: (item["rank"], item["account_id"], item["currency_pocket"]))
    return {
        "generated_for": iso,
        "coverage": page["coverage_summary"],
        "partial_position": partial,
        "supported_cutoff": page["supported_cutoff"],
        "current_position_supported": page["current_position_supported"],
        "remedies": remedies,
    }


def coverage_remedies(database: Path, *, today: date) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return coverage_remedies_from(connection, today=today)
    finally:
        connection.close()


def movement_certifications_from(connection: sqlite3.Connection) -> dict[str, Any]:
    """Every active certification, so one can be reviewed and retired."""
    rows = connection.execute(
        """
        SELECT c.certification, c.account_id, c.currency_pocket, c.period_start,
               c.period_end, c.basis, a.display_name, a.institution
        FROM movement_coverage_certifications c
        LEFT JOIN accounts a ON a.id = c.account_id
        WHERE c.retired = 0
        ORDER BY c.period_start, c.period_end, c.certification
        """
    ).fetchall()
    return {
        "certifications": [
            {
                "certification": row["certification"],
                "account_id": row["account_id"],
                "currency_pocket": row["currency_pocket"],
                "display_name": row["display_name"],
                "institution": row["institution"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "basis": row["basis"],
            }
            for row in rows
        ]
    }


def movement_certifications(database: Path) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return movement_certifications_from(connection)
    finally:
        connection.close()
