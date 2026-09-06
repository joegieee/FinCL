"""User-created taxonomy additions are originating journal decisions, not SQL authority."""
from pathlib import Path
from typing import Any

from .common import category_strings
from .decision_journal import default_journal_path, read_records
from .errors import FinCLError


def validate_creation(anchors: dict[str, str], payload: dict[str, Any], existing: frozenset[str]) -> str:
    name = payload.get("category", "")
    if not isinstance(name, str) or name.count(" / ") != 1:
        raise FinCLError("Choose a group and category name")
    group, label = name.split(" / ")
    if any(not part.strip() or part != part.strip() or len(part) > 80 or any(ord(c) < 32 for c in part) for part in (group, label)):
        raise FinCLError("Group and category must contain 1–80 characters without control characters")
    if group.casefold() in {"internal", "unclassified"}:
        raise FinCLError("Internal and Unclassified are reserved groups")
    if name.casefold() in {c.casefold() for c in category_strings() | existing}:
        raise FinCLError("This category already exists")
    if anchors != {"category": name}:
        raise FinCLError("Category anchor must match its name")
    if not isinstance(payload.get("reason"), str) or not str(payload["reason"]).strip():
        raise FinCLError("A reason is required")
    return name


def custom_categories(database: Path, through_seq: int | None = None) -> frozenset[str]:
    result: set[str] = set()
    for record in read_records(default_journal_path(database)):
        if through_seq is not None and record.seq > through_seq:
            break
        if record.decision_kind == "CATEGORY_CREATE":
            result.add(validate_creation(record.anchors, record.payload, frozenset(result)))
    return frozenset(result)
