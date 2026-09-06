"""Structural validation of the ``PERSONAL_DEBT_*`` / ``ENTRY_RESIDUAL_*`` kinds.

Pure shape and value checks only — no database. Chronology, anchor resolution,
component-partition and closure-conflict checks run against the projection in
:mod:`fincl.debts.preview` before a durable append and, for the subset that can
raise during replay, again in the projection.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    DIRECTIONS,
    ENTRY_RESIDUAL_RETIRE,
    ENTRY_RESIDUAL_SET,
    EXPENSE_CATEGORY_EVENTS,
    INBOUND_CLASSIFICATION_RETIRE,
    OFF_LEDGER_EVENTS,
    OPENING_EVENTS,
    PERSONAL_DEBT_CLOSE,
    PERSONAL_DEBT_DETAILS_SET,
    PERSONAL_DEBT_EVENT_RETIRE,
    PERSONAL_DEBT_EVENT_SET,
    PERSONAL_DEBT_OPEN,
    PERSONAL_DEBT_REOPEN,
    events_for_direction,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REASON_MAX = 2000
_LABEL_MAX = 200

# Slice 1 accepts identity, details, opening balances and lifecycle only. Source
# -linked allocations and shared-expense residuals are slice 2; flip this to
# False in that change.
_SLICE1_ONLY = True


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
        raise FinCLError("currency must be a three-letter uppercase code")


def _label(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > _LABEL_MAX:
        raise FinCLError(f"{label} must be 1-{_LABEL_MAX} characters")


def _positive_money(value: object, label: str) -> ExactMoney:
    if not isinstance(value, str):
        raise FinCLError(f"{label} must be a decimal string")
    try:
        money = ExactMoney.parse(value)
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"{label} is not a valid amount: {value!r}") from exc
    if money.coefficient <= 0:
        raise FinCLError(f"{label} must be strictly positive")
    return money


def _nonnegative_money(value: object, label: str) -> ExactMoney:
    if not isinstance(value, str):
        raise FinCLError(f"{label} must be a decimal string")
    try:
        money = ExactMoney.parse(value)
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"{label} is not a valid amount: {value!r}") from exc
    if money.coefficient < 0:
        raise FinCLError(f"{label} may not be negative")
    return money


def _validate_open(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"debt"}:
        raise FinCLError("PERSONAL_DEBT_OPEN needs a single debt anchor")
    _uuid(anchors["debt"], "the debt id")
    expected = {"direction", "currency", "counterparty_label", "opened_on", "due_on", "reason"}
    if set(payload) != expected:
        raise FinCLError(f"PERSONAL_DEBT_OPEN payload is exactly {sorted(expected)}")
    if payload["direction"] not in DIRECTIONS:
        raise FinCLError("direction must be PAYABLE or RECEIVABLE")
    _currency(payload["currency"])
    _label(payload["counterparty_label"], "the counterparty label")
    opened = _calendar_date(payload["opened_on"], "opened_on")
    if payload["due_on"] is not None:
        due = _calendar_date(payload["due_on"], "due_on")
        if due < opened:
            raise FinCLError("due_on may not precede opened_on")
    _reason(payload)


def _validate_details(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"debt"}:
        raise FinCLError("PERSONAL_DEBT_DETAILS_SET needs a single debt anchor")
    _uuid(anchors["debt"], "the debt id")
    if set(payload) != {"counterparty_label", "due_on", "reason"}:
        raise FinCLError(
            "PERSONAL_DEBT_DETAILS_SET payload is exactly {counterparty_label, due_on, reason}"
        )
    _label(payload["counterparty_label"], "the counterparty label")
    if payload["due_on"] is not None:
        _calendar_date(payload["due_on"], "due_on")
    _reason(payload)


def _validate_allocation_item(item: object, index: int) -> None:
    expected = {
        "entry_anchor",
        "principal_amount",
        "interest_amount",
        "fee_amount",
        "separate_fee_amount",
    }
    if not isinstance(item, dict) or set(item) != expected:
        raise FinCLError(f"allocation {index} must be exactly {sorted(expected)}")
    if not isinstance(item["entry_anchor"], str) or not item["entry_anchor"]:
        raise FinCLError(f"allocation {index} needs an entry anchor")
    _nonnegative_money(item["principal_amount"], f"allocation {index} principal")
    _nonnegative_money(item["interest_amount"], f"allocation {index} interest")
    _nonnegative_money(item["fee_amount"], f"allocation {index} fee")
    _nonnegative_money(item["separate_fee_amount"], f"allocation {index} separate fee")


def _validate_event(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"debt", "event"}:
        raise FinCLError("PERSONAL_DEBT_EVENT_SET needs a debt and an event anchor")
    _uuid(anchors["debt"], "the debt id")
    _uuid(anchors["event"], "the event id")
    expected = {
        "event_kind",
        "direction",
        "principal_amount",
        "effective_on",
        "entry_allocations",
        "opening_balance",
        "expense_category",
        "reason",
    }
    if set(payload) != expected:
        raise FinCLError(f"PERSONAL_DEBT_EVENT_SET payload is exactly {sorted(expected)}")
    direction = payload["direction"]
    if direction not in DIRECTIONS:
        raise FinCLError("direction must be PAYABLE or RECEIVABLE")
    kinds = events_for_direction(direction)
    if payload["event_kind"] not in kinds:
        raise FinCLError(
            f"{payload['event_kind']!r} is not a {direction} event kind"
        )
    _positive_money(payload["principal_amount"], "the event principal")
    _calendar_date(payload["effective_on"], "effective_on")

    if not isinstance(payload["opening_balance"], bool):
        raise FinCLError("opening_balance must be true or false")
    if payload["opening_balance"] and payload["event_kind"] not in OPENING_EVENTS:
        raise FinCLError("opening_balance is only valid on a BORROWED or LENT event")

    allocations = payload["entry_allocations"]
    if not isinstance(allocations, list):
        raise FinCLError("entry_allocations must be a list")
    for index, item in enumerate(allocations):
        _validate_allocation_item(item, index)

    off_ledger = payload["event_kind"] in OFF_LEDGER_EVENTS or payload["opening_balance"]
    if off_ledger and allocations:
        raise FinCLError(
            "an off-ledger or opening-balance event carries an empty entry_allocations list"
        )
    if not off_ledger and not allocations:
        raise FinCLError(
            "a normal cash debt event needs at least one entry allocation"
        )

    if payload["expense_category"] is not None:
        if payload["event_kind"] not in EXPENSE_CATEGORY_EVENTS:
            raise FinCLError(
                "expense_category is only carried by an EXPENSE_PAID_FOR_ME event"
            )
        if not isinstance(payload["expense_category"], str) or not payload["expense_category"]:
            raise FinCLError("expense_category must be a category key")
    elif payload["event_kind"] in EXPENSE_CATEGORY_EVENTS:
        raise FinCLError("EXPENSE_PAID_FOR_ME requires an expense_category")

    _reason(payload)

    if _SLICE1_ONLY and not off_ledger:
        raise FinCLError(
            "source-linked debt events arrive in v0.3 slice 2; slice 1 records "
            "explicit opening balances and off-ledger adjustments only"
        )
    if _SLICE1_ONLY and payload["event_kind"] in OFF_LEDGER_EVENTS:
        raise FinCLError(
            "off-ledger expense, forgiveness and write-off events arrive in v0.3 "
            "slice 2/3; slice 1 records opening balances only"
        )


def _validate_event_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"debt", "event"}:
        raise FinCLError("PERSONAL_DEBT_EVENT_RETIRE needs a debt and an event anchor")
    _uuid(anchors["debt"], "the debt id")
    _uuid(anchors["event"], "the event id")
    if set(payload) != {"reason"}:
        raise FinCLError("PERSONAL_DEBT_EVENT_RETIRE payload is exactly {reason}")
    _reason(payload)


def _validate_lifecycle(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"debt"}:
        raise FinCLError(f"{kind} needs a single debt anchor")
    _uuid(anchors["debt"], "the debt id")
    if set(payload) != {"effective_on", "reason"}:
        raise FinCLError(f"{kind} payload is exactly {{effective_on, reason}}")
    _calendar_date(payload["effective_on"], "effective_on")
    _reason(payload)
    if _SLICE1_ONLY:
        raise FinCLError(
            "close / reopen arrive in v0.3 slice 3"
        )


def _validate_residual(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"entry"}:
        raise FinCLError(f"{kind} needs an entry anchor")
    if kind == ENTRY_RESIDUAL_RETIRE:
        if set(payload) != {"reason"}:
            raise FinCLError("ENTRY_RESIDUAL_RETIRE payload is exactly {reason}")
        _reason(payload)
        return
    if set(payload) != {"expense_amount", "expense_category", "reason"}:
        raise FinCLError(
            "ENTRY_RESIDUAL_SET payload is exactly {expense_amount, expense_category, reason}"
        )
    _positive_money(payload["expense_amount"], "the residual expense amount")
    if not isinstance(payload["expense_category"], str) or not payload["expense_category"]:
        raise FinCLError("expense_category must be a category key")
    _reason(payload)
    if _SLICE1_ONLY:
        raise FinCLError("shared-expense residuals arrive in v0.3 slice 2")


def _validate_inbound_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"entry"}:
        raise FinCLError("INBOUND_CLASSIFICATION_RETIRE needs an entry anchor")
    if set(payload) != {"reason"}:
        raise FinCLError("INBOUND_CLASSIFICATION_RETIRE payload is exactly {reason}")
    _reason(payload)
    if _SLICE1_ONLY:
        raise FinCLError(
            "INBOUND_CLASSIFICATION_RETIRE is authored inside a v0.3 slice 2 "
            "coupled correction batch"
        )


def validate_debt_payload(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind == PERSONAL_DEBT_OPEN:
        _validate_open(anchors, payload)
    elif kind == PERSONAL_DEBT_DETAILS_SET:
        _validate_details(anchors, payload)
    elif kind == PERSONAL_DEBT_EVENT_SET:
        _validate_event(anchors, payload)
    elif kind == PERSONAL_DEBT_EVENT_RETIRE:
        _validate_event_retire(anchors, payload)
    elif kind in {PERSONAL_DEBT_CLOSE, PERSONAL_DEBT_REOPEN}:
        _validate_lifecycle(kind, anchors, payload)
    elif kind in {ENTRY_RESIDUAL_SET, ENTRY_RESIDUAL_RETIRE}:
        _validate_residual(kind, anchors, payload)
    elif kind == INBOUND_CLASSIFICATION_RETIRE:
        _validate_inbound_retire(anchors, payload)
    else:  # pragma: no cover - guarded by the dispatcher
        raise FinCLError(f"{kind} is not a personal-debt decision")
