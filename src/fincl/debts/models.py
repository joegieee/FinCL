"""Debt vocabulary: journal kinds, directions, and the event-kind tables.

Single source of truth for which ``event_kind`` a debt of a given direction may
carry and which way it moves principal (DESIGN-debts-and-context.md §1). The
projection and validation both import from here so a kind can never be accepted
by one and rejected by the other.
"""

from __future__ import annotations

PERSONAL_DEBT_OPEN = "PERSONAL_DEBT_OPEN"
PERSONAL_DEBT_DETAILS_SET = "PERSONAL_DEBT_DETAILS_SET"
PERSONAL_DEBT_EVENT_SET = "PERSONAL_DEBT_EVENT_SET"
PERSONAL_DEBT_EVENT_RETIRE = "PERSONAL_DEBT_EVENT_RETIRE"
PERSONAL_DEBT_CLOSE = "PERSONAL_DEBT_CLOSE"
PERSONAL_DEBT_REOPEN = "PERSONAL_DEBT_REOPEN"
ENTRY_RESIDUAL_SET = "ENTRY_RESIDUAL_SET"
ENTRY_RESIDUAL_RETIRE = "ENTRY_RESIDUAL_RETIRE"
INBOUND_CLASSIFICATION_RETIRE = "INBOUND_CLASSIFICATION_RETIRE"

DEBT_DECISION_KINDS = frozenset(
    {
        PERSONAL_DEBT_OPEN,
        PERSONAL_DEBT_DETAILS_SET,
        PERSONAL_DEBT_EVENT_SET,
        PERSONAL_DEBT_EVENT_RETIRE,
        PERSONAL_DEBT_CLOSE,
        PERSONAL_DEBT_REOPEN,
        ENTRY_RESIDUAL_SET,
        ENTRY_RESIDUAL_RETIRE,
        INBOUND_CLASSIFICATION_RETIRE,
    }
)

DIRECTIONS = frozenset({"PAYABLE", "RECEIVABLE"})

# event_kind -> signed principal move (+1 increases the obligation, -1 reduces).
PAYABLE_EVENTS: dict[str, int] = {
    "BORROWED": +1,
    "PRINCIPAL_REPAID": -1,
    "EXPENSE_PAID_FOR_ME": +1,
    "PAYABLE_FORGIVEN": -1,
}
RECEIVABLE_EVENTS: dict[str, int] = {
    "LENT": +1,
    "PRINCIPAL_RETURNED": -1,
    "EXPENSE_PAID_FOR_OTHER": +1,
    "RECEIVABLE_WRITTEN_OFF": -1,
}

# Events whose principal is not backed by a resolved bank movement: they carry an
# empty ``entry_allocations`` list and are the only place an off-ledger amount
# enters a debt (DESIGN-debts-and-context.md §1, §3).
#
# ``EXPENSE_PAID_FOR_OTHER`` is *not* here: someone-else's-bill-I-paid is funded
# by my own bank outflow, so it carries source allocations against that entry and
# the owner's share is recorded once through an ``ENTRY_RESIDUAL_SET`` (§3, the
# "shared restaurant bill 120, owner's portion 40" example).
OFF_LEDGER_EVENTS = frozenset(
    {
        "EXPENSE_PAID_FOR_ME",
        "PAYABLE_FORGIVEN",
        "RECEIVABLE_WRITTEN_OFF",
    }
)

# Exceptional off-ledger reductions (forgiveness / write-off). Distinct actions
# with their own analytical disclosure; they land in v0.3 slice 3.
EXCEPTIONAL_EVENTS = frozenset({"PAYABLE_FORGIVEN", "RECEIVABLE_WRITTEN_OFF"})

# The two kinds that may additionally carry ``opening_balance: true`` to record a
# pre-existing obligation with no observed funding.
OPENING_EVENTS = frozenset({"BORROWED", "LENT"})

# EXPENSE_PAID_FOR_ME requires an ``expense_category`` (it creates a synthetic
# owner expense, §3); no other kind accepts one.
EXPENSE_CATEGORY_EVENTS = frozenset({"EXPENSE_PAID_FOR_ME"})


def events_for_direction(direction: str) -> dict[str, int]:
    return PAYABLE_EVENTS if direction == "PAYABLE" else RECEIVABLE_EVENTS
