"""Cash-flow framework Stage 4A — minimal sinking funds.

A sinking fund is the answer to an expense that is large, real and not monthly:
set money aside for it over the dates that remain, and know at any moment how
much of the cash on hand is already spoken for.

The stage rests on one invariant, stated in :mod:`fincl.funds.models` and
enforced by its arithmetic: total cash, earmarked funds and unallocated cash
are three separate quantities.  Setting money aside does not spend it; paying
the bill does, exactly once.

Like Stage 3B this exposes no API route — the read and the journal kinds are
the deliverable, and presentation is owned by a later stage.

Import shape
------------
The journal side (vocabulary, validation, projection) is imported eagerly:
``fincl.decisions`` needs it at import time to close its kind vocabulary.  The
read side is loaded on first attribute access instead, because it composes the
Stage 1/2/3 adapters, and those reach back to ``fincl.decisions`` — an eager
import here would close that loop.
"""

from typing import Any

from .models import (
    FUND_DECISION_KINDS,
    SINKING_FUND_ALLOCATION_RETIRE,
    SINKING_FUND_ALLOCATION_SET,
    SINKING_FUND_RELEASE_SET,
    SINKING_FUND_SET,
    CashSeparation,
    contribution_plan,
)
from .projection import apply_fund_decision
from .validation import validate_fund_payload

_LAZY = {
    "fund_due_source_id": "fincl.funds.forecast",
    "fund_forecast_events": "fincl.funds.forecast",
    "fund_proposals": "fincl.funds.proposals",
    "sinking_funds_page": "fincl.funds.queries",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)


__all__ = [
    "FUND_DECISION_KINDS",
    "SINKING_FUND_ALLOCATION_RETIRE",
    "SINKING_FUND_ALLOCATION_SET",
    "SINKING_FUND_RELEASE_SET",
    "SINKING_FUND_SET",
    "CashSeparation",
    "apply_fund_decision",
    "contribution_plan",
    "fund_due_source_id",
    "fund_forecast_events",
    "fund_proposals",
    "sinking_funds_page",
    "validate_fund_payload",
]
