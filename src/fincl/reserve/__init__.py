"""The reserve policy: how many months of essential spending to hold back.

The framework always derived a minimum reserve, but it was pinned at three
months by a constant, so reviewing the default was not the same as *choosing* a
policy. This package makes the month count an operator decision, journalled like
any other, while keeping the rule that matters: the operator chooses the number
of months, and the monthly amount it multiplies stays calculated from evidence.
There is no amount anywhere in this package's payload or table, so an adjustable
policy can never become a back door for typing in a living cost.
"""

from .models import (
    DEFAULT_RESERVE_MONTHS,
    MAX_RESERVE_MONTHS,
    RESERVE_DECISION_KINDS,
    RESERVE_POLICY_SET,
    RESERVE_POLICY_VERSION,
    ReservePolicy,
)
from .projection import apply_reserve_policy_decision
from .queries import reserve_policy
from .validation import validate_reserve_payload

__all__ = [
    "DEFAULT_RESERVE_MONTHS",
    "MAX_RESERVE_MONTHS",
    "RESERVE_DECISION_KINDS",
    "RESERVE_POLICY_SET",
    "RESERVE_POLICY_VERSION",
    "ReservePolicy",
    "apply_reserve_policy_decision",
    "reserve_policy",
    "validate_reserve_payload",
]
