"""The resolved reserve policy."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from .models import DEFAULT_RESERVE_MONTHS, ReservePolicy


def reserve_policy(connection: sqlite3.Connection) -> ReservePolicy:
    """The policy singleton, defaulting to the framework's three months.

    An absent decision resolves to the default rather than an error, so every
    reader can ask for the policy unconditionally and a ledger that has never
    recorded one behaves exactly as it did before the policy existed.
    """
    row = connection.execute(
        "SELECT months, journal_seq, decided_at, policy_version FROM reserve_policy WHERE id = 1"
    ).fetchone()
    if row is None:
        return ReservePolicy(
            months=DEFAULT_RESERVE_MONTHS,
            journal_seq=None,
            decided_at=None,
            policy_version=None,
            is_default=True,
            reason=(
                "no reserve policy has been chosen; the framework default of "
                f"{DEFAULT_RESERVE_MONTHS} months of essential spending applies"
            ),
        )
    return ReservePolicy(
        months=Decimal(str(row["months"])),
        journal_seq=int(row["journal_seq"]),
        decided_at=str(row["decided_at"]),
        policy_version=str(row["policy_version"]),
        is_default=False,
        reason=None,
    )


__all__ = ["reserve_policy"]
