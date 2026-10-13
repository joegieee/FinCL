"""Structural validation of the ``CARD_FACILITY_*`` / ``CARD_LIMIT_OBSERVATION_*``
decision kinds.

Pure shape and value checks — no database. Facility membership exclusivity,
account-kind, currency consistency and dependent-observation checks run against
the projection in :mod:`fincl.cards.preview` and again in the projection pass.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    CARD_FACILITY_RETIRE,
    CARD_FACILITY_SET,
    CARD_LIMIT_OBSERVATION_RETIRE,
    CARD_LIMIT_OBSERVATION_SET,
    CARD_PAYMENT_ALLOCATION_RETIRE,
    CARD_PAYMENT_ALLOCATION_SET,
    CARD_REPAYMENT_PLAN_RETIRE,
    CARD_REPAYMENT_PLAN_SET,
    REPAYMENT_RULES,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REASON_MAX = 2000
_LABEL_MAX = 200


def _reason(payload: dict[str, Any]) -> None:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX:
        raise FinCLError("a reason of 1-2,000 characters is required")


def _uuid(value: object, label: str) -> None:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise FinCLError(f"{label} must be a lowercase UUID")


def _currency(value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isalpha()
        or not value.isupper()
    ):
        raise FinCLError("currency must be a three-letter uppercase code")


def _calendar_date(value: object, label: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise FinCLError(f"{label} must be an ISO calendar date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError(f"{label} is not a real calendar date: {value}") from exc


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


def _validate_facility(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"facility"}:
        raise FinCLError("CARD_FACILITY_SET needs a single facility anchor")
    _uuid(anchors["facility"], "the facility id")
    if set(payload) != {"label", "account_ids", "currency", "reason"}:
        raise FinCLError(
            "CARD_FACILITY_SET payload is exactly {label, account_ids, currency, reason}"
        )
    if (
        not isinstance(payload["label"], str)
        or not payload["label"].strip()
        or len(payload["label"]) > _LABEL_MAX
    ):
        raise FinCLError(f"the facility label must be 1-{_LABEL_MAX} characters")
    _currency(payload["currency"])
    account_ids = payload["account_ids"]
    if (
        not isinstance(account_ids, list)
        or not account_ids
        or not all(isinstance(a, str) and a for a in account_ids)
    ):
        raise FinCLError("account_ids must be a non-empty list of account ids")
    if len(set(account_ids)) != len(account_ids):
        raise FinCLError("account_ids must not repeat an account")
    _reason(payload)


def _validate_facility_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"facility"}:
        raise FinCLError("CARD_FACILITY_RETIRE needs a single facility anchor")
    _uuid(anchors["facility"], "the facility id")
    if set(payload) != {"reason"}:
        raise FinCLError("CARD_FACILITY_RETIRE payload is exactly {reason}")
    _reason(payload)


def _validate_limit(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"facility", "observation"}:
        raise FinCLError(
            "CARD_LIMIT_OBSERVATION_SET needs a facility and an observation anchor"
        )
    _uuid(anchors["facility"], "the facility id")
    _uuid(anchors["observation"], "the observation id")
    if set(payload) != {"observed_on", "limit", "source_ref", "reason"}:
        raise FinCLError(
            "CARD_LIMIT_OBSERVATION_SET payload is exactly "
            "{observed_on, limit, source_ref, reason}"
        )
    _calendar_date(payload["observed_on"], "observed_on")
    _positive_money(payload["limit"], "the observed limit")
    if payload["source_ref"] is not None and (
        not isinstance(payload["source_ref"], str) or not payload["source_ref"].strip()
    ):
        raise FinCLError("source_ref must be a non-empty string or null")
    _reason(payload)


def _validate_limit_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"facility", "observation"}:
        raise FinCLError(
            "CARD_LIMIT_OBSERVATION_RETIRE needs a facility and an observation anchor"
        )
    _uuid(anchors["facility"], "the facility id")
    _uuid(anchors["observation"], "the observation id")
    if set(payload) != {"reason"}:
        raise FinCLError("CARD_LIMIT_OBSERVATION_RETIRE payload is exactly {reason}")
    _reason(payload)


def _validate_payment_allocation(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"card_entry"}:
        raise FinCLError("CARD_PAYMENT_ALLOCATION_SET needs a single card_entry anchor")
    if not isinstance(anchors["card_entry"], str) or not anchors["card_entry"]:
        raise FinCLError("the card_entry anchor must be a source entry anchor")
    if set(payload) != {"allocations", "reason"}:
        raise FinCLError(
            "CARD_PAYMENT_ALLOCATION_SET payload is exactly {allocations, reason}"
        )
    allocations = payload["allocations"]
    if not isinstance(allocations, list) or not allocations:
        raise FinCLError("allocations must be a non-empty list")
    seen: set[str] = set()
    for index, item in enumerate(allocations):
        if not isinstance(item, dict) or set(item) != {"statement_id", "amount"}:
            raise FinCLError(f"allocation {index} must be exactly {{statement_id, amount}}")
        if not isinstance(item["statement_id"], str) or not item["statement_id"]:
            raise FinCLError(f"allocation {index} needs a statement id")
        if item["statement_id"] in seen:
            raise FinCLError(f"allocation {index} repeats statement {item['statement_id']}")
        seen.add(item["statement_id"])
        _positive_money(item["amount"], f"allocation {index} amount")
    _reason(payload)


def _validate_payment_allocation_retire(
    anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if set(anchors) != {"card_entry"}:
        raise FinCLError(
            "CARD_PAYMENT_ALLOCATION_RETIRE needs a single card_entry anchor"
        )
    if not isinstance(anchors["card_entry"], str) or not anchors["card_entry"]:
        raise FinCLError("the card_entry anchor must be a source entry anchor")
    if set(payload) != {"reason"}:
        raise FinCLError("CARD_PAYMENT_ALLOCATION_RETIRE payload is exactly {reason}")
    _reason(payload)


def _validate_repayment_plan(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"account"} or not isinstance(anchors["account"], str) or not anchors["account"]:
        raise FinCLError(f"{kind} is anchored to one card account")
    if kind == CARD_REPAYMENT_PLAN_SET:
        if set(payload) != {"rule", "reason"}:
            raise FinCLError("CARD_REPAYMENT_PLAN_SET payload is exactly {rule, reason}")
        if payload["rule"] not in REPAYMENT_RULES:
            raise FinCLError(f"the repayment rule is one of {', '.join(REPAYMENT_RULES)}")
    elif set(payload) != {"reason"}:
        raise FinCLError("CARD_REPAYMENT_PLAN_RETIRE payload is exactly {reason}")
    _reason(payload)


def validate_card_payload(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind == CARD_FACILITY_SET:
        _validate_facility(anchors, payload)
    elif kind == CARD_FACILITY_RETIRE:
        _validate_facility_retire(anchors, payload)
    elif kind == CARD_LIMIT_OBSERVATION_SET:
        _validate_limit(anchors, payload)
    elif kind == CARD_LIMIT_OBSERVATION_RETIRE:
        _validate_limit_retire(anchors, payload)
    elif kind == CARD_PAYMENT_ALLOCATION_SET:
        _validate_payment_allocation(anchors, payload)
    elif kind == CARD_PAYMENT_ALLOCATION_RETIRE:
        _validate_payment_allocation_retire(anchors, payload)
    elif kind in (CARD_REPAYMENT_PLAN_SET, CARD_REPAYMENT_PLAN_RETIRE):
        _validate_repayment_plan(kind, anchors, payload)
    else:  # pragma: no cover - guarded by the dispatcher
        raise FinCLError(f"{kind} is not a credit-card decision")
