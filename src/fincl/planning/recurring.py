"""Recurring commitments, candidate dismissals and occurrence associations
(DESIGN-personal-finance §7).

Detection stays an observation, not an originating decision. A commitment is an
explicit reviewed record; a dismissal suppresses one deterministic candidate
identity; an occurrence association adds context to one scheduled date and never
removes or recounts an expense. All project into disposable tables rebuilt by
journal replay.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Any

from ..decision_journal import JournalRecord
from ..errors import FinCLError
from ..money import ExactMoney
from .schedules import parse_schedule

RECURRING_COMMITMENT_SET = "RECURRING_COMMITMENT_SET"
RECURRING_CANDIDATE_DISMISS = "RECURRING_CANDIDATE_DISMISS"
RECURRING_OCCURRENCE_SET = "RECURRING_OCCURRENCE_SET"
RECURRING_OCCURRENCE_RETIRE = "RECURRING_OCCURRENCE_RETIRE"
RECURRING_DECISION_KINDS = frozenset(
    {RECURRING_COMMITMENT_SET, RECURRING_CANDIDATE_DISMISS,
     RECURRING_OCCURRENCE_SET, RECURRING_OCCURRENCE_RETIRE}
)

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_NAME = re.compile(r"^[^\x00-\x1f]{1,80}$")
_MAX_LIST = 50


def _reason(payload: dict[str, Any]) -> None:
    value = payload.get("reason")
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise FinCLError("A reason of 1–2,000 characters is required")


def _currency(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise FinCLError("Choose a three-letter uppercase currency")
    return value


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise FinCLError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError(f"{field} is not a real date") from exc


def _money(value: Any, *, allow_zero: bool) -> ExactMoney:
    if not isinstance(value, str):
        raise FinCLError("Amounts must be exact decimal strings")
    try:
        money = ExactMoney.parse(value)
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"{value!r} is not an exact amount") from exc
    if money.scale > 2:
        raise FinCLError("Amounts allow at most two decimal places")
    if money.coefficient < 0 or (not allow_zero and money.coefficient == 0):
        raise FinCLError("A non-negative amount is required" if allow_zero else "A positive amount is required")
    return money


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_LIST:
        raise FinCLError(f"{field} must be a list of at most {_MAX_LIST} strings")
    if not all(isinstance(item, str) and item.strip() and len(item) <= 200 for item in value):
        raise FinCLError(f"{field} entries must be non-empty strings")
    return list(value)


def validate_recurring(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    _reason(payload)
    if kind == RECURRING_COMMITMENT_SET:
        if set(anchors) != {"commitment"} or not _ID.fullmatch(anchors["commitment"]):
            raise FinCLError("RECURRING_COMMITMENT_SET anchors must be {commitment}")
        expected = {"name", "currency", "merchant_key", "account_ids", "seed_entry_anchors",
                    "schedule", "expected_amount", "amount_tolerance", "status", "ended_on", "reason"}
        if set(payload) != expected:
            raise FinCLError(f"RECURRING_COMMITMENT_SET payload fields must be exactly {sorted(expected)}")
        if not str(payload["name"]).strip() or not _SAFE_NAME.fullmatch(str(payload["name"]).strip()):
            raise FinCLError("Commitment name must contain 1–80 characters without control characters")
        _currency(payload["currency"])
        if not isinstance(payload["merchant_key"], str) or not payload["merchant_key"].strip() or len(payload["merchant_key"]) > 200:
            raise FinCLError("merchant_key must be a non-empty string")
        _string_list(payload["account_ids"], "account_ids")
        _string_list(payload["seed_entry_anchors"], "seed_entry_anchors")
        parse_schedule(payload["schedule"])
        _money(payload["expected_amount"], allow_zero=False)
        _money(payload["amount_tolerance"], allow_zero=True)
        if payload["status"] not in {"active", "paused", "ended"}:
            raise FinCLError("status must be active, paused or ended")
        if payload["status"] == "ended":
            _iso_date(payload["ended_on"], "ended_on")
        elif payload["ended_on"] is not None:
            raise FinCLError("ended_on must be null unless status is 'ended'")
    elif kind == RECURRING_CANDIDATE_DISMISS:
        if set(anchors) != {"candidate"} or not _ID.fullmatch(anchors["candidate"]):
            raise FinCLError("RECURRING_CANDIDATE_DISMISS anchors must be {candidate}")
        if set(payload) != {"reason"}:
            raise FinCLError("RECURRING_CANDIDATE_DISMISS payload must be exactly {reason}")
    elif kind == RECURRING_OCCURRENCE_SET:
        if set(anchors) != {"commitment", "scheduled_on"} or not _ID.fullmatch(anchors["commitment"]):
            raise FinCLError("RECURRING_OCCURRENCE_SET anchors must be {commitment, scheduled_on}")
        _iso_date(anchors["scheduled_on"], "scheduled_on")
        if set(payload) != {"entry", "reason"}:
            raise FinCLError("RECURRING_OCCURRENCE_SET payload fields must be exactly {entry, reason}")
        if payload["entry"] is not None and (not isinstance(payload["entry"], str) or not payload["entry"].strip()):
            raise FinCLError("entry must be a transaction anchor or null (explicitly skipped)")
    elif kind == RECURRING_OCCURRENCE_RETIRE:
        if set(anchors) != {"commitment", "scheduled_on"} or not _ID.fullmatch(anchors["commitment"]):
            raise FinCLError("RECURRING_OCCURRENCE_RETIRE anchors must be {commitment, scheduled_on}")
        _iso_date(anchors["scheduled_on"], "scheduled_on")
        if set(payload) != {"reason"}:
            raise FinCLError("RECURRING_OCCURRENCE_RETIRE payload must be exactly {reason}")
    else:  # pragma: no cover - guarded by the caller
        raise FinCLError(f"{kind} is not a recurring decision kind")


def apply_recurring(connection: sqlite3.Connection, record: JournalRecord) -> None:
    payload = record.payload
    if record.decision_kind == RECURRING_COMMITMENT_SET:
        commitment = record.anchors["commitment"]
        existing = connection.execute(
            "SELECT currency FROM recurring_commitments WHERE commitment = ?", (commitment,)
        ).fetchone()
        if existing is not None and existing["currency"] != payload["currency"]:
            raise FinCLError(
                f"decision journal record {record.seq}: commitment {commitment} currency "
                f"is {existing['currency']} and cannot change"
            )
        expected = ExactMoney.parse(str(payload["expected_amount"]))
        tolerance = ExactMoney.parse(str(payload["amount_tolerance"]))
        connection.execute(
            "INSERT OR REPLACE INTO recurring_commitments(commitment, name, currency, "
            "merchant_key, account_ids_json, seed_anchors_json, schedule_json, "
            "expected_coefficient, expected_scale, tolerance_coefficient, tolerance_scale, "
            "status, ended_on, journal_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                commitment, str(payload["name"]).strip(), str(payload["currency"]),
                str(payload["merchant_key"]),
                json.dumps(_string_list(payload["account_ids"], "account_ids"), sort_keys=True),
                json.dumps(_string_list(payload["seed_entry_anchors"], "seed_entry_anchors"), sort_keys=True),
                json.dumps(parse_schedule(payload["schedule"]), sort_keys=True),
                expected.coefficient, expected.scale,
                tolerance.coefficient, tolerance.scale,
                str(payload["status"]), payload["ended_on"], record.seq,
            ),
        )
    elif record.decision_kind == RECURRING_CANDIDATE_DISMISS:
        connection.execute(
            "INSERT OR REPLACE INTO recurring_candidate_dismissals(candidate, journal_seq, reason) "
            "VALUES (?,?,?)",
            (record.anchors["candidate"], record.seq, str(payload["reason"]).strip()),
        )
    elif record.decision_kind == RECURRING_OCCURRENCE_SET:
        commitment = record.anchors["commitment"]
        if connection.execute(
            "SELECT 1 FROM recurring_commitments WHERE commitment = ?", (commitment,)
        ).fetchone() is None:
            raise FinCLError(
                f"decision journal record {record.seq}: occurrence references unknown "
                f"commitment {commitment}"
            )
        connection.execute(
            "INSERT OR REPLACE INTO recurring_occurrences(commitment, scheduled_on, "
            "entry_anchor, journal_seq, bound, dangling_reason) VALUES (?,?,?,?,1,NULL)",
            (commitment, record.anchors["scheduled_on"], payload["entry"], record.seq),
        )
    elif record.decision_kind == RECURRING_OCCURRENCE_RETIRE:
        connection.execute(
            "DELETE FROM recurring_occurrences WHERE commitment = ? AND scheduled_on = ?",
            (record.anchors["commitment"], record.anchors["scheduled_on"]),
        )
