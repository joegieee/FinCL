from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .errors import SchemaVersionError
from .money import MAX_ABS_COEFFICIENT, MAX_SCALE

SCHEMA_VERSION = 5

# Busy handler timeout for the single-writer local database. Concurrent imports
# wait for the lock to clear instead of failing immediately with "database is
# locked".
BUSY_TIMEOUT_MS = 5000

# In-place upgrade steps keyed by the schema version they upgrade *from*. Each
# callable runs inside its own transaction and must leave the database at
# `from_version + 1`. There is no deployed database older than SCHEMA_VERSION, so
# the registry is intentionally empty; add an entry here whenever SCHEMA_VERSION
# is bumped in a way that existing databases cannot satisfy through the
# idempotent CREATE TABLE IF NOT EXISTS statements below.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


def _migrate_3_to_4(connection: sqlite3.Connection) -> None:
    """Add per-leg transfer facts without discarding existing links."""
    existing = {row[1] for row in connection.execute("PRAGMA table_info(transfer_links)")}
    additions = (
        ("link_kind", "TEXT NOT NULL DEFAULT 'SAME_CURRENCY_TRANSFER'"),
        ("left_currency", "TEXT"),
        ("left_amount_text", "TEXT"),
        ("left_amount_coefficient", "INTEGER"),
        ("left_amount_scale", "INTEGER"),
        ("right_currency", "TEXT"),
        ("right_amount_text", "TEXT"),
        ("right_amount_coefficient", "INTEGER"),
        ("right_amount_scale", "INTEGER"),
        ("implied_rate_numerator", "INTEGER"),
        ("implied_rate_denominator", "INTEGER"),
    )
    for name, declaration in additions:
        if name not in existing:
            connection.execute(f"ALTER TABLE transfer_links ADD COLUMN {name} {declaration}")
    if "currency" in existing:
        connection.execute(
            """
            UPDATE transfer_links
            SET left_currency = currency, left_amount_text = amount_text,
                left_amount_coefficient = amount_coefficient, left_amount_scale = amount_scale,
                right_currency = currency, right_amount_text = amount_text,
                right_amount_coefficient = amount_coefficient, right_amount_scale = amount_scale
            WHERE left_currency IS NULL
            """
        )


_MIGRATIONS[3] = _migrate_3_to_4


def _migrate_4_to_5(connection: sqlite3.Connection) -> None:
    """Discard derived links made by the superseded broad Revolut matcher."""
    connection.execute(
        "DELETE FROM transfer_links WHERE method_version = ?",
        ("dbs-revolut-exact-amount-currency-window-description-v1",),
    )


_MIGRATIONS[4] = _migrate_4_to_5


class ConnectionFactory(Protocol):
    def __call__(self, path: Path) -> sqlite3.Connection: ...


