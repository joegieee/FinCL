"""Card vocabulary: the versioned statement-fact roles and reconciliation states.

Single source of truth shared by the projection and the read queries so a role
can never be folded one way in one place and another way elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

# Account source types this build recognizes as credit-card facilities. A card
# is any account that carries `card_statement_facts` rows; this set is the
# fallback classifier for an account with none yet. A facility's member accounts
# must all be one of these (credit-liability accounts, §2).
CARD_SOURCE_TYPES = frozenset({"MARI_CARD"})

# v0.4 credit-card decision-journal kinds. `CARD_FACILITY_SET` records an
# issuer's explicit shared-limit scope and `CARD_LIMIT_OBSERVATION_SET` a dated
# limit reading against a facility (slice 2); `CARD_PAYMENT_ALLOCATION_SET`
# records an optional operator-entered bill allocation against an observed card
# repayment (slice 4). Each has a RETIRE. A kind is only added here in the same
# change that teaches the projection to replay it (Confirmed 32).
CARD_FACILITY_SET = "CARD_FACILITY_SET"
CARD_FACILITY_RETIRE = "CARD_FACILITY_RETIRE"
CARD_LIMIT_OBSERVATION_SET = "CARD_LIMIT_OBSERVATION_SET"
CARD_LIMIT_OBSERVATION_RETIRE = "CARD_LIMIT_OBSERVATION_RETIRE"
CARD_PAYMENT_ALLOCATION_SET = "CARD_PAYMENT_ALLOCATION_SET"
CARD_PAYMENT_ALLOCATION_RETIRE = "CARD_PAYMENT_ALLOCATION_RETIRE"
# How the operator says a card is repaid, when the ledger cannot show it (a
# revolving balance). `pay_in_full` is the only rule: each statement is paid in
# full by its due date from now on. Anchored to the card account.
CARD_REPAYMENT_PLAN_SET = "CARD_REPAYMENT_PLAN_SET"
CARD_REPAYMENT_PLAN_RETIRE = "CARD_REPAYMENT_PLAN_RETIRE"
REPAYMENT_RULES = ("pay_in_full",)

CARD_DECISION_KINDS = frozenset(
    {
        CARD_FACILITY_SET,
        CARD_FACILITY_RETIRE,
        CARD_LIMIT_OBSERVATION_SET,
        CARD_LIMIT_OBSERVATION_RETIRE,
        CARD_PAYMENT_ALLOCATION_SET,
        CARD_PAYMENT_ALLOCATION_RETIRE,
        CARD_REPAYMENT_PLAN_SET,
        CARD_REPAYMENT_PLAN_RETIRE,
    }
)

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
