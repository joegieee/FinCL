"""Structural validation of the ``FORECAST_SNAPSHOT_ISSUE`` / ``FORECAST_MATCH_*`` kinds.

Pure shape and value checks only — no database.  Three rules carry the stage:

* a snapshot document must state *every* part of what was issued.  The key set
  is exact rather than a minimum, so a build that starts recording one more
  input cannot quietly produce snapshots that an older reader misreads as
  complete;
* the digest travels inside the payload and must equal the document's own.  The
  journal record is hash-chained, so a document and a digest that disagree can
  only mean the payload was assembled wrongly, and it is refused at authoring
  and again at every replay; and
* a match names one issued event, one actual movement and the amount bound
  between them.  A partial match must be strictly less than the event's issued
  amount — a "partial" match for the whole amount is a full one, and letting the
  two blur is how a remainder goes missing.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    FLOW_CLASSES,
    FORECAST_MATCH_RETIRE,
    FORECAST_MATCH_SET,
    FORECAST_SNAPSHOT_ISSUE,
    MATCH_KINDS,
    SNAPSHOT_CONTRACT_VERSION,
    snapshot_digest,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,200}$")
# A movement id carries a ledger natural anchor, which is
# `<evidence sha>:<source anchor>:<ordinal>` and may hold characters an
# identifier pattern would reject. Only control characters and length are
# constrained here; the projection is what checks it resolves.
_REFERENCE_RE = re.compile(r"^[^\x00-\x1f]{1,400}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REASON_MAX = 2000

_DOCUMENT_KEYS = frozenset(
    {
        "contract_version",
        "engine_version",
        "issued_at",
        "generation",
        "currency",
        "scale",
        "case",
        "settings",
        "basis",
        "horizon",
        "reserve",
        "opening_balance",
        "events",
        "outputs",
        "limitations",
    }
)
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "source_id",
        "flow_class",
        "direction",
        "on",
        "amount",
        "label",
        "timing",
        "confidence",
        "recurring_salary",
        "conditional",
        "condition",
    }
)
_TIMING_KEYS = frozenset({"basis", "earliest", "latest", "precision"})
_HORIZON_KEYS = frozenset({"start", "first_modelled_day", "end", "months"})
_BASIS_KEYS = frozenset({"supported_cash_cutoff", "manifest_as_of", "issued_as_of"})
_SETTINGS_KEYS = frozenset({"likely_income_policy", "strong_income_interruption"})
_RESERVE_KEYS = frozenset({"available", "target", "reason"})
_OUTPUT_KEYS = frozenset(
    {
        "months",
        "totals",
        "closing_balance",
        "lowest_balance",
        "first_zero",
        "first_reserve_breach",
    }
)


def _reason(payload: dict[str, Any]) -> None:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX:
        raise FinCLError("a reason of 1-2,000 characters is required")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise FinCLError(f"{label} must be 1-200 identifier characters")
    return value


def _reference(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REFERENCE_RE.fullmatch(value):
        raise FinCLError(f"{label} must be 1-400 characters without control characters")
    return value


def _calendar_date(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise FinCLError(f"{label} must be an ISO calendar date (YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError(f"{label} is not a real calendar date: {value}") from exc
    return value


def _amount(value: object, label: str, *, allow_zero: bool) -> ExactMoney:
    if not isinstance(value, str):
        raise FinCLError(f"{label} must be an exact decimal string")
    try:
        money = ExactMoney.parse(value)
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"{label} is not a valid amount: {value!r}") from exc
    if money.coefficient < 0:
        raise FinCLError(f"{label} is a magnitude, never a sign")
    if money.coefficient == 0 and not allow_zero:
        raise FinCLError(f"{label} must be greater than zero")
    return money


def _mapping(value: object, label: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FinCLError(f"{label} must be an object")
    if set(value) != keys:
        raise FinCLError(f"{label} is exactly {sorted(keys)}")
    return value


def _validate_event(item: object, index: int) -> str:
    event = _mapping(item, f"snapshot event {index}", _EVENT_KEYS)
    event_id = _reference(event["event_id"], f"snapshot event {index} event_id")
    _reference(event["source_id"], f"snapshot event {index} source_id")
    if event["flow_class"] not in FLOW_CLASSES:
        raise FinCLError(
            f"snapshot event {index} flow_class must be one of {sorted(FLOW_CLASSES)}"
        )
    if event["direction"] not in ("inflow", "outflow"):
        raise FinCLError(f"snapshot event {index} direction must be inflow or outflow")
    _calendar_date(event["on"], f"snapshot event {index} date")
    _amount(event["amount"], f"snapshot event {index} amount", allow_zero=True)
    if not isinstance(event["label"], str):
        raise FinCLError(f"snapshot event {index} label must be a string")
    timing = _mapping(event["timing"], f"snapshot event {index} timing", _TIMING_KEYS)
    earliest = _calendar_date(timing["earliest"], f"snapshot event {index} window start")
    latest = _calendar_date(timing["latest"], f"snapshot event {index} window end")
    if earliest > latest:
        raise FinCLError(f"snapshot event {index} has an inverted timing window")
    if not (earliest <= str(event["on"]) <= latest):
        raise FinCLError(f"snapshot event {index} falls outside its timing window")
    if not isinstance(event["confidence"], str) or not event["confidence"]:
        raise FinCLError(f"snapshot event {index} must state the confidence it was issued at")
    for flag in ("recurring_salary", "conditional"):
        if not isinstance(event[flag], bool):
            raise FinCLError(f"snapshot event {index} {flag} must be a boolean")
    if event["conditional"] and not event["condition"]:
        raise FinCLError(f"snapshot event {index} is conditional but states no condition")
    return event_id


def validate_snapshot_document(document: object) -> None:
    """Every part of what was issued, checked without consulting the ledger."""
    body = _mapping(document, "the snapshot document", _DOCUMENT_KEYS)
    if body["contract_version"] != SNAPSHOT_CONTRACT_VERSION:
        raise FinCLError(
            f"the snapshot document states contract {body['contract_version']!r}; "
            f"this build issues and reads {SNAPSHOT_CONTRACT_VERSION!r}"
        )
    _identifier(body["engine_version"], "engine_version")
    if not isinstance(body["issued_at"], str):
        raise FinCLError("issued_at must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(str(body["issued_at"]))
    except ValueError as exc:
        raise FinCLError(f"issued_at is not an ISO-8601 timestamp: {body['issued_at']!r}") from exc
    _identifier(body["generation"], "generation")
    currency = body["currency"]
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise FinCLError("currency must be a three-letter uppercase code")
    scale = body["scale"]
    if not isinstance(scale, int) or isinstance(scale, bool) or scale < 2:
        raise FinCLError("scale must be an integer of at least two")
    _identifier(body["case"], "case")
    settings = _mapping(body["settings"], "settings", _SETTINGS_KEYS)
    _identifier(settings["likely_income_policy"], "likely_income_policy")
    if not isinstance(settings["strong_income_interruption"], bool):
        raise FinCLError("strong_income_interruption must be a boolean")

    basis = _mapping(body["basis"], "basis", _BASIS_KEYS)
    for key in _BASIS_KEYS:
        if basis[key] is not None:
            _calendar_date(basis[key], f"basis {key}")
    horizon = _mapping(body["horizon"], "horizon", _HORIZON_KEYS)
    start = _calendar_date(horizon["start"], "the horizon start")
    first_day = _calendar_date(horizon["first_modelled_day"], "the first modelled day")
    end = _calendar_date(horizon["end"], "the horizon end")
    if not start < first_day <= end:
        raise FinCLError(
            "the horizon must open on its cutoff, model from the following day and "
            "end on or after that day"
        )
    months = horizon["months"]
    if not isinstance(months, int) or isinstance(months, bool) or months < 1:
        raise FinCLError("the horizon month count must be a positive integer")

    reserve = _mapping(body["reserve"], "reserve", _RESERVE_KEYS)
    if not isinstance(reserve["available"], bool):
        raise FinCLError("reserve availability must be a boolean")
    if reserve["available"]:
        if reserve["target"] is None:
            raise FinCLError("an available reserve must state its target")
        _amount(reserve["target"], "the reserve target", allow_zero=True)
    elif reserve["target"] is not None:
        raise FinCLError("an unavailable reserve must not carry a target amount")
    _amount(body["opening_balance"], "the opening balance", allow_zero=True)

    events = body["events"]
    if not isinstance(events, list):
        raise FinCLError("the snapshot events must be a list")
    seen: set[str] = set()
    for index, item in enumerate(events):
        event_id = _validate_event(item, index)
        if event_id in seen:
            raise FinCLError(f"snapshot event id {event_id} is issued twice")
        seen.add(event_id)
    _mapping(body["outputs"], "outputs", _OUTPUT_KEYS)
    if not isinstance(body["outputs"]["months"], list):
        raise FinCLError("the issued outputs must carry a month list")
    limitations = body["limitations"]
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) for item in limitations
    ):
        raise FinCLError(
            "limitations must be a list of strings; a forecast issued with known "
            "gaps must carry them, so it is never read back as unqualified"
        )


def _validate_snapshot_issue(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"snapshot"}:
        raise FinCLError("FORECAST_SNAPSHOT_ISSUE needs a single snapshot anchor")
    _identifier(anchors["snapshot"], "the snapshot id")
    expected = {"document", "digest", "reason"}
    if set(payload) != expected:
        raise FinCLError(f"FORECAST_SNAPSHOT_ISSUE payload is exactly {sorted(expected)}")
    validate_snapshot_document(payload["document"])
    digest = payload["digest"]
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise FinCLError("the snapshot digest must be a sha256: value")
    if digest != snapshot_digest(payload["document"]):
        raise FinCLError(
            "the snapshot digest does not cover the document it was issued with; "
            "the snapshot is refused rather than stored under a digest that does "
            "not prove what it holds"
        )
    _reason(payload)


def _validate_match_set(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"snapshot", "match"}:
        raise FinCLError("FORECAST_MATCH_SET needs snapshot and match anchors")
    _identifier(anchors["snapshot"], "the snapshot id")
    _identifier(anchors["match"], "the match id")
    expected = {"event_id", "movement_id", "matched_on", "amount", "match_kind", "reason"}
    if set(payload) != expected:
        raise FinCLError(f"FORECAST_MATCH_SET payload is exactly {sorted(expected)}")
    _reference(payload["event_id"], "event_id")
    _reference(payload["movement_id"], "movement_id")
    _calendar_date(payload["matched_on"], "matched_on")
    # A zero-amount match binds nothing; it would claim an event was answered
    # while moving no money against it.
    _amount(payload["amount"], "the matched amount", allow_zero=False)
    if payload["match_kind"] not in MATCH_KINDS:
        raise FinCLError(f"match_kind must be one of {sorted(MATCH_KINDS)}")
    _reason(payload)


def _validate_match_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"snapshot", "match"}:
        raise FinCLError("FORECAST_MATCH_RETIRE needs snapshot and match anchors")
    _identifier(anchors["snapshot"], "the snapshot id")
    _identifier(anchors["match"], "the match id")
    if set(payload) != {"reason"}:
        raise FinCLError("FORECAST_MATCH_RETIRE payload is exactly ['reason']")
    _reason(payload)


def validate_variance_payload(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind == FORECAST_SNAPSHOT_ISSUE:
        _validate_snapshot_issue(anchors, payload)
    elif kind == FORECAST_MATCH_SET:
        _validate_match_set(anchors, payload)
    elif kind == FORECAST_MATCH_RETIRE:
        _validate_match_retire(anchors, payload)
    else:  # pragma: no cover - guarded by the dispatcher
        raise FinCLError(f"{kind} is not a forecast-variance decision")


__all__ = ["validate_snapshot_document", "validate_variance_payload"]
