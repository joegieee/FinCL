"""Deterministically rebuild derived SQLite state from verified evidence."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect_sqlite
from .decision_journal import default_journal_path
from .document_service import DocumentImportService, link_narrow_transfers
from .engine import categorize
from .errors import FinCLError
from .evidence import PlaintextEvidenceStore
from .selection import recompute_selection
from .service import FinCLService


@dataclass(frozen=True, slots=True)
class RebuildResult:
    database: Path
    sources_rebuilt: int
    accepted_extractions: int
    quarantined_extractions: int
    decisions_bound: int
    decisions_dangling: int


@dataclass(frozen=True, slots=True)
class _Source:
    sha256: str
    evidence_key: str
    profile: str
    original_path: str


def rebuild_database(database: Path, evidence_directory: Path, output: Path) -> RebuildResult:
    """Build ``output`` from evidence without modifying ``database`` or ``output``.

    The output path must not already exist.  Work is written to a sibling staging
    database and atomically published only after every verified evidence object
    has been re-imported successfully.
    """
    database = Path(database).resolve()
    evidence_directory = Path(evidence_directory).resolve()
    output = Path(output).resolve()
    if database == output:
        raise FinCLError("rebuild output must differ from the source database")
    if not database.is_file():
        raise FinCLError(f"source database does not exist: {database}")
    if output.exists():
        raise FinCLError(f"rebuild output already exists: {output}")
    if not evidence_directory.is_dir():
        raise FinCLError(f"evidence directory does not exist: {evidence_directory}")

    sources = _sources_to_rebuild(database)
    if not sources:
        raise FinCLError("source database has no extraction runs to rebuild")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.rebuild-{uuid.uuid4().hex}.sqlite3"
    store = PlaintextEvidenceStore(evidence_directory)
    accepted = 0
    quarantined = 0
    try:
        for source in sources:
            suffix = ".csv" if source.profile.startswith("revolut-") else ".pdf"
            materialized_path: Path | None = None
            try:
                with (
                    store.open_verified(source.sha256, source.evidence_key) as handle,
                    tempfile.NamedTemporaryFile(
                        mode="wb", suffix=suffix, dir=evidence_directory, delete=False
                    ) as materialized,
                ):
                    materialized.write(handle.read())
                    materialized.flush()
                    os.fsync(materialized.fileno())
                    materialized_path = Path(materialized.name)
                result = _import_source(staging, evidence_directory, materialized_path, source.profile)
            finally:
                if materialized_path is not None:
                    materialized_path.unlink(missing_ok=True)
            _restore_source_path(staging, source.sha256, source.original_path)
            if result == "ACCEPTED":
                accepted += 1
            else:
                quarantined += 1
        _recompute_narrow_links(staging)
        _carry_forward_fx_rates(database, staging)
        projection = connect_sqlite(staging)
        try:
            journal = default_journal_path(database)
            link_narrow_transfers(projection, journal_path=journal)
            categorize(projection, journal_path=journal)
            decisions_bound, decisions_dangling = projection.execute(
                "SELECT (SELECT count(*) FROM link_decisions WHERE bound=1) + "
                "(SELECT count(*) FROM invariant_escapes WHERE bound=1) + "
                "(SELECT count(*) FROM inbound_classifications WHERE bound=1) + "
                "(SELECT count(*) FROM category_overrides WHERE bound=1), "
                "(SELECT count(*) FROM link_decisions WHERE bound=0) + "
                "(SELECT count(*) FROM invariant_escapes WHERE bound=0) + "
                "(SELECT count(*) FROM inbound_classifications WHERE bound=0) + "
                "(SELECT count(*) FROM category_overrides WHERE bound=0)"
            ).fetchone()
        finally:
            projection.close()
        os.replace(staging, output)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return RebuildResult(output, len(sources), accepted, quarantined, decisions_bound, decisions_dangling)


def _sources_to_rebuild(database: Path) -> list[_Source]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT x.evidence_sha256, s.evidence_key, x.source_profile_version, s.original_path
            FROM extraction_runs x
            JOIN source_content s ON s.sha256 = x.evidence_sha256
            WHERE x.status IN ('ACCEPTED', 'QUARANTINED')
              AND x.selection_state <> 'SUPERSEDED'
            ORDER BY x.evidence_sha256, x.source_profile_version
            """
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise FinCLError(f"cannot read rebuild sources: {exc}") from exc
    finally:
        connection.close()
    seen: set[str] = set()
    sources: list[_Source] = []
    for sha256, evidence_key, profile, original_path in rows:
        if sha256 in seen:
            raise FinCLError(f"evidence {sha256} has more than one extraction profile")
        seen.add(sha256)
        sources.append(_Source(sha256, evidence_key, profile, original_path))
    return sources


def _import_source(database: Path, evidence_directory: Path, source: Path, profile: str) -> str:
    if profile.startswith("revolut-"):
        return FinCLService.plaintext_phase1(database, evidence_directory).import_revolut(source).status
    service = DocumentImportService.plaintext_phase2(database, evidence_directory)
    if profile.startswith("dbs-consolidated-"):
        return service.import_dbs(source).status
    if profile.startswith("dbs-paylah-"):
        return service.import_paylah(source).status
    if profile.startswith("mari-savings-"):
        return service.import_maribank(source).status
    if profile.startswith("mari-card-"):
        return service.import_mari_card(source).status
    raise FinCLError(f"unsupported source profile for rebuild: {profile}")


def _restore_source_path(database: Path, sha256: str, original_path: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE source_content SET original_path = ?, last_observed_path = ? WHERE sha256 = ?",
            (original_path, original_path, sha256),
        )
        connection.execute(
            "UPDATE import_runs SET source_path = ? WHERE evidence_sha256 = ?",
            (original_path, sha256),
        )
        connection.commit()
    finally:
        connection.close()


def _carry_forward_fx_rates(source: Path, staging: Path) -> None:
    """Copy reference FX rates into the rebuilt database.

    Rates are external reference data, not derived from evidence and not
    originating decisions, so a rebuild must preserve them verbatim. A source
    database that predates the ``fx_rates`` table simply has none to copy.
    """
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        has_table = reader.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fx_rates'"
        ).fetchone()
        if has_table is None:
            return
        rows = reader.execute(
            """
            SELECT base_currency, quote_currency, period, rate_numerator,
                   rate_denominator, rate_text, source, source_version, imported_at
            FROM fx_rates
            """
        ).fetchall()
    finally:
        reader.close()
    if not rows:
        return
    writer = connect_sqlite(staging)
    try:
        with writer:
            writer.executemany(
                """
                INSERT INTO fx_rates(
                    base_currency, quote_currency, period, rate_numerator,
                    rate_denominator, rate_text, source, source_version, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(base_currency, quote_currency, period, source_version)
                DO NOTHING
                """,
                rows,
            )
    finally:
        writer.close()


def _recompute_narrow_links(database: Path) -> None:
    """Make derived links independent of the evidence import order.

    ``link_narrow_transfers`` fully deletes and rebuilds every automatic link
    method from the evidence currently selected, so a single call after all
    sources are re-imported converges the staging database with an in-place one.
    """
    connection = connect_sqlite(database)
    try:
        recompute_selection(connection)
        link_narrow_transfers(connection)
    finally:
        connection.close()
