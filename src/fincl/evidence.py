from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from .errors import EvidenceError

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class EvidenceCapture:
    sha256: str
    byte_size: int
    evidence_key: str
    storage_kind: str
    reused: bool
    original_path: str


class EvidenceStore(Protocol):
    def capture(self, source: Path) -> EvidenceCapture: ...

    def open_verified(self, sha256: str, evidence_key: str) -> BinaryIO: ...


class PlaintextEvidenceStore:
    """Replaceable Phase 1 store for immutable, byte-identical evidence."""

    storage_kind = "plaintext-v1"

    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def _fingerprint(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
        )

    def capture(self, source: Path) -> EvidenceCapture:
        source = Path(source)
        try:
            before = source.stat()
        except OSError as exc:
            raise EvidenceError(f"cannot stat source: {exc}") from exc
        if not source.is_file():
            raise EvidenceError("source is not a regular file")

        staging_dir = self.root / ".staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(staging_dir, 0o700)
        digest = hashlib.sha256()
        byte_size = 0
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=staging_dir, delete=False) as staged:
                temp_path = Path(staged.name)
                with source.open("rb") as incoming:
                    while chunk := incoming.read(CHUNK_SIZE):
                        digest.update(chunk)
                        byte_size += len(chunk)
                        staged.write(chunk)
                staged.flush()
                os.fsync(staged.fileno())
            after = source.stat()
            if self._fingerprint(before) != self._fingerprint(after) or byte_size != before.st_size:
                raise EvidenceError("source changed while it was being captured")

            sha256 = digest.hexdigest()
            evidence_key = f"sha256/{sha256[:2]}/{sha256}"
            destination = self.root / evidence_key
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(destination.parent, 0o700)
            reused = destination.exists()
            if reused:
                self._verify_path(destination, sha256)
            else:
                try:
                    os.link(temp_path, destination)
                except FileExistsError:
                    reused = True
                self._verify_path(destination, sha256)
                self._fsync_directory(destination.parent)
            os.chmod(destination, 0o600)
            return EvidenceCapture(
                sha256=sha256,
                byte_size=byte_size,
                evidence_key=evidence_key,
                storage_kind=self.storage_kind,
                reused=reused,
                original_path=str(source.resolve()),
            )
        except OSError as exc:
            raise EvidenceError(f"evidence capture failed: {exc}") from exc
        finally:
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink(missing_ok=True)

    def open_verified(self, sha256: str, evidence_key: str) -> BinaryIO:
        expected_key = f"sha256/{sha256[:2]}/{sha256}"
        if evidence_key != expected_key:
            raise EvidenceError("evidence key does not match its content hash")
        path = self.root / evidence_key
        self._verify_path(path, sha256)
        return path.open("rb")

    @staticmethod
    def _verify_path(path: Path, expected_sha256: str) -> None:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(CHUNK_SIZE):
                    digest.update(chunk)
        except OSError as exc:
            raise EvidenceError(f"cannot read evidence object: {exc}") from exc
        if digest.hexdigest() != expected_sha256:
            raise EvidenceError("evidence object failed SHA-256 verification")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
