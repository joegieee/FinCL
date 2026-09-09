"""Stage 2B — the unified obligations read model.

Debts, installment plans, card statement dues and recurring commitments each
state an obligation with their own evidence. :mod:`fincl.obligations.queries`
merges them behind one shape and stable identities; :mod:`fincl.obligations.rules`
holds the pure decision rules that keep the merge honest.
"""

from .queries import obligations_page
from .rules import (
    baseline_component_link,
    history_confidence,
    payment_state,
    resolve_amount_basis,
)

__all__ = [
    "baseline_component_link",
    "history_confidence",
    "obligations_page",
    "payment_state",
    "resolve_amount_basis",
]
