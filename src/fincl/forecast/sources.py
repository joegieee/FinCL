"""Forecast source adapters beyond the built-in obligation, income and card reads.

An adapter turns one kind of recorded intent (a sinking fund's due expense, a
planned flow) into sourced forecast events. Every adapter is called from inside
the cash timeline itself, so the Forecast tab, the sufficiency answer, Controls,
money flow and variance snapshots all see the same events; none of them can
add a source the others miss.

An adapter reads from the request's connection and returns everything the
timeline needs to keep its own guarantees: its generation joins the
mixed-generation guard, its manifest source is listed beside the others, and
its action items become to-dos exactly like the manifest's own.

Adapters import :class:`SourceResult` inside the adapter function (or under
``TYPE_CHECKING``): this module imports the adapters to register them, so a
module-level import the other way would be circular.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import ForecastEvent


@dataclass(frozen=True)
class SourceContext:
    """What an adapter may rely on, resolved once by the timeline."""

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
    # A gap the user cannot fix in the UI. It is shown beside the answer and
    # makes the timeline partial, but never hides a usable currency.
    limitations: list[str] = field(default_factory=list)


SourceAdapter = Callable[[sqlite3.Connection, SourceContext], SourceResult]

from ..funds.forecast import funds_source  # noqa: E402

EXTRA_SOURCES: list[SourceAdapter] = [funds_source]

__all__ = ["EXTRA_SOURCES", "SourceAdapter", "SourceContext", "SourceResult"]
