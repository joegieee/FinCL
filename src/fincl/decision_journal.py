"""Durable append-only journal for originating operator decisions.

The journal is deliberately outside SQLite because the database is derived state
and may be rebuilt.  SQLite projections are added by W2; W0 uses this module to
make recovery actions durable before their derived rows are cleared.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import FinCLError

JOURNAL_FILENAME = "decision-journal.jsonl"
JOURNAL_VERSION = 1
GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class JournalRecord:
    seq: int
    decided_at: str
    decision_kind: str
    anchor_version: str
    anchors: dict[str, str]
    payload: dict[str, object]
    policy_version: str
    snapshot: str
    prev_hash: str
    record_hash: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "anchor_version": self.anchor_version,
            "anchors": self.anchors,
            "decided_at": self.decided_at,
            "decision_kind": self.decision_kind,
            "journal_version": JOURNAL_VERSION,
            "payload": self.payload,
            "policy_version": self.policy_version,
            "prev_hash": self.prev_hash,
            "record_hash": self.record_hash,
            "seq": self.seq,
            "snapshot": self.snapshot,
        }


def default_journal_path(database_path: Path) -> Path:
    """Store originating decisions beside the local database and evidence root."""
    return Path(database_path).resolve().parent / JOURNAL_FILENAME


def append_decision(
    journal_path: Path,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, object],
    policy_version: str,
    snapshot: str,
    anchor_version: str = "natural-anchor-v1",
) -> JournalRecord:
    """Append and fsync one hash-chained decision before acknowledging it."""
    if not decision_kind:
        raise FinCLError("decision journal requires a decision kind")
    if not anchors:
        raise FinCLError("decision journal requires at least one natural anchor")
    if not snapshot:
        raise FinCLError("decision journal requires a human-readable snapshot")

    path = Path(journal_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod_directory(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.chmod(path, 0o600)
        records = read_records(path, descriptor=descriptor)
        prev_hash = records[-1].record_hash if records else GENESIS_HASH
        decided_at = datetime.now(UTC).isoformat(timespec="microseconds")
        seq = len(records) + 1
        unsigned_record = {
            "anchor_version": anchor_version,
            "anchors": anchors,
            "decided_at": decided_at,
            "decision_kind": decision_kind,
            "journal_version": JOURNAL_VERSION,
            "payload": payload,
            "policy_version": policy_version,
            "prev_hash": prev_hash,
            "seq": seq,
            "snapshot": snapshot,
        }
        record_hash = _record_hash(unsigned_record)
        record = JournalRecord(
            seq=seq,
            decided_at=decided_at,
            decision_kind=decision_kind,
            anchor_version=anchor_version,
            anchors=anchors,
            payload=payload,
            policy_version=policy_version,
            snapshot=snapshot,
            prev_hash=prev_hash,
            record_hash=record_hash,
        )
        os.write(descriptor, _canonical_json(record.as_mapping()).encode("utf-8") + b"\n")
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    _fsync_directory(path.parent)
    return record


def read_records(journal_path: Path, *, descriptor: int | None = None) -> list[JournalRecord]:
    """Read and verify the complete journal chain before relying on it."""
    path = Path(journal_path)
    if not path.exists():
        return []
    if descriptor is None:
        raw = path.read_bytes()
    else:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    if raw and not raw.endswith(b"\n"):
        raise FinCLError("decision journal ends with an incomplete record")

    records: list[JournalRecord] = []
    previous_hash = GENESIS_HASH
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            item = json.loads(line)
            record = JournalRecord(
                seq=_required_int(item, "seq"),
                decided_at=_required_str(item, "decided_at"),
                decision_kind=_required_str(item, "decision_kind"),
                anchor_version=_required_str(item, "anchor_version"),
                anchors=_required_string_mapping(item, "anchors"),
                payload=_required_object_mapping(item, "payload"),
                policy_version=_required_str(item, "policy_version"),
                snapshot=_required_str(item, "snapshot"),
                prev_hash=_required_str(item, "prev_hash"),
                record_hash=_required_str(item, "record_hash"),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise FinCLError(f"decision journal record {line_number} is malformed") from exc
        if item.get("journal_version") != JOURNAL_VERSION:
            raise FinCLError(f"decision journal record {line_number} has an unsupported version")
        if record.seq != line_number:
            raise FinCLError(f"decision journal record {line_number} has a non-contiguous sequence")
        if record.prev_hash != previous_hash:
            raise FinCLError(f"decision journal record {line_number} has a broken hash chain")
        unsigned = record.as_mapping()
        unsigned.pop("record_hash")
        if record.record_hash != _record_hash(unsigned):
            raise FinCLError(f"decision journal record {line_number} failed integrity verification")
        records.append(record)
        previous_hash = record.record_hash
    return records


def _record_hash(record: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _required_str(item: dict[str, Any], key: str) -> str:
    value = item[key]
    if not isinstance(value, str) or not value:
        raise ValueError(key)
    return value


def _required_int(item: dict[str, Any], key: str) -> int:
    value = item[key]
    if not isinstance(value, int) or value < 1:
        raise ValueError(key)
    return value


def _required_string_mapping(item: dict[str, Any], key: str) -> dict[str, str]:
    value = item[key]
    if not isinstance(value, dict) or not value or not all(
        isinstance(mapping_key, str) and isinstance(mapping_value, str)
        for mapping_key, mapping_value in value.items()
    ):
        raise ValueError(key)
    return value


def _required_object_mapping(item: dict[str, Any], key: str) -> dict[str, object]:
    value = item[key]
    if not isinstance(value, dict):
        raise ValueError(key)
    return value


def _chmod_directory(path: Path) -> None:
    with suppress(OSError):
        # The subsequent file fsync will still surface an unusable directory.
        os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
