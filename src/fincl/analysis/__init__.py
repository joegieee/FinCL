"""Shared analytical projection (v0.2 release, slice 1).

`components.py` owns the one eligibility + bucket classification used by both the
economic-flow report and the dashboard/contributor read surface, so a total and
the rows behind it can never be computed by two disagreeing predicates. Later
v0.2 slices add `scopes.py`, `queries.py` and `allocations.py` beside it as those
responsibilities land.
"""
