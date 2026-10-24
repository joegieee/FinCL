"""Personal debts and shared expenses (v0.3).

Informal payable/receivable principal tracked as decision-journal kinds. No
bank-loan amortization, compound interest, or payment initiation. Slice 1 covers
debt identity, details, and explicit off-ledger opening balances plus the
separate payable/receivable list and detail reads; source-linked events and
shared-expense residuals land in slice 2, and backdated corrections /
forgiveness / close-reopen in slice 3.
"""

from .models import (
    DEBT_DECISION_KINDS,
    PERSONAL_DEBT_CLOSE,
    PERSONAL_DEBT_DETAILS_SET,
    PERSONAL_DEBT_EVENT_RETIRE,
    PERSONAL_DEBT_EVENT_SET,
    PERSONAL_DEBT_OPEN,
    PERSONAL_DEBT_REOPEN,
)
from .projection import apply_debt, resolve_debts
from .validation import validate_debt_payload

__all__ = [
    "DEBT_DECISION_KINDS",
    "PERSONAL_DEBT_OPEN",
    "PERSONAL_DEBT_DETAILS_SET",
    "PERSONAL_DEBT_EVENT_SET",
    "PERSONAL_DEBT_EVENT_RETIRE",
    "PERSONAL_DEBT_CLOSE",
    "PERSONAL_DEBT_REOPEN",
    "apply_debt",
    "resolve_debts",
    "validate_debt_payload",
]
