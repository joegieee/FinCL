"""The forecast source-adapter contract, restated for this package.

These carry exactly the names and fields of ``fincl.forecast.sources``; the
adapter switches to that module once it is part of this tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..forecast.engine import ForecastEvent


@dataclass(frozen=True)
class SourceContext:
    cutoff: str
    start: str
    end: str
    today: str
    residual_category_ids: frozenset[str]
    existing_source_ids: frozenset[str]


@dataclass(frozen=True)
class SourceResult:
    events: list[ForecastEvent]
    action_items: list[dict[str, Any]]
    generation: str | None
    manifest_source: dict[str, Any] | None
    prospective_card_purchases: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


__all__ = ["SourceContext", "SourceResult"]
