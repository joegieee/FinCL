"""Command-API decision orchestration (MVP M1b §§2, 5).

``record_decision`` is the one idempotent way to turn a client request into a
journalled decision: under the ledger writer lock it replays the journal, checks
the ``command_receipts`` projection for the request id, and only then appends —
carrying a ``{request_id, intent_hash}`` envelope — and refreshes. A retry with
the same id after a crash finds the durable prior append and returns its receipt
instead of appending a duplicate; the same id with a different intent is
refused.

The HTTP surface that calls this (``api.py``) and the preview endpoint are M2/M3
work and are not part of this module yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .db import connect_sqlite, initialize_schema
from .decision_journal import append_decision, default_journal_path
from .decisions import replay_journal
from .errors import FinCLError
from .generation import current_generation
from .locking import ledger_writer
from .refresh import rebuild_projection


class RequestIdConflict(FinCLError):
    """The request id is already bound to a different intent (409)."""


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    request_id: str
    intent_hash: str
    journal_seq: int
    record_hash: str
    decision_kind: str
    # False: this call made the durable append. True: an earlier append with
    # this request id and intent was found and reused.
    appended: bool
    generation_token: str | None
    # True when the journal append is durable but the projection refresh failed
    # (the §2 `recorded_refresh_pending` outcome). A later `fincl refresh`
    # converges; the receipt makes a retry idempotent in the meantime.
    refresh_pending: bool = False


def record_decision(
    database_path: Path,
    *,
    request_id: str,
    intent_hash: str,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, object],
    policy_version: str,
    snapshot: str,
    anchor_version: str = "natural-anchor-v1",
    refresh: bool = True,
) -> DecisionReceipt:
    """Append ``decision_kind`` once for ``request_id`` and refresh the projection."""
    if not request_id or not intent_hash:
        raise FinCLError("record_decision needs a request_id and an intent_hash")
    with ledger_writer(database_path):
        connection = connect_sqlite(database_path)
        try:
            initialize_schema(connection)
            # Rebuild command_receipts from the durable journal before deciding.
            replay_journal(connection)
            existing = connection.execute(
                "SELECT request_id, intent_hash, journal_seq, record_hash, decision_kind "
                "FROM command_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["intent_hash"] != intent_hash:
                    raise RequestIdConflict(
                        f"request id {request_id} is already bound to a different intent"
                    )
                generation = current_generation(connection)
                return DecisionReceipt(
                    request_id=existing["request_id"],
                    intent_hash=existing["intent_hash"],
                    journal_seq=int(existing["journal_seq"]),
                    record_hash=existing["record_hash"],
                    decision_kind=existing["decision_kind"],
                    appended=False,
                    generation_token=(
                        generation["generation_token"] if generation is not None else None
                    ),
                )

            record = append_decision(
                default_journal_path(database_path),
                decision_kind=decision_kind,
                anchors=anchors,
                payload=payload,
                policy_version=policy_version,
                snapshot=snapshot,
                anchor_version=anchor_version,
                command={"request_id": request_id, "intent_hash": intent_hash},
            )
            # The append is durable now. Project the receipt so a retry is
            # idempotent even if the refresh below fails.
            replay_journal(connection)

            token: str | None = None
            refresh_failed = False
            if refresh:
                try:
                    _, token = rebuild_projection(connection)
                except Exception:
                    refresh_failed = True

            return DecisionReceipt(
                request_id=request_id,
                intent_hash=intent_hash,
                journal_seq=record.seq,
                record_hash=record.record_hash,
                decision_kind=decision_kind,
                appended=True,
                generation_token=token,
                refresh_pending=refresh_failed,
            )
        finally:
            connection.close()
