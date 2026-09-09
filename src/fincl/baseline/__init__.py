"""Baseline discovery (cash-flow framework Stage 1).

The baseline answers "what does my normal life cost?" from imported actuals —
never from a typed estimate. Stage 1A (:mod:`fincl.baseline.readiness`) is the
gate in front of it: an account/pocket-by-month view of what evidence exists,
what is only observed, and what is missing, so a fabricated zero month can never
slip into a baseline total.
"""

from .queries import cost_income_table
from .readiness import baseline_readiness

__all__ = ["baseline_readiness", "cost_income_table"]
