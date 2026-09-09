"""Explicit, versioned schema migrations (F27, §5).

Normal startup never migrates — a version-mismatched database is refused and the
operator chooses. ``apply_pending_migrations`` runs only on explicit request and
only after a verified backup: each step is one transaction, integrity and
foreign-key checks run before commit, and the new version is stamped only at
commit. There is no lossy down-migration; reversal restores the backup.

The registry holds the 8->9 upgrade (MVP M1b, two derived generation/receipt
tables), the 9->10 upgrade (v0.2 analysis slice 1, the `analysis_components`
projection tables), the 10->11 upgrade (v0.2 analysis slices 3-4, the
relationship projection tables), the 11->12 upgrade (v0.5 bulk actions,
dropping the pre-batch `journal_seq UNIQUE` from the six categorization
projection tables), the 12->13 upgrade (v0.3 personal debts, the four debt
projection tables) and the 13->14 upgrade (v0.4 credit cards, the
`card_statement_facts` extraction-derived table) through to the 21->22 upgrade
(cash-flow framework Stage 5A, the forecast-snapshot and match projection
tables). Every step either adds derived tables or relaxes a constraint on one,
rewriting no rows, so a migrated ledger and a fresh one stay byte-identical.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .backup import verify_backup_bundle
from .common import utc_now
from .db import (
    SCHEMA_9_TABLES_SQL,
    SCHEMA_10_TABLES_SQL,
    SCHEMA_11_TABLES_SQL,
    SCHEMA_12_MIGRATION_SQL,
    SCHEMA_13_TABLES_SQL,
    SCHEMA_14_TABLES_SQL,
    SCHEMA_15_TABLES_SQL,
    SCHEMA_16_TABLES_SQL,
    SCHEMA_17_TABLES_SQL,
    SCHEMA_18_TABLES_SQL,
    SCHEMA_19_TABLES_SQL,
    SCHEMA_20_TABLES_SQL,
    SCHEMA_21_TABLES_SQL,
    SCHEMA_22_TABLES_SQL,
    SCHEMA_VERSION,
    connect_sqlite,
)
from .errors import FinCLError, SchemaVersionError
from .locking import WriterContext, ledger_writer


@dataclass(frozen=True, slots=True)
class Migration:
    from_version: int
    to_version: int
    checksum: str
    apply: Callable[[sqlite3.Connection], None]


def _apply_table_ddl(connection: sqlite3.Connection, ddl: str) -> None:
    """Run each ``CREATE`` statement on its own so the migration stays inside the
    caller's ``BEGIN IMMEDIATE`` transaction — ``executescript`` would commit it
    first.
    """
    for statement in filter(None, (s.strip() for s in ddl.split(";"))):
        connection.execute(statement)


def _migrate_8_to_9(connection: sqlite3.Connection) -> None:
    """Add `projection_generation` and `command_receipts`; no data is rewritten."""
    _apply_table_ddl(connection, SCHEMA_9_TABLES_SQL)


def _migrate_9_to_10(connection: sqlite3.Connection) -> None:
    """Add the schema-10 `analysis_components` projection tables; no data rewrite.

    The tables are populated by the next `fincl refresh`, in the same
    transaction that publishes the generation.
    """
    _apply_table_ddl(connection, SCHEMA_10_TABLES_SQL)


def _migrate_10_to_11(connection: sqlite3.Connection) -> None:
    """Add the schema-11 relationship projection tables; no data rewrite.

    Populated by the next journal replay (duplicate suppressions, refund
    allocations); empty until an operator authors those decisions.
    """
    _apply_table_ddl(connection, SCHEMA_11_TABLES_SQL)


def _migrate_11_to_12(connection: sqlite3.Connection) -> None:
    """Drop the pre-batch ``journal_seq UNIQUE`` from the six categorization
    projection tables by rebuilding each in place.

    Every existing row is copied verbatim; only the redundant unique index is
    gone, so a ``DECISION_BATCH`` record can project one child row per item
    without a second child colliding on the outer record's seq. The tables are
    disposable projections rebuilt by the next journal replay regardless.
    """
    _apply_table_ddl(connection, SCHEMA_12_MIGRATION_SQL)


def _migrate_12_to_13(connection: sqlite3.Connection) -> None:
    """Add the schema-13 personal-debt projection tables; no data rewrite.

    Populated by the next journal replay from the `PERSONAL_DEBT_*` and
    `ENTRY_RESIDUAL_*` decision kinds; empty until an operator authors a debt.
    """
    _apply_table_ddl(connection, SCHEMA_13_TABLES_SQL)


def _migrate_13_to_14(connection: sqlite3.Connection) -> None:
    """Add the schema-14 `card_statement_facts` extraction-derived table; no
    data rewrite.

    Populated on the next `fincl rebuild` or re-import of a card statement, the
    same way schema 10/11/13 tables fill; empty until then.
    """
    _apply_table_ddl(connection, SCHEMA_14_TABLES_SQL)


def _migrate_14_to_15(connection: sqlite3.Connection) -> None:
    """Add the schema-15 card-facility / limit-observation projection tables;
    no data rewrite.

    Populated by the next journal replay from the `CARD_FACILITY_*` /
    `CARD_LIMIT_OBSERVATION_*` decision kinds; empty until an operator authors a
    facility.
    """
    _apply_table_ddl(connection, SCHEMA_15_TABLES_SQL)


def _migrate_15_to_16(connection: sqlite3.Connection) -> None:
    """Add the schema-16 installment projection tables; no data rewrite.

    Populated by the next journal replay from the `INSTALLMENT_PLAN_SET` /
    `INSTALLMENT_ALLOCATION_SET` / `INSTALLMENT_SETTLEMENT_SET` decision kinds;
    empty until an operator confirms an installment chain.
    """
    _apply_table_ddl(connection, SCHEMA_16_TABLES_SQL)


def _migrate_16_to_17(connection: sqlite3.Connection) -> None:
    """Add the schema-17 `card_payment_allocations` projection table; no data
    rewrite.

    Populated by the next journal replay from the `CARD_PAYMENT_ALLOCATION_SET` /
    `_RETIRE` decision kinds; empty until an operator records a manual bill
    allocation.
    """
    _apply_table_ddl(connection, SCHEMA_17_TABLES_SQL)


def _migrate_17_to_18(connection: sqlite3.Connection) -> None:
    """Add v0.6 budget, goal and recurring planning projections."""
    _apply_table_ddl(connection, SCHEMA_18_TABLES_SQL)


def _migrate_18_to_19(connection: sqlite3.Connection) -> None:
    """Add the schema-19 `balance_observations` projection table; no data rewrite.

    Populated by the next journal replay from the `BALANCE_OBSERVATION_SET` /
    `_RETIRE` decision kinds; empty until an operator records a manual balance
    observation.
    """
    _apply_table_ddl(connection, SCHEMA_19_TABLES_SQL)


def _migrate_19_to_20(connection: sqlite3.Connection) -> None:
    """Add the schema-20 `component_provenance` projection table; no data rewrite.

    Repopulated by the next refresh alongside `analysis_components`, so a ledger
    migrated here carries no provenance until it is refreshed — which the card
    cash-conversion read reports as an unresolved requirement rather than
    silently treating every component as a bank movement.
    """
    _apply_table_ddl(connection, SCHEMA_20_TABLES_SQL)


def _migrate_20_to_21(connection: sqlite3.Connection) -> None:
    """Add the schema-21 sinking-fund projection tables; no data rewrite.

    Populated by the next journal replay from the `SINKING_FUND_*` decision
    kinds; empty until an operator confirms a fund, which is exactly right —
    a migrated ledger has no earmarks, so its whole liquid position stays
    unallocated cash rather than acquiring silent reservations.
    """
    _apply_table_ddl(connection, SCHEMA_21_TABLES_SQL)


def _migrate_21_to_22(connection: sqlite3.Connection) -> None:
    """Add the schema-22 forecast-snapshot and match projection tables; no data
    rewrite.

    Populated by the next journal replay from the `FORECAST_SNAPSHOT_ISSUE` /
    `FORECAST_MATCH_*` kinds; empty until an operator explicitly issues a
    snapshot. That is the right starting state — a migrated ledger has issued no
    forecast, so it has nothing to be held to, and the variance read reports
    "no snapshot" rather than manufacturing one from today's inputs and scoring
    the past against a plan nobody made.
    """
    _apply_table_ddl(connection, SCHEMA_22_TABLES_SQL)


# The checksum is an immutable hash of the exact DDL the step applies, so a later
# edit to that DDL is a new migration rather than a silent change under one id.
MIGRATION_8_TO_9_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_9_TABLES_SQL.encode()
).hexdigest()
MIGRATION_9_TO_10_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_10_TABLES_SQL.encode()
).hexdigest()
MIGRATION_10_TO_11_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_11_TABLES_SQL.encode()
).hexdigest()
MIGRATION_11_TO_12_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_12_MIGRATION_SQL.encode()
).hexdigest()
MIGRATION_12_TO_13_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_13_TABLES_SQL.encode()
).hexdigest()
MIGRATION_13_TO_14_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_14_TABLES_SQL.encode()
).hexdigest()
MIGRATION_14_TO_15_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_15_TABLES_SQL.encode()
).hexdigest()
MIGRATION_15_TO_16_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_16_TABLES_SQL.encode()
).hexdigest()
MIGRATION_16_TO_17_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_17_TABLES_SQL.encode()
).hexdigest()
MIGRATION_17_TO_18_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_18_TABLES_SQL.encode()
).hexdigest()
MIGRATION_18_TO_19_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_19_TABLES_SQL.encode()
).hexdigest()
MIGRATION_19_TO_20_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_20_TABLES_SQL.encode()
).hexdigest()
MIGRATION_20_TO_21_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_21_TABLES_SQL.encode()
).hexdigest()
MIGRATION_21_TO_22_CHECKSUM = "sha256:" + hashlib.sha256(
    SCHEMA_22_TABLES_SQL.encode()
).hexdigest()

# Keyed by the version each migration upgrades *from*.
MIGRATIONS: dict[int, Migration] = {
    8: Migration(8, 9, MIGRATION_8_TO_9_CHECKSUM, _migrate_8_to_9),
    9: Migration(9, 10, MIGRATION_9_TO_10_CHECKSUM, _migrate_9_to_10),
    10: Migration(10, 11, MIGRATION_10_TO_11_CHECKSUM, _migrate_10_to_11),
    11: Migration(11, 12, MIGRATION_11_TO_12_CHECKSUM, _migrate_11_to_12),
    12: Migration(12, 13, MIGRATION_12_TO_13_CHECKSUM, _migrate_12_to_13),
    13: Migration(13, 14, MIGRATION_13_TO_14_CHECKSUM, _migrate_13_to_14),
    14: Migration(14, 15, MIGRATION_14_TO_15_CHECKSUM, _migrate_14_to_15),
    15: Migration(15, 16, MIGRATION_15_TO_16_CHECKSUM, _migrate_15_to_16),
    16: Migration(16, 17, MIGRATION_16_TO_17_CHECKSUM, _migrate_16_to_17),
    17: Migration(17, 18, MIGRATION_17_TO_18_CHECKSUM, _migrate_17_to_18),
    18: Migration(18, 19, MIGRATION_18_TO_19_CHECKSUM, _migrate_18_to_19),
    19: Migration(19, 20, MIGRATION_19_TO_20_CHECKSUM, _migrate_19_to_20),
    20: Migration(20, 21, MIGRATION_20_TO_21_CHECKSUM, _migrate_20_to_21),
    21: Migration(21, 22, MIGRATION_21_TO_22_CHECKSUM, _migrate_21_to_22),
}


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


def apply_pending_migrations(
    database: Path, *, backup_dir: Path, writer: WriterContext | None = None
) -> MigrationResult:
    """Upgrade ``database`` to the current schema version after a verified backup.

    ``backup_dir`` must be a bundle produced by ``fincl backup`` for this same
    ledger; it is verified in full before a single table is touched. Runs under
    the one ledger writer lock; pass ``writer`` from an outer lock holder.
    """
    database = Path(database).resolve()
    if not database.is_file():
        raise FinCLError(f"database does not exist: {database}")

    with ledger_writer(database, existing=writer):
        return _apply_pending_migrations(database, backup_dir)


def _apply_pending_migrations(database: Path, backup_dir: Path) -> MigrationResult:
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
