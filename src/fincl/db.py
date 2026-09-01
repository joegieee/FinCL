from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .common import LEDGER_EVENT_KINDS
from .errors import SchemaVersionError
from .money import MAX_ABS_COEFFICIENT, MAX_SCALE

_EVENT_KIND_IN = ", ".join(f"'{kind}'" for kind in sorted(LEDGER_EVENT_KINDS))

SCHEMA_VERSION = 7

# Busy handler timeout for the single-writer local database. Concurrent imports
# wait for the lock to clear instead of failing immediately with "database is
# locked".
BUSY_TIMEOUT_MS = 5000

# In-place upgrade steps keyed by the schema version they upgrade *from*. There
# is no deployed database older than SCHEMA_VERSION and processed entries are
# fully derived from the evidence vault, so the registry is intentionally empty:
# a database that predates SCHEMA_VERSION is refused with a SchemaVersionError
# that points at `fincl rebuild` rather than being migrated in place.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


def _create_decision_projection(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS decision_watermark (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            applied_seq INTEGER NOT NULL CHECK (applied_seq >= 0)
        ) STRICT;
        INSERT OR IGNORE INTO decision_watermark(id, applied_seq) VALUES (1, 0);

        CREATE TABLE IF NOT EXISTS link_decisions (
            left_anchor TEXT NOT NULL,
            right_anchor TEXT NOT NULL,
            anchor_version TEXT NOT NULL,
            link_kind TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('CONFIRM', 'REJECT')),
            policy_version TEXT NOT NULL,
            journal_seq INTEGER NOT NULL UNIQUE,
            snapshot TEXT NOT NULL,
            bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
            dangling_reason TEXT,
            PRIMARY KEY(left_anchor, right_anchor, anchor_version, link_kind)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS invariant_escapes (
            invariant_code TEXT NOT NULL,
            anchors_json TEXT NOT NULL,
            anchor_version TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            journal_seq INTEGER NOT NULL UNIQUE,
            snapshot TEXT NOT NULL,
            bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
            dangling_reason TEXT,
            PRIMARY KEY(invariant_code, anchors_json, anchor_version)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS inbound_classifications (
            anchor TEXT NOT NULL,
            anchor_version TEXT NOT NULL,
            classification TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            journal_seq INTEGER NOT NULL UNIQUE,
            snapshot TEXT NOT NULL,
            bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
            dangling_reason TEXT,
            PRIMARY KEY(anchor, anchor_version)
        ) STRICT;
        """
    )


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
                "processed entries are fully derived from the evidence vault, so run "
                "`fincl rebuild` to regenerate this database, or back up "
                "var/fincl.sqlite3 and re-import into a fresh database"
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

        CREATE TABLE IF NOT EXISTS ledger_entries (
            id TEXT PRIMARY KEY,
            source_family TEXT NOT NULL CHECK (source_family IN ('CSV', 'DOCUMENT')),
            extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),

            evidence_sha256 TEXT NOT NULL REFERENCES source_content(sha256),
            raw_record_id TEXT UNIQUE REFERENCES raw_records(id),
            document_record_id TEXT UNIQUE REFERENCES document_records(id),
            statement_id TEXT REFERENCES statements(id),
            natural_anchor TEXT NOT NULL,

            account_id TEXT NOT NULL REFERENCES accounts(id),
            currency_pocket TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            effective_precision TEXT NOT NULL DEFAULT 'DAY' CHECK (effective_precision IN ('DAY', 'MONTH', 'UNKNOWN')),
            amount_text TEXT NOT NULL,
            amount_coefficient INTEGER NOT NULL CHECK (amount_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            amount_scale INTEGER NOT NULL CHECK (amount_scale BETWEEN 0 AND {MAX_SCALE}),
            event_kind TEXT NOT NULL CHECK (event_kind IN ({_EVENT_KIND_IN})),
            economic_direction TEXT NOT NULL CHECK (economic_direction IN ('IN', 'OUT', 'ZERO')),
            lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('COMPLETED', 'PENDING', 'REVERTED')),
            analytics_eligible INTEGER NOT NULL DEFAULT 0 CHECK (analytics_eligible IN (0, 1)),

            alt_date_role TEXT CHECK (alt_date_role IN ('STARTED', 'TRANSACTION')),
            alt_date_value TEXT,
            alt_date_raw TEXT,
            alt_date_precision TEXT CHECK (alt_date_precision IN ('DAY', 'MONTH', 'UNKNOWN')),

            fee_text TEXT,
            fee_coefficient INTEGER CHECK (fee_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            fee_scale INTEGER CHECK (fee_scale BETWEEN 0 AND {MAX_SCALE}),
            balance_text TEXT,
            balance_coefficient INTEGER CHECK (balance_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            balance_scale INTEGER CHECK (balance_scale BETWEEN 0 AND {MAX_SCALE}),
            source_currency TEXT,
            source_amount_text TEXT,
            source_amount_coefficient INTEGER CHECK (source_amount_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
            source_amount_scale INTEGER CHECK (source_amount_scale BETWEEN 0 AND {MAX_SCALE}),

            CHECK ((raw_record_id IS NOT NULL) + (document_record_id IS NOT NULL) = 1),
            CHECK ((source_family = 'DOCUMENT') = (document_record_id IS NOT NULL)),
            CHECK ((alt_date_role IS NULL) = (alt_date_value IS NULL AND alt_date_raw IS NULL))
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
        CREATE INDEX IF NOT EXISTS ledger_entries_by_account_date
            ON ledger_entries(account_id, effective_at, currency_pocket);
        CREATE INDEX IF NOT EXISTS ledger_entries_by_pocket_state
            ON ledger_entries(extraction_run_id, currency_pocket, lifecycle_state);
        CREATE INDEX IF NOT EXISTS ledger_entries_by_anchor
            ON ledger_entries(natural_anchor);
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
            left_entry_id TEXT NOT NULL REFERENCES ledger_entries(id),
            right_entry_id TEXT NOT NULL REFERENCES ledger_entries(id),
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
            UNIQUE (left_entry_id, right_entry_id, method_version)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS fx_rates (
            base_currency TEXT NOT NULL CHECK (length(base_currency) = 3),
            quote_currency TEXT NOT NULL CHECK (length(quote_currency) = 3),
            period TEXT NOT NULL CHECK (length(period) = 7),
            rate_numerator INTEGER NOT NULL CHECK (rate_numerator > 0),
            rate_denominator INTEGER NOT NULL CHECK (rate_denominator > 0),
            rate_text TEXT NOT NULL,
            source TEXT NOT NULL,
            source_version TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (base_currency, quote_currency, period, source_version)
        ) STRICT;

        CREATE INDEX IF NOT EXISTS statements_by_identity
            ON statements(statement_identity, selection_state);
        CREATE INDEX IF NOT EXISTS document_records_by_extraction
            ON document_records(extraction_run_id, extraction_ordinal);
        CREATE INDEX IF NOT EXISTS page_coverage_by_extraction
            ON page_coverage(extraction_run_id, page_number);
        CREATE UNIQUE INDEX IF NOT EXISTS confirmed_transfer_link_left
            ON transfer_links(left_entry_id)
            WHERE status = 'CONFIRMED';
        CREATE UNIQUE INDEX IF NOT EXISTS confirmed_transfer_link_right
            ON transfer_links(right_entry_id)
            WHERE status = 'CONFIRMED';

        """
    )
    _create_decision_projection(connection)
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
