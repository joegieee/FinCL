"""Durable append-only, hash-chained originating operator decisions."""

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
    # Optional command-API idempotency envelope: {request_id, intent_hash}.
    # Absent from every pre-M1b record, so it is left out of `as_mapping` when
    # None and those records' hashes verify unchanged.
    command: dict[str, str] | None = None

    def as_mapping(self) -> dict[str, object]:
        mapping: dict[str, object] = {
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
        if self.command is not None:
            mapping["command"] = self.command
        return mapping


def default_journal_path(database_path: Path) -> Path:
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
    command: dict[str, str] | None = None,
) -> JournalRecord:
    """Append, fsync the file and directory, then acknowledge a decision.

    ``command`` is an optional ``{request_id, intent_hash}`` idempotency
    envelope for command-API decisions; it is folded into the hash-chained
    record and drives ``command_receipts`` on replay.
    """
    if not decision_kind or not anchors or not snapshot:
        raise FinCLError("decision journal requires kind, anchors, and snapshot")
    if command is not None and set(command) != {"request_id", "intent_hash"}:
        raise FinCLError("command envelope must be exactly {request_id, intent_hash}")
    path = Path(journal_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod_directory(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.chmod(path, 0o600)
        records = read_records(path, descriptor=descriptor)
        previous = records[-1].record_hash if records else GENESIS_HASH
        unsigned: dict[str, object] = {
            "anchor_version": anchor_version,
            "anchors": anchors,
            "decided_at": datetime.now(UTC).isoformat(timespec="microseconds"),
            "decision_kind": decision_kind,
            "journal_version": JOURNAL_VERSION,
            "payload": payload,
            "policy_version": policy_version,
            "prev_hash": previous,
            "seq": len(records) + 1,
            "snapshot": snapshot,
        }
        if command is not None:
            unsigned["command"] = command
        record = JournalRecord(
            seq=unsigned["seq"],  # type: ignore[arg-type]
            decided_at=unsigned["decided_at"],  # type: ignore[arg-type]
            decision_kind=decision_kind,
            anchor_version=anchor_version,
            anchors=anchors,
            payload=payload,
            policy_version=policy_version,
            snapshot=snapshot,
            prev_hash=previous,
            record_hash=_record_hash(unsigned),
            command=command,
        )
        os.write(descriptor, (_canonical_json(record.as_mapping()) + "\n").encode())
        os.fsync(descriptor)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    _fsync_directory(path.parent)
    return record


def read_records(journal_path: Path, *, descriptor: int | None = None) -> list[JournalRecord]:
    """Read and completely validate the journal before it is projected."""
    path = Path(journal_path)
    if not path.exists():
        return []
    if descriptor is None:
        raw = path.read_bytes()
    else:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        raw = b"".join(chunks)
    if raw and not raw.endswith(b"\n"):
        raise FinCLError("decision journal ends with an incomplete record")
    records: list[JournalRecord] = []
    previous = GENESIS_HASH
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            item = json.loads(line)
            record = JournalRecord(
                seq=_integer(item, "seq"),
                decided_at=_string(item, "decided_at"),
                decision_kind=_string(item, "decision_kind"),
                anchor_version=_string(item, "anchor_version"),
                anchors=_strings(item, "anchors"),
                payload=_objects(item, "payload"),
                policy_version=_string(item, "policy_version"),
                snapshot=_string(item, "snapshot"),
                prev_hash=_string(item, "prev_hash"),
                record_hash=_string(item, "record_hash"),
                command=_command(item),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise FinCLError(f"decision journal record {line_number} is malformed") from exc
        if item.get("journal_version") != JOURNAL_VERSION:
            raise FinCLError(f"decision journal record {line_number} has an unsupported version")
        unsigned = record.as_mapping()
        unsigned.pop("record_hash")
        if record.seq != line_number or record.prev_hash != previous:
            raise FinCLError(f"decision journal record {line_number} has a broken sequence or hash chain")
        if record.record_hash != _record_hash(unsigned):
            raise FinCLError(f"decision journal record {line_number} failed integrity verification")
        records.append(record)
        previous = record.record_hash
    return records


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _record_hash(record: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(record).encode()).hexdigest()


def _string(item: dict[str, Any], key: str) -> str:
    value = item[key]
    if not isinstance(value, str) or not value:
        raise ValueError(key)
    return value


def _integer(item: dict[str, Any], key: str) -> int:
    value = item[key]
    if not isinstance(value, int) or value < 1:
        raise ValueError(key)
    return value


def _strings(item: dict[str, Any], key: str) -> dict[str, str]:
    value = item[key]
    if not isinstance(value, dict) or not value or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError(key)
    return value


def _objects(item: dict[str, Any], key: str) -> dict[str, object]:
    value = item[key]
    if not isinstance(value, dict):
        raise ValueError(key)
    return value


def _command(item: dict[str, Any]) -> dict[str, str] | None:
    if "command" not in item:
        return None
    value = item["command"]
    if (
        not isinstance(value, dict)
        or set(value) != {"request_id", "intent_hash"}
        or not all(isinstance(v, str) and v for v in value.values())
    ):
        raise ValueError("command")
    return value


def _chmod_directory(path: Path) -> None:
    with suppress(OSError):
        os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
