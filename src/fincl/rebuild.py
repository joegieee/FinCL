"""Deterministically reconstruct derived SQLite state from verified evidence.

Reconstruction (F25–F26) inventories every ``source_content`` blob independent
of analytical selection, groups extraction history by blob and supported source
family, verifies every evidence object before staging, re-imports each unique
dispatchable blob once with current code, carries forward reference FX rows
exactly, replays the shared journal, then verifies the candidate through the
normal read path before a no-clobber publish. It never activates the candidate
or touches the source. See ``docs/DESIGN-recovery-and-application.md`` §5.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .association import copy_association_for_rebuild
from .db import connect_sqlite, initialize_schema
from .decision_journal import default_journal_path
from .decisions import replay_journal as _guarded_replay
from .document_service import DocumentImportService
from .engine import categorize
from .errors import EvidenceError, FinCLError
from .evidence import PlaintextEvidenceStore
from .historical import require_reconstructable_schema
from .reconciliation import link_narrow_transfers
from .selection import recompute_selection
from .service import FinCLService

# Profile-version prefix -> the source family it dispatches to. One family may
# have several parser/profile versions; they are one input to the current
# adapter (§5). This is the single source of truth for both grouping and
# dispatch.
_ADAPTERS: dict[str, str] = {
    "revolut-": "REVOLUT_CSV",
    "revolut-pdf-": "REVOLUT_PDF",
    "dbs-consolidated-": "DBS_CONSOLIDATED",
    "dbs-paylah-": "DBS_PAYLAH",
    "mari-savings-": "MARI_SAVINGS",
    "mari-card-": "MARI_CARD",
    "trust-savings-": "TRUST_SAVINGS",
}


@dataclass(frozen=True, slots=True)
class RebuildResult:
    database: Path
    sources_rebuilt: int
    accepted_extractions: int
    quarantined_extractions: int
    decisions_bound: int
    decisions_dangling: int
    unsupported_inputs: int = 0


@dataclass(frozen=True, slots=True)
class _Source:
    sha256: str
    evidence_key: str
    profile: str
    original_path: str
    family: str


def _family_for_profile(profile: str) -> str | None:
    # Longest matching prefix wins, so "revolut-pdf-" is never shadowed by the
    # broader "revolut-" CSV prefix while both are registered.
    best: tuple[int, str] | None = None
    for prefix, family in _ADAPTERS.items():
        if profile.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), family)
    return best[1] if best is not None else None


def rebuild_database(database: Path, evidence_directory: Path, output: Path) -> RebuildResult:
    """Build ``output`` from evidence without modifying ``database`` or ``output``.

    The output path must not already exist and must sit in the source ledger's
    directory (F24). Work is written to a sibling staging database and published
    atomically, with no clobber, only after every evidence object has been
    re-imported and the candidate has been reopened through the normal read
    path.
    """
    database = Path(database).resolve()
    evidence_directory = Path(evidence_directory).resolve()
    output = Path(output).resolve()
    if database == output:
        raise FinCLError("rebuild output must differ from the source database")
    if output.parent != database.parent:
        raise FinCLError(
            "the first F24 implementation only supports a same-directory rebuild: the "
            "rebuilt copy keeps the source ledger identity and shares its one decision "
            "journal. Use `fincl backup` / `fincl restore` to relocate a ledger."
        )
    if not database.is_file():
        raise FinCLError(f"source database does not exist: {database}")
    if output.exists():
        raise FinCLError(f"rebuild output already exists: {output}")
    if not evidence_directory.is_dir():
        raise FinCLError(f"evidence directory does not exist: {evidence_directory}")

    require_reconstructable_schema(database)
    sources, unsupported = _inventory_evidence(database)
    if not sources and not unsupported:
        raise FinCLError("source database has no evidence to reconstruct")
    if unsupported:
        listed = ", ".join(sha[:12] for sha in unsupported)
        raise FinCLError(
            f"{len(unsupported)} evidence blob(s) have no supported extraction history "
            f"and cannot be reconstructed: {listed}. The bytes are retained; adopt a "
            "build that can parse them or remove them from the inventory."
        )

    store = PlaintextEvidenceStore(evidence_directory)
    _verify_all_evidence(store, sources)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.rebuild-{uuid.uuid4().hex}.sqlite3"
    accepted = 0
    quarantined = 0
    try:
        # Establish the staging association up front so each per-source import
        # (they replay the shared journal on open) validates against the source
        # ledger identity and checkpoint rather than refusing an un-adopted
        # database (F24).
        _initialize_staging_association(database, staging)
        for source in sources:
            suffix = ".csv" if source.family == "REVOLUT_CSV" else ".pdf"
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
                result = _import_source(
                    staging, evidence_directory, materialized_path, source.family
                )
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

        _verify_candidate(database, staging, {source.sha256 for source in sources})
        _publish_without_clobber(staging, output)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    return RebuildResult(
        output,
        len(sources),
        accepted,
        quarantined,
        decisions_bound,
        decisions_dangling,
        len(unsupported),
    )


def _inventory_evidence(database: Path) -> tuple[list[_Source], list[str]]:
    """Inventory every ``source_content`` blob, grouped by blob and family.

    Returns ``(dispatchable, unsupported)``. Selection state and extraction
    status are ignored: current, quarantined, superseded-only, and
    failed/running-only histories are all accounted for. A blob whose profile
    versions dispatch to more than one family is an ambiguous input and refuses
    the whole reconstruction.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        blobs = connection.execute(
            "SELECT sha256, evidence_key, original_path FROM source_content ORDER BY sha256"
        ).fetchall()
        profile_rows = connection.execute(
            "SELECT DISTINCT evidence_sha256, source_profile_version FROM extraction_runs"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise FinCLError(f"cannot read the reconstruction inventory: {exc}") from exc
    finally:
        connection.close()

    profiles_by_blob: dict[str, set[str]] = {}
    for row in profile_rows:
        profiles_by_blob.setdefault(row["evidence_sha256"], set()).add(
            row["source_profile_version"]
        )

    dispatchable: list[_Source] = []
    unsupported: list[str] = []
    for blob in blobs:
        sha = blob["sha256"]
        profiles = profiles_by_blob.get(sha, set())
        families: dict[str, str] = {}
        for profile in profiles:
            family = _family_for_profile(profile)
            if family is None:
                continue
            # Keep the lexically greatest profile version per family as the
            # representative; every version of a family uses the one adapter.
            if family not in families or profile > families[family]:
                families[family] = profile
        if len(families) > 1:
            raise FinCLError(
                f"evidence {sha} dispatches to more than one source family "
                f"({', '.join(sorted(families))}); reconstruction cannot choose one"
            )
        if not families:
            unsupported.append(sha)
            continue
        family, profile = next(iter(families.items()))
        dispatchable.append(
            _Source(sha, blob["evidence_key"], profile, blob["original_path"], family)
        )
    return dispatchable, unsupported


def _verify_all_evidence(store: PlaintextEvidenceStore, sources: list[_Source]) -> None:
    """Fail before staging if any inventoried evidence object is missing or bad."""
    for source in sources:
        try:
            store.open_verified(source.sha256, source.evidence_key).close()
        except EvidenceError as exc:
            raise FinCLError(
                f"evidence {source.sha256[:12]} ({source.original_path}) is missing or "
                f"corrupt: {exc}"
            ) from exc


def _verify_candidate(database: Path, staging: Path, expected_evidence: set[str]) -> None:
    """Prove the candidate before publishing it.

    Re-extraction may change entry counts, so evidence-set equality is the
    coverage gate — not row counts or old surrogate IDs. FX rows must be carried
    forward exactly, and the candidate must reopen cleanly through the normal
    read path so journal continuity is proven beyond the staging replay.
    """
    staged = sqlite3.connect(f"file:{staging}?mode=ro", uri=True)
    try:
        rebuilt_evidence = {
            row[0] for row in staged.execute("SELECT sha256 FROM source_content")
        }
    finally:
        staged.close()
    if rebuilt_evidence != expected_evidence:
        missing = expected_evidence - rebuilt_evidence
        extra = rebuilt_evidence - expected_evidence
        raise FinCLError(
            "reconstructed evidence set does not match the source inventory "
            f"(missing {sorted(s[:12] for s in missing)}, extra {sorted(s[:12] for s in extra)})"
        )
    if _fx_rows(database) != _fx_rows(staging):
        raise FinCLError("reference FX rows were not carried forward exactly")

    candidate = connect_sqlite(staging)
    try:
        initialize_schema(candidate)
        _guarded_replay(candidate)
        candidate.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except FinCLError as exc:
        raise FinCLError(
            f"reconstructed candidate failed to reopen through the normal read path: {exc}"
        ) from exc
    finally:
        candidate.close()


def _fx_rows(database: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fx_rates'"
        ).fetchone()
        if has_table is None:
            return []
        return sorted(
            tuple(row)
            for row in connection.execute(
                "SELECT base_currency, quote_currency, period, rate_numerator, "
                "rate_denominator, rate_text, source, source_version FROM fx_rates"
            )
        )
    finally:
        connection.close()


def _publish_without_clobber(staging: Path, output: Path) -> None:
    """Atomically publish, refusing a destination that appeared after preflight."""
    try:
        os.link(staging, output)
    except FileExistsError as exc:
        raise FinCLError(
            f"rebuild output appeared during reconstruction: {output}"
        ) from exc
    os.unlink(staging)
    descriptor = os.open(output, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.open(output.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _initialize_staging_association(source: Path, staging: Path) -> None:
    connection = connect_sqlite(staging)
    try:
        initialize_schema(connection)
        reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        reader.row_factory = sqlite3.Row
        try:
            copy_association_for_rebuild(reader, connection)
        finally:
            reader.close()
        connection.commit()
    finally:
        connection.close()


def _import_source(database: Path, evidence_directory: Path, source: Path, family: str) -> str:
    if family == "REVOLUT_CSV":
        return FinCLService.plaintext_phase1(database, evidence_directory).import_revolut(source).status
    service = DocumentImportService.plaintext_phase2(database, evidence_directory)
    if family == "DBS_CONSOLIDATED":
        return service.import_dbs(source).status
    if family == "DBS_PAYLAH":
        return service.import_paylah(source).status
    if family == "MARI_SAVINGS":
        return service.import_maribank(source).status
    if family == "MARI_CARD":
        return service.import_mari_card(source).status
    if family == "TRUST_SAVINGS":
        return service.import_trust(source).status
    if family == "REVOLUT_PDF":
        return service.import_revolut_pdf(source).status
    raise FinCLError(f"unsupported source family for reconstruction: {family}")


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
