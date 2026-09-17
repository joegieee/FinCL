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
from .schedules import CADENCE_SCHEDULES

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


# Each cadence is an inclusive day-gap band around its nominal period, wide
# enough for weekend shifts and month lengths but narrow enough not to overlap a
# neighbour. Ordered shortest first so the first band a gap falls in is the
# tightest one that fits.
_CADENCE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("weekly", 6, 8),
    ("fortnightly", 13, 16),
    ("four-weekly", 27, 29),
    ("monthly", 30, 35),
    ("quarterly", 85, 95),
    ("semiannual", 178, 190),
    ("annual", 355, 380),
)

# How many observed gaps may disagree with the winning cadence before the group
# has no cadence at all. One tolerated outlier is what keeps a single late or
# early payment — a bill that slipped over a public holiday, a salary paid early
# before a break — from erasing an otherwise obvious schedule. Two disagreements
# are not a schedule with a hiccup; they are the absence of one.
_CADENCE_OUTLIER_ALLOWANCE = 1

# Gaps considered when classifying. A longer memory than the old three lets an
# annual or quarterly bill be recognised at all, while the outlier allowance
# keeps a long history from being derailed by one odd interval.
_CADENCE_WINDOW = 6


def _band_of(gap: int) -> str | None:
    for name, low, high in _CADENCE_BANDS:
        if low <= gap <= high:
            return name
    return None


def _cadence(gaps: list[int]) -> str | None:
    """The cadence the recent observed gaps agree on, or None.

    "Agree" means: most recent gaps fall in one band, and at most
    ``_CADENCE_OUTLIER_ALLOWANCE`` of them do not. The winner is the band with
    the most gaps; a tie is no cadence, because two equally-supported readings
    of the same history is exactly the case where picking one would be a guess.

    Note that four-weekly (27-29 days) and monthly (30-35) are deliberately
    disjoint bands rather than one wide one. They drift apart by nearly two
    weeks over a year, so a forecast that models a four-weekly subscription as
    monthly loses a payment; keeping them apart costs only that a group split
    across the 29/30 boundary reads as having no cadence, which is the honest
    answer for a group that genuinely does both.
    """
    recent = gaps[-_CADENCE_WINDOW:]
    if not recent:
        return None
    counts: dict[str, int] = {}
    for gap in recent:
        band = _band_of(gap)
        if band is not None:
            counts[band] = counts.get(band, 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    winners = [name for name, count in counts.items() if count == best]
    if len(winners) != 1:
        return None
    if len(recent) - best > _CADENCE_OUTLIER_ALLOWANCE:
        return None
    return winners[0]


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


def _drift(amounts: list[Decimal]) -> dict[str, Any]:
    """Whether a variable amount is trending, and by how much per occurrence.

    A utility bill that has climbed every quarter is not the same forecasting
    problem as one that bounces around a stable mean, and the latest receipt
    alone cannot tell them apart. The slope here is deliberately the simplest
    thing that can be checked by hand — total change divided by the number of
    intervals — and it is reported as an observation, not applied: the expected
    amount stays the latest observed receipt, and a caller that wants to project
    the trend has to opt into doing so with the figure in view.

    ``direction`` is ``flat`` unless the total change exceeds a tenth of the
    mean, so ordinary noise is not dressed up as a trend.
    """
    if len(amounts) < 3:
        return {"available": False, "reason": "a trend needs at least three observations"}
    change = amounts[-1] - amounts[0]
    intervals = len(amounts) - 1
    mean = sum(amounts, Decimal(0)) / Decimal(len(amounts))
    threshold = mean / Decimal(10)
    if abs(change) <= threshold:
        direction = "flat"
    else:
        direction = "rising" if change > 0 else "falling"
    return {
        "available": True,
        "direction": direction,
        "total_change": _money(change),
        "per_occurrence": _money(change / Decimal(intervals)),
        "observations": len(amounts),
        "basis": "first to latest observed amount, divided by the intervals between them",
        "applied": False,
        "note": (
            "the expected amount remains the latest observed receipt; this trend "
            "is reported for review and is never projected automatically"
        ),
    }


def _suggested_schedule(cadence: str, days: list[date]) -> dict[str, Any] | None:
    """The schedule payload that would reproduce this observed cadence.

    Anchored on the latest observed occurrence, so the phase of the cycle comes
    from the evidence rather than from a default. Returned as a suggestion only:
    confirming a commitment is still an explicit decision.
    """
    mapping = CADENCE_SCHEDULES.get(cadence)
    if mapping is None:
        return None
    kind, interval = mapping
    last = days[-1]
    if kind == "weekly":
        return {"kind": "weekly", "weekday": last.weekday(), "anchor_on": last.isoformat()}
    if kind == "monthly":
        return {"kind": "monthly", "day": last.day, "anchor_on": last.isoformat()}
    if kind == "interval_weeks":
        return {"kind": "interval_weeks", "weeks": interval, "anchor_on": last.isoformat()}
    return {
        "kind": "interval_months", "months": interval,
        "day": last.day, "anchor_on": last.isoformat(),
    }


def _timing_window(days: list[date], gaps: list[int], cadence: str) -> dict[str, Any]:
    last = days[-1]
    # Only the gaps that agree with the winning cadence shape the expectation.
    # A tolerated outlier is evidence that one payment slipped, not evidence
    # that the next one could land anywhere in a five-month window.
    agreeing = [gap for gap in gaps if _band_of(gap) == cadence] or gaps
    low, high = min(agreeing), max(agreeing)
    window: dict[str, Any] = {
        "cadence": cadence,
        "first_observed_on": days[0].isoformat(),
        "last_observed_on": last.isoformat(),
        "min_gap_days": low,
        "max_gap_days": high,
        "observed_gap_days": gaps,
        "outlier_gap_days": [gap for gap in gaps if _band_of(gap) != cadence],
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
    # Income grouping reads pending inflows too, not only receipts already
    # classified as income: the workflow is to discover a recurring payer while
    # it is still unreviewed and confirm its history in one step (§3.3).
    rows = connection.execute(
        f"SELECT natural_anchor, merchant, currency_pocket, account_id, bucket, "
        f"substr(effective_at,1,10) day, amount_coefficient, amount_scale, "
        f"description, account "
        f"FROM ({_ENTRY_VIEW}) WHERE eligible AND bucket IN ('spending', 'income', 'pending_review') "
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
        variation["drift"] = _drift(amounts)
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
                "suggested_schedule": _suggested_schedule(cadence, days),
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
                # Every dated source, so a grouped confirmation can select the
                # exact history it is classifying (§3.3).
                # Description and account distinguish occurrences that share
                # a date/amount, since the review question is about identity,
                # not arithmetic (see the review_question above).
                "occurrences": [
                    {
                        "anchor": item["natural_anchor"],
                        "date": day.isoformat(),
                        "amount": _money(amount),
                        "description": item["description"],
                        "account": item["account"],
                    }
                    for item, day, amount in zip(items, days, amounts, strict=False)
                ],
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
