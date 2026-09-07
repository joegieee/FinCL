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

SCHEMA_VERSION = 14

# Busy handler timeout for the single-writer local database. Concurrent imports
# wait for the lock to clear instead of failing immediately with "database is
# locked".
BUSY_TIMEOUT_MS = 5000

# Legacy startup migration registry remains empty. Historical ledgers require
# a tested reader or the explicit, backup-verified runner in migrations.py;
# startup must not suggest evidence-only reconstruction of originating state.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

# Schema 9 derived tables (MVP M1b, DESIGN-application-contract.md §3). Shared
# verbatim between a fresh `initialize_schema` and the registered 8->9 migration
# so a migrated ledger and a fresh one have byte-identical layout.
SCHEMA_9_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS projection_generation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation_token TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    completed_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS command_receipts (
    request_id TEXT PRIMARY KEY,
    intent_hash TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    record_hash TEXT NOT NULL,
    decision_kind TEXT NOT NULL
) STRICT;
"""

# Schema 10 derived tables (v0.2 analysis, slice 1). The one committed economic
# component projection the report totals and the `/contributors/{handle}` rows
# both read. Disposable: `refresh` deletes and repopulates them for the current
# generation in the same transaction that publishes it. Shared verbatim between a
# fresh `initialize_schema` and the registered 9->10 migration so a migrated
# ledger and a fresh one have byte-identical layout.
SCHEMA_10_TABLES_SQL = f"""
CREATE TABLE IF NOT EXISTS analysis_components (
    component_id TEXT PRIMARY KEY,
    origin_component_id TEXT NOT NULL,
    generation_token TEXT NOT NULL,
    lens TEXT NOT NULL,
    origin_kind TEXT NOT NULL,
    origin_anchor TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('amount', 'fee')),
    bucket TEXT NOT NULL,
    signed_coefficient INTEGER NOT NULL
        CHECK (signed_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
    scale INTEGER NOT NULL CHECK (scale BETWEEN 0 AND {MAX_SCALE}),
    currency TEXT NOT NULL,
    represented_start TEXT NOT NULL,
    represented_end TEXT NOT NULL,
    date_precision TEXT NOT NULL,
    period TEXT NOT NULL,
    category_key TEXT NOT NULL,
    merchant_key TEXT,
    merchant_display TEXT NOT NULL,
    account TEXT NOT NULL,
    source_location TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    decision_seqs TEXT NOT NULL,
    nets INTEGER NOT NULL CHECK (nets IN (0, 1)),
    eligible INTEGER NOT NULL DEFAULT 1 CHECK (eligible IN (0, 1))
) STRICT;

CREATE INDEX IF NOT EXISTS analysis_components_scope
    ON analysis_components(generation_token, lens, currency, represented_start);
CREATE INDEX IF NOT EXISTS analysis_components_bucket
    ON analysis_components(generation_token, lens, bucket);
CREATE INDEX IF NOT EXISTS analysis_components_category
    ON analysis_components(generation_token, lens, category_key);
CREATE INDEX IF NOT EXISTS analysis_components_merchant
    ON analysis_components(generation_token, lens, merchant_key);

CREATE TABLE IF NOT EXISTS component_relationships (
    component_id TEXT NOT NULL,
    relationship_kind TEXT NOT NULL,
    relationship_id TEXT NOT NULL,
    PRIMARY KEY (component_id, relationship_kind, relationship_id)
) STRICT;

CREATE TABLE IF NOT EXISTS component_source_refs (
    component_id TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    locator TEXT NOT NULL,
    PRIMARY KEY (component_id, evidence_sha256, locator)
) STRICT;
"""


# Schema 11 derived tables (v0.2 analysis slices 3-4). The replayable relationship
# projection: manual duplicate suppressions and refund allocations, each the
# latest journal record per identity (a RETIRE leaves no row). Rebuilt by every
# journal replay, never migrated in place. Shared verbatim between a fresh
# `initialize_schema` and the registered 10->11 migration.
SCHEMA_11_TABLES_SQL = f"""
CREATE TABLE IF NOT EXISTS relationship_decisions (
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    batch_index INTEGER,
    payload_json TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
    dangling_reason TEXT,
    PRIMARY KEY (kind, identity)
) STRICT;

