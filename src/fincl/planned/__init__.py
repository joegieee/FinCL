"""Planned flows: future spending and income the owner knows about.

The journal side (vocabulary, validation, projection) is imported eagerly
because ``fincl.decisions`` needs it to close its kind vocabulary. The read
side composes the position and forecast adapters, which reach back to
``fincl.decisions``, so it loads on first attribute access.
"""

from typing import Any

from .models import PLANNED_DECISION_KINDS
from .projection import apply_planned, check_planned_sequence
from .validation import validate_planned

_LAZY = {
    "planned_page": "fincl.planned.queries",
    "planned_page_from": "fincl.planned.queries",
    "match_candidates": "fincl.planned.queries",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)


__all__ = [
    "PLANNED_DECISION_KINDS",
    "apply_planned",
    "check_planned_sequence",
    "match_candidates",
    "planned_page",
    "planned_page_from",
    "validate_planned",
]
