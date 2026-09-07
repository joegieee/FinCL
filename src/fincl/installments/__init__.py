"""Credit-card installment plans (v0.4 slice 3, DESIGN-relationships-and-cost.md §4).

Confirmed source-proven chains of card statement occurrences, with known or
explicitly-unknown principal and term. Occurrence identity is an evidence
anchor, not list position. A projected schedule is distinct from the actual
observed occurrences even after a due date passes. Purchase attribution and
early settlement reuse the shared `analysis.allocations` component-claim
registry — there is no parallel over-claim checker.
"""

from .models import (
    INSTALLMENT_ALLOCATION_RETIRE,
    INSTALLMENT_ALLOCATION_SET,
    INSTALLMENT_DECISION_KINDS,
    INSTALLMENT_PLAN_RETIRE,
    INSTALLMENT_PLAN_SET,
    INSTALLMENT_SETTLEMENT_RETIRE,
    INSTALLMENT_SETTLEMENT_SET,
)
from .projection import apply_installment, resolve_installments
from .validation import validate_installment_payload

__all__ = [
    "INSTALLMENT_DECISION_KINDS",
    "INSTALLMENT_PLAN_SET",
    "INSTALLMENT_PLAN_RETIRE",
    "INSTALLMENT_ALLOCATION_SET",
    "INSTALLMENT_ALLOCATION_RETIRE",
    "INSTALLMENT_SETTLEMENT_SET",
    "INSTALLMENT_SETTLEMENT_RETIRE",
    "apply_installment",
    "resolve_installments",
    "validate_installment_payload",
]
