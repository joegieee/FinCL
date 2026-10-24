"""Bind a ledger database to its decision journal (F24).

A SQLite ``ledger_association`` row and a companion journal identity file
together prove that this database and this journal belong to the same ledger,
and record a sequence/hash checkpoint. The checkpoint is evidence an internally
valid hash chain cannot provide on its own: a chain that is intact end to end
still cannot show that a complete tail of records was removed.

This is the first F24 implementation. It validates identity and the checkpoint
before any projection is deleted, bootstraps a brand-new ledger, adopts a legacy
one explicitly, and refuses to treat a missing journal as an empty history.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .decision_journal import GENESIS_HASH, default_journal_path, read_records
from .errors import FinCLError

JOURNAL_IDENTITY_FILENAME = "decision-journal.identity.json"

_PROJECTION_TABLES = (
    "link_decisions",
    "invariant_escapes",
    "inbound_classifications",
    "merchant_aliases",
    "category_rules",
    "category_overrides",
)


@dataclass(frozen=True, slots=True)
class LedgerAssociation:
    ledger_id: str
    journal_relative_path: str
    journal_id: str
    verified_seq: int
    verified_hash: str


def identity_file_path(database_path: Path) -> Path:
    return Path(database_path).resolve().parent / JOURNAL_IDENTITY_FILENAME


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# --------------------------------------------------------------------------- #
# SQLite association row
# --------------------------------------------------------------------------- #


def read_association(connection: sqlite3.Connection) -> LedgerAssociation | None:
    have_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ledger_association'"
    ).fetchone()
    if have_table is None:
        return None
    row = connection.execute(
        "SELECT ledger_id, journal_relative_path, journal_id, verified_seq, verified_hash "
        "FROM ledger_association WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return LedgerAssociation(
        str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4])
    )


def _write_association(connection: sqlite3.Connection, association: LedgerAssociation) -> None:
    connection.execute(
        """
        INSERT INTO ledger_association(
            id, ledger_id, journal_relative_path, journal_id, verified_seq, verified_hash
        ) VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            ledger_id = excluded.ledger_id,
            journal_relative_path = excluded.journal_relative_path,
            journal_id = excluded.journal_id,
            verified_seq = excluded.verified_seq,
            verified_hash = excluded.verified_hash
        """,
        (
            association.ledger_id,
            association.journal_relative_path,
            association.journal_id,
            association.verified_seq,
            association.verified_hash,
        ),
    )


# --------------------------------------------------------------------------- #
# Journal identity file
# --------------------------------------------------------------------------- #


def read_identity_file(database_path: Path) -> dict[str, str] | None:
    path = identity_file_path(database_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise FinCLError(f"journal identity file is unreadable: {path}") from exc
    if not isinstance(data, dict) or "journal_id" not in data or "ledger_id" not in data:
        raise FinCLError(f"journal identity file is malformed: {path}")
    return {"journal_id": str(data["journal_id"]), "ledger_id": str(data["ledger_id"])}


def _write_identity_file(database_path: Path, *, journal_id: str, ledger_id: str) -> None:
    path = identity_file_path(database_path)
    payload = json.dumps(
        {"journal_id": journal_id, "ledger_id": ledger_id}, sort_keys=True, indent=2
    )
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, (payload + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


# --------------------------------------------------------------------------- #
# Path confinement
# --------------------------------------------------------------------------- #


def _ledger_dir(database_path: Path) -> Path:
    return Path(database_path).resolve().parent


def _journal_relative(database_path: Path, journal_path: Path) -> str:
    ledger_dir = _ledger_dir(database_path)
    try:
        relative = Path(journal_path).resolve().relative_to(ledger_dir)
    except ValueError as exc:
        raise FinCLError(
            f"decision journal {journal_path} is not inside the ledger directory {ledger_dir}"
        ) from exc
    return str(relative)


def _resolve_confined(database_path: Path, relative: str) -> Path:
    if os.path.isabs(relative) or ".." in Path(relative).parts:
        raise FinCLError(
            f"journal_relative_path is not a confined relative path: {relative!r}"
        )
    ledger_dir = _ledger_dir(database_path)
    resolved = (ledger_dir / relative).resolve()
    if resolved != ledger_dir and ledger_dir not in resolved.parents:
        raise FinCLError(
            f"journal_relative_path escapes the ledger directory: {relative!r}"
        )
    return resolved


# --------------------------------------------------------------------------- #
# Projection emptiness
# --------------------------------------------------------------------------- #


def _watermark(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT applied_seq FROM decision_watermark WHERE id = 1"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _projections_are_empty(connection: sqlite3.Connection) -> bool:
    for table in _PROJECTION_TABLES:
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            return False
    return _watermark(connection) == 0


# --------------------------------------------------------------------------- #
# Lifecycle operations
# --------------------------------------------------------------------------- #


def create_new_ledger_identity(
    connection: sqlite3.Connection, database_path: Path
) -> LedgerAssociation:
    """Durably create the identity file and an empty journal, then the row."""
    journal_path = default_journal_path(database_path)
    if journal_path.exists() and read_records(journal_path):
        raise FinCLError(
            "cannot initialize a new ledger identity: a non-empty decision journal "
            f"already exists at {journal_path}; run `fincl adopt-ledger` instead"
        )
    if not _projections_are_empty(connection):
        raise FinCLError(
            "cannot initialize a new ledger identity: decision projections are not "
            "empty; run `fincl adopt-ledger` instead"
        )
    # Complete an interrupted initialization by reusing the identity file it
    # already wrote, rather than minting a second identity for the same ledger.
    identity = read_identity_file(database_path)
    if identity is not None and _is_uuid(identity["journal_id"]) and _is_uuid(identity["ledger_id"]):
        ledger_id, journal_id = identity["ledger_id"], identity["journal_id"]
    else:
        ledger_id, journal_id = str(uuid.uuid4()), str(uuid.uuid4())
    if not journal_path.exists():
        journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(journal_path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(journal_path.parent)
    _write_identity_file(database_path, journal_id=journal_id, ledger_id=ledger_id)
    association = LedgerAssociation(
        ledger_id,
        _journal_relative(database_path, journal_path),
        journal_id,
        0,
        GENESIS_HASH,
    )
    _write_association(connection, association)
    connection.commit()
    return association


def adopt_ledger(
    connection: sqlite3.Connection, database_path: Path
) -> LedgerAssociation:
    """Explicitly bind an existing ledger to its journal (legacy adoption)."""
    if read_association(connection) is not None:
        raise FinCLError("this ledger already has a journal association")
    journal_path = default_journal_path(database_path)
    records = read_records(journal_path)
    watermark = _watermark(connection)
    if not records:
        if watermark != 0 or not _projections_are_empty(connection):
            raise FinCLError(
                "the decision journal is missing but this ledger has recorded "
                "decisions; restore decision-journal.jsonl from a backup before "
                "adopting this ledger"
            )
        return create_new_ledger_identity(connection, database_path)
    if len(records) < watermark:
        raise FinCLError(
            f"the decision journal holds {len(records)} records but the projection "
            f"watermark is {watermark}; the journal tail is missing — restore it "
            "from a backup before adopting this ledger"
        )
    identity = read_identity_file(database_path)
    if identity is not None:
        if not _is_uuid(identity["journal_id"]) or not _is_uuid(identity["ledger_id"]):
            raise FinCLError("the existing journal identity file holds non-UUID identifiers")
        journal_id, ledger_id = identity["journal_id"], identity["ledger_id"]
    else:
        journal_id, ledger_id = str(uuid.uuid4()), str(uuid.uuid4())
        _write_identity_file(database_path, journal_id=journal_id, ledger_id=ledger_id)
    association = LedgerAssociation(
        ledger_id,
        _journal_relative(database_path, journal_path),
        journal_id,
        len(records),
        records[-1].record_hash,
    )
    _write_association(connection, association)
    connection.commit()
    return association


def require_valid_association(
    connection: sqlite3.Connection, database_path: Path
) -> LedgerAssociation:
    """Validate identity and the checkpoint before a projection is deleted.

    Bootstraps a brand-new ledger (no journal, empty projections). Anything
    else without an association is refused with an adoption instruction: an
    absent association is never silently treated as an empty history.
    """
    journal_path = default_journal_path(database_path)
    association = read_association(connection)
    if association is None:
        identity = read_identity_file(database_path)
        journal_empty = not journal_path.exists() or not read_records(journal_path)
        if identity is None and journal_empty and _projections_are_empty(connection):
            return create_new_ledger_identity(connection, database_path)
        detail = (
            " (an earlier initialization did not finish)" if identity is not None else ""
        )
        raise FinCLError(
            f"this ledger has no journal association{detail}; run "
            "`fincl adopt-ledger --database <db>` to bind it to its decision journal"
        )
    if not _is_uuid(association.ledger_id) or not _is_uuid(association.journal_id):
        raise FinCLError("the ledger association holds non-UUID identifiers")
    resolved = _resolve_confined(database_path, association.journal_relative_path)
    if resolved != journal_path.resolve():
        raise FinCLError(
            f"the ledger association points at {association.journal_relative_path}, "
            "not this ledger's decision journal"
        )
    identity = read_identity_file(database_path)
    if identity is None:
        raise FinCLError(
            f"the journal identity file {identity_file_path(database_path)} is missing; "
            "restore it from a backup"
        )
    if (
        identity["journal_id"] != association.journal_id
        or identity["ledger_id"] != association.ledger_id
    ):
        raise FinCLError(
            "the journal identity file does not match the ledger association; this "
            "database and journal are from different ledgers"
        )
    records = read_records(journal_path)
    if len(records) < association.verified_seq:
        raise FinCLError(
            f"the decision journal holds {len(records)} records but its checkpoint is "
            f"at seq {association.verified_seq}; a complete tail was removed — restore "
            "the journal from a backup"
        )
    if (
        association.verified_seq > 0
        and records[association.verified_seq - 1].record_hash != association.verified_hash
    ):
        raise FinCLError(
            f"the decision journal diverges from its checkpoint at seq "
            f"{association.verified_seq}; restore the journal from a backup"
        )
    return association


def advance_checkpoint(
    connection: sqlite3.Connection,
    database_path: Path,
    record_count: int,
    head_hash: str,
) -> None:
    """Move the checkpoint to the journal head after a verified full replay."""
    association = read_association(connection)
    if association is None or record_count < association.verified_seq:
        return
    connection.execute(
        "UPDATE ledger_association SET verified_seq = ?, verified_hash = ? WHERE id = 1",
        (record_count, head_hash),
    )


def copy_association_for_rebuild(
    source_connection: sqlite3.Connection,
    staging_connection: sqlite3.Connection,
) -> None:
    """Carry the source ledger's identity and checkpoint into a rebuilt copy.

    A same-directory rebuild is a replacement candidate for one ledger, not an
    independent fork: it keeps the identity and shares the one journal.
    """
    association = read_association(source_connection)
    if association is None:
        return
    _write_association(staging_connection, association)
