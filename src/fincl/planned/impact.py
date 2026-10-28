"""What a draft planned item would do to the answer, before it is saved.

The draft is read in place of the recorded item (``draft_overlay``) while the
real sufficiency answer and cash timeline are computed once without it and once
with it, so the preview cannot differ from what saving produces.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..db import read_snapshot
from ..errors import FinCLError
from ..forecast.engine import cash_timeline_from
from ..generation import current_generation
from ..planning.sufficiency import sufficiency_page_from
from .models import PLANNED_FLOW_SET
from .queries import draft_overlay, money
from .validation import validate_planned

_CASES = (("base", "base"), ("cautious", "conservative"))


def _answer(page: dict[str, Any]) -> dict[str, Any]:
    combined = page.get("combined_answer") or {}
    return {
        "answer": combined.get("answer"),
        "complete": combined.get("complete", False),
        "reporting_currency": combined.get("reporting_currency"),
        **{
            case: {
                key: (combined.get(case) or {}).get(key)
                for key in ("verdict", "lowest", "lowest_on", "first_short_on", "shortfall")
            }
            for case, _ in _CASES
        },
    }


def _closings(timeline: dict[str, Any], currency: str, case: str) -> dict[str, Decimal]:
    months = ((timeline.get("by_currency") or {}).get(currency) or {}).get(case, {}).get("months") or []
    return {str(month["month"]): Decimal(str(month["closing"])) for month in months}


def planned_impact(
    database: Path, *, flow: str | None, payload: dict[str, Any], today: date,
) -> dict[str, Any]:
    """Before and after for one draft ``PLANNED_FLOW_SET`` payload (its reason may be omitted)."""
    flow_id = flow or str(uuid.uuid4())
    draft = {**payload, "reason": payload.get("reason") or "preview"}
    validate_planned(PLANNED_FLOW_SET, {"flow": flow_id}, draft)
    with read_snapshot(database) as connection:
        generation = current_generation(connection)
        if generation is None:
            raise FinCLError("Ledger is not ready. Run fincl refresh first.")
        before_page = sufficiency_page_from(connection, today=today)
        before_timeline = cash_timeline_from(connection, today=today)
        with draft_overlay(flow_id, draft):
            after_page = sufficiency_page_from(connection, today=today)
            after_timeline = cash_timeline_from(connection, today=today)
    before, after = _answer(before_page), _answer(after_page)
    currency = str(draft["currency"])
    months: list[dict[str, Any]] = []
    changes: dict[str, dict[str, tuple[Decimal | None, Decimal | None]]] = {}
    for case, key in _CASES:
        was, now = _closings(before_timeline, currency, key), _closings(after_timeline, currency, key)
        for month in sorted(set(was) | set(now)):
            changes.setdefault(month, {})[case] = (was.get(month), now.get(month))
    for month, cases in changes.items():
        cells = {
            case: {
                "before": money(was) if was is not None else None,
                "after": money(now) if now is not None else None,
                "change": money(now - was) if was is not None and now is not None else None,
            }
            for case, (was, now) in cases.items()
        }
        if any(cell["change"] != "0.00" for cell in cells.values()):
            months.append({"month": month, "currency": currency, **cells})
    return {
        "generation": str(generation["generation_token"]),
        "flow": flow_id,
        "currency": currency,
        "before": before,
        "after": after,
        "flips": before["answer"] != after["answer"],
        "months": months,
    }


__all__ = ["planned_impact"]
