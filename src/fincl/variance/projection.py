"""Replay the ``FORECAST_SNAPSHOT_ISSUE`` / ``FORECAST_MATCH_*`` kinds into schema 22.

A snapshot projects as the journalled document verbatim plus its digest.  No
figure inside it is copied into a column of its own, so there is nowhere for a
stored number to drift from the bytes that were issued, and nothing in the
codebase can write a snapshot row except a replay of a journal record.

Re-issuing a snapshot id is refused rather than merged or ignored.  That refusal
is the whole immutability guarantee: a later recalculation is a *different*
forecast and gets its own id, and an attempt to file it under an existing one
fails loudly instead of quietly replacing the numbers a decision was made on.

A match is projected only against a snapshot that exists and an event that
snapshot actually issued, and only when no live match already binds the same
movement to the same event.  Binding one movement to one event twice would count
the same cash twice, which is precisely the arithmetic a variance report exists
to expose.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    FORECAST_MATCH_RETIRE,
    FORECAST_MATCH_SET,
    FORECAST_SNAPSHOT_ISSUE,
    canonical_document,
    snapshot_digest,
)


def _snapshot_document(
    connection: sqlite3.Connection, snapshot: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT document_json FROM forecast_snapshots WHERE snapshot = ?", (snapshot,)
    ).fetchone()
    if row is None:
        return None
    document: dict[str, Any] = json.loads(row["document_json"])
    return document


def apply_variance_decision(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
) -> None:
    snapshot = anchors["snapshot"]
    if decision_kind == FORECAST_SNAPSHOT_ISSUE:
        document = payload["document"]
        existing = connection.execute(
            "SELECT document_digest FROM forecast_snapshots WHERE snapshot = ?",
            (snapshot,),
        ).fetchone()
        if existing is not None:
            raise FinCLError(
                f"forecast snapshot {snapshot} has already been issued; an issued "
                "snapshot is immutable and is never re-stated. Issue the "
                "recalculated forecast under a new snapshot id"
            )
        connection.execute(
            """
            INSERT INTO forecast_snapshots
                (snapshot, issued_at, document_json, document_digest, journal_seq)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                snapshot,
                str(document["issued_at"]),
                canonical_document(document),
                snapshot_digest(document),
                journal_seq,
            ),
        )
        return

    if decision_kind == FORECAST_MATCH_SET:
        document = _snapshot_document(connection, snapshot)
        if document is None:
            raise FinCLError(
                f"match references unknown forecast snapshot {snapshot}"
            )
        event_id = str(payload["event_id"])
        if not any(str(item["event_id"]) == event_id for item in document["events"]):
            raise FinCLError(
                f"forecast snapshot {snapshot} issued no cash event {event_id}; a "
                "match is recorded against the forecast as issued, never against "
                "an event a later recalculation would have produced"
            )
        movement_id = str(payload["movement_id"])
        clash = connection.execute(
            "SELECT match FROM forecast_actual_matches "
            "WHERE snapshot = ? AND event_id = ? AND movement_id = ? "
            "AND retired = 0 AND match <> ?",
            (snapshot, event_id, movement_id, anchors["match"]),
        ).fetchone()
        if clash is not None:
            raise FinCLError(
                f"match {clash['match']} already binds movement {movement_id} to "
                f"event {event_id}; binding the same cash twice would count it twice"
            )
        amount = ExactMoney.parse(str(payload["amount"]))
        connection.execute(
            """
            INSERT INTO forecast_actual_matches
                (snapshot, match, event_id, movement_id, matched_on,
                 amount_coefficient, amount_scale, match_kind, journal_seq, retired)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(snapshot, match) DO UPDATE SET
                event_id = excluded.event_id,
                movement_id = excluded.movement_id,
                matched_on = excluded.matched_on,
                amount_coefficient = excluded.amount_coefficient,
                amount_scale = excluded.amount_scale,
                match_kind = excluded.match_kind,
                journal_seq = excluded.journal_seq,
                retired = 0
            """,
            (
                snapshot,
                anchors["match"],
                event_id,
                movement_id,
                str(payload["matched_on"]),
                amount.coefficient,
                amount.scale,
                str(payload["match_kind"]),
                journal_seq,
            ),
        )
        return

    if decision_kind == FORECAST_MATCH_RETIRE:
        updated = connection.execute(
            "UPDATE forecast_actual_matches SET retired = 1, journal_seq = ? "
            "WHERE snapshot = ? AND match = ?",
            (journal_seq, snapshot, anchors["match"]),
        ).rowcount
        if updated == 0:
            raise FinCLError(
                f"match {anchors['match']} was retired before it was recorded"
            )
        return

    raise FinCLError(  # pragma: no cover - guarded by the dispatcher
        f"{decision_kind} is not a forecast-variance decision"
    )


__all__ = ["apply_variance_decision"]