def connect_sqlite(path: Path) -> sqlite3.Connection:
    """Replaceable persistence boundary; Phase 1 uses plaintext SQLite."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    connection = sqlite3.connect(path)
    os.chmod(path, 0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


def _stored_schema_version(connection: sqlite3.Connection) -> int | None:
    """Return the schema version recorded in an existing database, or None."""
    has_metadata = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_metadata'"
    ).fetchone()
    if has_metadata is None:
        return None
    row = connection.execute(
        "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise SchemaVersionError(
            f"schema_metadata.schema_version is not an integer: {row[0]!r}"
        ) from exc


def _run_migrations(connection: sqlite3.Connection, from_version: int) -> None:
    version = from_version
    while version < SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise SchemaVersionError(
                f"no migration path from schema version {version} to {SCHEMA_VERSION}; "
                "back up var/fincl.sqlite3 and re-import into a fresh database"
            )
        with connection:
            migration(connection)
            connection.execute(
                "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
                (str(version + 1),),
            )
        version += 1


def initialize_schema(connection: sqlite3.Connection) -> None:
    stored = _stored_schema_version(connection)
    if stored is not None and stored > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema version {stored} is newer than this build supports "
            f"({SCHEMA_VERSION}); upgrade fincl before using this database"
        )
    if stored is not None and stored < SCHEMA_VERSION:
        _run_migrations(connection, stored)
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS schema_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS import_runs (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
            source_path TEXT NOT NULL,
            evidence_sha256 TEXT REFERENCES source_content(sha256),
            extraction_run_id TEXT REFERENCES extraction_runs(id),
            extraction_reused INTEGER NOT NULL DEFAULT 0 CHECK (extraction_reused IN (0, 1)),
            error_message TEXT
        ) STRICT;

        CREATE TABLE IF NOT EXISTS source_content (
            sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
            byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
            media_type TEXT NOT NULL,
            evidence_key TEXT NOT NULL UNIQUE,
            storage_kind TEXT NOT NULL,
            original_path TEXT NOT NULL,
            last_observed_path TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            integrity_status TEXT NOT NULL CHECK (integrity_status = 'VERIFIED')
        ) STRICT;

        CREATE TABLE IF NOT EXISTS extraction_runs (
            id TEXT PRIMARY KEY,
            evidence_sha256 TEXT NOT NULL REFERENCES source_content(sha256),
            source_profile_version TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            extraction_policy_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('RUNNING', 'ACCEPTED', 'QUARANTINED', 'FAILED')),
            header_text TEXT,
            header_cells_json TEXT,
            header_sha256 TEXT,
            physical_data_row_count INTEGER CHECK (physical_data_row_count >= 0),
            raw_record_count INTEGER CHECK (raw_record_count >= 0),
            canonical_entry_count INTEGER CHECK (canonical_entry_count >= 0),
            UNIQUE (evidence_sha256, source_profile_version, parser_version, extraction_policy_version)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS raw_records (
            id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
            evidence_sha256 TEXT NOT NULL REFERENCES source_content(sha256),
            source_profile_version TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            header_sha256 TEXT NOT NULL,
            source_anchor TEXT NOT NULL,
            extraction_ordinal INTEGER NOT NULL CHECK (extraction_ordinal > 0),
            physical_line_start INTEGER NOT NULL CHECK (physical_line_start > 0),
            physical_line_end INTEGER NOT NULL CHECK (physical_line_end >= physical_line_start),
            record_kind TEXT NOT NULL CHECK (record_kind = 'TRANSACTION'),
            raw_record_text TEXT NOT NULL,
            raw_cells_json TEXT NOT NULL,
            type_raw TEXT,
            product_raw TEXT,
            started_at_raw TEXT,
            completed_at_raw TEXT,
            description_raw TEXT,
            amount_text TEXT,
            amount_coefficient INTEGER CHECK (amount_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            amount_scale INTEGER CHECK (amount_scale BETWEEN 0 AND {MAX_SCALE}),
            fee_text TEXT,
            fee_coefficient INTEGER CHECK (fee_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            fee_scale INTEGER CHECK (fee_scale BETWEEN 0 AND {MAX_SCALE}),
            currency_raw TEXT,
            state_raw TEXT,
            balance_text TEXT,
            balance_coefficient INTEGER CHECK (balance_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            balance_scale INTEGER CHECK (balance_scale BETWEEN 0 AND {MAX_SCALE}),
            confidence TEXT NOT NULL CHECK (confidence IN ('HIGH', 'UNRESOLVED')),
            required_fields_complete INTEGER NOT NULL CHECK (required_fields_complete IN (0, 1)),
            UNIQUE (extraction_run_id, source_anchor),
            UNIQUE (extraction_run_id, extraction_ordinal)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS canonical_entries (
            id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
            raw_record_id TEXT NOT NULL UNIQUE REFERENCES raw_records(id),
            account_id TEXT NOT NULL,
            currency_pocket TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            effective_at TEXT NOT NULL,
            amount_text TEXT NOT NULL,
            amount_coefficient INTEGER NOT NULL CHECK (amount_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            amount_scale INTEGER NOT NULL CHECK (amount_scale BETWEEN 0 AND {MAX_SCALE}),
            fee_text TEXT NOT NULL,
            fee_coefficient INTEGER NOT NULL CHECK (fee_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            fee_scale INTEGER NOT NULL CHECK (fee_scale BETWEEN 0 AND {MAX_SCALE}),
            balance_text TEXT,
            balance_coefficient INTEGER CHECK (balance_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            balance_scale INTEGER CHECK (balance_scale BETWEEN 0 AND {MAX_SCALE}),
            lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('COMPLETED', 'PENDING', 'REVERTED')),
            event_kind TEXT NOT NULL,
            economic_direction TEXT NOT NULL CHECK (economic_direction IN ('IN', 'OUT', 'ZERO')),
            analytics_eligible INTEGER NOT NULL CHECK (analytics_eligible IN (0, 1))
        ) STRICT;

        CREATE TABLE IF NOT EXISTS extraction_issues (
            id INTEGER PRIMARY KEY,
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
            raw_record_id TEXT REFERENCES raw_records(id),
            severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'BLOCKING')),
            code TEXT NOT NULL,
            message TEXT NOT NULL,
            context_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (extraction_run_id, raw_record_id, code)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS reconciliation_checks (
            id INTEGER PRIMARY KEY,
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
            currency_pocket TEXT NOT NULL,
            prior_raw_record_id TEXT NOT NULL REFERENCES raw_records(id),
            current_raw_record_id TEXT NOT NULL REFERENCES raw_records(id),
            equation_version TEXT NOT NULL,
            expected_text TEXT NOT NULL,
            expected_coefficient INTEGER NOT NULL CHECK (expected_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            expected_scale INTEGER NOT NULL CHECK (expected_scale BETWEEN 0 AND {MAX_SCALE}),
            actual_text TEXT NOT NULL,
            actual_coefficient INTEGER NOT NULL CHECK (actual_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            actual_scale INTEGER NOT NULL CHECK (actual_scale BETWEEN 0 AND {MAX_SCALE}),
            difference_text TEXT NOT NULL,
            difference_coefficient INTEGER NOT NULL CHECK (difference_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            difference_scale INTEGER NOT NULL CHECK (difference_scale BETWEEN 0 AND {MAX_SCALE}),
            passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
            UNIQUE (extraction_run_id, currency_pocket, prior_raw_record_id, current_raw_record_id)
        ) STRICT;

        CREATE INDEX IF NOT EXISTS raw_records_by_extraction
            ON raw_records(extraction_run_id, extraction_ordinal);
        CREATE INDEX IF NOT EXISTS canonical_entries_by_pocket_state
            ON canonical_entries(extraction_run_id, currency_pocket, lifecycle_state);
        CREATE INDEX IF NOT EXISTS issues_by_extraction
            ON extraction_issues(extraction_run_id, severity);

        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            institution TEXT NOT NULL,
            source_type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            masked_identifier TEXT NOT NULL,
            UNIQUE (institution, source_type, masked_identifier)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS statements (
            id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL UNIQUE REFERENCES extraction_runs(id),
            evidence_sha256 TEXT NOT NULL REFERENCES source_content(sha256),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            institution TEXT NOT NULL,
            source_type TEXT NOT NULL,
            statement_date TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT,
            statement_identity TEXT NOT NULL,
            selection_state TEXT NOT NULL CHECK (selection_state IN ('SELECTED', 'CANDIDATE', 'QUARANTINED')),
            conflict_state TEXT NOT NULL CHECK (conflict_state IN ('NONE', 'CONFLICT')),
            UNIQUE (statement_identity, evidence_sha256)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS statement_sections (
            id TEXT PRIMARY KEY,
            statement_id TEXT NOT NULL REFERENCES statements(id),
            section_ordinal INTEGER NOT NULL CHECK (section_ordinal > 0),
            section_kind TEXT NOT NULL,
            currency TEXT,
            heading_raw TEXT NOT NULL,
            start_page INTEGER NOT NULL CHECK (start_page > 0),
            end_page INTEGER NOT NULL CHECK (end_page >= start_page),
            UNIQUE (statement_id, section_ordinal)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS page_coverage (
            id INTEGER PRIMARY KEY,
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
            statement_id TEXT NOT NULL REFERENCES statements(id),
            page_number INTEGER NOT NULL CHECK (page_number > 0),
            classification TEXT NOT NULL CHECK (classification IN ('SUMMARY', 'TRANSACTION', 'MIXED', 'INFORMATION', 'UNKNOWN')),
            word_count INTEGER NOT NULL CHECK (word_count >= 0),
            upright_word_count INTEGER NOT NULL CHECK (upright_word_count >= 0),
            rotated_ignored_count INTEGER NOT NULL CHECK (rotated_ignored_count >= 0),
            transaction_rows INTEGER NOT NULL CHECK (transaction_rows >= 0),
            supporting_records INTEGER NOT NULL CHECK (supporting_records >= 0),
            status TEXT NOT NULL CHECK (status IN ('ACCOUNTED', 'UNRESOLVED')),
            context_json TEXT NOT NULL,
            UNIQUE (extraction_run_id, page_number)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS document_records (
            id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
            evidence_sha256 TEXT NOT NULL REFERENCES source_content(sha256),
            statement_id TEXT NOT NULL REFERENCES statements(id),
            section_id TEXT REFERENCES statement_sections(id),
            source_profile_version TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            source_anchor TEXT NOT NULL,
            extraction_ordinal INTEGER NOT NULL CHECK (extraction_ordinal > 0),
            page_number INTEGER NOT NULL CHECK (page_number > 0),
            x0_mpt INTEGER,
            top_mpt INTEGER,
            x1_mpt INTEGER,
            bottom_mpt INTEGER,
            record_kind TEXT NOT NULL CHECK (record_kind IN ('TRANSACTION', 'SUPPORTING_BALANCE', 'SUMMARY', 'RECOGNIZED_REGION', 'UNRESOLVED')),
            raw_text TEXT NOT NULL,
            raw_fields_json TEXT NOT NULL,
            date_raw TEXT,
            date_value TEXT,
            description_raw TEXT,
            reference_raw TEXT,
            debit_text TEXT,
            debit_coefficient INTEGER CHECK (debit_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            debit_scale INTEGER CHECK (debit_scale BETWEEN 0 AND {MAX_SCALE}),
            credit_text TEXT,
            credit_coefficient INTEGER CHECK (credit_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            credit_scale INTEGER CHECK (credit_scale BETWEEN 0 AND {MAX_SCALE}),
            amount_text TEXT,
            amount_coefficient INTEGER CHECK (amount_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            amount_scale INTEGER CHECK (amount_scale BETWEEN 0 AND {MAX_SCALE}),
            direction_marker TEXT,
            currency_raw TEXT,
            balance_text TEXT,
            balance_coefficient INTEGER CHECK (balance_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            balance_scale INTEGER CHECK (balance_scale BETWEEN 0 AND {MAX_SCALE}),
            confidence TEXT NOT NULL CHECK (confidence IN ('HIGH', 'MEDIUM', 'UNRESOLVED')),
            required_fields_complete INTEGER NOT NULL CHECK (required_fields_complete IN (0, 1)),
            UNIQUE (extraction_run_id, source_anchor),
            UNIQUE (extraction_run_id, extraction_ordinal)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS document_entries (
            id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
            statement_id TEXT NOT NULL REFERENCES statements(id),
            document_record_id TEXT NOT NULL UNIQUE REFERENCES document_records(id),
            account_id TEXT NOT NULL REFERENCES accounts(id),
            currency_pocket TEXT NOT NULL,
            posted_date TEXT NOT NULL,
            amount_text TEXT NOT NULL,
            amount_coefficient INTEGER NOT NULL CHECK (amount_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            amount_scale INTEGER NOT NULL CHECK (amount_scale BETWEEN 0 AND {MAX_SCALE}),
            event_kind TEXT NOT NULL,
            economic_direction TEXT NOT NULL CHECK (economic_direction IN ('IN', 'OUT', 'ZERO')),
            analytics_eligible INTEGER NOT NULL CHECK (analytics_eligible IN (0, 1))
        ) STRICT;

        CREATE TABLE IF NOT EXISTS document_reconciliation_checks (
            id INTEGER PRIMARY KEY,
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
            statement_id TEXT NOT NULL REFERENCES statements(id),
            currency_pocket TEXT NOT NULL,
            check_type TEXT NOT NULL,
            equation_version TEXT NOT NULL,
            expected_text TEXT,
            expected_coefficient INTEGER CHECK (expected_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            expected_scale INTEGER CHECK (expected_scale BETWEEN 0 AND {MAX_SCALE}),
            actual_text TEXT,
            actual_coefficient INTEGER CHECK (actual_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            actual_scale INTEGER CHECK (actual_scale BETWEEN 0 AND {MAX_SCALE}),
            difference_text TEXT,
            difference_coefficient INTEGER CHECK (difference_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            difference_scale INTEGER CHECK (difference_scale BETWEEN 0 AND {MAX_SCALE}),
            passed INTEGER CHECK (passed IN (0, 1)),
            blocking INTEGER NOT NULL CHECK (blocking IN (0, 1)),
            context_json TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS transfer_links (
            id TEXT PRIMARY KEY,
            left_entry_kind TEXT NOT NULL CHECK (left_entry_kind IN ('CSV', 'DOCUMENT')),
            left_entry_id TEXT NOT NULL,
            right_entry_kind TEXT NOT NULL CHECK (right_entry_kind IN ('CSV', 'DOCUMENT')),
            right_entry_id TEXT NOT NULL,
            link_kind TEXT NOT NULL CHECK (link_kind IN ('SAME_CURRENCY_TRANSFER', 'CURRENCY_EXCHANGE')),
            left_currency TEXT NOT NULL,
            left_amount_text TEXT NOT NULL,
            left_amount_coefficient INTEGER NOT NULL CHECK (left_amount_coefficient BETWEEN 0 AND {MAX_ABS_COEFFICIENT}),
            left_amount_scale INTEGER NOT NULL CHECK (left_amount_scale BETWEEN 0 AND {MAX_SCALE}),
            right_currency TEXT NOT NULL,
            right_amount_text TEXT NOT NULL,
            right_amount_coefficient INTEGER NOT NULL CHECK (right_amount_coefficient BETWEEN 0 AND {MAX_ABS_COEFFICIENT}),
            right_amount_scale INTEGER NOT NULL CHECK (right_amount_scale BETWEEN 0 AND {MAX_SCALE}),
            implied_rate_numerator INTEGER CHECK (implied_rate_numerator > 0),
            implied_rate_denominator INTEGER CHECK (implied_rate_denominator > 0),
            method_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'PROPOSED', 'REJECTED')),
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (left_entry_kind, left_entry_id, right_entry_kind, right_entry_id, method_version)
        ) STRICT;

        CREATE INDEX IF NOT EXISTS statements_by_identity
            ON statements(statement_identity, selection_state);
        CREATE INDEX IF NOT EXISTS document_records_by_extraction
            ON document_records(extraction_run_id, extraction_ordinal);
        CREATE INDEX IF NOT EXISTS document_entries_by_account_date
            ON document_entries(account_id, posted_date, currency_pocket);
        CREATE INDEX IF NOT EXISTS page_coverage_by_extraction
            ON page_coverage(extraction_run_id, page_number);
        CREATE UNIQUE INDEX IF NOT EXISTS confirmed_transfer_link_left
            ON transfer_links(left_entry_kind, left_entry_id)
            WHERE status = 'CONFIRMED';
        CREATE UNIQUE INDEX IF NOT EXISTS confirmed_transfer_link_right
            ON transfer_links(right_entry_kind, right_entry_id)
            WHERE status = 'CONFIRMED';

        CREATE TABLE IF NOT EXISTS extraction_recovery_events (
            id TEXT PRIMARY KEY,
            extraction_run_id TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
            prior_status TEXT NOT NULL CHECK (prior_status IN ('RUNNING', 'FAILED')),
            reason TEXT NOT NULL,
            recovered_at TEXT NOT NULL
        ) STRICT;
        """
    )
    connection.execute(
        "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    connection.execute(
        "INSERT INTO schema_metadata(key, value) VALUES ('storage_security', 'plaintext-phase1') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    connection.commit()
