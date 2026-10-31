"""The shared cross-domain component-claim registry (v0.3, PLAN §5.5).

One authority decides whether a source entry's amount or fee component has been
over-claimed — by a personal-debt allocation, a recognized non-debt residual, a
refund attribution, or (later) an installment settlement. Debt / card / budget
code calls :func:`assert_partition` and :func:`open_residual`; it never
re-implements the "claims never exceed the component" check.

Amounts are exact: every helper returns a :class:`fractions.Fraction` of the
ledger's minor units so a scale-2 claim is never normalized against a scale-0
bound.

Slice 1 wires the two live claim sources that already have projection tables —
personal-debt allocations (empty until v0.3 slice 2) and refund attributions —
plus the owner residual. Adding a claim source later means adding a term to
:func:`_claimed_minor`, never a parallel checker. v0.4 adds the installment
term: original-purchase component claims and early-settlement cash components.
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction

from ..errors import FinCLError


def _entry_component_minor(
    connection: sqlite3.Connection, entry_anchor: str, role: str
) -> Fraction:
    """The magnitude of one selected entry's ``amount`` or ``fee`` component."""
    column = "amount" if role == "amount" else "fee"
    row = connection.execute(
        f"""
        SELECT e.{column}_coefficient AS coefficient, e.{column}_scale AS scale
        FROM ledger_entries e
        JOIN extraction_runs x
          ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
        WHERE e.natural_anchor = ?
        LIMIT 1
        """,
        (entry_anchor,),
    ).fetchone()
    if row is None or row["coefficient"] is None:
        return Fraction(0)
    return Fraction(abs(int(row["coefficient"])), 10 ** int(row["scale"]))


def _debt_claim_minor(
    connection: sqlite3.Connection, entry_anchor: str, role: str
) -> Fraction:
    total = Fraction(0)
    for row in connection.execute(
        "SELECT principal_coefficient, principal_scale, interest_coefficient, "
        "interest_scale, fee_coefficient, fee_scale, separate_fee_coefficient, "
        "separate_fee_scale FROM personal_debt_allocations WHERE entry_anchor = ? "
        "AND bound = 1",
        (entry_anchor,),
    ):
        if role == "amount":
            total += Fraction(int(row["principal_coefficient"]), 10 ** int(row["principal_scale"]))
            total += Fraction(int(row["interest_coefficient"]), 10 ** int(row["interest_scale"]))
            total += Fraction(int(row["fee_coefficient"]), 10 ** int(row["fee_scale"]))
        else:
            total += Fraction(
                int(row["separate_fee_coefficient"]), 10 ** int(row["separate_fee_scale"])
            )
    return total


def _residual_claim_minor(connection: sqlite3.Connection, entry_anchor: str, role: str) -> Fraction:
    if role != "amount":
        return Fraction(0)
    row = connection.execute(
        "SELECT expense_coefficient, expense_scale FROM entry_residuals "
        "WHERE entry_anchor = ? AND bound = 1",
        (entry_anchor,),
    ).fetchone()
    if row is None:
        return Fraction(0)
    return Fraction(int(row["expense_coefficient"]), 10 ** int(row["expense_scale"]))


def _refund_claim_minor(connection: sqlite3.Connection, entry_anchor: str, role: str) -> Fraction:
    """Refund attribution credits the *purchase* entry's amount component."""
    if role != "amount":
        return Fraction(0)
    total = Fraction(0)
    for row in connection.execute(
        "SELECT purchase_coefficient, purchase_scale FROM refund_allocations "
        "WHERE purchase_anchor = ?",
        (entry_anchor,),
    ):
        total += Fraction(int(row["purchase_coefficient"]), 10 ** int(row["purchase_scale"]))
    return total


