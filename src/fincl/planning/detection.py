"""Stage 2B — source-backed recurring candidate inference for bills and income.

Detection reads selected entries only and proposes candidates for *both*
variable bills and income streams.  Everything a candidate carries — expected
amount, tolerance, cadence, timing window — is derived from its own sources, so
confirming one is a semantic review ("yes, this payer is my salary") and never
an amount-entry form.

Two rules are load-bearing here:

* a candidate inferred from history is never ``confirmed``.  Income in
  particular tops out at ``likely``: past receipts do not evidence a future one.
  Only a future receipt or a recorded commitment can raise it, and neither is
  something detection can manufacture.
* loan movements, owned transfers and refunds are removed from income grouping
  *before* any statistic is computed — never netted off afterwards, which would
  leave a wrong stream amount standing.

An amount that moves month to month no longer disqualifies a bill: a variable
utility bill is exactly what Stage 2B has to surface.  What still disqualifies a
group is the absence of timing evidence — no recognisable cadence means there is
nothing to schedule, and inventing one would be a guess.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ..queries import _ENTRY_VIEW

DETECTOR_VERSION = "detv1"

# Three dated sources is the smallest group that yields two gaps, i.e. the
# smallest group whose cadence is observed rather than assumed.
_MIN_SOURCES = 3
# A group whose extremes sit inside this ratio is a stable amount; anything
# wider is a variable bill, which is surfaced with its observed spread rather
# than dropped.
_STABLE_RATIO = Decimal("1.1")
_CENT = Decimal("0.01")


def _decimal(coefficient: int, scale: int) -> Decimal:
    return abs(Decimal(coefficient).scaleb(-scale))


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _cadence(gaps: list[int]) -> str | None:
    """The cadence the last three observed gaps agree on, or None."""
    recent = gaps[-3:]
    if not recent:
        return None
    if all(25 <= gap <= 35 for gap in recent):
        return "monthly"
    if all(6 <= gap <= 8 for gap in recent):
        return "weekly"
    return None


def _identity(kind: str, merchant: str, currency: str, accounts: list[str], cadence: str) -> str:
    """Stable candidate key.

    The bill form is byte-identical to the pre-Stage-2B detector so dismissals
    already recorded against a bill candidate keep matching it.  Income carries
    its kind in the payload and therefore hashes to its own identity.
    """
    parts: list[Any] = [DETECTOR_VERSION]
    if kind != "bill":
        parts.append(kind)
    parts.extend([merchant, currency, accounts, cadence])
    raw = json.dumps(parts, separators=(",", ":"))
    return f"{DETECTOR_VERSION}:" + hashlib.sha256(raw.encode()).hexdigest()


def _excluded_income_anchors(connection: sqlite3.Connection) -> set[str]:
    """Anchors that must never be grouped as an income stream.

    Loan movements (a debt allocation names the entry) are borrowing or its
    repayment, not earnings.  Owned transfers and refunds never reach this read:
    the entry view buckets a confirmed transfer as ``internal`` and a refund as
    ``refunds``, so neither is in the ``income`` bucket to begin with.
    """
    return {
        str(row["entry_anchor"])
        for row in connection.execute(
            "SELECT DISTINCT entry_anchor FROM personal_debt_allocations"
        )
    }


def _variation(amounts: list[Decimal]) -> dict[str, Any]:
    low, high = min(amounts), max(amounts)
    spread = high - low
    stable = high <= low * _STABLE_RATIO
    relative = (spread / high).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if high else Decimal(0)
    total = sum(amounts, Decimal(0))
    return {
        "basis": "stable" if stable else "variable",
        "min_amount": _money(low),
        "max_amount": _money(high),
        "spread": _money(spread),
        "relative_spread": str(relative),
        "mean_amount": _money(total / Decimal(len(amounts))),
        "latest_amount": _money(amounts[-1]),
    }


def _timing_window(days: list[date], gaps: list[int], cadence: str) -> dict[str, Any]:
    last = days[-1]
    low, high = min(gaps), max(gaps)
    window: dict[str, Any] = {
        "cadence": cadence,
        "first_observed_on": days[0].isoformat(),
        "last_observed_on": last.isoformat(),
        "min_gap_days": low,
        "max_gap_days": high,
        "next_expected_between": [
            (last + timedelta(days=low)).isoformat(),
            (last + timedelta(days=high)).isoformat(),
        ],
    }
    if cadence == "monthly":
        window["day_of_month_min"] = min(day.day for day in days)
        window["day_of_month_max"] = max(day.day for day in days)
    return window


def candidates(
    connection: sqlite3.Connection, *, dismissed: frozenset[str] | set[str] = frozenset()
) -> dict[str, Any]:
    """Inferred bill and income candidates, keyed by kind.

    Returns ``{"bill": [...], "income": [...], "income_exclusions": {...}}``.
    Both lists are ordered by merchant then currency so a page renders
    deterministically.
    """
    rows = connection.execute(
        f"SELECT natural_anchor, merchant, currency_pocket, account_id, bucket, "
        f"substr(effective_at,1,10) day, amount_coefficient, amount_scale "
        f"FROM ({_ENTRY_VIEW}) WHERE eligible AND bucket IN ('spending', 'income') "
        f"AND effective_precision != 'MONTH' "
        f"ORDER BY merchant, currency_pocket, account_id, effective_at"
    ).fetchall()
    blocked = _excluded_income_anchors(connection)

    groups: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
    excluded_income_sources = 0
    for row in rows:
        kind = "bill" if row["bucket"] == "spending" else "income"
        if kind == "income" and str(row["natural_anchor"]) in blocked:
            excluded_income_sources += 1
            continue
        groups[(kind, row["merchant"], row["currency_pocket"], row["account_id"])].append(row)

    found: dict[str, list[dict[str, Any]]] = {"bill": [], "income": []}
    for (kind, merchant, currency, account), items in sorted(groups.items()):
        if len(items) < _MIN_SOURCES:
            continue
        days = [date.fromisoformat(item["day"]) for item in items]
        gaps = [(later - earlier).days for earlier, later in zip(days, days[1:], strict=False)]
        cadence = _cadence(gaps)
        if cadence is None:
            continue
        amounts = [
            _decimal(int(item["amount_coefficient"]), int(item["amount_scale"]))
            for item in items
        ]
        key = _identity(kind, merchant, currency, [account], cadence)
        if key in dismissed:
            continue
        variation = _variation(amounts)
        timing = _timing_window(days, gaps, cadence)
        # A variable bill still needs an amount to schedule against.  The latest
        # receipt is the observation, and the observed spread is the tolerance —
        # both source-backed, so review is about identity, not arithmetic.
        expected = variation["latest_amount"]
        tolerance = variation["spread"]
        found[kind].append(
            {
                "candidate": key,
                "detector_version": DETECTOR_VERSION,
                "kind": kind,
                "merchant_key": merchant,
                "currency": currency,
                "account_ids": [account],
                "cadence": cadence,
                "count": len(items),
                "source_count": len(items),
                "last_receipt_on": timing["last_observed_on"],
                "last_observed_on": timing["last_observed_on"],
                "first_observed_on": timing["first_observed_on"],
                "amount_variation": variation,
                "timing_window": timing,
                "expected_amount": expected,
                "suggested_expected_amount": expected,
                "suggested_tolerance": tolerance,
                "amount_tolerance": tolerance,
                "confidence": "likely",
                "confidence_reason": (
                    "grouped from recorded receipts only; a future receipt or a "
                    "recorded commitment is what raises this to confirmed"
                    if kind == "income"
                    else "grouped from recorded payments only; confirmation is a "
                    "semantic review of the identity, not of the amount"
                ),
                "auto_confirmable": False,
                "review_question": (
                    f"Is {merchant} a recurring {'income stream' if kind == 'income' else 'bill'}?"
                ),
                "seed_entry_anchors": [item["natural_anchor"] for item in items[-4:]],
            }
        )
    result: dict[str, Any] = dict(found)
    result["income_exclusions"] = {
        "excluded_source_count": excluded_income_sources,
        "rule": (
            "loan movements, owned transfers and refunds are removed before "
            "grouping; they are never netted out of a stream afterwards"
        ),
    }
    return result


__all__ = ["DETECTOR_VERSION", "candidates"]
