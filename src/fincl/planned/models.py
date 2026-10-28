"""Vocabulary for planned flows: money the owner knows is coming or going.

A planned flow is something the ledger cannot discover from history — a laptop
bought next month, a course paid in four instalments, a bonus expected in
March. It is a named list of occurrences, each with its own amount and either
one day or a window ("sometime in March"). Nothing here is evidence that money
moved: an occurrence is settled only by matching it to real transactions or by
the owner saying it is done or did not happen.
"""

from __future__ import annotations

PLANNED_FLOW_SET = "PLANNED_FLOW_SET"
PLANNED_FLOW_RETIRE = "PLANNED_FLOW_RETIRE"
PLANNED_MATCH_SET = "PLANNED_MATCH_SET"
PLANNED_MATCH_RETIRE = "PLANNED_MATCH_RETIRE"
PLANNED_OCCURRENCE_RESOLVE_SET = "PLANNED_OCCURRENCE_RESOLVE_SET"
PLANNED_OCCURRENCE_RESOLVE_RETIRE = "PLANNED_OCCURRENCE_RESOLVE_RETIRE"

PLANNED_DECISION_KINDS = frozenset(
    {
        PLANNED_FLOW_SET,
        PLANNED_FLOW_RETIRE,
        PLANNED_MATCH_SET,
        PLANNED_MATCH_RETIRE,
        PLANNED_OCCURRENCE_RESOLVE_SET,
        PLANNED_OCCURRENCE_RESOLVE_RETIRE,
    }
)

DIRECTION_OUTFLOW = "outflow"
DIRECTION_INFLOW = "inflow"
DIRECTIONS = (DIRECTION_OUTFLOW, DIRECTION_INFLOW)

CERTAINTY_SURE = "sure"
CERTAINTY_EXPECTED = "expected"
CERTAINTY_MAYBE = "maybe"
CERTAINTIES = (CERTAINTY_SURE, CERTAINTY_EXPECTED, CERTAINTY_MAYBE)

# `extra` adds to the usual spending; `irregular_allowance` says this is the
# kind of one-off the irregular allowance already provides for, so it replaces
# that allowance in its month instead of adding to it.
TREATMENT_EXTRA = "extra"
TREATMENT_IRREGULAR = "irregular_allowance"
TREATMENTS = (TREATMENT_EXTRA, TREATMENT_IRREGULAR)

LINK_TRIP = "trip"
LINK_KINDS = (LINK_TRIP,)

OUTCOME_DONE = "done"
OUTCOME_CANCELLED = "cancelled"
OUTCOMES = (OUTCOME_DONE, OUTCOME_CANCELLED)

STATE_UPCOMING = "upcoming"
STATE_PARTLY_MATCHED = "partly_matched"
STATE_MATCHED = "matched"
STATE_OVERDUE = "overdue"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"

MAX_OCCURRENCES = 60

__all__ = [
    "CERTAINTIES",
    "CERTAINTY_EXPECTED",
    "CERTAINTY_MAYBE",
    "CERTAINTY_SURE",
    "DIRECTIONS",
    "DIRECTION_INFLOW",
    "DIRECTION_OUTFLOW",
    "LINK_KINDS",
    "LINK_TRIP",
    "MAX_OCCURRENCES",
    "OUTCOMES",
    "OUTCOME_CANCELLED",
    "OUTCOME_DONE",
    "PLANNED_DECISION_KINDS",
    "PLANNED_FLOW_RETIRE",
    "PLANNED_FLOW_SET",
    "PLANNED_MATCH_RETIRE",
    "PLANNED_MATCH_SET",
    "PLANNED_OCCURRENCE_RESOLVE_RETIRE",
    "PLANNED_OCCURRENCE_RESOLVE_SET",
    "STATE_CANCELLED",
    "STATE_DONE",
    "STATE_MATCHED",
    "STATE_OVERDUE",
    "STATE_PARTLY_MATCHED",
    "STATE_UPCOMING",
    "TREATMENTS",
    "TREATMENT_EXTRA",
    "TREATMENT_IRREGULAR",
]
