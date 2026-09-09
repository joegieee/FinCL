"""Structural validation of the ``SINKING_FUND_*`` kinds.

Pure shape and value checks only — no database.  The rules that matter here are
the ones that keep a fund honest at the authoring boundary:

* an amount and a due date are each independently optional, but each must name
  the basis it came from, and an absent one must name ``unknown`` rather than
  borrow a basis it does not have;
* ``source_reference`` is mandatory in every case, so no fund exists without
  pointing at the evidence it was proposed from; and
* a contribution's ``amount`` is a recorded movement, so it is strictly
  positive — a zero contribution is not a contribution, and a negative one is a
  withdrawal wearing the wrong name.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    AMOUNT_BASIS_UNKNOWN,
    DATE_BASIS_UNKNOWN,
    FUND_AMOUNT_BASES,
    FUND_DATE_BASES,
    SINKING_FUND_ALLOCATION_RETIRE,
    SINKING_FUND_ALLOCATION_SET,
    SINKING_FUND_RELEASE_SET,
    SINKING_FUND_SET,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAME_RE = re.compile(r"^[^\x00-\x1f]{1,80}$")
_REASON_MAX = 2000


def _reason(payload: dict[str, Any]) -> None:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX:
        raise FinCLError("a reason of 1-2,000 characters is required")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise FinCLError(f"{label} must be 1-200 identifier characters")
    return value


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
        raise FinCLError("currency must be a three-letter uppercase code")


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


def _validate_fund_set(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"fund"}:
        raise FinCLError("SINKING_FUND_SET needs a single fund anchor")
    _identifier(anchors["fund"], "the fund id")
    expected = {
        "name",
        "currency",
        "amount_due",
        "due_on",
        "amount_basis",
        "date_basis",
        "source_reference",
        "contribution_dates",
        "linked_event_id",
        "reason",
    }
    if set(payload) != expected:
        raise FinCLError(f"SINKING_FUND_SET payload is exactly {sorted(expected)}")
    name = payload["name"]
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name.strip()):
        raise FinCLError("name must be 1-80 characters without control characters")
    _currency(payload["currency"])

    if payload["amount_basis"] not in FUND_AMOUNT_BASES:
        raise FinCLError(f"amount_basis must be one of {sorted(FUND_AMOUNT_BASES)}")
    if payload["date_basis"] not in FUND_DATE_BASES:
        raise FinCLError(f"date_basis must be one of {sorted(FUND_DATE_BASES)}")
    if payload["amount_due"] is None:
        if payload["amount_basis"] != AMOUNT_BASIS_UNKNOWN:
            raise FinCLError("a fund with no amount due must state an unknown amount_basis")
    else:
        if payload["amount_basis"] == AMOUNT_BASIS_UNKNOWN:
            raise FinCLError("a sourced amount due must name the basis it came from")
        _amount(payload["amount_due"], "amount_due", allow_zero=False)
    if payload["due_on"] is None:
        if payload["date_basis"] != DATE_BASIS_UNKNOWN:
            raise FinCLError("a fund with no due date must state an unknown date_basis")
    else:
        if payload["date_basis"] == DATE_BASIS_UNKNOWN:
            raise FinCLError("a sourced due date must name the basis it came from")
        _calendar_date(payload["due_on"], "due_on")

    _identifier(payload["source_reference"], "source_reference")
    dates = payload["contribution_dates"]
    if not isinstance(dates, list):
        raise FinCLError("contribution_dates must be a list of ISO calendar dates")
    seen: set[str] = set()
    for value in dates:
        _calendar_date(value, "a contribution date")
        if value in seen:
            raise FinCLError(f"contribution date {value} is listed twice")
        seen.add(str(value))
    if payload["linked_event_id"] is not None:
        _identifier(payload["linked_event_id"], "linked_event_id")


def _validate_allocation_set(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"fund", "allocation"}:
        raise FinCLError("SINKING_FUND_ALLOCATION_SET needs fund and allocation anchors")
    _identifier(anchors["fund"], "the fund id")
    _identifier(anchors["allocation"], "the allocation id")
    expected = {"allocated_on", "amount", "reason"}
    if set(payload) != expected:
        raise FinCLError(
            f"SINKING_FUND_ALLOCATION_SET payload is exactly {sorted(expected)}"
        )
    _calendar_date(payload["allocated_on"], "allocated_on")
    _amount(payload["amount"], "amount", allow_zero=False)
    _reason(payload)


def _validate_allocation_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"fund", "allocation"}:
        raise FinCLError(
            "SINKING_FUND_ALLOCATION_RETIRE needs fund and allocation anchors"
        )
    _identifier(anchors["fund"], "the fund id")
    _identifier(anchors["allocation"], "the allocation id")
    if set(payload) != {"reason"}:
        raise FinCLError("SINKING_FUND_ALLOCATION_RETIRE payload is exactly {reason}")
    _reason(payload)


def _validate_release_set(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"fund"}:
        raise FinCLError("SINKING_FUND_RELEASE_SET needs a single fund anchor")
    _identifier(anchors["fund"], "the fund id")
    expected = {"released_on", "amount", "reason"}
    if set(payload) != expected:
        raise FinCLError(f"SINKING_FUND_RELEASE_SET payload is exactly {sorted(expected)}")
    _calendar_date(payload["released_on"], "released_on")
    # A fund whose bill arrived at zero still releases; the earmark must come
    # back to unallocated cash either way.
    _amount(payload["amount"], "amount", allow_zero=True)
    _reason(payload)


def validate_fund_payload(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind == SINKING_FUND_SET:
        _validate_fund_set(anchors, payload)
        _reason(payload)
    elif kind == SINKING_FUND_ALLOCATION_SET:
        _validate_allocation_set(anchors, payload)
    elif kind == SINKING_FUND_ALLOCATION_RETIRE:
        _validate_allocation_retire(anchors, payload)
    elif kind == SINKING_FUND_RELEASE_SET:
        _validate_release_set(anchors, payload)
    else:  # pragma: no cover - guarded by the dispatcher
        raise FinCLError(f"{kind} is not a sinking-fund decision")


__all__ = ["validate_fund_payload"]
