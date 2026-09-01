from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .db import connect_sqlite, initialize_schema
from .decision_journal import JournalRecord, append_decision, default_journal_path
from .errors import FinCLError


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    event_id: str
    extraction_run_id: str
    evidence_sha256: str
    prior_status: str
    journal_seq: int
    journal_record_hash: str
    journal_path: Path


def recover_extraction(
    database_path: Path,
    extraction_run_id: str,
    reason: str,
    *,
    allow_running: bool = False,
    journal_path: Path | None = None,
) -> RecoveryResult:
    """Repair an incomplete generation after durably recording the decision."""
    if not reason.strip():
        raise FinCLError("a non-empty recovery reason is required")
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
        resolved_journal_path = journal_path or default_journal_path(database_path)
        journal_record = _journal_repair(
            resolved_journal_path,
            event_id=event_id,
            extraction_run_id=extraction["id"],
            evidence_sha256=extraction["evidence_sha256"],
            prior_status=extraction["status"],
            reason=reason.strip(),
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
                "DELETE FROM document_entries WHERE extraction_run_id = ?", (extraction_run_id,)
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
                "DELETE FROM reconciliation_checks WHERE extraction_run_id = ?",
                (extraction_run_id,),
            )
            connection.execute(
                "DELETE FROM extraction_issues WHERE extraction_run_id = ?", (extraction_run_id,)
            )
            connection.execute(
                "DELETE FROM canonical_entries WHERE extraction_run_id = ?", (extraction_run_id,)
            )
            connection.execute(
                "DELETE FROM raw_records WHERE extraction_run_id = ?", (extraction_run_id,)
            )
            connection.execute(
                "UPDATE import_runs SET extraction_run_id = NULL WHERE extraction_run_id = ?",
                (extraction_run_id,),
            )
            connection.execute("DELETE FROM extraction_runs WHERE id = ?", (extraction_run_id,))
            connection.execute(
                """
                INSERT INTO extraction_recovery_events(
                    id, extraction_run_id, evidence_sha256, prior_status, reason, recovered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    extraction_run_id,
                    extraction["evidence_sha256"],
                    extraction["status"],
                    reason.strip(),
                    datetime.now(UTC).isoformat(timespec="microseconds"),
                ),
            )
        return RecoveryResult(
            event_id,
            extraction_run_id,
            extraction["evidence_sha256"],
            extraction["status"],
            journal_record.seq,
            journal_record.record_hash,
            resolved_journal_path,
        )
    finally:
        connection.close()


def _delete_transfer_links(connection: sqlite3.Connection, extraction_run_id: str) -> None:
    connection.execute(
        """
        DELETE FROM transfer_links
        WHERE (left_entry_kind = 'CSV' AND left_entry_id IN
                  (SELECT id FROM canonical_entries WHERE extraction_run_id = ?))
           OR (right_entry_kind = 'CSV' AND right_entry_id IN
                  (SELECT id FROM canonical_entries WHERE extraction_run_id = ?))
           OR (left_entry_kind = 'DOCUMENT' AND left_entry_id IN
                  (SELECT id FROM document_entries WHERE extraction_run_id = ?))
           OR (right_entry_kind = 'DOCUMENT' AND right_entry_id IN
                  (SELECT id FROM document_entries WHERE extraction_run_id = ?))
        """,
        (extraction_run_id,) * 4,
    )


def _journal_repair(
    journal_path: Path,
    *,
    event_id: str,
    extraction_run_id: str,
    evidence_sha256: str,
    prior_status: str,
    reason: str,
) -> JournalRecord:
    return append_decision(
        journal_path,
        decision_kind="EXTRACTION_REPAIR",
        anchors={
            "evidence_sha256": evidence_sha256,
            "extraction_identity": extraction_run_id,
        },
        payload={
            "event_id": event_id,
            "prior_status": prior_status,
            "reason": reason,
        },
        policy_version="repair-v1",
        snapshot=(
            f"Cleared incomplete extraction {extraction_run_id} "
            f"for evidence {evidence_sha256} after status {prior_status}."
        ),
    )
