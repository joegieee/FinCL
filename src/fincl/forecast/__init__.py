"""Forecast framework (cash-flow framework Stage 3).

Stage 3A is the readiness gate and the input manifest: one read that gathers the
Stage 1 baseline / role outputs and the Stage 2 cash / obligation adapters,
pins them to a single ledger generation, and classifies — per forecast output,
never globally — whether the evidence supports an established-input estimate, a
limited-history estimate, or nothing at all.

Stage 3B is the pure exact-money six-month cash timeline and its manifest-gated
base and conservative scenario construction.  It deliberately exposes no API
route; presentation is owned by the later UI stage.

Stage 3C is the minimal temporal-holdout validation harness.  It reports direct
errors and observed cash misses without turning backtest improvement into a
release gate.
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
from .validation import (
    CashBridge,
    MonthlyObservation,
    ReviewPolicyVersion,
    forecast_validation,
    temporal_holdout_validation,
)

__all__ = [
    "FORECAST_READINESS_LEVELS",
    "CashBridge",
    "ForecastEvent",
    "READINESS_ESTABLISHED",
    "READINESS_LIMITED",
    "READINESS_UNAVAILABLE",
    "ReviewPolicyVersion",
    "ResidualCategory",
    "TimelineEvent",
    "MonthlyObservation",
    "build_cash_timeline",
    "cash_timeline",
    "forecast_cash_timeline",
    "forecast_input_manifest",
    "forecast_validation",
    "run_timeline",
    "temporal_holdout_validation",
]
