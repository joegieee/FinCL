"""Forecast framework (cash-flow framework Stage 3).

Stage 3A is the readiness gate and the input manifest: one read that gathers the
Stage 1 baseline / role outputs and the Stage 2 cash / obligation adapters,
pins them to a single ledger generation, and classifies — per forecast output,
never globally — whether the evidence supports an established-input estimate, a
limited-history estimate, or nothing at all.

Stage 3B is the pure exact-money six-month cash timeline and its manifest-gated
base and conservative scenario construction.  It deliberately exposes no API
route; presentation is owned by the later UI stage.
"""

from .engine import (
    ForecastEvent,
    ResidualCategory,
    TimelineEvent,
    build_cash_timeline,
    cash_timeline,
    forecast_cash_timeline,
    run_timeline,
)
from .manifest import (
    FORECAST_READINESS_LEVELS,
    READINESS_ESTABLISHED,
    READINESS_LIMITED,
    READINESS_UNAVAILABLE,
    forecast_input_manifest,
)

__all__ = [
    "FORECAST_READINESS_LEVELS",
    "ForecastEvent",
    "READINESS_ESTABLISHED",
    "READINESS_LIMITED",
    "READINESS_UNAVAILABLE",
    "ResidualCategory",
    "TimelineEvent",
    "build_cash_timeline",
    "cash_timeline",
    "forecast_cash_timeline",
    "forecast_input_manifest",
    "run_timeline",
]
