"""Card vocabulary: the versioned statement-fact roles and reconciliation states.

Single source of truth shared by the projection and the read queries so a role
can never be folded one way in one place and another way elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

# Account source types this build recognizes as credit-card facilities. A card
# is any account that carries `card_statement_facts` rows; this set is the
# fallback classifier for an account with none yet.
CARD_SOURCE_TYPES = frozenset({"MARI_CARD"})

# Field roles projected into `card_statement_facts`. Money unless noted.
ROLE_STATEMENT_DATE = "statement_date"  # date
ROLE_PAYMENT_DUE_DATE = "payment_due_date"  # date
ROLE_PERIOD_START = "statement_period_start"  # date
ROLE_PERIOD_END = "statement_period_end"  # date
ROLE_CREDIT_LIMIT = "credit_limit"
ROLE_PAYMENT_DUE = "payment_due"
ROLE_MINIMUM_PAYMENT = "minimum_payment"
ROLE_CLOSING_LIABILITY = "closing_liability"
ROLE_OPENING_LIABILITY = "opening_liability"

DATE_ROLES = frozenset(
    {ROLE_STATEMENT_DATE, ROLE_PAYMENT_DUE_DATE, ROLE_PERIOD_START, ROLE_PERIOD_END}
)


@dataclass(frozen=True, slots=True)
class CardStatementFact:
    """One projected `card_statement_facts` row."""

    field_role: str
    currency_pocket: str
    value_kind: str  # 'money' | 'date'
    value_text: str
    value_coefficient: int | None
    value_scale: int | None
    source_locator: str
    parser_version: str
    role_version: str

    @property
    def is_money(self) -> bool:
        return self.value_kind == "money"


class ReconciliationStatus:
    """The reconciliation state of one observed statement pocket.

    Not an enum so the string values are stable across the wire without a
    serializer. `RECONCILED` means every blocking source equation for the pocket
    passed; `FAILED` means at least one did not; `UNVERIFIED` means the printed
    equation was recognized but no blocking check was recorded; `NO_EQUATION`
    means the parser could not identify the printed formula at all.
    """

    RECONCILED = "reconciled"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    NO_EQUATION = "no_equation"
