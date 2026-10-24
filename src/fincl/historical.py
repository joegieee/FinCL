"""Read a source database's schema version for reconstruction (F25).

Reconstruction opens the source read-only and never calls the current
``initialize_schema``. A version this build has no reader for fails with the
list of versions it does support; bumping a version integer in a test is not a
historical fixture, so the registry only grows when a real prior layout is
committed as a fixture.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import SCHEMA_VERSION
from .errors import SchemaVersionError

# Layouts this build can reconstruct from. Only the current schema has a
# committed real fixture so far; an older layout is added here together with its
# fixture and its explicit reader.
SUPPORTED_HISTORICAL_SCHEMAS: frozenset[int] = frozenset({SCHEMA_VERSION})


def read_source_schema_version(database: Path) -> int:
    """Return the recorded schema version of a source database, read-only."""
    connection = sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True)
    try:
        has_metadata = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_metadata'"
        ).fetchone()
        if has_metadata is None:
            raise SchemaVersionError(
                "source database has no schema_metadata table; it is older than any "
                f"layout this build can reconstruct (supported: "
                f"{sorted(SUPPORTED_HISTORICAL_SCHEMAS)})"
            )
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SchemaVersionError(f"cannot read the source schema version: {exc}") from exc
    finally:
        connection.close()
    if row is None:
        raise SchemaVersionError("source database does not record a schema version")
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise SchemaVersionError(
            f"source schema version is not an integer: {row[0]!r}"
        ) from exc
    return version


def require_reconstructable_schema(database: Path) -> int:
    version = read_source_schema_version(database)
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"source database schema version {version} is newer than this build "
            f"supports ({SCHEMA_VERSION}); upgrade fincl before reconstructing it"
        )
    if version not in SUPPORTED_HISTORICAL_SCHEMAS:
        raise SchemaVersionError(
            f"source database schema version {version} has no historical reconstruction "
            f"reader in this build; supported versions: {sorted(SUPPORTED_HISTORICAL_SCHEMAS)}"
        )
    return version
