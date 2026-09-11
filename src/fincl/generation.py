"""The committed projection generation and its identity token (M1b, §3).

Every refresh ends by writing a single ``projection_generation`` row. Its
``generation_token`` is the SHA-256 of the canonical manifest bytes with
completion timestamps excluded, so an unchanged refresh reproduces the same
token and any change to an input — evidence selection, the FX row inventory, a
policy version, the journal head, the categorization run — produces a new one.

A reader reads this row first and then every other row in the same snapshot, so
two coordinated panels can never mix generations. Runtime failure or busy
details are transient and never recorded here.
"""

from __future__ import annotations

import hashlib
import sqlite3
from functools import lru_cache
from pathlib import Path

from . import __version__
from .common import TAXONOMY_VERSION, canonical_json, utc_now
from .db import SCHEMA_VERSION
from .decision_journal import GENESIS_HASH
from .decisions import NATURAL_ANCHOR_VERSION
from .engine import (
    DIRECTION_POLICY_VERSION,
    ENGINE_VERSION,
    LINK_PURPOSE_VERSION,
    PRECEDENCE_POLICY,
)
from .intervals import COVERAGE_POLICY_VERSION
from .merchant import MERCHANT_NORM_VERSION, PROJECTION_VERSION
from .reporting_currency import RATE_PATH_POLICY_VERSION, reporting_policy

FX_SELECTION_POLICY = "fx-selection-latest-first-import-v1"


@lru_cache(maxsize=1)
def source_digest() -> str:
    """SHA-256 over every ``fincl`` source file, so an uncommitted edit shows.

    A package version alone is insufficient (§3): a developer running a modified
    build must produce a different generation token than a released one.
    """
    package_dir = Path(__file__).resolve().parent
    hasher = hashlib.sha256()
    for path in sorted(package_dir.rglob("*.py")):
        hasher.update(path.relative_to(package_dir).as_posix().encode())
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _policy_versions() -> dict[str, str]:
    """Policy versions that shape a projection but are not recorded per row.

    Parser, profile and balance-equation versions are captured instead through
    the extraction-selection and reconciliation-check digests below.
    """
    return {
        "coverage": COVERAGE_POLICY_VERSION,
        "natural_anchor": NATURAL_ANCHOR_VERSION,
        "taxonomy": TAXONOMY_VERSION,
        "category_engine": ENGINE_VERSION,
        "precedence": PRECEDENCE_POLICY,
        "link_purpose": LINK_PURPOSE_VERSION,
        "direction_policy": DIRECTION_POLICY_VERSION,
        "merchant_norm": MERCHANT_NORM_VERSION,
        "entry_projection": PROJECTION_VERSION,
        "fx_selection": FX_SELECTION_POLICY,
        "rate_path": RATE_PATH_POLICY_VERSION,
    }


def _rows_digest(connection: sqlite3.Connection, sql: str) -> str:
    hasher = hashlib.sha256()
    for row in connection.execute(sql):
        hasher.update(canonical_json([*row]).encode())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _journal_head(connection: sqlite3.Connection) -> tuple[int, str]:
    row = connection.execute(
        "SELECT verified_seq, verified_hash FROM ledger_association WHERE id = 1"
    ).fetchone()
    if row is None:
        watermark = connection.execute(
            "SELECT applied_seq FROM decision_watermark WHERE id = 1"
        ).fetchone()
        return (int(watermark[0]) if watermark else 0), GENESIS_HASH
    return int(row["verified_seq"]), str(row["verified_hash"])


def build_manifest(connection: sqlite3.Connection) -> dict[str, object]:
    """The complete input state a refresh consumed, as a canonical mapping."""
    seq, head = _journal_head(connection)
    categorization_run = connection.execute(
        "SELECT id FROM categorization_runs ORDER BY completed_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return {
        "schema_version": SCHEMA_VERSION,
        "source_digest": source_digest(),
        "app_version": __version__,
        "policy_versions": _policy_versions(),
        "inventory_digest": _rows_digest(
            connection,
            "SELECT sha256, byte_size, media_type, evidence_key, storage_kind "
            "FROM source_content ORDER BY sha256",
        ),
        "extraction_selection_digest": _rows_digest(
            connection,
            "SELECT id, evidence_sha256, source_profile_version, parser_version, "
            "extraction_policy_version, status, selection_state "
            "FROM extraction_runs ORDER BY id",
        ),
        "statement_selection_digest": _rows_digest(
            connection,
            "SELECT id, extraction_run_id, statement_identity, selection_state, "
            "conflict_state FROM statements ORDER BY id",
        ),
        "reconciliation_equation_digest": _rows_digest(
            connection,
            "SELECT DISTINCT equation_version FROM reconciliation_checks "
            "UNION SELECT DISTINCT equation_version FROM document_reconciliation_checks "
            "ORDER BY 1",
        ),
        "raw_input_digest": _rows_digest(
            connection,
            "SELECT 'raw' AS k, evidence_sha256, count(*) FROM raw_records "
            "GROUP BY evidence_sha256 "
            "UNION ALL "
            "SELECT 'doc' AS k, evidence_sha256, count(*) FROM document_records "
            "GROUP BY evidence_sha256 "
            "ORDER BY 1, 2",
        ),
        "fx_row_inventory_digest": _rows_digest(
            connection,
            "SELECT base_currency, quote_currency, period, rate_numerator, "
            "rate_denominator, source, source_version, imported_at "
            "FROM fx_rates ORDER BY base_currency, quote_currency, period, source_version",
        ),
        "journal_seq": seq,
        "journal_head": head,
        "categorization_run_id": categorization_run["id"] if categorization_run else None,
        "reporting_policy": _reporting_policy_manifest_input(connection),
    }


def _reporting_policy_manifest_input(connection: sqlite3.Connection) -> dict[str, object]:
    """The resolved reporting policy as a manifest input (§4).

    So a default-policy change (or projection corruption) changes the
    generation token exactly like any other input, whether or not an operator
    has ever recorded a ``REPORTING_CURRENCY_SET`` decision.
    """
    policy = reporting_policy(connection)
    return {
        "currency": policy.currency,
        "minor_unit_scale": policy.minor_unit_scale,
        "journal_seq": policy.journal_seq,
        "is_default": policy.is_default,
    }


def generation_token(manifest: dict[str, object]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(manifest).encode()).hexdigest()


def publish_generation(connection: sqlite3.Connection) -> str:
    """Write the singleton generation row for the state now in ``connection``.

    Idempotent: an unchanged manifest reproduces the same token and rewrites the
    same row. Returns the token. The caller owns the surrounding transaction.
    """
    manifest = build_manifest(connection)
    token = generation_token(manifest)
    connection.execute(
        """
        INSERT INTO projection_generation(id, generation_token, manifest_json, completed_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            generation_token = excluded.generation_token,
            manifest_json = excluded.manifest_json,
            completed_at = excluded.completed_at
        """,
        (token, canonical_json(manifest), utc_now()),
    )
    return token


def current_generation(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT generation_token, manifest_json, completed_at "
        "FROM projection_generation WHERE id = 1"
    ).fetchone()
