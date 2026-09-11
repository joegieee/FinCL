"""Replay ``REPORTING_CURRENCY_SET`` into the schema-23 ``reporting_policy``
singleton (§4).

The projection keeps exactly the latest decision: replay applies records in
journal order, so the last ``REPORTING_CURRENCY_SET`` record it sees is the one
left standing after ``INSERT ... ON CONFLICT DO UPDATE``. No prior decision is
rewritten or deleted from the journal; only the projected singleton changes.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .models import currency_definition


def apply_reporting_currency_decision(
    connection: sqlite3.Connection,
    *,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
    decided_at: str,
    policy_version: str,
) -> None:
    currency = str(payload["currency"])
    definition = currency_definition(currency)
    assert definition is not None, "validated before apply"
    connection.execute(
        """
        INSERT INTO reporting_policy (id, currency, minor_unit_scale, journal_seq, decided_at, policy_version)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            currency = excluded.currency,
            minor_unit_scale = excluded.minor_unit_scale,
            journal_seq = excluded.journal_seq,
            decided_at = excluded.decided_at,
            policy_version = excluded.policy_version
        """,
        (currency, definition.minor_unit_scale, journal_seq, decided_at, policy_version),
    )


__all__ = ["apply_reporting_currency_decision"]
