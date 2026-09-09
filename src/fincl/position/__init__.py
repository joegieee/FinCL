"""Liquid position (cash-flow framework Stage 1).

A statement-derived as-of cash balance per ``(account_id, currency_pocket)``,
with a newer dated manual observation allowed to supersede it. The statement
half needs no new extraction — ``ledger_entries`` already stores exact per-entry
running balances — so this module is a query plus one small journalled kind for
the manual override.

Nothing here is a mutable authoritative balance: every figure is resolved on
read from the exact evidence (a printed closing balance) or a journalled
operator observation, and each carries its basis, as-of date and staleness.
"""

from .models import (
    BALANCE_OBSERVATION_RETIRE,
    BALANCE_OBSERVATION_SET,
    POSITION_DECISION_KINDS,
)
from .projection import apply_balance_observation
from .queries import position_page
from .validation import validate_position_payload

__all__ = [
    "BALANCE_OBSERVATION_SET",
    "BALANCE_OBSERVATION_RETIRE",
    "POSITION_DECISION_KINDS",
    "apply_balance_observation",
    "position_page",
    "validate_position_payload",
]
