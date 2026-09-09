"""Cash-flow framework Stage 5A — forecast versus actual.

A forecast that can be recomputed is not a forecast that was *issued*.  Asking
whether the plan was right is only meaningful against the numbers the plan
actually stated, so this stage does two things and keeps them apart:

* it saves an **immutable, explicitly requested** snapshot of an issued forecast
  — its inputs, event identities, scenario settings, basis dates, reserve,
  currency, horizon and the outputs that were shown — as a hash-chained journal
  record.  Recalculation has no write path to it and re-issuing an id is
  refused; and
* it compares that snapshot against **actual bank cash**, not economic purchase
  recognition, through matches the operator records by hand.

The distinctions the stage exists to preserve: a delayed receipt is not a
missing one, a card purchase is not the bank payment that later settles it, and
a period nobody has evidence for is not a period in which nothing happened.

Like Stages 3B and 4 this exposes no API route — the read and the journal kinds
are the deliverable, and presentation is owned by a later stage.

Import shape
------------
The journal side (vocabulary, validation, projection) is imported eagerly:
``fincl.decisions`` needs it at import time to close its kind vocabulary.  The
read side is loaded on first attribute access instead, because it composes the
Stage 3 forecast engine and the coverage contract, and those reach back to
``fincl.decisions`` — an eager import here would close that loop.
"""

from typing import Any

from .models import (
    FORECAST_MATCH_RETIRE,
    FORECAST_MATCH_SET,
    FORECAST_SNAPSHOT_ISSUE,
    VARIANCE_DECISION_KINDS,
    realization,
    snapshot_digest,
    variance_reading,
)
from .projection import apply_variance_decision
from .validation import validate_snapshot_document, validate_variance_payload

_LAZY = {
    "build_snapshot_document": "fincl.variance.snapshots",
    "cash_movements": "fincl.variance.actuals",
    "cash_movements_page": "fincl.variance.actuals",
    "forecast_snapshots_page": "fincl.variance.snapshots",
    "forecast_variance": "fincl.variance.queries",
    "forecast_variance_page": "fincl.variance.queries",
    "movement_id": "fincl.variance.actuals",
    "snapshot_index": "fincl.variance.snapshots",
    "snapshot_payload": "fincl.variance.snapshots",
    "stored_snapshot": "fincl.variance.snapshots",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)


__all__ = [
    "FORECAST_MATCH_RETIRE",
    "FORECAST_MATCH_SET",
    "FORECAST_SNAPSHOT_ISSUE",
    "VARIANCE_DECISION_KINDS",
    "apply_variance_decision",
    "build_snapshot_document",
    "cash_movements",
    "cash_movements_page",
    "forecast_snapshots_page",
    "forecast_variance",
    "forecast_variance_page",
    "movement_id",
    "realization",
    "snapshot_digest",
    "snapshot_index",
    "snapshot_payload",
    "stored_snapshot",
    "validate_snapshot_document",
    "validate_variance_payload",
    "variance_reading",
]
