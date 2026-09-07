"""Credit cards and observed statements (v0.4, docs/releases/v0.4-cards.md).

Slice 1 is read-only: the selected card statement facts
(`card_statement_facts`), the printed source equation and its reconciliation
status (`document_reconciliation_checks`), and per-card / per-pocket statement
coverage. There is no mutable card-balance service — a "recorded liability as
of" view is derived only where a trusted opening value and complete activity
exist, and no timeless "current balance" label is ever produced.

Facilities, limit observations and manual bill allocation are journal kinds and
land in slice 2 (`CARD_FACILITY_SET`, `CARD_LIMIT_OBSERVATION_SET`,
`CARD_PAYMENT_ALLOCATION_SET`).
"""

from .models import (
    CARD_DECISION_KINDS,
    CARD_FACILITY_RETIRE,
    CARD_FACILITY_SET,
    CARD_LIMIT_OBSERVATION_RETIRE,
    CARD_LIMIT_OBSERVATION_SET,
    CARD_PAYMENT_ALLOCATION_RETIRE,
    CARD_PAYMENT_ALLOCATION_SET,
    CARD_SOURCE_TYPES,
    CardStatementFact,
    ReconciliationStatus,
)
from .projection import apply_card, resolve_cards
from .validation import validate_card_payload

# NOTE: `cards.queries` (the read surface) is intentionally not imported here.
# `decisions.py` imports this package during module init, and the queries module
# pulls in `generation` which pulls in `decisions` — a cycle. API and CLI import
# `fincl.cards.queries` directly and lazily instead, exactly as the debts module
# does.

__all__ = [
    "CARD_SOURCE_TYPES",
    "CARD_DECISION_KINDS",
    "CARD_FACILITY_SET",
    "CARD_FACILITY_RETIRE",
    "CARD_LIMIT_OBSERVATION_SET",
    "CARD_LIMIT_OBSERVATION_RETIRE",
    "CARD_PAYMENT_ALLOCATION_SET",
    "CARD_PAYMENT_ALLOCATION_RETIRE",
    "CardStatementFact",
    "ReconciliationStatus",
    "apply_card",
    "resolve_cards",
    "validate_card_payload",
]
