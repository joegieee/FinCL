"""How fresh each account's evidence is, and which accounts hold the start back.

The forecast opens on one date shared by every liquid pocket: the latest day
all of them can prove. One account behind the others pulls that date back for
everyone, and nothing in the figure itself says which. This read names, per
account, the furthest day its own evidence proves, how old that is, what proves
it, and whether it is the reason the shared start is not later.

Every figure is copied from the position page; nothing is recomputed here.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from .models import STALE_AFTER_DAYS
from .queries import position_page_from

__all__ = ["account_freshness", "account_freshness_from"]

BASIS_STATEMENT = "statement"
BASIS_OBSERVATION = "observation"
BASIS_CERTIFICATION = "certification"


def _certified_on(
    connection: sqlite3.Connection, account_id: str, currency: str, day: str
) -> bool:
    return connection.execute(
        """
        SELECT 1 FROM movement_coverage_certifications
        WHERE account_id = ? AND currency_pocket = ? AND retired = 0
          AND period_start <= ? AND period_end >= ?
        LIMIT 1
        """,
        (account_id, currency, day, day),
    ).fetchone() is not None


def _basis(
    connection: sqlite3.Connection, pocket: dict[str, Any], day: str
) -> str:
    """What proves the pocket's balance on ``day``, in the reader's words."""
    if pocket.get("best_supported_basis") == "manual-observation":
        return BASIS_OBSERVATION
    if pocket.get("best_supported_basis") == "actual-movement-bridge" and _certified_on(
        connection, pocket["account_id"], pocket["currency_pocket"], day
    ):
        return BASIS_CERTIFICATION
    return BASIS_STATEMENT


def account_freshness_from(connection: sqlite3.Connection, *, today: date) -> dict[str, Any]:
    page = position_page_from(connection, today=today)
    cutoff = page["supported_cutoff"]
    short = {
        (entry["account_id"], entry["currency_pocket"])
        for entry in page["partial_position"]["short_pockets"]
    }
    by_account: dict[str, list[dict[str, Any]]] = {}
    for pocket in page["included_pockets"]:
        by_account.setdefault(pocket["account_id"], []).append(pocket)

    weakest_by_account: dict[str, dict[str, Any]] = {}
    for account_id, pockets in by_account.items():
        # An account is as fresh as its least fresh pocket.
        unproven = [pocket for pocket in pockets if pocket["best_supported_cutoff"] is None]
        weakest_by_account[account_id] = unproven[0] if unproven else min(
            pockets, key=lambda pocket: (pocket["best_supported_cutoff"], pocket["currency_pocket"])
        )
    oldest = min(
        (p["best_supported_cutoff"] for p in weakest_by_account.values() if p["best_supported_cutoff"]),
        default=None,
    )

    accounts: list[dict[str, Any]] = []
    for account_id, pockets in sorted(by_account.items()):
        weakest = weakest_by_account[account_id]
        through = weakest["best_supported_cutoff"]
        age = (today - date.fromisoformat(through)).days if through else None
        if cutoff is None:
            holds_back = any((account_id, p["currency_pocket"]) in short for p in pockets)
        else:
            # The shared start is never later than the least fresh account.
            holds_back = cutoff < today.isoformat() and through == oldest
        archived_on = weakest.get("archived_on")
        accounts.append({
            "account": account_id,
            "display_name": weakest["display_name"] or account_id,
            "currencies": sorted(pocket["currency_pocket"] for pocket in pockets),
            "evidence_through": through,
            "age_days": age,
            "stale": age is None or age > STALE_AFTER_DAYS,
            "basis": _basis(connection, weakest, through) if through else None,
            "holds_back_start": holds_back,
            "archived_on": archived_on,
            "closed_with": [
                {"currency_pocket": pocket["currency_pocket"], "balance": pocket["balance"]}
                for pocket in pockets if archived_on and pocket["balance"] is not None
            ],
        })
    return {
        "generated_for": today.isoformat(),
        "stale_after_days": STALE_AFTER_DAYS,
        "supported_cutoff": cutoff,
        "accounts": accounts,
    }


def account_freshness(database: Path, *, today: date) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return account_freshness_from(connection, today=today)
    finally:
        connection.close()
