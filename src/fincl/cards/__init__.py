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
    CARD_SOURCE_TYPES,
    CardStatementFact,
    ReconciliationStatus,
)
from .queries import card_detail, card_statements, cards_page

__all__ = [
    "CARD_SOURCE_TYPES",
    "CardStatementFact",
    "ReconciliationStatus",
    "cards_page",
    "card_statements",
    "card_detail",
]
