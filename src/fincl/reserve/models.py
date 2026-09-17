"""Reserve-policy vocabulary and its resolved shape."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

RESERVE_POLICY_SET = "RESERVE_POLICY_SET"

RESERVE_DECISION_KINDS = frozenset({RESERVE_POLICY_SET})

RESERVE_POLICY_VERSION = "reserve-policy-v1"

# The framework's stated default, unchanged. A ledger that never records a
# decision must read back exactly as it did when this was a bare constant.
DEFAULT_RESERVE_MONTHS = Decimal("3")

# Twelve months is where a reserve stops being an emergency buffer and becomes a
# savings goal, which the product models separately. The bound is a guard
# against a typo, not a judgement: a month count inside it is the operator's
# call and this module has no opinion on it.
MAX_RESERVE_MONTHS = Decimal("12")


@dataclass(frozen=True, slots=True)
class ReservePolicy:
    """The resolved policy.

    ``is_default`` is what lets a reader tell "three months because that is the
    framework default" apart from "three months because the operator chose it" —
    the same figure, but only the second one is a decision, and a checklist that
    asks the operator to choose a reserve policy must not be satisfied by the
    first.
    """

    months: Decimal
    journal_seq: int | None
    decided_at: str | None
    policy_version: str | None
    is_default: bool
    reason: str | None = None


__all__ = [
    "DEFAULT_RESERVE_MONTHS",
    "MAX_RESERVE_MONTHS",
    "RESERVE_DECISION_KINDS",
    "RESERVE_POLICY_SET",
    "RESERVE_POLICY_VERSION",
    "ReservePolicy",
]
