from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect_sqlite, initialize_schema
from .decision_journal import append_decision, default_journal_path
from .errors import FinCLError
from .locking import ledger_writer


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    event_id: str
    extraction_run_id: str
    evidence_sha256: str
    prior_status: str
    journal_seq: int
    journal_record_hash: str


def recover_extraction(
    database_path: Path,
    extraction_run_id: str,
    reason: str,
    *,
    allow_running: bool = False,
) -> RecoveryResult:
    """Repair an incomplete generation after recording an originating decision."""
    if not reason.strip():
        raise FinCLError("a non-empty recovery reason is required")
    with ledger_writer(database_path):
        return _recover_extraction_locked(
            database_path, extraction_run_id, reason, allow_running=allow_running
        )


def _recover_extraction_locked(
    database_path: Path,
    extraction_run_id: str,
    reason: str,
    *,
    allow_running: bool,
) -> RecoveryResult:
    connection = connect_sqlite(database_path)
    initialize_schema(connection)
    try:
        extraction = connection.execute(
            "SELECT id, evidence_sha256, status FROM extraction_runs WHERE id = ?",
            (extraction_run_id,),
        ).fetchone()
        if extraction is None:
            raise FinCLError("extraction run was not found")
        if extraction["status"] not in {"RUNNING", "FAILED"}:
            raise FinCLError("only RUNNING or FAILED extraction runs can be recovered")
        if extraction["status"] == "RUNNING" and not allow_running:
            raise FinCLError(
                "RUNNING extraction recovery requires --allow-running after confirming no importer is active"
            )

        event_id = "rec_" + uuid.uuid4().hex
        journal_record = append_decision(
            default_journal_path(database_path),
            decision_kind="EXTRACTION_REPAIR",
            anchors={"evidence_sha256": extraction["evidence_sha256"], "extraction_identity": extraction["id"]},
            payload={"event_id": event_id, "prior_status": extraction["status"], "reason": reason.strip()},
            policy_version="repair-v1",
            snapshot=f"Cleared incomplete extraction {extraction_run_id}: {reason.strip()}",
        )
        with connection:
            _delete_transfer_links(connection, extraction_run_id)
            connection.execute(
                "DELETE FROM document_reconciliation_checks WHERE extraction_run_id = ?",
                (extraction_run_id,),
            )
            connection.execute(
                "DELETE FROM page_coverage WHERE extraction_run_id = ?", (extraction_run_id,)
            )
            connection.execute(
                "DELETE FROM ledger_entries WHERE extraction_run_id = ? "
                "AND source_family = 'DOCUMENT'",
                (extraction_run_id,),
            )
            connection.execute(
                "DELETE FROM document_records WHERE extraction_run_id = ?", (extraction_run_id,)
            )
            connection.execute(
                "DELETE FROM statement_sections WHERE statement_id IN "
                "(SELECT id FROM statements WHERE extraction_run_id = ?)",
                (extraction_run_id,),
            )
            connection.execute(
                "DELETE FROM statements WHERE extraction_run_id = ?", (extraction_run_id,)
            )
            connection.execute(
                "DELETE FROM extraction_issues WHERE extraction_run_id = ?", (extraction_run_id,)
            )
            connection.execute(
                "UPDATE import_runs SET extraction_run_id = NULL WHERE extraction_run_id = ?",
                (extraction_run_id,),
            )
            connection.execute("DELETE FROM extraction_runs WHERE id = ?", (extraction_run_id,))
        return RecoveryResult(
            event_id,
            extraction_run_id,
            extraction["evidence_sha256"],
            extraction["status"],
            journal_record.seq,
            journal_record.record_hash,
        )
    finally:
        connection.close()


def _delete_transfer_links(connection: sqlite3.Connection, extraction_run_id: str) -> None:
    connection.execute(
        """
        DELETE FROM transfer_links
        WHERE left_entry_id IN
                  (SELECT id FROM ledger_entries WHERE extraction_run_id = ?)
           OR right_entry_id IN
                  (SELECT id FROM ledger_entries WHERE extraction_run_id = ?)
        """,
        (extraction_run_id,) * 2,
    )