def _installment_claim_minor(
    connection: sqlite3.Connection, entry_anchor: str, role: str
) -> Fraction:
    """Installment claims against one source component.

    An original-purchase allocation with ``component_role = 'principal'`` claims
    the purchase entry's ``amount``; ``'fee'`` claims its ``fee``. An early
    settlement's principal and fee components consume the settlement source
    repayment's ``amount`` (cash); a waived amount is not a cash claim. A
    conversion credit attached to a plan is the plan's principal being borrowed,
    so the plan claims that credit's whole ``amount``.
    """
    total = Fraction(0)
    for row in connection.execute(
        "SELECT component_role, amount_coefficient, amount_scale "
        "FROM installment_purchase_allocations WHERE entry_anchor = ? AND bound = 1",
        (entry_anchor,),
    ):
        wants = "amount" if row["component_role"] == "principal" else "fee"
        if wants == role:
            total += Fraction(
                int(row["amount_coefficient"]), 10 ** int(row["amount_scale"])
            )
    if role == "amount":
        for row in connection.execute(
            "SELECT principal_coefficient, principal_scale, fee_coefficient, fee_scale "
            "FROM installment_settlements WHERE source_entry_anchor = ? AND bound = 1",
            (entry_anchor,),
        ):
            total += Fraction(
                int(row["principal_coefficient"]), 10 ** int(row["principal_scale"])
            )
            total += Fraction(
                int(row["fee_coefficient"]), 10 ** int(row["fee_scale"])
            )
        if _table_exists(connection, "installment_conversions") and connection.execute(
            "SELECT 1 FROM installment_conversions WHERE entry_anchor = ? AND bound = 1",
            (entry_anchor,),
        ).fetchone():
            total += _entry_component_minor(connection, entry_anchor, "amount")
    return total


def _card_payment_claim_minor(
    connection: sqlite3.Connection, entry_anchor: str, role: str
) -> Fraction:
    """A manual card bill allocation claims the source repayment's ``amount``."""
    if role != "amount":
        return Fraction(0)
    total = Fraction(0)
    for row in connection.execute(
        "SELECT amount_coefficient, amount_scale FROM card_payment_allocations "
        "WHERE card_entry_anchor = ? AND bound = 1",
        (entry_anchor,),
    ):
        total += Fraction(
            int(row["amount_coefficient"]), 10 ** int(row["amount_scale"])
        )
    return total


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _claimed_minor(connection: sqlite3.Connection, entry_anchor: str, role: str) -> Fraction:
    total = Fraction(0)
    if _table_exists(connection, "personal_debt_allocations"):
        total += _debt_claim_minor(connection, entry_anchor, role)
    if _table_exists(connection, "entry_residuals"):
        total += _residual_claim_minor(connection, entry_anchor, role)
    if _table_exists(connection, "refund_allocations"):
        total += _refund_claim_minor(connection, entry_anchor, role)
    if _table_exists(connection, "installment_purchase_allocations"):
        total += _installment_claim_minor(connection, entry_anchor, role)
    if _table_exists(connection, "card_payment_allocations"):
        total += _card_payment_claim_minor(connection, entry_anchor, role)
    return total


def claimed_amount(connection: sqlite3.Connection, entry_anchor: str, *, role: str = "amount") -> Fraction:
    """Sum of every registered claim against one entry component, in minor units."""
    return _claimed_minor(connection, entry_anchor, role)


def open_residual(connection: sqlite3.Connection, entry_anchor: str, *, role: str = "amount") -> Fraction:
    """The component magnitude not yet claimed by any domain (never negative)."""
    remaining = _entry_component_minor(connection, entry_anchor, role) - _claimed_minor(
        connection, entry_anchor, role
    )
    return remaining if remaining > 0 else Fraction(0)


def over_claim(connection: sqlite3.Connection, entry_anchor: str, *, role: str = "amount") -> Fraction:
    """How far the registered claims exceed the component magnitude (zero when they fit)."""
    excess = _claimed_minor(connection, entry_anchor, role) - _entry_component_minor(
        connection, entry_anchor, role
    )
    return excess if excess > 0 else Fraction(0)


def assert_partition(
    connection: sqlite3.Connection, entry_anchor: str, *, role: str = "amount"
) -> None:
    """Raise when the registered claims exceed the entry's component magnitude."""
    gross = _entry_component_minor(connection, entry_anchor, role)
    claimed = _claimed_minor(connection, entry_anchor, role)
    if claimed > gross:
        raise FinCLError(
            f"allocations against {entry_anchor} ({role}) total {claimed} which "
            f"exceeds the component magnitude {gross}"
        )
