"""Structural validation of the ``INSTALLMENT_*`` decision kinds.

Pure shape and value checks — no database. Occurrence identity is an evidence
anchor, not list position; unknown principal / term is allowed and shown as
unknown, but an unknown principal may not carry an invented schedule. Chronology,
anchor resolution and the shared component-partition run in
:mod:`fincl.installments.preview` and again in the projection.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney, exact_sum, format_decimal
from .models import (
    INSTALLMENT_ALLOCATION_RETIRE,
    INSTALLMENT_ALLOCATION_SET,
    INSTALLMENT_PLAN_RETIRE,
    INSTALLMENT_PLAN_SET,
    INSTALLMENT_SETTLEMENT_RETIRE,
    INSTALLMENT_SETTLEMENT_SET,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REASON_MAX = 2000
_LABEL_MAX = 200
_PRECISIONS = frozenset({"DAY", "MONTH", "UNKNOWN"})


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


def _positive_int_or_none(value: object, label: str) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
        raise FinCLError(f"{label} must be a positive integer or null")


def _validate_occurrence(item: object, index: int) -> str:
    expected = {
        "occurrence_id",
        "occurrence_index",
        "occurrence_count",
        "entry_anchor",
        "effective_on",
        "amount",
        "date_precision",
    }
    if not isinstance(item, dict) or set(item) != expected:
        raise FinCLError(f"occurrence {index} must be exactly {sorted(expected)}")
    _uuid(item["occurrence_id"], f"occurrence {index} id")
    _positive_int_or_none(item["occurrence_index"], f"occurrence {index} index")
    _positive_int_or_none(item["occurrence_count"], f"occurrence {index} count")
    if not isinstance(item["entry_anchor"], str) or not item["entry_anchor"]:
        raise FinCLError(f"occurrence {index} needs an entry anchor")
    _calendar_date(item["effective_on"], f"occurrence {index} effective_on")
    _positive_money(item["amount"], f"occurrence {index} amount")
    if item["date_precision"] not in _PRECISIONS:
        raise FinCLError(f"occurrence {index} date_precision must be DAY, MONTH or UNKNOWN")
    return str(item["entry_anchor"])


def _validate_schedule_row(item: object, index: int) -> ExactMoney:
    expected = {"due_on", "principal", "fee"}
    if not isinstance(item, dict) or set(item) != expected:
        raise FinCLError(f"schedule row {index} must be exactly {sorted(expected)}")
    _calendar_date(item["due_on"], f"schedule row {index} due_on")
    principal = _positive_money(item["principal"], f"schedule row {index} principal")
    _nonnegative_money(item["fee"], f"schedule row {index} fee")
    return principal


def _validate_plan(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"plan"}:
        raise FinCLError("INSTALLMENT_PLAN_SET needs a single plan anchor")
    _uuid(anchors["plan"], "the plan id")
    expected = {
        "card_account_id",
        "currency",
        "plan_reference",
        "merchant_label",
        "term_months",
        "known_principal",
        "financing_fee",
        "opened_on",
        "status",
        "occurrences",
        "schedule",
        "reason",
    }
    if set(payload) != expected:
        raise FinCLError(f"INSTALLMENT_PLAN_SET payload is exactly {sorted(expected)}")
    if not isinstance(payload["card_account_id"], str) or not payload["card_account_id"]:
        raise FinCLError("card_account_id is required")
    _currency(payload["currency"])
    if payload["plan_reference"] is not None and (
        not isinstance(payload["plan_reference"], str) or not payload["plan_reference"].strip()
    ):
        raise FinCLError("plan_reference must be a non-empty string or null")
    if (
        not isinstance(payload["merchant_label"], str)
        or not payload["merchant_label"].strip()
        or len(payload["merchant_label"]) > _LABEL_MAX
    ):
        raise FinCLError(f"merchant_label must be 1-{_LABEL_MAX} characters")
    _positive_int_or_none(payload["term_months"], "term_months")
    _calendar_date(payload["opened_on"], "opened_on")
    if payload["status"] not in {"PROPOSED", "CONFIRMED"}:
        raise FinCLError("status must be PROPOSED or CONFIRMED")

    known_principal: ExactMoney | None = None
    if payload["known_principal"] is not None:
        known_principal = _positive_money(payload["known_principal"], "known_principal")
    if payload["financing_fee"] is not None:
        _nonnegative_money(payload["financing_fee"], "financing_fee")

    occurrences = payload["occurrences"]
    if not isinstance(occurrences, list) or not occurrences:
        raise FinCLError("occurrences must be a non-empty list")
    seen_ids: set[str] = set()
    seen_anchors: set[str] = set()
    for index, item in enumerate(occurrences):
        anchor = _validate_occurrence(item, index)
        occ_id = str(item["occurrence_id"])
        if occ_id in seen_ids:
            raise FinCLError(f"occurrence {index} repeats occurrence id {occ_id}")
        seen_ids.add(occ_id)
        if anchor in seen_anchors:
            raise FinCLError(
                f"occurrence {index} reuses a source anchor already claimed by "
                "another occurrence in this plan"
            )
        seen_anchors.add(anchor)

    schedule = payload["schedule"]
    if not isinstance(schedule, list):
        raise FinCLError("schedule must be a list")
    if schedule and known_principal is None:
        raise FinCLError(
            "an unknown-principal plan carries an empty schedule; no invented "
            "future payment follows from an unknown principal"
        )
    if schedule:
        principals = [_validate_schedule_row(row, i) for i, row in enumerate(schedule)]
        total_coeff, total_scale = exact_sum(*principals)
        assert known_principal is not None
        common = max(total_scale, known_principal.scale)
        scheduled = total_coeff * 10 ** (common - total_scale)
        want = known_principal.coefficient * 10 ** (common - known_principal.scale)
        # The schedule principal sums exactly to the known principal, allowing a
        # single disclosed rounding remainder no larger than one minor unit per
        # scheduled row.
        remainder = abs(scheduled - want)
        if remainder > len(schedule):
            raise FinCLError(
                "the schedule principal must sum to the known principal within a "
                f"disclosed rounding remainder ({format_decimal(scheduled, common)} "
                f"!= {format_decimal(want, common)})"
            )
    _reason(payload)


def _validate_plan_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"plan"}:
        raise FinCLError("INSTALLMENT_PLAN_RETIRE needs a single plan anchor")
    _uuid(anchors["plan"], "the plan id")
    if set(payload) != {"reason"}:
        raise FinCLError("INSTALLMENT_PLAN_RETIRE payload is exactly {reason}")
    _reason(payload)


def _validate_allocation(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"plan"}:
        raise FinCLError("INSTALLMENT_ALLOCATION_SET needs a single plan anchor")
    _uuid(anchors["plan"], "the plan id")
    if set(payload) != {"allocations", "reason"}:
        raise FinCLError("INSTALLMENT_ALLOCATION_SET payload is exactly {allocations, reason}")
    allocations = payload["allocations"]
    if not isinstance(allocations, list) or not allocations:
        raise FinCLError("allocations must be a non-empty list")
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(allocations):
        if not isinstance(item, dict) or set(item) != {"entry_anchor", "component_role", "amount"}:
            raise FinCLError(
                f"allocation {index} must be exactly {{entry_anchor, component_role, amount}}"
            )
        if not isinstance(item["entry_anchor"], str) or not item["entry_anchor"]:
            raise FinCLError(f"allocation {index} needs an entry anchor")
        if item["component_role"] not in {"principal", "fee"}:
            raise FinCLError(f"allocation {index} component_role must be principal or fee")
        _positive_money(item["amount"], f"allocation {index} amount")
        key = (str(item["entry_anchor"]), str(item["component_role"]))
        if key in seen:
            raise FinCLError(
                f"allocation {index} repeats ({key[0]}, {key[1]}); "
                "each source component is claimed once"
            )
        seen.add(key)
    _reason(payload)


def _validate_allocation_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"plan"}:
        raise FinCLError("INSTALLMENT_ALLOCATION_RETIRE needs a single plan anchor")
    _uuid(anchors["plan"], "the plan id")
    if set(payload) != {"reason"}:
        raise FinCLError("INSTALLMENT_ALLOCATION_RETIRE payload is exactly {reason}")
    _reason(payload)


def _validate_settlement(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"plan", "settlement"}:
        raise FinCLError("INSTALLMENT_SETTLEMENT_SET needs a plan and a settlement anchor")
    _uuid(anchors["plan"], "the plan id")
    _uuid(anchors["settlement"], "the settlement id")
    expected = {
        "settled_on",
        "source_entry_anchor",
        "principal_amount",
        "fee_amount",
        "waiver_amount",
        "reason",
    }
    if set(payload) != expected:
        raise FinCLError(f"INSTALLMENT_SETTLEMENT_SET payload is exactly {sorted(expected)}")
    _calendar_date(payload["settled_on"], "settled_on")
    if not isinstance(payload["source_entry_anchor"], str) or not payload["source_entry_anchor"]:
        raise FinCLError("source_entry_anchor is required")
    principal = _nonnegative_money(payload["principal_amount"], "principal_amount")
    fee = _nonnegative_money(payload["fee_amount"], "fee_amount")
    waiver = _nonnegative_money(payload["waiver_amount"], "waiver_amount")
    if principal.coefficient == 0 and fee.coefficient == 0 and waiver.coefficient == 0:
        raise FinCLError("a settlement needs a positive principal, fee or waiver component")
    _reason(payload)


def _validate_settlement_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"plan", "settlement"}:
        raise FinCLError(
            "INSTALLMENT_SETTLEMENT_RETIRE needs a plan and a settlement anchor"
        )
    _uuid(anchors["plan"], "the plan id")
    _uuid(anchors["settlement"], "the settlement id")
    if set(payload) != {"reason"}:
        raise FinCLError("INSTALLMENT_SETTLEMENT_RETIRE payload is exactly {reason}")
    _reason(payload)


def validate_installment_payload(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind == INSTALLMENT_PLAN_SET:
        _validate_plan(anchors, payload)
    elif kind == INSTALLMENT_PLAN_RETIRE:
        _validate_plan_retire(anchors, payload)
    elif kind == INSTALLMENT_ALLOCATION_SET:
        _validate_allocation(anchors, payload)
    elif kind == INSTALLMENT_ALLOCATION_RETIRE:
        _validate_allocation_retire(anchors, payload)
    elif kind == INSTALLMENT_SETTLEMENT_SET:
        _validate_settlement(anchors, payload)
    elif kind == INSTALLMENT_SETTLEMENT_RETIRE:
        _validate_settlement_retire(anchors, payload)
    else:  # pragma: no cover - guarded by the dispatcher
        raise FinCLError(f"{kind} is not an installment decision")
