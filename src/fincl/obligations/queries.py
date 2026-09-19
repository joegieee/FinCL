"""Stage 2B — one obligations interface over debts, installments, cards and
recurring events.

Four sources state "something is owed" in FinCL, each with its own evidence and
its own idea of a date.  This read merges them behind one shape without
flattening what makes them different: every obligation keeps its ``origin``, the
basis of its amount and of its date, the actual payments recorded against it,
its confidence, and how it relates to the Stage 1 baseline.

Identity is stable across reads and across generations — ``debt:<debt_id>``,
``installment:<plan_id>``, ``card_statement:<statement_id>:<pocket>``,
``recurring:<commitment>`` — so a caller can follow one obligation over time
without re-matching it by amount.

The rules that keep the merged view honest live in :mod:`fincl.obligations.rules`
and are applied here, not re-implemented per adapter.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..baseline.queries import _month_list, cost_income_table_from
from ..cards import projection as card_projection
from ..cards.models import ROLE_PAYMENT_DUE, ROLE_PAYMENT_DUE_DATE
from ..db import connect_readonly
from ..debts.queries import _balance_minor
from ..errors import FinCLError
from ..generation import current_generation
from ..intervals import month_span
from ..money import format_decimal
from ..planning.schedules import occurrences as schedule_occurrences
from .rules import (
    baseline_component_link,
    history_confidence,
    money,
    payment_state,
    resolve_amount_basis,
)


def _as_of(value: str | None, today: date) -> str:
    if value is None:
        return today.isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise FinCLError("as_of must be an ISO calendar date (YYYY-MM-DD)") from exc


def _generation(connection: sqlite3.Connection) -> str:
    row = current_generation(connection)
    if row is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    return str(row["generation_token"])


def _amount(coefficient: Any, scale: Any) -> Decimal:
    return Decimal(int(coefficient)).scaleb(-int(scale))



def _baseline_portions_from(
    connection: sqlite3.Connection, token: str, today: date
) -> dict[str, Any]:
    """Monthly historical cost per (currency, category) over the baseline window.

    This is the "historical baseline remainder" a recurring event replaces its
    own share of.  When no baseline window is available the portions are empty
    and every link says so rather than inventing a denominator.
    """
    table = cost_income_table_from(connection, today=today)
    if not table["available"]:
        return {
            "available": False,
            "reason": table["reason"],
            "portions": {},
            "months": 0,
            "window": table["baseline_window"],
        }
    months = _month_list(
        table["baseline_window"]["start_month"], table["baseline_window"]["end_month"]
    )
    rows = connection.execute(
        """SELECT currency, category_key, signed_coefficient, scale
             FROM analysis_components
            WHERE generation_token=? AND lens='recorded_flow'
              AND bucket IN ('spending', 'fees')
              AND represented_start>=? AND represented_end<=?""",
        (token, month_span(months[0]).start, month_span(months[-1]).end),
    ).fetchall()
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for row in rows:
        totals[(row["currency"], row["category_key"])] += abs(
            _amount(row["signed_coefficient"], row["scale"])
        )
    count = Decimal(len(months))
    return {
        "available": True,
        "portions": {key: money(value / count) for key, value in totals.items()},
        "months": len(months),
        "window": table["baseline_window"],
        "basis": "mean monthly recorded cost per component over the baseline window",
    }


def _debt_obligations(connection: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
    """Personal debts and receivables.

    A debt states a lump-sum principal and, optionally, one due date; nothing in
    the journal derives a repayment schedule from it, so the amount basis stays
    a lump sum and says so.  Recorded payments are the debt's own decrease
    events, which is the only payment evidence this source has.
    """
    result: list[dict[str, Any]] = []
    for debt in connection.execute("SELECT * FROM personal_debts ORDER BY debt_id"):
        rows = connection.execute(
            "SELECT * FROM personal_debt_events WHERE debt_id = ? "
            "ORDER BY effective_on, event_id",
            (debt["debt_id"],),
        ).fetchall()
        total, scale = _balance_minor(rows, as_of)
        remaining = format_decimal(total, scale)
        payments = [
            {
                "effective_on": str(row["effective_on"]),
                "amount": format_decimal(
                    int(row["principal_coefficient"]), int(row["principal_scale"])
                ),
                "event_kind": str(row["event_kind"]),
                "bound": bool(row["bound"]),
            }
            for row in rows
            if int(row["signed"]) < 0 and str(row["effective_on"]) <= as_of
        ]
        closed = str(debt["status"]) == "CLOSED"
        basis = resolve_amount_basis(
            lump_sum=remaining, schedule=None, currency=str(debt["currency"])
        )
        state = payment_state(
            due_on=debt["due_on"],
            as_of=as_of,
            payment_count=len(payments),
            remaining=remaining,
            closed=closed,
            closed_reason="the debt is recorded closed",
        )
        result.append(
            {
                "obligation_id": f"debt:{debt['debt_id']}",
                "origin": "personal_debt",
                "source": {"kind": "personal_debt", "id": str(debt["debt_id"])},
                "label": str(debt["counterparty_label"]),
                "currency": str(debt["currency"]),
                "direction": "outflow" if debt["direction"] == "PAYABLE" else "inflow",
                "date_basis": {
                    "kind": "stated_due_date" if debt["due_on"] else "no_stated_date",
                    "value": debt["due_on"],
                },
                "actual_payments": {
                    "count": len(payments),
                    "total": _payments_total(payments),
                    "entries": payments,
                },
                "cash_effect": "bank_withdrawal",
                "baseline_link": {
                    "available": False,
                    "category_key": None,
                    "relation": "not_a_baseline_component",
                    "reason": (
                        "a principal movement is a balance-sheet event, not part "
                        "of the historical cost baseline"
                    ),
                },
                **basis,
                **history_confidence(commitment_recorded=True),
                **state,
            }
        )
    return result


def _payments_total(payments: list[dict[str, Any]]) -> str:
    return money(sum((Decimal(item["amount"]) for item in payments), Decimal(0)))


def _installment_obligations(
    connection: sqlite3.Connection, as_of: str
) -> list[dict[str, Any]]:
    """Installment plans, scheduled first.

    A plan's remaining rows are a real schedule, so they replace the plan's
    lump-sum remaining obligation.  A plan bound to a card account settles
    inside that card's statement due — it is not an additional bank withdrawal.
    """
    result: list[dict[str, Any]] = []
    for plan in connection.execute("SELECT * FROM installment_plans ORDER BY plan_id"):
        currency = str(plan["currency"])
        occurrences = connection.execute(
            "SELECT effective_on, amount_coefficient, amount_scale, entry_anchor, bound "
            "FROM installment_occurrences WHERE plan_id = ? ORDER BY effective_on",
            (plan["plan_id"],),
        ).fetchall()
        schedule_rows = connection.execute(
            "SELECT schedule_index, due_on, principal_coefficient, principal_scale, "
            "fee_coefficient, fee_scale, is_projected FROM installment_schedule "
            "WHERE plan_id = ? ORDER BY schedule_index",
            (plan["plan_id"],),
        ).fetchall()
        paid = len(occurrences)
        # The first ``paid`` schedule rows are the ones the card has already
        # shown; the rest are what remains. An unobserved row keeps its due date
        # even once that date has passed — see ``payment_state``.
        remaining_rows: list[dict[str, Any]] = [
            {
                "due_on": str(row["due_on"]),
                "amount": money(
                    _amount(row["principal_coefficient"], row["principal_scale"])
                    + _amount(row["fee_coefficient"], row["fee_scale"])
                ),
                "schedule_index": int(row["schedule_index"]),
                "is_projected": bool(row["is_projected"]),
            }
            for row in schedule_rows[paid:]
        ]
        lump = None
        if plan["principal_coefficient"] is not None:
            observed = sum(
                (_amount(row["amount_coefficient"], row["amount_scale"]) for row in occurrences),
                Decimal(0),
            )
            principal = _amount(plan["principal_coefficient"], plan["principal_scale"])
            lump = money(max(principal - observed, Decimal(0)))
        basis = resolve_amount_basis(
            lump_sum=lump, schedule=remaining_rows or None, currency=currency
        )
        payments = [
            {
                "effective_on": str(row["effective_on"]),
                "amount": money(_amount(row["amount_coefficient"], row["amount_scale"])),
                "entry_anchor": str(row["entry_anchor"]),
                "bound": bool(row["bound"]),
            }
            for row in occurrences
            if str(row["effective_on"]) <= as_of
        ]
        next_due = remaining_rows[0]["due_on"] if remaining_rows else None
        on_card = plan["card_account_id"] is not None
        result.append(
            {
                "obligation_id": f"installment:{plan['plan_id']}",
                "origin": "installment_plan",
                "source": {"kind": "installment_plan", "id": str(plan["plan_id"])},
                "label": str(plan["merchant_label"]),
                "currency": currency,
                "direction": "outflow",
                "date_basis": {
                    "kind": "scheduled_due_date" if next_due else "no_remaining_due_date",
                    "value": next_due,
                },
                "actual_payments": {
                    "count": len(payments),
                    "total": _payments_total(payments),
                    "entries": payments,
                },
                "cash_effect": "via_card_statement" if on_card else "unknown",
                "cash_effect_reason": (
                    "settled inside the card's statement due, not as a separate "
                    "bank withdrawal"
                    if on_card
                    else "no card account is bound to this plan"
                ),
                "baseline_link": {
                    "available": False,
                    "category_key": None,
                    "relation": "not_a_baseline_component",
                    "reason": (
                        "the plan's purchase cost was recognised once at open; "
                        "its occurrences are not additional baseline cost"
                    ),
                },
                **basis,
                **history_confidence(
                    commitment_recorded=str(plan["status"]) == "CONFIRMED"
                ),
                **payment_state(
                    due_on=next_due,
                    as_of=as_of,
                    payment_count=len(payments),
                    remaining=(
                        basis["remaining_amount"]["value"]
                        if basis["remaining_amount"]
                        else None
                    ),
                    closed=str(plan["status"]) == "SETTLED",
                    closed_reason="the plan is recorded settled",
                ),
            }
        )
    return result


def _card_obligations(connection: sqlite3.Connection, as_of: str) -> list[dict[str, Any]]:
    """The latest statement due per card pocket, net of recorded payments.

    The remaining due is the printed ``payment_due`` less the payments actually
    allocated to that statement.  Installment occurrences that posted inside the
    statement period are named on the obligation: they are already part of this
    due and must not be counted again as bank withdrawals.
    """
    result: list[dict[str, Any]] = []
    for account in card_projection.selected_card_accounts(connection):
        statements = [
            statement
            for statement in card_projection.selected_statements(
                connection, str(account["id"])
            )
            if str(statement["period_start"] or statement["statement_date"]) <= as_of
        ]
        if not statements:
            continue
        statement = statements[-1]
        pockets = card_projection.statement_facts_by_pocket(
            connection, str(statement["id"])
        )
        for pocket, facts in sorted(pockets.items()):
            due_fact = facts.get(ROLE_PAYMENT_DUE)
            if due_fact is None or due_fact.value_coefficient is None:
                continue
            due_date_fact = facts.get(ROLE_PAYMENT_DUE_DATE)
            due = _amount(due_fact.value_coefficient, due_fact.value_scale)
            allocations = connection.execute(
                "SELECT card_entry_anchor, amount_coefficient, amount_scale "
                "FROM card_payment_allocations "
                "WHERE statement_id = ? AND currency = ? AND bound = 1 "
                "ORDER BY card_entry_anchor, seq_in_entry",
                (str(statement["id"]), pocket),
            ).fetchall()
            payments = [
                {
                    "entry_anchor": str(row["card_entry_anchor"]),
                    "amount": money(_amount(row["amount_coefficient"], row["amount_scale"])),
                }
                for row in allocations
            ]
            paid = sum((Decimal(item["amount"]) for item in payments), Decimal(0))
            remaining = money(max(due - paid, Decimal(0)))
            inside = _installments_inside(
                connection, str(account["id"]), statement, pocket
            )
            basis = resolve_amount_basis(
                lump_sum=remaining, schedule=None, currency=pocket
            )
            result.append(
                {
                    "obligation_id": f"card_statement:{statement['id']}:{pocket}",
                    "origin": "card_statement",
                    "source": {
                        "kind": "card_statement",
                        "id": str(statement["id"]),
                        "account_id": str(account["id"]),
                    },
                    "label": str(account["display_name"]),
                    "currency": pocket,
                    "direction": "outflow",
                    "date_basis": {
                        "kind": "statement_due_date"
                        if due_date_fact
                        else "statement_due_date_not_printed",
                        "value": due_date_fact.value_text if due_date_fact else None,
                    },
                    "statement_payment_due": money(due),
                    "actual_payments": {
                        "count": len(payments),
                        "total": money(paid),
                        "entries": payments,
                    },
                    "cash_effect": "bank_withdrawal",
                    "includes_installment_occurrences": inside,
                    "double_count_guard": (
                        "installments listed here are already inside this due and "
                        "are never counted as separate bank withdrawals"
                    ),
                    "baseline_link": {
                        "available": False,
                        "category_key": None,
                        "relation": "not_a_baseline_component",
                        "reason": (
                            "a statement due is a settlement of costs already "
                            "recognised in their own components"
                        ),
                    },
                    **basis,
                    **history_confidence(future_receipt_evidenced=True),
                    **payment_state(
                        due_on=due_date_fact.value_text if due_date_fact else None,
                        as_of=as_of,
                        payment_count=len(payments),
                        remaining=remaining,
                    ),
                }
            )
    return result


def _installments_inside(
    connection: sqlite3.Connection, account_id: str, statement: Any, pocket: str
) -> list[dict[str, Any]]:
    start, end = statement["period_start"], statement["period_end"]
    if start is None or end is None:
        return []
    rows = connection.execute(
        "SELECT o.plan_id, o.effective_on, o.amount_coefficient, o.amount_scale "
        "FROM installment_occurrences o JOIN installment_plans p ON p.plan_id = o.plan_id "
        "WHERE p.card_account_id = ? AND p.currency = ? "
        "AND o.effective_on BETWEEN ? AND ? ORDER BY o.effective_on, o.plan_id",
        (account_id, pocket, start, end),
    ).fetchall()
    return [
        {
            "obligation_id": f"installment:{row['plan_id']}",
            "effective_on": str(row["effective_on"]),
            "amount": money(_amount(row["amount_coefficient"], row["amount_scale"])),
            "inside_statement_due": True,
        }
        for row in rows
    ]


def _recurring_obligations(
    connection: sqlite3.Connection, as_of: str, portions: dict[tuple[str, str], str]
) -> list[dict[str, Any]]:
    """Active recurring commitments, linked to their own baseline component."""
    token = _generation(connection)
    result: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT * FROM recurring_commitments WHERE status = 'active' ORDER BY name"
    ):
        commitment = str(row["commitment"])
        currency = str(row["currency"])
        expected = money(_amount(row["expected_coefficient"], row["expected_scale"]))
        bound = connection.execute(
            "SELECT scheduled_on, entry_anchor FROM recurring_occurrences "
            "WHERE commitment = ? AND bound = 1 AND entry_anchor IS NOT NULL "
            "ORDER BY scheduled_on",
            (commitment,),
        ).fetchall()
        payments = [
            {
                "scheduled_on": str(item["scheduled_on"]),
                "entry_anchor": str(item["entry_anchor"]),
                "amount": expected,
            }
            for item in bound
            if str(item["scheduled_on"]) <= as_of
        ]
        categories = _categories_for(connection, token, [item["entry_anchor"] for item in bound])
        schedule = json.loads(row["schedule_json"])
        today = date.fromisoformat(as_of)
        upcoming = schedule_occurrences(schedule, today, date.fromordinal(today.toordinal() + 62))
        matched = {str(item["scheduled_on"]) for item in bound}
        overdue = [
            occurrence.isoformat()
            for occurrence in schedule_occurrences(
                schedule, date.fromordinal(today.toordinal() - 62), today
            )
            if occurrence.isoformat() not in matched and occurrence.isoformat() < as_of
        ]
        next_due = upcoming[0].isoformat() if upcoming else None
        due_on = overdue[0] if overdue else next_due
        result.append(
            {
                "obligation_id": f"recurring:{commitment}",
                "origin": "recurring_commitment",
                "source": {"kind": "recurring_commitment", "id": commitment},
                "label": str(row["name"]),
                "currency": currency,
                "direction": "outflow",
                "date_basis": {
                    "kind": "scheduled_occurrence" if due_on else "no_scheduled_occurrence",
                    "value": due_on,
                    "schedule": schedule,
                },
                "unmatched_past_occurrences": overdue,
                "actual_payments": {
                    "count": len(payments),
                    "total": _payments_total(payments),
                    "entries": payments,
                },
                "cash_effect": "bank_withdrawal",
                "baseline_link": baseline_component_link(
                    categories=categories, currency=currency, portions=portions
                ),
                **resolve_amount_basis(
                    lump_sum=expected, schedule=None, currency=currency
                ),
                **history_confidence(commitment_recorded=True),
                **payment_state(
                    due_on=overdue[0] if overdue else None,
                    as_of=as_of,
                    payment_count=0 if overdue else len(payments),
                    remaining=expected,
                ),
            }
        )
    return result


def _categories_for(
    connection: sqlite3.Connection, token: str, anchors: list[Any]
) -> set[str]:
    if not anchors:
        return set()
    marks = ",".join("?" for _ in anchors)
    rows = connection.execute(
        f"SELECT DISTINCT category_key FROM analysis_components "
        f"WHERE generation_token = ? AND lens = 'recorded_flow' "
        f"AND bucket IN ('spending', 'fees') AND origin_anchor IN ({marks})",
        (token, *[str(anchor) for anchor in anchors]),
    ).fetchall()
    return {str(row["category_key"]) for row in rows}


def _component_effects(
    obligations: list[dict[str, Any]], baseline: dict[str, Any]
) -> list[dict[str, Any]]:
    """Per baseline component: what a recurring event replaces, and what stays.

    A component nobody links to keeps its whole historical portion — that is the
    point of naming the link: rent replaces rent, and groceries are untouched.
    """
    replaced: dict[tuple[str, str], list[str]] = defaultdict(list)
    for obligation in obligations:
        link = obligation["baseline_link"]
        if link.get("available") and link.get("monthly_baseline_portion") is not None:
            replaced[(link["currency"], link["category_key"])].append(
                obligation["obligation_id"]
            )
    effects: list[dict[str, Any]] = []
    for (currency, category), portion in sorted(baseline["portions"].items()):
        owners = replaced.get((currency, category), [])
        effects.append(
            {
                "currency": currency,
                "category_key": category,
                "monthly_baseline_portion": portion,
                "replaced_by": owners,
                "remaining_historical_portion": "0.00" if owners else portion,
                "relation": "replaced_by_recurring_event" if owners else "unchanged",
            }
        )
    return effects


def obligations_page(
    database: Path, *, as_of: str | None = None, today: date
) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return obligations_page_from(connection, as_of=as_of, today=today)
    finally:
        connection.close()


def obligations_page_from(
    connection: sqlite3.Connection, *, as_of: str | None = None, today: date
) -> dict[str, Any]:
    """Every obligation from every source, behind one shape.

    ``today`` is required (§5.3): resolved once by the caller, never by this
    reader. ``as_of`` remains an optional historical override that defaults to
    ``today`` rather than to the wall clock.
    """
    as_of = _as_of(as_of, today)
    if not connection.in_transaction:
        connection.execute("BEGIN")
    token = _generation(connection)
    baseline = _baseline_portions_from(connection, token, today)
    obligations = [
        *_debt_obligations(connection, as_of),
        *_installment_obligations(connection, as_of),
        *_card_obligations(connection, as_of),
        *_recurring_obligations(connection, as_of, baseline["portions"]),
    ]
    obligations.sort(key=lambda item: item["obligation_id"])

    totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"bank_withdrawal": Decimal(0), "via_card_statement": Decimal(0)}
    )
    for obligation in obligations:
        amount = obligation["remaining_amount"]
        if (
            amount is None
            or obligation["direction"] != "outflow"
            or not obligation["stays_visible"]
        ):
            continue
        lane = (
            "via_card_statement"
            if obligation["cash_effect"] == "via_card_statement"
            else "bank_withdrawal"
        )
        totals[obligation["currency"]][lane] += Decimal(amount["value"])
    return {
        "generation": token,
        "as_of": as_of,
        "obligations": obligations,
        "totals_by_currency": {
            currency: {
                "bank_withdrawal": money(lanes["bank_withdrawal"]),
                "via_card_statement": money(lanes["via_card_statement"]),
                "policy": (
                    "an obligation settled inside a card statement due is counted "
                    "in its own lane only, never again as a bank withdrawal"
                ),
            }
            for currency, lanes in sorted(totals.items())
        },
        "baseline": {
            "available": baseline["available"],
            "window": baseline["window"],
            "month_count": baseline["months"],
            "reason": baseline.get("reason"),
            "basis": baseline.get("basis"),
        },
        "baseline_component_effects": _component_effects(obligations, baseline),
        "rules": [
            "a remaining schedule replaces the lump-sum due; the superseded lump "
            "sum is shown but never counted",
            "a passed due date is not evidence of a missed payment; an overdue or "
            "unmatched obligation stays visible until payment, forgiveness or "
            "correction is recorded",
            "a remaining statement due is net of the payments actually recorded "
            "against that statement",
            "a recurring event replaces its own baseline component only; unknown "
            "terms stay unknown",
        ],
    }


__all__ = ["obligations_page"]
