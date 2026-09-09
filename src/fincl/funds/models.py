"""Stage 4A vocabulary and the pure sinking-fund arithmetic.

Two things live here and nothing else: the journalled decision kinds (so a
payload can never be accepted by ``validation`` and rejected by ``projection``),
and the arithmetic that the whole stage rests on.  Both are database-free, so
the invariant below is tested against the numbers themselves rather than
against a fixture that happens to produce them.

The central invariant
---------------------
Total cash, earmarked funds and unallocated cash are three separate quantities:

    unallocated_cash = total_cash - earmarked_funds

A *contribution* is a movement inside liquid cash.  It raises the earmark and
lowers unallocated cash, and it does **not** reduce total cash — the money has
not gone anywhere, it has been spoken for.  The *due expense* is what finally
moves cash out: it reduces total cash and releases the fund's earmark, exactly
once, leaving unallocated cash untouched by the release.  Modelling a
contribution as a cash outflow would double-count the expense; modelling the
payment as earmark-only would leave phantom cash on the books.  Both failures
are ruled out by construction in :class:`CashSeparation`.

Two consequences are deliberate.  A schedule of contributions changes no
balance on any day, so it cannot rescue an insolvent cash path — planning to
save is not saving.  And an earmark larger than the cash that backs it is not
clamped away: it is reported with an explicit unfunded warning, because a fund
that cannot be funded is the thing the operator most needs to see.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..money import format_decimal

SINKING_FUND_SET = "SINKING_FUND_SET"
SINKING_FUND_ALLOCATION_SET = "SINKING_FUND_ALLOCATION_SET"
SINKING_FUND_ALLOCATION_RETIRE = "SINKING_FUND_ALLOCATION_RETIRE"
SINKING_FUND_RELEASE_SET = "SINKING_FUND_RELEASE_SET"

FUND_DECISION_KINDS = frozenset(
    {
        SINKING_FUND_SET,
        SINKING_FUND_ALLOCATION_SET,
        SINKING_FUND_ALLOCATION_RETIRE,
        SINKING_FUND_RELEASE_SET,
    }
)

# Where a fund's amount came from. `unknown` is a first-class answer: an
# evidenced expense with no stated amount keeps the fund and the data-derived
# irregular allowance rather than acquiring a guessed target.
AMOUNT_BASIS_OBSERVED_EXPENSE = "observed_irregular_expense"
AMOUNT_BASIS_OBLIGATION = "recorded_obligation"
AMOUNT_BASIS_FUTURE_INVOICE = "evidenced_future_invoice"
AMOUNT_BASIS_GOAL_RECORD = "manual_goal_target"
AMOUNT_BASIS_UNKNOWN = "unknown"
FUND_AMOUNT_BASES = frozenset(
    {
        AMOUNT_BASIS_OBSERVED_EXPENSE,
        AMOUNT_BASIS_OBLIGATION,
        AMOUNT_BASIS_FUTURE_INVOICE,
        AMOUNT_BASIS_GOAL_RECORD,
        AMOUNT_BASIS_UNKNOWN,
    }
)

# Where a fund's due date came from. An observed month is not a due date, so a
# fund sourced from one isolated historical purchase carries
# `observed_expense_month` and stays incomplete until a real date is evidenced.
DATE_BASIS_STATED_DUE_DATE = "stated_due_date"
DATE_BASIS_SCHEDULED_OCCURRENCE = "scheduled_occurrence"
DATE_BASIS_INVOICE_DATE = "evidenced_invoice_date"
DATE_BASIS_GOAL_TARGET_DATE = "manual_goal_target_date"
DATE_BASIS_OBSERVED_MONTH = "observed_expense_month"
DATE_BASIS_UNKNOWN = "unknown"
FUND_DATE_BASES = frozenset(
    {
        DATE_BASIS_STATED_DUE_DATE,
        DATE_BASIS_SCHEDULED_OCCURRENCE,
        DATE_BASIS_INVOICE_DATE,
        DATE_BASIS_GOAL_TARGET_DATE,
        DATE_BASIS_OBSERVED_MONTH,
        DATE_BASIS_UNKNOWN,
    }
)

# A basis that names manually recorded intent rather than observed money. A
# fund resting on one of these must disclose it: neither a goal target nor a
# goal's manually typed "saved" figure is evidence that cash exists.
MANUAL_BASES = frozenset({AMOUNT_BASIS_GOAL_RECORD, DATE_BASIS_GOAL_TARGET_DATE})

CENTRAL_INVARIANT = (
    "total cash, earmarked funds and unallocated cash are three separate "
    "quantities: a contribution raises the earmark and lowers unallocated cash "
    "without reducing total cash; the due expense reduces total cash and "
    "releases its earmark exactly once"
)

CONTRIBUTION_ROUNDING_RULE = (
    "final-contribution-adjustment-v1: every contribution date carries the "
    "same whole minor-unit amount and the final date absorbs the remainder, so "
    "the scheduled contributions sum to the shortfall exactly"
)

ALLOCATION_IS_NOT_CASH_RULE = (
    "a recorded allocation is journalled money set aside, never a planned one; "
    "time passing over a contribution date proves nothing was saved"
)


def contribution_plan(
    *,
    amount_due_minor: int | None,
    allocated_now_minor: int,
    remaining_dates: list[str],
) -> dict[str, Any]:
    """Split the outstanding shortfall across the remaining contribution dates.

    ``required_contribution = max(0, amount_due - allocated_now) / remaining_dates``,
    rounded per :data:`CONTRIBUTION_ROUNDING_RULE`.  Amounts stay whole minor
    units and the per-date amounts sum to the shortfall exactly — the final
    date carries the remainder, so no contribution is ever negative and no
    rounding leaves the fund a cent short at the due date.

    With no remaining dates there is no schedule to state.  That is not a
    refusal: the shortfall is still returned, and the caller reports it as an
    immediate funding requirement.
    """
    if allocated_now_minor < 0:
        raise ValueError("a recorded allocation is never negative")
    if amount_due_minor is None:
        return {
            "available": False,
            "reason": "the fund has no sourced amount due",
            "shortfall_minor": None,
            "required_contribution_minor": None,
            "final_contribution_minor": None,
            "schedule": [],
            "sums_exactly": False,
        }
    if amount_due_minor < 0:
        raise ValueError("an amount due is a magnitude, never a sign")
    shortfall = max(0, amount_due_minor - allocated_now_minor)
    count = len(remaining_dates)
    if count == 0:
        return {
            "available": False,
            "reason": "no remaining contribution date falls before the due date",
            "shortfall_minor": shortfall,
            "required_contribution_minor": None,
            "final_contribution_minor": None,
            "schedule": [],
            "sums_exactly": False,
        }
    base, remainder = divmod(shortfall, count)
    amounts = [base] * count
    amounts[-1] = base + remainder
    return {
        "available": True,
        "reason": None,
        "shortfall_minor": shortfall,
        "required_contribution_minor": base,
        "final_contribution_minor": amounts[-1],
        "schedule": [
            {"on": day, "amount_minor": amount}
            for day, amount in zip(remaining_dates, amounts, strict=True)
        ],
        "sums_exactly": sum(amounts) == shortfall,
        "rounding_rule": CONTRIBUTION_ROUNDING_RULE,
    }


@dataclass(frozen=True, slots=True)
class CashSeparation:
    """Total cash, earmarks and unallocated cash held apart, in minor units.

    Every quantity is derived from the two stored ones, so no arithmetic here
    can drift them out of agreement: ``total_cash_minor`` comes from the Stage
    2A position adapter and ``earmarked_minor`` from the recorded allocations
    that have not been released.
    """

    currency: str
    total_cash_minor: int
    earmarked_minor: int
    reserve_minor: int = 0

    def __post_init__(self) -> None:
        if self.earmarked_minor < 0:
            raise ValueError("an earmark is never negative")
        if self.reserve_minor < 0:
            raise ValueError("a minimum reserve is never negative")

    @property
    def unallocated_minor(self) -> int:
        return self.total_cash_minor - self.earmarked_minor

    @property
    def reserve_adjusted_unallocated_minor(self) -> int:
        """Unallocated cash with the minimum reserve deducted as well.

        The reserve is protected cash, not an earmark: it is deducted here and
        never added to ``earmarked_minor``, so a fund is never confused with
        the buffer that sits behind every fund.
        """
        return self.unallocated_minor - self.reserve_minor

    @property
    def unfunded(self) -> bool:
        """True when the earmarks claim more cash than the position supports."""
        return self.earmarked_minor > self.total_cash_minor

    def with_contribution(self, amount_minor: int) -> CashSeparation:
        """A contribution: earmarks rise, unallocated falls, total cash stands."""
        if amount_minor < 0:
            raise ValueError("a contribution is a magnitude, never a sign")
        return replace(self, earmarked_minor=self.earmarked_minor + amount_minor)

    def with_settlement(self, *, amount_minor: int, release_minor: int) -> CashSeparation:
        """The due expense: total cash falls and the earmark is released once.

        ``release_minor`` may not exceed the standing earmark — that is what
        makes "released exactly once" arithmetic rather than bookkeeping
        discipline.  It may differ from ``amount_minor``: a fund that saved
        less than the bill releases what it held and the rest of the payment
        comes out of unallocated cash, which is precisely the shortfall the
        operator needs to see.
        """
        if amount_minor < 0:
            raise ValueError("a settlement is a magnitude, never a sign")
        if release_minor < 0:
            raise ValueError("a release is a magnitude, never a sign")
        if release_minor > self.earmarked_minor:
            raise ValueError(
                "a fund cannot release more earmark than it holds; the earmark "
                "is released exactly once"
            )
        return replace(
            self,
            total_cash_minor=self.total_cash_minor - amount_minor,
            earmarked_minor=self.earmarked_minor - release_minor,
        )

    def as_dict(self, scale: int) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "total_cash": format_decimal(self.total_cash_minor, scale),
            "earmarked_funds": format_decimal(self.earmarked_minor, scale),
            "unallocated_cash": format_decimal(self.unallocated_minor, scale),
            "minimum_reserve": format_decimal(self.reserve_minor, scale),
            "reserve_adjusted_unallocated_cash": format_decimal(
                self.reserve_adjusted_unallocated_minor, scale
            ),
            "unfunded": self.unfunded,
            "unfunded_warning": (
                f"{format_decimal(self.earmarked_minor - self.total_cash_minor, scale)} "
                f"{self.currency} of earmarks is not backed by liquid cash; the "
                "allocations are shown in full rather than reduced to fit"
                if self.unfunded
                else None
            ),
            "reserve_shortfall": (
                format_decimal(-self.reserve_adjusted_unallocated_minor, scale)
                if self.reserve_adjusted_unallocated_minor < 0
                else None
            ),
            "invariant": CENTRAL_INVARIANT,
        }


__all__ = [
    "ALLOCATION_IS_NOT_CASH_RULE",
    "AMOUNT_BASIS_FUTURE_INVOICE",
    "AMOUNT_BASIS_GOAL_RECORD",
    "AMOUNT_BASIS_OBLIGATION",
    "AMOUNT_BASIS_OBSERVED_EXPENSE",
    "AMOUNT_BASIS_UNKNOWN",
    "CENTRAL_INVARIANT",
    "CONTRIBUTION_ROUNDING_RULE",
    "DATE_BASIS_GOAL_TARGET_DATE",
    "DATE_BASIS_INVOICE_DATE",
    "DATE_BASIS_OBSERVED_MONTH",
    "DATE_BASIS_SCHEDULED_OCCURRENCE",
    "DATE_BASIS_STATED_DUE_DATE",
    "DATE_BASIS_UNKNOWN",
    "FUND_AMOUNT_BASES",
    "FUND_DATE_BASES",
    "FUND_DECISION_KINDS",
    "MANUAL_BASES",
    "SINKING_FUND_ALLOCATION_RETIRE",
    "SINKING_FUND_ALLOCATION_SET",
    "SINKING_FUND_RELEASE_SET",
    "SINKING_FUND_SET",
    "CashSeparation",
    "contribution_plan",
]
