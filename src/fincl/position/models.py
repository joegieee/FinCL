"""Position vocabulary: the two journalled balance-observation kinds.

Single source of truth shared by ``validation`` and ``projection`` so a payload
can never be accepted by one and rejected by the other.
"""

from __future__ import annotations

BALANCE_OBSERVATION_SET = "BALANCE_OBSERVATION_SET"
BALANCE_OBSERVATION_RETIRE = "BALANCE_OBSERVATION_RETIRE"

POSITION_DECISION_KINDS = frozenset(
    {
        BALANCE_OBSERVATION_SET,
        BALANCE_OBSERVATION_RETIRE,
    }
)

# Deposit / savings / wallet pockets count toward liquid cash; a credit-card
# pocket does not (its outstanding balance is a future obligation, not cash on
# hand). A pocket whose manual or statement basis is older than this many days
# is still returned — a three-week-old statement balance is the best evidence
# available and withholding it would leave the operator with nothing — but it is
# disclosed as stale.
STALE_AFTER_DAYS = 14
