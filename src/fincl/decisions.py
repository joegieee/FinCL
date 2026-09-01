"""Replay the originating decision journal into disposable SQLite projections."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .decision_journal import JournalRecord, default_journal_path, read_records
from .errors import FinCLError

NATURAL_ANCHOR_VERSION = "natural-anchor-v1"


def journal_path_for(connection: sqlite3.Connection) -> Path:
    row = connection.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        raise FinCLError("cannot determine the SQLite database path for decision replay")
    return default_journal_path(Path(row[2]))


def entry_anchor(connection: sqlite3.Connection, entry_id: str) -> str:
    row = connection.execute(
        "SELECT natural_anchor FROM ledger_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise FinCLError("entry does not resolve to a natural anchor")
    return str(row[0])


def resolve_anchor(connection: sqlite3.Connection, anchor: str) -> str | None:
    row = connection.execute(
        "SELECT id FROM ledger_entries WHERE natural_anchor = ? ORDER BY id LIMIT 1",
        (anchor,),
    ).fetchone()
    return None if row is None else str(row[0])


def invariant_violation_is_live(
    connection: sqlite3.Connection, code: str, entry_ids: list[str]
) -> bool:
    """True when ``code`` is a currently-raised BLOCKING issue on one of these entries.

    An ``INVARIANT_ESCAPE`` may only downgrade a check that is failing right now
    (ARCHITECTURE.md §17.1). A violation is anchored through the offending raw
    record, so the escape's entry anchors must map back to a raw record that
    carries a live ``BLOCKING`` ``extraction_issues`` row for that exact code.
    """
    if not entry_ids:
        return False
    entry_placeholders = ",".join("?" for _ in entry_ids)
    record_ids = [
        row[0]
        for row in connection.execute(
            f"SELECT raw_record_id FROM ledger_entries "
            f"WHERE id IN ({entry_placeholders}) AND raw_record_id IS NOT NULL",
            tuple(entry_ids),
        )
    ]
    if not record_ids:
        return False
    record_placeholders = ",".join("?" for _ in record_ids)
    hit = connection.execute(
        f"SELECT 1 FROM extraction_issues "
        f"WHERE code = ? AND severity = 'BLOCKING' "
        f"AND raw_record_id IN ({record_placeholders}) LIMIT 1",
        (code, *record_ids),
    ).fetchone()
    return hit is not None


def replay_journal(connection: sqlite3.Connection, journal_path: Path | None = None) -> tuple[int, int]:
    """Verify and project the complete journal; return bound and dangling counts."""
    records = read_records(journal_path or journal_path_for(connection))
    with connection:
        connection.execute("DELETE FROM link_decisions")
        connection.execute("DELETE FROM invariant_escapes")
        connection.execute("DELETE FROM inbound_classifications")
        for record in records:
            _apply_record(connection, record)
        connection.execute("UPDATE decision_watermark SET applied_seq = ? WHERE id = 1", (len(records),))
    bound = connection.execute(
        "SELECT (SELECT count(*) FROM link_decisions WHERE bound=1) + "
        "(SELECT count(*) FROM invariant_escapes WHERE bound=1) + "
        "(SELECT count(*) FROM inbound_classifications WHERE bound=1)"
    ).fetchone()[0]
    dangling = connection.execute(
        "SELECT (SELECT count(*) FROM link_decisions WHERE bound=0) + "
        "(SELECT count(*) FROM invariant_escapes WHERE bound=0) + "
        "(SELECT count(*) FROM inbound_classifications WHERE bound=0)"
    ).fetchone()[0]
    return bound, dangling


def _apply_record(connection: sqlite3.Connection, record: JournalRecord) -> None:
    if record.anchor_version != NATURAL_ANCHOR_VERSION:
        return
    if record.decision_kind in {"LINK_CONFIRM", "LINK_REJECT"}:
        left, right = record.anchors.get("left"), record.anchors.get("right")
        bound = bool(left and right and resolve_anchor(connection, left) and resolve_anchor(connection, right))
        connection.execute(
            """INSERT INTO link_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(left_anchor,right_anchor,anchor_version,link_kind) DO UPDATE SET
               decision=excluded.decision, policy_version=excluded.policy_version, journal_seq=excluded.journal_seq,
               snapshot=excluded.snapshot, bound=excluded.bound, dangling_reason=excluded.dangling_reason""",
            (left or "", right or "", record.anchor_version, record.payload.get("link_kind", "SAME_CURRENCY_TRANSFER"),
             "CONFIRM" if record.decision_kind == "LINK_CONFIRM" else "REJECT", record.policy_version,
             record.seq, record.snapshot, int(bound), None if bound else "anchor no longer resolves"),
        )
    elif record.decision_kind == "INVARIANT_ESCAPE":
        anchors_json = json.dumps(record.anchors, sort_keys=True, separators=(",", ":"))
        bound = all(resolve_anchor(connection, anchor) for anchor in record.anchors.values())
        connection.execute(
            """INSERT INTO invariant_escapes VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(invariant_code,anchors_json,anchor_version) DO UPDATE SET policy_version=excluded.policy_version,
               journal_seq=excluded.journal_seq, snapshot=excluded.snapshot, bound=excluded.bound, dangling_reason=excluded.dangling_reason""",
            (record.payload.get("code", ""), anchors_json, record.anchor_version, record.policy_version,
             record.seq, record.snapshot, int(bound), None if bound else "anchor no longer resolves"),
        )
    elif record.decision_kind == "INBOUND_CLASSIFICATION":
        anchor = record.anchors.get("entry", "")
        bound = resolve_anchor(connection, anchor) is not None
        connection.execute(
            """INSERT INTO inbound_classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(anchor,anchor_version) DO UPDATE SET classification=excluded.classification,
               policy_version=excluded.policy_version, journal_seq=excluded.journal_seq, snapshot=excluded.snapshot,
               bound=excluded.bound, dangling_reason=excluded.dangling_reason""",
            (anchor, record.anchor_version, record.payload.get("classification", "other"), record.policy_version,
             record.seq, record.snapshot, int(bound), None if bound else "anchor no longer resolves"),
        )
