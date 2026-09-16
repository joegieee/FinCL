"""Structural validation of the ``BALANCE_OBSERVATION_*`` and
``MOVEMENT_COVERAGE_*`` kinds.

Pure shape and value checks only — no database. An observation is an absolute
as-of balance for one ``(account_id, currency_pocket)``, never a delta, and its
``observed_on`` may not be in the future. A certification is a closed date range
for one pocket plus the named basis the operator inspected; it carries no amount
at all, so no amount check exists here to relax later.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    BALANCE_OBSERVATION_RETIRE,
    BALANCE_OBSERVATION_SET,
    MOVEMENT_COVERAGE_BASES,
    MOVEMENT_COVERAGE_CERTIFY,
    MOVEMENT_COVERAGE_RETIRE,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REASON_MAX = 2000
_ID_MAX = 200


def _reason(payload: dict[str, Any]) -> None:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX:
        raise FinCLError("a reason of 1-2,000 characters is required")


def _uuid(value: object, label: str) -> None:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise FinCLError(f"{label} must be a lowercase UUID")


def _calendar_date(value: object, label: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise FinCLError(f"{label} must be an ISO calendar date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError(f"{label} is not a real calendar date: {value}") from exc


def _currency(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isalpha()
        or not value.isupper()
    ):
        raise FinCLError("currency_pocket must be a three-letter uppercase code")


def _identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > _ID_MAX:
        raise FinCLError(f"{label} must be 1-{_ID_MAX} characters")


def _validate_set(anchors: dict[str, str], payload: dict[str, Any], *, today: date) -> None:
    if set(anchors) != {"observation"}:
        raise FinCLError("BALANCE_OBSERVATION_SET needs a single observation anchor")
    _uuid(anchors["observation"], "the observation id")
    expected = {"account_id", "currency_pocket", "observed_on", "balance", "reason"}
    if set(payload) != expected:
        raise FinCLError(
            f"BALANCE_OBSERVATION_SET payload is exactly {sorted(expected)}"
        )
    _identifier(payload["account_id"], "account_id")
    _currency(payload["currency_pocket"])
    observed = _calendar_date(payload["observed_on"], "observed_on")
    if observed > today:
        raise FinCLError("observed_on may not be in the future")
    if not isinstance(payload["balance"], str):
        raise FinCLError("balance must be a decimal string")
    try:
        ExactMoney.parse(payload["balance"])
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"balance is not a valid amount: {payload['balance']!r}") from exc
    _reason(payload)


def _validate_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"observation"}:
        raise FinCLError("BALANCE_OBSERVATION_RETIRE needs a single observation anchor")
    _uuid(anchors["observation"], "the observation id")
    if set(payload) != {"reason"}:
        raise FinCLError("BALANCE_OBSERVATION_RETIRE payload is exactly {reason}")
    _reason(payload)


def _validate_certify(
    anchors: dict[str, str], payload: dict[str, Any], *, today: date
) -> None:
    if set(anchors) != {"certification"}:
        raise FinCLError("MOVEMENT_COVERAGE_CERTIFY needs a single certification anchor")
    _uuid(anchors["certification"], "the certification id")
    expected = {
        "account_id", "currency_pocket", "period_start", "period_end", "basis", "reason",
    }
    if set(payload) != expected:
        raise FinCLError(
            f"MOVEMENT_COVERAGE_CERTIFY payload is exactly {sorted(expected)}"
        )
    _identifier(payload["account_id"], "account_id")
    _currency(payload["currency_pocket"])
    start = _calendar_date(payload["period_start"], "period_start")
    end = _calendar_date(payload["period_end"], "period_end")
    if start > end:
        raise FinCLError("period_start may not be after period_end")
    if end > today:
        # Completeness can only be attested for days that have happened.
        raise FinCLError("period_end may not be in the future")
    if payload["basis"] not in MOVEMENT_COVERAGE_BASES:
        raise FinCLError(
            "basis must be one of " + ", ".join(MOVEMENT_COVERAGE_BASES)
        )
    _reason(payload)


def _validate_certify_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"certification"}:
        raise FinCLError("MOVEMENT_COVERAGE_RETIRE needs a single certification anchor")
    _uuid(anchors["certification"], "the certification id")
    if set(payload) != {"reason"}:
        raise FinCLError("MOVEMENT_COVERAGE_RETIRE payload is exactly {reason}")
    _reason(payload)


def validate_position_payload(
    kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    *,
    today: date | None = None,
) -> None:
    today = today or date.today()
    if kind == BALANCE_OBSERVATION_SET:
        _validate_set(anchors, payload, today=today)
    elif kind == BALANCE_OBSERVATION_RETIRE:
        _validate_retire(anchors, payload)
    elif kind == MOVEMENT_COVERAGE_CERTIFY:
        _validate_certify(anchors, payload, today=today)
    elif kind == MOVEMENT_COVERAGE_RETIRE:
        _validate_certify_retire(anchors, payload)
    else:  # pragma: no cover - guarded by the dispatcher
        raise FinCLError(f"{kind} is not a position decision")
