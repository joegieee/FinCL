"""Explicit, versioned schema migrations (F27, §5).

Normal startup never migrates — a version-mismatched database is refused and the
operator chooses. ``apply_pending_migrations`` runs only on explicit request and
only after a verified backup: each step is one transaction, integrity and
foreign-key checks run before commit, and the new version is stamped only at
commit. There is no lossy down-migration; reversal restores the backup.

The registry is empty: schema 8 is the only deployed layout. A migration is
added here with an immutable checksum together with its historical fixture.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .backup import verify_backup_bundle
from .common import utc_now
from .db import SCHEMA_VERSION, connect_sqlite
from .errors import FinCLError, SchemaVersionError


@dataclass(frozen=True, slots=True)
class Migration:
    from_version: int
    to_version: int
    checksum: str
    apply: Callable[[sqlite3.Connection], None]


# Keyed by the version each migration upgrades *from*. Immutable checksums are
# committed alongside each real migration.
MIGRATIONS: dict[int, Migration] = {}


@dataclass(frozen=True, slots=True)
class MigrationResult:
    from_version: int
    to_version: int
    steps: list[int]


def _stored_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        raise SchemaVersionError("database does not record a schema version")
    return int(row[0])


def apply_pending_migrations(database: Path, *, backup_dir: Path) -> MigrationResult:
    """Upgrade ``database`` to the current schema version after a verified backup.

    ``backup_dir`` must be a bundle produced by ``fincl backup`` for this same
    ledger; it is verified in full before a single table is touched.
    """
    database = Path(database).resolve()
    if not database.is_file():
        raise FinCLError(f"database does not exist: {database}")

    connection = connect_sqlite(database)
    # Manage each migration's transaction explicitly so that a step which only
    # issues DDL before failing is still fully rolled back.
    connection.isolation_level = None
    try:
        current = _stored_version(connection)
        if current > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema version {current} is newer than this build "
                f"supports ({SCHEMA_VERSION}); upgrade fincl"
            )
        if current == SCHEMA_VERSION:
            return MigrationResult(current, current, [])

        bundle = verify_backup_bundle(backup_dir)
        ledger_id = connection.execute(
            "SELECT ledger_id FROM ledger_association WHERE id = 1"
        ).fetchone()
        if ledger_id is None or ledger_id[0] != bundle.ledger_id:
            raise FinCLError(
                "the supplied backup is for a different ledger than the database "
                "being migrated"
            )
        if bundle.schema_version != current:
            raise FinCLError(
                f"the supplied backup captured schema version {bundle.schema_version}, "
                f"not the database's current version {current}"
            )

        steps: list[int] = []
        version = current
        while version < SCHEMA_VERSION:
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise SchemaVersionError(
                    f"no migration is registered from schema version {version}; "
                    "restore the backup and use a build that provides one, or "
                    "reconstruct the ledger with `fincl rebuild`"
                )
            if migration.to_version != version + 1:
                raise SchemaVersionError(
                    f"migration from {version} does not advance exactly one version"
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration.apply(connection)
                _check_integrity(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(to_version, from_version, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (migration.to_version, migration.from_version, migration.checksum, utc_now()),
                )
                connection.execute(
                    "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
                    (str(migration.to_version),),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            steps.append(migration.to_version)
            version = migration.to_version
        return MigrationResult(current, version, steps)
    finally:
        connection.close()


def _check_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise FinCLError(f"post-migration integrity check failed: {integrity}")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise FinCLError(f"post-migration foreign-key check failed: {violations}")
