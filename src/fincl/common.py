"""Deterministic helpers shared by the CSV and document import services.

These produce persisted identifiers, timestamps, and canonical JSON. Both
services must produce byte-identical output, so the one definition lives here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime


def stable_id(prefix: str, *parts: str) -> str:
    """A rebuild-stable id: ``<prefix>_<sha256 of NUL-joined parts>``."""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def utc_now() -> str:
    """Current UTC instant, ISO-8601 with microsecond precision."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def canonical_json(value: object) -> str:
    """Compact, key-sorted JSON for stored context and evidence blobs."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
