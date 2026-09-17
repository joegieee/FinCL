"""Replay ``RESERVE_POLICY_SET`` into the schema-25 ``reserve_policy`` singleton.

Replay applies records in journal order, so the last decision seen is the one
left standing. No earlier decision is deleted from the journal; only the
projected singleton changes, which is what makes "what was the policy when this
forecast was issued" answerable from the journal rather than from this row.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def apply_reserve_policy_decision(
    connection: sqlite3.Connection,
    *,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
    decided_at: str,
    policy_version: str,
) -> None:
    connection.execute(
        """
        INSERT INTO reserve_policy (id, months, journal_seq, decided_at, policy_version)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            months = excluded.months,
            journal_seq = excluded.journal_seq,
            decided_at = excluded.decided_at,
            policy_version = excluded.policy_version
        """,
        (str(payload["months"]), journal_seq, decided_at, policy_version),
    )


__all__ = ["apply_reserve_policy_decision"]
