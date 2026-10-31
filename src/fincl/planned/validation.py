"""Shape checks for the ``PLANNED_*`` kinds, shared by preview, batches and replay.

Only the payload itself is checked here. Whether a flow exists, whether an
occurrence still has matches, whether a transaction resolves — those depend on
the projection and live in :mod:`fincl.planned.projection`.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    CERTAINTIES,
    DIRECTION_INFLOW,
    DIRECTIONS,
    LINK_KINDS,
    MAX_OCCURRENCES,
    OUTCOMES,
    PLANNED_FLOW_RETIRE,
    PLANNED_FLOW_SET,
    PLANNED_MATCH_RETIRE,
    PLANNED_MATCH_SET,
    PLANNED_OCCURRENCE_RESOLVE_RETIRE,
    PLANNED_OCCURRENCE_RESOLVE_SET,
    TREATMENT_EXTRA,
    TREATMENTS,
)

_KEY = re.compile(r"^[a-z0-9-]{1,40}$")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
# A natural entry anchor from a CSV source runs to ~200 characters (see
# ``planning.recurring``), so entries get a generous bound of their own.
_ANCHOR = re.compile(r"^[A-Za-z0-9._:-]{1,512}$")
_NAME = re.compile(r"^[^\x00-\x1f]{1,80}$")
_FLOW_FIELDS = frozenset({
    "name", "direction", "currency", "account_id", "category", "occurrences",
    "certainty", "baseline_treatment", "link", "reason",
})


def _reason(payload: dict[str, Any]) -> None:
    value = payload.get("reason")
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise FinCLError("A reason of 1–2,000 characters is required")


def _flow_id(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise FinCLError("A planned item is identified by a lowercase UUID") from exc
    return value


def _key(value: Any) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise FinCLError("An occurrence key is 1-40 lowercase letters, digits or dashes")
    return value


def _day(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise FinCLError(f"{label} must be a calendar date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError(f"{label} must be a calendar date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != value:
        raise FinCLError(f"{label} must be a calendar date (YYYY-MM-DD)")
    return parsed


def parse_amount(value: Any, label: str = "amount") -> ExactMoney:
    if not isinstance(value, str):
        raise FinCLError(f"{label} must be an exact decimal string")
    try:
        money = ExactMoney.parse(value)
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"{value!r} is not an exact amount") from exc
    if money.scale > 2:
        raise FinCLError("Amounts allow at most two decimal places")
    if money.coefficient <= 0:
        raise FinCLError(f"{label} must be greater than zero")
    return money


def _occurrences(value: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_OCCURRENCES:
        raise FinCLError(f"A planned item has 1-{MAX_OCCURRENCES} occurrences")
    keys: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"key", "earliest", "latest", "amount"}:
            raise FinCLError(f"occurrence {index} is exactly {{key, earliest, latest, amount}}")
        key = _key(item["key"])
        if key in keys:
            raise FinCLError(f"occurrence key {key} is used twice")
        keys.add(key)
        if _day(item["earliest"], "earliest") > _day(item["latest"], "latest"):
            raise FinCLError(f"occurrence {key} ends before it starts")
        parse_amount(item["amount"])


def _link(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"kind", "id"}:
        raise FinCLError("A link is null or exactly {kind, id}")
    if value["kind"] not in LINK_KINDS:
        raise FinCLError(f"A planned item can link only to: {', '.join(LINK_KINDS)}")
    if not isinstance(value["id"], str) or not _ID.fullmatch(value["id"]):
        raise FinCLError("The linked id is 1-200 identifier characters")


def validate_planned(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if kind in {PLANNED_FLOW_SET, PLANNED_FLOW_RETIRE}:
        if set(anchors) != {"flow"}:
            raise FinCLError(f"{kind} is anchored to one planned item")
    elif kind in {PLANNED_OCCURRENCE_RESOLVE_SET, PLANNED_OCCURRENCE_RESOLVE_RETIRE}:
        if set(anchors) != {"flow", "occurrence"}:
            raise FinCLError(f"{kind} anchors are {{flow, occurrence}}")
        _key(anchors["occurrence"])
    elif kind in {PLANNED_MATCH_SET, PLANNED_MATCH_RETIRE}:
        if set(anchors) != {"flow", "occurrence", "entry"}:
            raise FinCLError(f"{kind} anchors are {{flow, occurrence, entry}}")
        _key(anchors["occurrence"])
        if not _ANCHOR.fullmatch(anchors["entry"]):
            raise FinCLError("entry must be a transaction anchor")
    else:
        raise FinCLError(f"{kind} is not a planned-item decision")
    _flow_id(anchors["flow"])

    if kind == PLANNED_FLOW_SET:
        if set(payload) != _FLOW_FIELDS:
            raise FinCLError(f"PLANNED_FLOW_SET payload is exactly {sorted(_FLOW_FIELDS)}")
        name = payload["name"]
        if not isinstance(name, str) or not name.strip() or not _NAME.fullmatch(name.strip()):
            raise FinCLError("A name is 1-80 characters without control characters")
        if payload["direction"] not in DIRECTIONS:
            raise FinCLError("direction must be outflow or inflow")
        currency = payload["currency"]
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha() or not currency.isupper():
            raise FinCLError("Choose a three-letter uppercase currency")
        for field in ("account_id", "category"):
            value = payload[field]
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
                raise FinCLError(f"{field} is null or a non-empty string")
        _occurrences(payload["occurrences"])
        if payload["certainty"] not in CERTAINTIES:
            raise FinCLError(f"certainty must be one of {', '.join(CERTAINTIES)}")
        if payload["baseline_treatment"] not in TREATMENTS:
            raise FinCLError(f"baseline_treatment must be one of {', '.join(TREATMENTS)}")
        if payload["direction"] == DIRECTION_INFLOW and payload["baseline_treatment"] != TREATMENT_EXTRA:
            raise FinCLError("Planned income is always extra: it never replaces spending")
        _link(payload["link"])
        if payload["direction"] == DIRECTION_INFLOW and payload["link"] is not None:
            raise FinCLError("Only planned spending can be part of a trip")
        if payload["link"] is not None and payload["baseline_treatment"] != TREATMENT_EXTRA:
            raise FinCLError("A trip budget replaces the trip allowance, never the one-off allowance")
    elif kind == PLANNED_MATCH_SET:
        if set(payload) != {"amount", "reason"}:
            raise FinCLError("PLANNED_MATCH_SET payload is exactly {amount, reason}")
        parse_amount(payload["amount"])
    elif kind == PLANNED_OCCURRENCE_RESOLVE_SET:
        if set(payload) != {"outcome", "reason"}:
            raise FinCLError("PLANNED_OCCURRENCE_RESOLVE_SET payload is exactly {outcome, reason}")
        if payload["outcome"] not in OUTCOMES:
            raise FinCLError(f"outcome must be one of {', '.join(OUTCOMES)}")
    elif set(payload) != {"reason"}:
        raise FinCLError(f"{kind} payload is exactly {{reason}}")
    _reason(payload)


__all__ = ["parse_amount", "validate_planned"]
