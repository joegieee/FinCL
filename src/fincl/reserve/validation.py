"""Structural validation of ``RESERVE_POLICY_SET``.

Pure shape and value checks, no database. The payload carries a month count and
a reason and nothing else: deliberately no amount, no currency and no per-account
detail, so the one thing an operator can change here is the policy, never the
evidence it is applied to.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..errors import FinCLError
from .models import MAX_RESERVE_MONTHS, RESERVE_POLICY_SET

_REASON_MAX = 2000


def _validate_set(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"policy"}:
        raise FinCLError("RESERVE_POLICY_SET needs a single policy anchor")
    if anchors["policy"] != "reserve":
        # The policy is a singleton; a second anchor value would imply two
        # simultaneous policies and silently let one shadow the other.
        raise FinCLError("the reserve policy anchor is always 'reserve'")
    expected = {"months", "reason"}
    if set(payload) != expected:
        raise FinCLError(f"RESERVE_POLICY_SET payload is exactly {sorted(expected)}")
    raw = payload["months"]
    if not isinstance(raw, str):
        raise FinCLError("months must be a decimal string")
    try:
        months = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise FinCLError(f"months is not a number: {raw!r}") from exc
    if not months.is_finite():
        raise FinCLError("months must be a finite number")
    if months <= 0:
        raise FinCLError("months must be greater than zero")
    if months > MAX_RESERVE_MONTHS:
        raise FinCLError(f"months must not exceed {MAX_RESERVE_MONTHS}")
    if months != months.quantize(Decimal("0.1")):
        raise FinCLError("months is recorded to at most one decimal place")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX:
        raise FinCLError("a reason of 1-2,000 characters is required")


def validate_reserve_payload(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind == RESERVE_POLICY_SET:
        _validate_set(anchors, payload)
    else:  # pragma: no cover - guarded by the dispatcher
        raise FinCLError(f"{kind} is not a reserve-policy decision")


__all__ = ["validate_reserve_payload"]
