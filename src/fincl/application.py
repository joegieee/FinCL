"""Command-API decision orchestration (MVP M1b §§2, 5).

``record_decision`` is the one idempotent way to turn a client request into a
journalled decision: under the ledger writer lock it replays the journal, checks
the ``command_receipts`` projection for the request id, and only then appends —
carrying a ``{request_id, intent_hash}`` envelope — and refreshes. A retry with
the same id after a crash finds the durable prior append and returns its receipt
instead of appending a duplicate; the same id with a different intent is
refused.

The HTTP surface in ``api.py`` validates previews, generation and domain intent
before calling this module under the shared writer context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .custom_categories import custom_categories
from .db import connect_sqlite, initialize_schema
from .decision_journal import append_decision, default_journal_path
from .decisions import replay_journal
from .errors import FinCLError
from .generation import current_generation
from .locking import WriterContext, ledger_writer
from .refresh import rebuild_projection
from .relationships import preview_batch, preview_refund_allocation


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
    writer: WriterContext | None = None,
) -> DecisionReceipt:
    """Append ``decision_kind`` once for ``request_id`` and refresh the projection."""
    if not request_id or not intent_hash:
        raise FinCLError("record_decision needs a request_id and an intent_hash")
    with ledger_writer(database_path, existing=writer):
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
                    refresh_pending=(generation is None or json.loads(generation["manifest_json"])["journal_seq"] < int(existing["journal_seq"])),
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


def record_batch(
    database_path: Path,
    *,
    request_id: str,
    intent_hash: str,
    items: list[dict[str, object]],
    reason: str,
    policy_version: str = "relationship-batch-v1",
    writer: WriterContext | None = None,
) -> DecisionReceipt:
    """Append one atomic ``DECISION_BATCH`` after an isolated final-state preview.

    The whole batch is validated against the current projection before a single
    byte is appended (Operations §1: no partial success, no durable record that
    cannot fully replay). The children then replay in array order inside the one
    refresh transaction.
    """
    with ledger_writer(database_path, existing=writer) as held:
        connection = connect_sqlite(database_path)
        try:
            initialize_schema(connection)
            replay_journal(connection)
            preview_batch(
                connection, items,
                custom_categories=frozenset(custom_categories(database_path)),
            )
        finally:
            connection.close()
        return record_decision(
            database_path,
            request_id=request_id,
            intent_hash=intent_hash,
            decision_kind="DECISION_BATCH",
            anchors={"batch": request_id},
            payload={"items": items, "reason": reason},
            policy_version=policy_version,
            snapshot=f"DECISION_BATCH: {len(items)} item(s)",
            writer=held,
        )


def record_debt_decision(
    database_path: Path,
    *,
    request_id: str,
    intent_hash: str,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, object],
    policy_version: str = "personal-debt-v1",
    writer: WriterContext | None = None,
) -> DecisionReceipt:
    """Append one ``PERSONAL_DEBT_*`` / ``ENTRY_RESIDUAL_*`` record.

    Shape validation runs in ``debts.validation`` (called by the HTTP/CLI
    preview and again during replay). Slice 2 adds a full-state ``preview_debt``
    here for chronology and component-partition checks before the append.
    """
    from .debts.validation import validate_debt_payload

    validate_debt_payload(decision_kind, anchors, payload)
    return record_decision(
        database_path,
        request_id=request_id,
        intent_hash=intent_hash,
        decision_kind=decision_kind,
        anchors=anchors,
        payload=payload,
        policy_version=policy_version,
        snapshot=f"{decision_kind}: {anchors}",
        writer=writer,
    )


def record_refund_allocation(
    database_path: Path,
    *,
    request_id: str,
    intent_hash: str,
    refund_anchor: str,
    allocations: list[dict[str, object]],
    reason: str,
    policy_version: str = "refund-allocation-v1",
    writer: WriterContext | None = None,
) -> DecisionReceipt:
    """Append a `REFUND_ALLOCATION_SET` after the full-state preview.

    The same-currency balance, refund-before-purchase and over-allocation checks
    run against the current projection first, so a durable record is never made
    for an allocation the design forbids.
    """
    from .decisions import resolve_anchor

    with ledger_writer(database_path, existing=writer) as held:
        connection = connect_sqlite(database_path)
        try:
            initialize_schema(connection)
            replay_journal(connection)
            preview_refund_allocation(
                connection, refund_anchor, [dict(a) for a in allocations],
                resolve_anchor=lambda a: resolve_anchor(connection, a),
            )
        finally:
            connection.close()
        return record_decision(
            database_path,
            request_id=request_id,
            intent_hash=intent_hash,
            decision_kind="REFUND_ALLOCATION_SET",
            anchors={"refund": refund_anchor},
            payload={"allocations": allocations, "reason": reason},
            policy_version=policy_version,
            snapshot=f"REFUND_ALLOCATION_SET: {len(allocations)} allocation(s)",
            writer=held,
        )