-- One row per entry that a bound DUPLICATE_CONFIRM suppresses. A suppressed
-- entry contributes no amount or fee component to any analysis.
CREATE TABLE IF NOT EXISTS duplicate_suppressions (
    suppressed_anchor TEXT PRIMARY KEY,
    primary_anchor TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    reason TEXT NOT NULL
) STRICT;

-- One row per (refund, purchase) allocation from a bound REFUND_ALLOCATION_SET.
-- Amounts are exact in the stated currency, with both sides carried for a
-- cross-currency allocation.
CREATE TABLE IF NOT EXISTS refund_allocations (
    refund_anchor TEXT NOT NULL,
    purchase_anchor TEXT NOT NULL,
    refund_coefficient INTEGER NOT NULL,
    refund_scale INTEGER NOT NULL CHECK (refund_scale BETWEEN 0 AND {MAX_SCALE}),
    purchase_coefficient INTEGER NOT NULL,
    purchase_scale INTEGER NOT NULL CHECK (purchase_scale BETWEEN 0 AND {MAX_SCALE}),
    currency TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    PRIMARY KEY (refund_anchor, purchase_anchor)
) STRICT;
"""


# Schema 12 (v0.5 bulk actions). The schema 8 categorization projection tables
# carried `journal_seq INTEGER NOT NULL UNIQUE`, an identity claim that predates
# DECISION_BATCH: one batch record now projects a child row per item, so several
# rows legitimately share the outer record's seq. This step rebuilds the six
# affected tables to drop that UNIQUE; every other column, check and primary key
# is unchanged, so a migrated ledger and a fresh one stay byte-identical. No row
# is rewritten. `initialize_schema` already emits the post-12 layout for fresh
# ledgers; this DDL is applied only by the registered 11->12 migration and must
# be kept free of semicolons outside statement terminators (see
# `migrations._apply_table_ddl`).
SCHEMA_12_MIGRATION_SQL = """
CREATE TABLE link_decisions__s12 (
    left_anchor TEXT NOT NULL,
    right_anchor TEXT NOT NULL,
    anchor_version TEXT NOT NULL,
    link_kind TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('CONFIRM', 'REJECT')),
    policy_version TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
    dangling_reason TEXT,
    link_purpose TEXT CHECK (link_purpose IN
        ('transfer', 'card_repayment', 'wallet_topup')),
    PRIMARY KEY(left_anchor, right_anchor, anchor_version, link_kind)
) STRICT;
INSERT INTO link_decisions__s12 SELECT * FROM link_decisions;
DROP TABLE link_decisions;
ALTER TABLE link_decisions__s12 RENAME TO link_decisions;

CREATE TABLE invariant_escapes__s12 (
    invariant_code TEXT NOT NULL,
    anchors_json TEXT NOT NULL,
    anchor_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
    dangling_reason TEXT,
    PRIMARY KEY(invariant_code, anchors_json, anchor_version)
) STRICT;
INSERT INTO invariant_escapes__s12 SELECT * FROM invariant_escapes;
DROP TABLE invariant_escapes;
ALTER TABLE invariant_escapes__s12 RENAME TO invariant_escapes;

CREATE TABLE inbound_classifications__s12 (
    anchor TEXT NOT NULL,
    anchor_version TEXT NOT NULL,
    classification TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
    dangling_reason TEXT,
    PRIMARY KEY(anchor, anchor_version)
) STRICT;
INSERT INTO inbound_classifications__s12 SELECT * FROM inbound_classifications;
DROP TABLE inbound_classifications;
ALTER TABLE inbound_classifications__s12 RENAME TO inbound_classifications;

CREATE TABLE merchant_aliases__s12 (
    canonical TEXT PRIMARY KEY,
    patterns_json TEXT NOT NULL,
    display_name TEXT,
    reason TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    record_hash TEXT NOT NULL
) STRICT;
INSERT INTO merchant_aliases__s12 SELECT * FROM merchant_aliases;
DROP TABLE merchant_aliases;
ALTER TABLE merchant_aliases__s12 RENAME TO merchant_aliases;

