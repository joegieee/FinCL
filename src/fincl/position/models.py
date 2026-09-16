"""Position vocabulary: the journalled balance-observation and
movement-coverage-certification kinds.

Single source of truth shared by ``validation`` and ``projection`` so a payload
can never be accepted by one and rejected by the other.
"""

from __future__ import annotations

BALANCE_OBSERVATION_SET = "BALANCE_OBSERVATION_SET"
BALANCE_OBSERVATION_RETIRE = "BALANCE_OBSERVATION_RETIRE"

# An operator attestation that one pocket's movement evidence is complete across
# a closed date range -- "my bank app shows no transactions between these two
# statements". It certifies *completeness*, never an amount, so it can unblock a
# cash bridge over a quiet interval without ever supplying a balance the ledger
# did not observe. `MOVEMENT_COVERAGE_BASES` names where that attestation came
# from; the basis is recorded and surfaced so a reader can weigh it.
MOVEMENT_COVERAGE_CERTIFY = "MOVEMENT_COVERAGE_CERTIFY"
MOVEMENT_COVERAGE_RETIRE = "MOVEMENT_COVERAGE_RETIRE"

MOVEMENT_COVERAGE_BASES = (
    "account-app-transaction-list",
    "statement-inspected-no-activity",
    "account-closed-or-dormant",
)

POSITION_DECISION_KINDS = frozenset(
    {
        BALANCE_OBSERVATION_SET,
        BALANCE_OBSERVATION_RETIRE,
        MOVEMENT_COVERAGE_CERTIFY,
        MOVEMENT_COVERAGE_RETIRE,
    }
)

# Deposit / savings / wallet pockets count toward liquid cash; a credit-card
# pocket does not (its outstanding balance is a future obligation, not cash on
# hand). A pocket whose manual or statement basis is older than this many days
# is still returned — a three-week-old statement balance is the best evidence
# available and withholding it would leave the operator with nothing — but it is
# disclosed as stale.
STALE_AFTER_DAYS = 14
