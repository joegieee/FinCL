"""Installment vocabulary: the decision-journal kinds and status values."""

from __future__ import annotations

INSTALLMENT_PLAN_SET = "INSTALLMENT_PLAN_SET"
INSTALLMENT_PLAN_RETIRE = "INSTALLMENT_PLAN_RETIRE"
INSTALLMENT_ALLOCATION_SET = "INSTALLMENT_ALLOCATION_SET"
INSTALLMENT_ALLOCATION_RETIRE = "INSTALLMENT_ALLOCATION_RETIRE"
INSTALLMENT_SETTLEMENT_SET = "INSTALLMENT_SETTLEMENT_SET"
INSTALLMENT_SETTLEMENT_RETIRE = "INSTALLMENT_SETTLEMENT_RETIRE"

INSTALLMENT_DECISION_KINDS = frozenset(
    {
        INSTALLMENT_PLAN_SET,
        INSTALLMENT_PLAN_RETIRE,
        INSTALLMENT_ALLOCATION_SET,
        INSTALLMENT_ALLOCATION_RETIRE,
        INSTALLMENT_SETTLEMENT_SET,
        INSTALLMENT_SETTLEMENT_RETIRE,
    }
)

# A plan is PROPOSED until an operator confirms the chain, CONFIRMED once the
# occurrences and (known) facts are accepted, and SETTLED once an early
# settlement closes it. Similar monthly amounts alone never reach CONFIRMED.
PLAN_STATUSES = frozenset({"PROPOSED", "CONFIRMED", "SETTLED"})

# The two explicit alternative lenses on an installment's cost (§1, §3).
LENS_RECOGNIZED_PURCHASE_COST = "recognized_purchase_cost"
LENS_SPREAD_ALLOCATION = "spread_allocation"