CREATE TABLE category_rules__s12 (
    rule_id TEXT PRIMARY KEY,
    match_json TEXT NOT NULL,
    group_name TEXT NOT NULL,
    category TEXT NOT NULL,
    migrated_from TEXT,
    reason TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    record_hash TEXT NOT NULL
) STRICT;
INSERT INTO category_rules__s12 SELECT * FROM category_rules;
DROP TABLE category_rules;
ALTER TABLE category_rules__s12 RENAME TO category_rules;

CREATE TABLE category_overrides__s12 (
    entry_anchor TEXT PRIMARY KEY,
    anchor_version TEXT NOT NULL,
    group_name TEXT NOT NULL,
    category TEXT NOT NULL,
    migrated_from TEXT,
    reason TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    record_hash TEXT NOT NULL,
    bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
    dangling_reason TEXT
) STRICT;
INSERT INTO category_overrides__s12 SELECT * FROM category_overrides;
DROP TABLE category_overrides;
ALTER TABLE category_overrides__s12 RENAME TO category_overrides;
"""


# Schema 13 derived tables (v0.3 personal debts and shared expenses,
# DESIGN-debts-and-context.md §§1-3). The disposable debt projection: identity
# and lifecycle in `personal_debts`, the dated principal timeline in
# `personal_debt_events`, per-source component claims in
# `personal_debt_allocations`, and the one-per-entry owner residual in
# `entry_residuals`. All are rebuilt by every journal replay from the
# `PERSONAL_DEBT_*` and `ENTRY_RESIDUAL_*` decision kinds; none carry a mutable
# authoritative balance (as-of balances are computed from the event rows).
# Shared verbatim between a fresh `initialize_schema` and the registered 12->13
# migration.
SCHEMA_13_TABLES_SQL = f"""
CREATE TABLE IF NOT EXISTS personal_debts (
    debt_id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK (direction IN ('PAYABLE', 'RECEIVABLE')),
    currency TEXT NOT NULL,
    counterparty_label TEXT NOT NULL,
    opened_on TEXT NOT NULL,
    due_on TEXT,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    closed_on TEXT,
    opened_seq INTEGER NOT NULL,
    details_seq INTEGER,
    lifecycle_seq INTEGER,
    reason TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS personal_debts_scope
    ON personal_debts(status, currency, direction);

CREATE TABLE IF NOT EXISTS personal_debt_events (
    debt_id TEXT NOT NULL REFERENCES personal_debts(debt_id),
    event_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    effective_on TEXT NOT NULL,
    principal_coefficient INTEGER NOT NULL
        CHECK (principal_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
    principal_scale INTEGER NOT NULL CHECK (principal_scale BETWEEN 0 AND {MAX_SCALE}),
    signed INTEGER NOT NULL CHECK (signed IN (-1, 1)),
    opening_balance INTEGER NOT NULL DEFAULT 0 CHECK (opening_balance IN (0, 1)),
    expense_category TEXT,
    journal_seq INTEGER NOT NULL,
    batch_index INTEGER,
    bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
    dangling_reason TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY (debt_id, event_id)
) STRICT;

CREATE INDEX IF NOT EXISTS personal_debt_events_timeline
    ON personal_debt_events(debt_id, effective_on, event_id);

CREATE TABLE IF NOT EXISTS personal_debt_allocations (
    debt_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    seq_in_event INTEGER NOT NULL,
    entry_anchor TEXT NOT NULL,
    entry_id TEXT,
    principal_coefficient INTEGER NOT NULL,
    principal_scale INTEGER NOT NULL CHECK (principal_scale BETWEEN 0 AND {MAX_SCALE}),
    interest_coefficient INTEGER NOT NULL,
    interest_scale INTEGER NOT NULL CHECK (interest_scale BETWEEN 0 AND {MAX_SCALE}),
    fee_coefficient INTEGER NOT NULL,
    fee_scale INTEGER NOT NULL CHECK (fee_scale BETWEEN 0 AND {MAX_SCALE}),
    separate_fee_coefficient INTEGER NOT NULL,
    separate_fee_scale INTEGER NOT NULL CHECK (separate_fee_scale BETWEEN 0 AND {MAX_SCALE}),
    currency TEXT NOT NULL,
    bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
    PRIMARY KEY (debt_id, event_id, seq_in_event),
    FOREIGN KEY (debt_id, event_id)
        REFERENCES personal_debt_events(debt_id, event_id)
) STRICT;

CREATE INDEX IF NOT EXISTS personal_debt_allocations_source
    ON personal_debt_allocations(entry_anchor);

CREATE TABLE IF NOT EXISTS entry_residuals (
    entry_anchor TEXT PRIMARY KEY,
    expense_coefficient INTEGER NOT NULL,
    expense_scale INTEGER NOT NULL CHECK (expense_scale BETWEEN 0 AND {MAX_SCALE}),
    expense_category TEXT NOT NULL,
    currency TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    batch_index INTEGER,
    bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
    dangling_reason TEXT,
    reason TEXT NOT NULL
) STRICT;
"""


# Schema 14 derived table (v0.4 credit cards, slice 1;
# docs/releases/v0.4-cards.md §0, §2). `card_statement_facts` is an
# extraction-derived projection — written in `document_service._persist_document`
# alongside `document_records` / `document_reconciliation_checks` and rebuilt by
# `fincl rebuild`, never a journal-replay projection. One row per
# (statement, currency pocket, versioned field role) carrying the exact printed
# value or date, a source locator, and the parser version that read it. The
# printed summary equation and its reconciliation status are NOT duplicated here;
# the cards projection folds those from `document_reconciliation_checks`. Shared
# verbatim between a fresh `initialize_schema` and the registered 13->14
# migration so a migrated ledger and a fresh one have byte-identical layout.
SCHEMA_14_TABLES_SQL = f"""
CREATE TABLE IF NOT EXISTS card_statement_facts (
    statement_id TEXT NOT NULL REFERENCES statements(id),
    extraction_run_id TEXT NOT NULL REFERENCES extraction_runs(id),
    currency_pocket TEXT NOT NULL,
    field_role TEXT NOT NULL,
    value_kind TEXT NOT NULL CHECK (value_kind IN ('money', 'date')),
    value_text TEXT NOT NULL,
    value_coefficient INTEGER
        CHECK (value_coefficient BETWEEN {-MAX_ABS_COEFFICIENT} AND {MAX_ABS_COEFFICIENT}),
    value_scale INTEGER CHECK (value_scale BETWEEN 0 AND {MAX_SCALE}),
    source_locator TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    role_version TEXT NOT NULL,
    CHECK ((value_kind = 'money') = (value_coefficient IS NOT NULL AND value_scale IS NOT NULL)),
    PRIMARY KEY (statement_id, currency_pocket, field_role)
) STRICT;

CREATE INDEX IF NOT EXISTS card_statement_facts_by_extraction
    ON card_statement_facts(extraction_run_id);
"""


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
            journal_seq INTEGER NOT NULL,
            snapshot TEXT NOT NULL,
            bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
            dangling_reason TEXT,
            -- Optional operator-stated Internal purpose for a confirmed
            -- SAME_CURRENCY_TRANSFER (§6.2): transfer / card_repayment /
            -- wallet_topup. NULL falls back to the versioned account-kind
            -- derivation.
            link_purpose TEXT CHECK (link_purpose IN
                ('transfer', 'card_repayment', 'wallet_topup')),
            PRIMARY KEY(left_anchor, right_anchor, anchor_version, link_kind)
        ) STRICT;

        CREATE TABLE IF NOT EXISTS invariant_escapes (
            invariant_code TEXT NOT NULL,
            anchors_json TEXT NOT NULL,
            anchor_version TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            journal_seq INTEGER NOT NULL,
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
            journal_seq INTEGER NOT NULL,
            snapshot TEXT NOT NULL,
            bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
            dangling_reason TEXT,
            PRIMARY KEY(anchor, anchor_version)
        ) STRICT;

        -- Categorization rules / aliases / overrides are decision-journal kinds
        -- (Confirmed 28). These are the replayable projection: the latest
        -- record per anchor wins, and an anchor whose latest record is a
        -- RETIRE is absent entirely. `record_hash` is carried so §5.3's
        -- rule_set_hash can be computed from the projection. `journal_seq` is
        -- provenance, not an identity: one DECISION_BATCH record projects a row
        -- per child, so many rows can legitimately share its seq (schema 12).
        CREATE TABLE IF NOT EXISTS merchant_aliases (
            canonical TEXT PRIMARY KEY,
            patterns_json TEXT NOT NULL,
            display_name TEXT,
            reason TEXT NOT NULL,
            journal_seq INTEGER NOT NULL,
            record_hash TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS category_rules (
            rule_id TEXT PRIMARY KEY,
            match_json TEXT NOT NULL,
            group_name TEXT NOT NULL,
            category TEXT NOT NULL,
            migrated_from TEXT,
            reason TEXT NOT NULL,
            journal_seq INTEGER NOT NULL,
            record_hash TEXT NOT NULL
        ) STRICT;

        CREATE TABLE IF NOT EXISTS category_overrides (
            entry_anchor TEXT PRIMARY KEY,
            anchor_version TEXT NOT NULL,
            group_name TEXT NOT NULL,
            category TEXT NOT NULL,
            migrated_from TEXT,
            reason TEXT NOT NULL,
            journal_seq INTEGER NOT NULL,
            record_hash TEXT NOT NULL,
            bound INTEGER NOT NULL CHECK (bound IN (0, 1)),
            dangling_reason TEXT
        ) STRICT;

        -- One reproducible categorization run: every version / hash / cutoff
        -- that shaped `entry_categories` (§5.2). `id` is a hash of every other
        -- column, so two runs with the same id produced the same categories.
        CREATE TABLE IF NOT EXISTS categorization_runs (
            id TEXT PRIMARY KEY,
            engine_version TEXT NOT NULL,
            precedence_policy TEXT NOT NULL,
            rule_set_hash TEXT NOT NULL,
            override_set_hash TEXT NOT NULL,
            link_purpose_version TEXT NOT NULL,
            direction_policy_version TEXT NOT NULL,
            taxonomy_version TEXT NOT NULL,
            merchant_norm_version TEXT NOT NULL,
            projection_version TEXT NOT NULL,
            journal_seq INTEGER NOT NULL,
            eligible_cutoff TEXT NOT NULL,
            eligible_entry_count INTEGER NOT NULL,
            eligible_set_hash TEXT NOT NULL,
            profile_set_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL
        ) STRICT;

        -- One (group, category) per eligible entry, with an inspectable basis
        -- (§5.1). Recomputed on categorize / rebuild, never migrated.
        CREATE TABLE IF NOT EXISTS entry_categories (
            entry_id TEXT PRIMARY KEY REFERENCES ledger_entries(id),
            group_name TEXT NOT NULL,
            category TEXT NOT NULL,
            precedence_tier INTEGER NOT NULL CHECK (precedence_tier BETWEEN 1 AND 6),
            basis TEXT NOT NULL,
            ambiguous INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous IN (0, 1)),
            direction_conflict INTEGER NOT NULL DEFAULT 0 CHECK (direction_conflict IN (0, 1)),
            run_id TEXT NOT NULL REFERENCES categorization_runs(id),
            categorized_at TEXT NOT NULL
        ) STRICT;

        -- The raw -> canonical merchant trail, one row per eligible ledger
        -- entry. Derived, recomputed on categorize / rebuild, never migrated
        -- (§4.4). NULLs propagate: a description-less row has NULL
        -- description_text / merchant_raw, not "".
        CREATE TABLE IF NOT EXISTS merchant_observations (
            entry_id TEXT PRIMARY KEY REFERENCES ledger_entries(id),
            description_text TEXT,
            paynow_to TEXT,
            merchant_raw TEXT,
            merchant_normalized TEXT,
            merchant_canonical TEXT,
            alias_applied INTEGER NOT NULL CHECK (alias_applied IN (0, 1)),
            alias_ambiguous INTEGER NOT NULL CHECK (alias_ambiguous IN (0, 1)),
            norm_version TEXT NOT NULL,
            projection_version TEXT NOT NULL
        ) STRICT;

        """
        + SCHEMA_9_TABLES_SQL
        + SCHEMA_10_TABLES_SQL
        + SCHEMA_11_TABLES_SQL
        + SCHEMA_13_TABLES_SQL
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


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing ledger for reading only (MVP M1a query surface).

    The connection is opened with SQLite's ``mode=ro`` flag and pinned with
    ``PRAGMA query_only`` so a read path cannot initialize the schema, replay
    the journal, or recompute a projection as a side effect of a query. The
    database must already exist and carry a schema stamp: preparation
    (`fincl refresh`) is an explicit step, not something a report does on the
    way past.
    """
    path = Path(path)
    if not path.exists():
        raise SchemaVersionError(
            f"ledger {path} does not exist; import a statement or run "
            "`fincl refresh` before querying it"
        )
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA query_only = ON")
    stored = _stored_schema_version(connection)
    if stored is None:
        connection.close()
        raise SchemaVersionError(
            f"ledger {path} has no schema stamp; run `fincl refresh` before querying it"
        )
    if stored != SCHEMA_VERSION:
        connection.close()
        raise SchemaVersionError(
            f"ledger {path} is at schema version {stored}, not {SCHEMA_VERSION}; "
            "run `fincl refresh` (or a supported migration) before querying it"
        )
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
                "preserve the database, evidence vault and journal; use a compatible "
                "FinCL version or backup until a tested migration or historical reader "
                "is available. Rebuild currently supports only schema 8"
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

        -- Binds this database to its decision journal (F24,
        -- DESIGN-recovery-and-application.md §3). `verified_seq`/`verified_hash`
        -- is a checkpoint an internally valid hash chain cannot supply: it is
        -- what detects removal of a complete journal tail. Added additively at
        -- schema 8 so an existing ledger keeps working and is bound explicitly
        -- with `fincl adopt-ledger` rather than forced through a rebuild.
        CREATE TABLE IF NOT EXISTS ledger_association (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ledger_id TEXT NOT NULL,
            journal_relative_path TEXT NOT NULL,
            journal_id TEXT NOT NULL,
            verified_seq INTEGER NOT NULL CHECK (verified_seq >= 0),
            verified_hash TEXT NOT NULL
        ) STRICT;

        -- Applied schema migrations (F27, DESIGN-recovery-and-application.md §5).
        -- `from_version`, `to_version`, and `checksum` are immutable once
        -- written. Normal startup never migrates; `fincl` applies a migration
        -- only on explicit request and only after a verified backup.
        CREATE TABLE IF NOT EXISTS schema_migrations (
            to_version INTEGER PRIMARY KEY,
            from_version INTEGER NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
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
            -- Which one ACCEPTED run per evidence blob analysis reads (Confirmed 30).
            -- Orthogonal to `status`: `status` is how extraction went, this is
            -- whether its rows are the selected ones. Recomputed by
            -- `selection.recompute_selection`; a run starts non-analytical.
            selection_state TEXT NOT NULL DEFAULT 'QUARANTINED'
                CHECK (selection_state IN ('SELECTED', 'SUPERSEDED', 'QUARANTINED')),
            header_text TEXT,
            header_cells_json TEXT,
            header_sha256 TEXT,
            physical_data_row_count INTEGER CHECK (physical_data_row_count >= 0),
            raw_record_count INTEGER CHECK (raw_record_count >= 0),
            canonical_entry_count INTEGER CHECK (canonical_entry_count >= 0),  -- legacy name; = ledger_entries for this run
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
        CREATE UNIQUE INDEX IF NOT EXISTS one_selected_run_per_evidence
            ON extraction_runs(evidence_sha256)
            WHERE selection_state = 'SELECTED';
        CREATE INDEX IF NOT EXISTS extraction_runs_by_selection
            ON extraction_runs(selection_state, evidence_sha256);
        CREATE UNIQUE INDEX IF NOT EXISTS confirmed_transfer_link_left
            ON transfer_links(left_entry_id)
            WHERE status = 'CONFIRMED';
        CREATE UNIQUE INDEX IF NOT EXISTS confirmed_transfer_link_right
            ON transfer_links(right_entry_id)
            WHERE status = 'CONFIRMED';

        """
        + SCHEMA_14_TABLES_SQL
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
