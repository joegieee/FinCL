"""Forecast framework (cash-flow framework Stage 3).

Stage 3A is the readiness gate and the input manifest: one read that gathers the
Stage 1 baseline / role outputs and the Stage 2 cash / obligation adapters,
pins them to a single ledger generation, and classifies — per forecast output,
never globally — whether the evidence supports an established-input estimate, a
limited-history estimate, or nothing at all.

The cash timeline, the scenarios and the forecast arithmetic are a later task
and deliberately live nowhere in this package yet.
"""

from .manifest import (
    FORECAST_READINESS_LEVELS,
    READINESS_ESTABLISHED,
    READINESS_LIMITED,
    READINESS_UNAVAILABLE,
    forecast_input_manifest,
)

__all__ = [
    "FORECAST_READINESS_LEVELS",
    "READINESS_ESTABLISHED",
    "READINESS_LIMITED",
    "READINESS_UNAVAILABLE",
    "forecast_input_manifest",
]
