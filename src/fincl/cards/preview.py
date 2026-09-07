"""Full-state pre-append check for the ``CARD_FACILITY_*`` /
``CARD_LIMIT_OBSERVATION_*`` kinds.

``validate_card_payload`` covers shape; this applies the pending decision inside
a SAVEPOINT and runs the real ``resolve_cards`` pass, so a durable record is
never made for a decision that could not fully replay (facility exists, account
kind, membership exclusivity, facility-currency consistency, dependent limit
observations on retire).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import FinCLError
from .models import (
    CARD_FACILITY_RETIRE,
    CARD_FACILITY_SET,
    CARD_LIMIT_OBSERVATION_RETIRE,
    CARD_LIMIT_OBSERVATION_SET,
)
from .projection import _is_card_account, apply_card, resolve_cards
from .validation import validate_card_payload

_PREVIEW_SEQ = 1 << 40


def preview_card(
    connection: sqlite3.Connection,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    *,
    resolve_anchor: Any,
) -> None:
    validate_card_payload(decision_kind, anchors, payload)

    facility_exists = bool(
        "facility" in anchors
        and connection.execute(
            "SELECT 1 FROM card_facilities WHERE facility_id = ?",
            (anchors["facility"],),
        ).fetchone()
    )
    if decision_kind == CARD_FACILITY_SET:
        not_cards = [
            a for a in payload["account_ids"] if not _is_card_account(connection, a)
        ]
        if not_cards:
            raise FinCLError(
                "a facility's member accounts must all be credit-liability card "
                "accounts; these are not: " + ", ".join(sorted(not_cards))
            )
    if decision_kind == CARD_FACILITY_SET and facility_exists:
        current = connection.execute(
            "SELECT currency FROM card_facilities WHERE facility_id = ?",
            (anchors["facility"],),
        ).fetchone()
        if (
            current is not None
            and str(current["currency"]) != payload["currency"]
            and connection.execute(
                "SELECT 1 FROM card_limit_observations WHERE facility_id = ? LIMIT 1",
                (anchors["facility"],),
            ).fetchone()
        ):
            raise FinCLError(
                "cannot change a facility currency while dated limit observations "
                "exist in the old currency"
            )
    if (
        decision_kind
        in {CARD_FACILITY_RETIRE, CARD_LIMIT_OBSERVATION_SET, CARD_LIMIT_OBSERVATION_RETIRE}
        and not facility_exists
    ):
        raise FinCLError("that card facility does not exist")

    connection.execute("SAVEPOINT card_preview")
    try:
        apply_card(
            connection,
            decision_kind=decision_kind,
            anchors=anchors,
            payload=payload,
            journal_seq=_PREVIEW_SEQ,
            batch_index=None,
            resolve_anchor=resolve_anchor,
        )
        resolve_cards(connection)
    finally:
        connection.execute("ROLLBACK TO card_preview")
        connection.execute("RELEASE card_preview")
