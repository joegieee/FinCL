"""Verified backup and restore of a complete ledger bundle (F27, §4).

A backup bundle is a directory holding the SQLite database (captured through
SQLite's own backup API, not a file copy), the decision journal and its identity
file at the checkpoint, every inventoried evidence object, and a ``backup-v1``
manifest of checksums and semantic summaries. Capture happens under the ledger
writer lock. Restore verifies every path and checksum, reopens the candidate
through the normal read path, compares it to the manifest, and only then
publishes into a new directory without overwriting anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .association import JOURNAL_IDENTITY_FILENAME, require_valid_association
from .common import utc_now
from .db import SCHEMA_VERSION, connect_sqlite, initialize_schema
from .decision_journal import JOURNAL_FILENAME, default_journal_path, read_records
from .decisions import replay_journal
from .errors import FinCLError, SchemaVersionError
from .locking import WriterContext, ledger_writer

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = "backup-v1"
_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class BackupManifest:
    app_version: str
    schema_version: int
    ledger_id: str
    journal_id: str
    journal_seq: int
    journal_head: str
    journal_sha256: str
    database_sha256: str
    identity_sha256: str
    evidence: list[dict[str, object]]
    fx_versions: list[str]
    semantic: dict[str, object]
    captured_at: str
    extras: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        payload = {
            "manifest_version": MANIFEST_VERSION,
            "app_version": self.app_version,
            "schema_version": self.schema_version,
            "ledger_id": self.ledger_id,
            "journal_id": self.journal_id,
            "journal": {
                "seq": self.journal_seq,
                "head": self.journal_head,
                "sha256": self.journal_sha256,
            },
            "database_sha256": self.database_sha256,
            "identity_sha256": self.identity_sha256,
            "evidence": self.evidence,
            "fx_versions": self.fx_versions,
            "semantic": self.semantic,
            "captured_at": self.captured_at,
            "extras": self.extras,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True, slots=True)
class BackupResult:
    location: Path
    ledger_id: str
    evidence_objects: int
    journal_seq: int


@dataclass(frozen=True, slots=True)
class RestoreResult:
    location: Path
    ledger_id: str
    evidence_objects: int


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _semantic_summary(connection: sqlite3.Connection) -> dict[str, object]:
    bound, dangling = replay_journal(connection)
    return {"decisions_bound": bound, "decisions_dangling": dangling}


def _evidence_inventory(connection: sqlite3.Connection) -> list[tuple[str, str, int]]:
    return [
        (row["sha256"], row["evidence_key"], int(row["byte_size"]))
        for row in connection.execute(
            "SELECT sha256, evidence_key, byte_size FROM source_content ORDER BY sha256"
        )
    ]


def _fx_versions(connection: sqlite3.Connection) -> list[str]:
    has_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fx_rates'"
    ).fetchone()
    if has_table is None:
        return []
    return sorted(
        str(row[0])
        for row in connection.execute("SELECT DISTINCT source_version FROM fx_rates")
    )


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #


def backup_ledger(
    database: Path,
    evidence_directory: Path,
    output: Path,
    *,
    writer: WriterContext | None = None,
    allow_stale_schema: bool = False,
) -> BackupResult:
    database = Path(database).resolve()
    evidence_directory = Path(evidence_directory).resolve()
    output = Path(output).resolve()
    if not database.is_file():
        raise FinCLError(f"database does not exist: {database}")
    if not evidence_directory.is_dir():
        raise FinCLError(f"evidence directory does not exist: {evidence_directory}")
    if output.exists():
        raise FinCLError(f"backup output already exists: {output}")

    with ledger_writer(database, existing=writer):
        return _capture_backup(
            database, evidence_directory, output, allow_stale_schema=allow_stale_schema
        )


def _capture_backup(
    database: Path,
    evidence_directory: Path,
    output: Path,
    *,
    allow_stale_schema: bool = False,
) -> BackupResult:
    source = connect_sqlite(database)
    try:
        # A migration backup must capture the *pre-upgrade* layout, so it skips
        # the schema check that would refuse an out-of-date but otherwise valid
        # ledger. Every other caller upgrades-or-refuses as before.
        if not allow_stale_schema:
            initialize_schema(source)
        association = require_valid_association(source, database)
        replay_journal(source)
        schema_version = int(
            source.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
        )
        inventory = _evidence_inventory(source)
        fx_versions = _fx_versions(source)
        semantic = _semantic_summary(source)

        staging = output.parent / f".backup-staging-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700, parents=True)
        try:
            dest = sqlite3.connect(staging / "fincl.sqlite3")
            try:
                source.backup(dest)
                # Fold the WAL into the main file and drop the sidecars so the
                # bundle is a single self-contained database file.
                dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dest.execute("PRAGMA journal_mode=DELETE")
            finally:
                dest.close()
            for sidecar in ("-wal", "-shm"):
                Path(str(staging / "fincl.sqlite3") + sidecar).unlink(missing_ok=True)
            os.chmod(staging / "fincl.sqlite3", 0o600)

            journal_path = default_journal_path(database)
            shutil.copy2(journal_path, staging / JOURNAL_FILENAME)
            identity_source = database.parent / JOURNAL_IDENTITY_FILENAME
            shutil.copy2(identity_source, staging / JOURNAL_IDENTITY_FILENAME)

            store_root = evidence_directory
            evidence_manifest: list[dict[str, object]] = []
            for sha, key, size in inventory:
                src = store_root / key
                if not src.is_file():
                    raise FinCLError(f"evidence object {sha[:12]} is missing from the vault")
                actual = _sha256_file(src)
                if actual != sha:
                    raise FinCLError(f"evidence object {sha[:12]} failed verification")
                dst = staging / "evidence" / key
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                evidence_manifest.append({"key": key, "sha256": sha, "byte_size": size})

            inventoried_keys = {key for _, key, _ in inventory}
            extras = sorted(
                str(path.relative_to(store_root))
                for path in store_root.rglob("*")
                if path.is_file()
                and ".staging" not in path.parts
                and str(path.relative_to(store_root)) not in inventoried_keys
            )

            records = read_records(journal_path)
            manifest = BackupManifest(
                app_version=__version__,
                schema_version=schema_version,
                ledger_id=association.ledger_id,
                journal_id=association.journal_id,
                journal_seq=len(records),
                journal_head=records[-1].record_hash if records else "0" * 64,
                journal_sha256=_sha256_file(journal_path),
                database_sha256=_sha256_file(staging / "fincl.sqlite3"),
                identity_sha256=_sha256_file(identity_source),
                evidence=evidence_manifest,
                fx_versions=fx_versions,
                semantic=semantic,
                captured_at=utc_now(),
                extras=extras,
            )
            (staging / MANIFEST_NAME).write_text(manifest.to_json() + "\n")

            _verify_captured_bundle(staging, manifest)

            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    _fsync_path(path)
            _fsync_path(staging)

            if output.exists():
                raise FinCLError(f"backup output appeared during capture: {output}")
            os.rename(staging, output)
            _fsync_path(output.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    finally:
        source.close()

    return BackupResult(output, manifest.ledger_id, len(manifest.evidence), manifest.journal_seq)


def _verify_captured_bundle(bundle: Path, manifest: BackupManifest) -> None:
    db_path = bundle / "fincl.sqlite3"
    if _sha256_file(db_path) != manifest.database_sha256:
        raise FinCLError("captured database checksum does not match the manifest")
    check = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise FinCLError("captured database failed integrity_check")
        if check.execute("PRAGMA foreign_key_check").fetchall():
            raise FinCLError("captured database failed foreign_key_check")
        db_evidence = {row[0] for row in check.execute("SELECT sha256 FROM source_content")}
    finally:
        check.close()
    if db_evidence != {str(item["sha256"]) for item in manifest.evidence}:
        raise FinCLError("captured evidence set does not match the database inventory")

    records = read_records(bundle / JOURNAL_FILENAME)
    if len(records) != manifest.journal_seq:
        raise FinCLError("captured journal length does not match the manifest")
    head = records[-1].record_hash if records else "0" * 64
    if head != manifest.journal_head:
        raise FinCLError("captured journal head does not match the manifest")

    for item in manifest.evidence:
        path = bundle / "evidence" / str(item["key"])
        if not path.is_file() or _sha256_file(path) != item["sha256"]:
            raise FinCLError(f"captured evidence object {str(item['sha256'])[:12]} is bad")


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #


def verify_backup_bundle(backup_dir: Path) -> BackupManifest:
    """Parse and fully checksum-verify a bundle without restoring it."""
    backup_dir = Path(backup_dir).resolve()
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FinCLError(f"no {MANIFEST_NAME} in backup directory: {backup_dir}")
    try:
        data = json.loads(manifest_path.read_text())
    except ValueError as exc:
        raise FinCLError("backup manifest is not valid JSON") from exc
    if data.get("manifest_version") != MANIFEST_VERSION:
        raise FinCLError(
            f"unsupported backup manifest version: {data.get('manifest_version')!r}"
        )
    schema_version = int(data["schema_version"])
    if schema_version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"backup was captured at schema version {schema_version}, newer than this "
            f"build supports ({SCHEMA_VERSION}); restore with fincl >= "
            f"{data.get('app_version')}"
        )

    _reject_unsafe_paths(backup_dir)

    for required in ("fincl.sqlite3", JOURNAL_FILENAME, JOURNAL_IDENTITY_FILENAME):
        if not (backup_dir / required).is_file():
            raise FinCLError(f"backup bundle is missing {required}")

    manifest = BackupManifest(
        app_version=str(data["app_version"]),
        schema_version=schema_version,
        ledger_id=str(data["ledger_id"]),
        journal_id=str(data["journal_id"]),
        journal_seq=int(data["journal"]["seq"]),
        journal_head=str(data["journal"]["head"]),
        journal_sha256=str(data["journal"]["sha256"]),
        database_sha256=str(data["database_sha256"]),
        identity_sha256=str(data["identity_sha256"]),
        evidence=list(data["evidence"]),
        fx_versions=[str(v) for v in data["fx_versions"]],
        semantic=dict(data["semantic"]),
        captured_at=str(data["captured_at"]),
        extras=[str(v) for v in data.get("extras", [])],
    )

    if _sha256_file(backup_dir / "fincl.sqlite3") != manifest.database_sha256:
        raise FinCLError("backup database checksum does not match the manifest")
    if _sha256_file(backup_dir / JOURNAL_FILENAME) != manifest.journal_sha256:
        raise FinCLError("backup journal checksum does not match the manifest")
    if _sha256_file(backup_dir / JOURNAL_IDENTITY_FILENAME) != manifest.identity_sha256:
        raise FinCLError("backup identity file checksum does not match the manifest")
    for item in manifest.evidence:
        key = Path(str(item["key"]))
        if key.is_absolute() or ".." in key.parts:
            raise FinCLError(f"backup manifest names an unsafe evidence path: {key}")
        path = backup_dir / "evidence" / key
        if not path.is_file() or _sha256_file(path) != item["sha256"]:
            raise FinCLError(
                f"backup evidence object {str(item['sha256'])[:12]} is missing or corrupt"
            )
    return manifest


def _reject_unsafe_paths(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise FinCLError(f"backup bundle contains a symlink: {path.relative_to(root)}")
        rel = path.relative_to(root)
        if ".." in rel.parts or rel.is_absolute():
            raise FinCLError(f"backup bundle contains an unsafe path: {rel}")


def restore_ledger(backup_dir: Path, output: Path) -> RestoreResult:
    backup_dir = Path(backup_dir).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise FinCLError(f"restore output already exists: {output}")

    manifest = verify_backup_bundle(backup_dir)

    staging = output.parent / f".restore-staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700, parents=True)
    try:
        shutil.copy2(backup_dir / "fincl.sqlite3", staging / "fincl.sqlite3")
        shutil.copy2(backup_dir / JOURNAL_FILENAME, staging / JOURNAL_FILENAME)
        shutil.copy2(
            backup_dir / JOURNAL_IDENTITY_FILENAME, staging / JOURNAL_IDENTITY_FILENAME
        )
        for item in manifest.evidence:
            dst = staging / "evidence" / str(item["key"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_dir / "evidence" / str(item["key"]), dst)

        candidate = connect_sqlite(staging / "fincl.sqlite3")
        try:
            # A bundle captured at an older schema (e.g. the pre-upgrade backup
            # `fincl migrate` takes) restores as-is: initialize_schema would
            # refuse it. The operator runs `fincl migrate` on the restored copy.
            if manifest.schema_version == SCHEMA_VERSION:
                initialize_schema(candidate)
            else:
                stored = candidate.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()
                if stored is None or int(stored[0]) != manifest.schema_version:
                    raise FinCLError(
                        "restored database schema version does not match the manifest"
                    )
            association = require_valid_association(candidate, staging / "fincl.sqlite3")
            if association.ledger_id != manifest.ledger_id:
                raise FinCLError("restored association does not match the manifest ledger id")
            bound, dangling = replay_journal(candidate)
            restored_evidence = {
                row[0] for row in candidate.execute("SELECT sha256 FROM source_content")
            }
            restored_fx = _fx_versions(candidate)
        finally:
            candidate.close()

        if restored_evidence != {str(item["sha256"]) for item in manifest.evidence}:
            raise FinCLError("restored evidence set does not match the manifest")
        if restored_fx != manifest.fx_versions:
            raise FinCLError("restored FX version set does not match the manifest")
        expected = manifest.semantic
        if (bound, dangling) != (
            int(str(expected["decisions_bound"])),
            int(str(expected["decisions_dangling"])),
        ):
            raise FinCLError("restored decision projection does not match the manifest")

        for path in sorted(staging.rglob("*")):
            if path.is_file():
                _fsync_path(path)
        _fsync_path(staging)
        if output.exists():
            raise FinCLError(f"restore output appeared during restore: {output}")
        os.rename(staging, output)
        _fsync_path(output.parent)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return RestoreResult(output, manifest.ledger_id, len(manifest.evidence))
