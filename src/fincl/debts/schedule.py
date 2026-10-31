"""Debt repayment schedules (schema 32): the plan, and how far the debt's own
repayments have got through it.

A schedule is a list of dated amounts. Nothing is matched to it by hand: every
decrease event dated on or after the schedule's ``effective_from`` (a repayment,
a return, a forgiveness or a write-off) pays its rows oldest first. The debt's
principal stays the authority, so the rows still owed are trimmed to it from the
latest backwards, and whatever principal the rows do not cover is an
unscheduled remainder due on the debt's own due date, if it has one.

All amounts are exact integers at one common scale per debt.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney, format_decimal
from ..planning.schedules import occurrences, parse_schedule

MAX_SCHEDULE_ROWS = 600
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def expand_plan(plan: dict[str, Any]) -> list[tuple[str, ExactMoney]]:
    """The rows a plan stands for, in date order."""
    if plan["kind"] == "rows":
        return [(str(row["due_on"]), ExactMoney.parse(str(row["amount"]))) for row in plan["rows"]]
    schedule = parse_schedule(plan["schedule"])
    count = int(plan["count"])
    amount = ExactMoney.parse(str(plan["amount"]))
    return [(day.isoformat(), amount) for day in series_dates(schedule, count)]


def series_dates(schedule: dict[str, Any], count: int, start: date | None = None) -> list[date]:
    """The first ``count`` dates of a schedule on or after ``start`` (its anchor by default)."""
    start = start or date.fromisoformat(schedule["anchor_on"])
    span = 400
    while True:
        end = min(start + timedelta(days=span), date(9999, 12, 31))
        found = occurrences(schedule, start, end)
        if len(found) >= count or end == date(9999, 12, 31):
            return found[:count]
        span *= 2


def _int_at(coefficient: int, scale: int, target: int) -> int:
    return coefficient * 10 ** (target - scale)


def schedule_state(
    connection: sqlite3.Connection, debt_id: str, *, as_of: str
) -> dict[str, Any] | None:
    """The debt's schedule as of ``as_of``, or ``None`` when it has none.

    A row is ``paid`` once repayments cover it, ``trimmed`` when the principal no
    longer needs it, ``overdue`` when it is due on or before ``as_of`` with
    something left, ``part_paid`` when partly covered, ``upcoming`` otherwise.
    """
    head = connection.execute(
        "SELECT * FROM personal_debt_schedules WHERE debt_id = ?", (debt_id,)
    ).fetchone()
    if head is None:
        return None
    debt = connection.execute(
        "SELECT due_on FROM personal_debts WHERE debt_id = ?", (debt_id,)
    ).fetchone()
    rows = connection.execute(
        "SELECT * FROM personal_debt_schedule_rows WHERE debt_id = ? ORDER BY due_on, row_index",
        (debt_id,),
    ).fetchall()
    events = connection.execute(
        "SELECT effective_on, signed, principal_coefficient, principal_scale "
        "FROM personal_debt_events WHERE debt_id = ? AND effective_on <= ?",
        (debt_id, as_of),
    ).fetchall()
    scale = max(
        [int(r["amount_scale"]) for r in rows] + [int(e["principal_scale"]) for e in events] + [0]
    )
    principal = sum(
        int(e["signed"]) * _int_at(int(e["principal_coefficient"]), int(e["principal_scale"]), scale)
        for e in events
    )
    paid = sum(
        _int_at(int(e["principal_coefficient"]), int(e["principal_scale"]), scale)
        for e in events
        if int(e["signed"]) < 0 and str(e["effective_on"]) >= str(head["effective_from"])
    )
    states: list[dict[str, Any]] = []
    for row in rows:
        amount = _int_at(int(row["amount_coefficient"]), int(row["amount_scale"]), scale)
        covered = min(amount, paid)
        paid -= covered
        states.append({"row": row, "amount": amount, "paid": covered, "remaining": amount - covered})
    # The principal is the authority: rows asking for more than is still owed
    # give way from the latest backwards.
    excess = sum(state["remaining"] for state in states) - max(principal, 0)
    for state in reversed(states):
        if excess <= 0:
            break
        cut = min(excess, state["remaining"])
        state["remaining"] -= cut
        state["trimmed"] = cut
        excess -= cut
    remainder = -excess if excess < 0 else 0

    def money(value: int) -> str:
        return format_decimal(value, scale)

    out = []
    for state in states:
        row = state["row"]
        due_on = str(row["due_on"])
        if state["remaining"] == 0:
            kind = "paid" if state["paid"] > 0 else "trimmed"
        elif due_on <= as_of:
            kind = "overdue"
        elif state["paid"] > 0:
            kind = "part_paid"
        else:
            kind = "upcoming"
        out.append({
            "schedule_index": int(row["row_index"]),
            "due_on": due_on,
            "amount": money(state["amount"]),
            "paid": money(state["paid"]),
            "remaining": money(state["remaining"]),
            "state": kind,
            "overdue": kind == "overdue",
        })
    debt_due: str | None = str(debt["due_on"]) if debt is not None and debt["due_on"] is not None else None
    unpaid = [row for row in out if row["state"] not in ("paid", "trimmed")]
    return {
        "effective_from": str(head["effective_from"]),
        "plan_kind": str(head["plan_kind"]),
        "reason": str(head["reason"]),
        "rows": out,
        "remaining_principal": money(principal),
        "scheduled_remaining": money(sum(state["remaining"] for state in states)),
        "unscheduled_remainder": None if remainder == 0 else {
            "amount": money(remainder), "due_on": debt_due,
            "overdue": debt_due is not None and debt_due <= as_of,
        },
        "next_due": unpaid[0]["due_on"] if unpaid else None,
        "overdue_count": sum(1 for row in out if row["overdue"]),
    }


def obligation_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    """What is still owed under a schedule, as the obligation's ``schedule`` rows.

    An undated remainder is a row with no due date: the forecast names it as a
    gap rather than placing it on a guessed day.
    """
    rows: list[dict[str, Any]] = [
        {"due_on": row["due_on"], "amount": row["remaining"], "schedule_index": row["schedule_index"],
         "state": row["state"], "overdue": row["overdue"]}
        for row in state["rows"] if row["state"] not in ("paid", "trimmed")
    ]
    remainder = state["unscheduled_remainder"]
    if remainder is not None:
        rows.append({"due_on": remainder["due_on"], "amount": remainder["amount"],
                     "schedule_index": "remainder", "state": "unscheduled", "overdue": remainder["overdue"]})
    return rows


def write_schedule(
    connection: sqlite3.Connection, debt_id: str, payload: dict[str, Any], journal_seq: int
) -> None:
    connection.execute("DELETE FROM personal_debt_schedule_rows WHERE debt_id = ?", (debt_id,))
    connection.execute(
        "INSERT OR REPLACE INTO personal_debt_schedules "
        "(debt_id, effective_from, plan_kind, journal_seq, reason) VALUES (?, ?, ?, ?, ?)",
        (debt_id, payload["effective_from"], payload["plan"]["kind"], journal_seq, payload["reason"]),
    )
    for index, (due_on, amount) in enumerate(expand_plan(payload["plan"])):
        connection.execute(
            "INSERT INTO personal_debt_schedule_rows "
            "(debt_id, row_index, due_on, amount_coefficient, amount_scale) VALUES (?, ?, ?, ?, ?)",
            (debt_id, index, due_on, amount.coefficient, amount.scale),
        )


def clear_schedule(connection: sqlite3.Connection, debt_id: str) -> None:
    connection.execute("DELETE FROM personal_debt_schedule_rows WHERE debt_id = ?", (debt_id,))
    connection.execute("DELETE FROM personal_debt_schedules WHERE debt_id = ?", (debt_id,))


def check_plan_shape(plan: Any) -> list[tuple[str, ExactMoney]]:
    """Validate a plan's shape and return its rows. ``count`` must be set here;
    a draft completes a null count before this runs."""
    if not isinstance(plan, dict) or plan.get("kind") not in ("rows", "series"):
        raise FinCLError("plan.kind must be rows or series")
    if plan["kind"] == "rows":
        if set(plan) != {"kind", "rows"}:
            raise FinCLError("a rows plan is exactly {kind, rows}")
        rows = plan["rows"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SCHEDULE_ROWS:
            raise FinCLError(f"a plan has 1-{MAX_SCHEDULE_ROWS} rows")
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {"due_on", "amount"}:
                raise FinCLError(f"plan row {index} is exactly {{due_on, amount}}")
    else:
        if set(plan) != {"kind", "schedule", "amount", "count"}:
            raise FinCLError("a series plan is exactly {kind, schedule, amount, count}")
        parse_schedule(plan["schedule"])
        count = plan["count"]
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_SCHEDULE_ROWS:
            raise FinCLError(f"plan.count must be an integer 1-{MAX_SCHEDULE_ROWS}")
    try:
        expanded = expand_plan(plan)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise FinCLError("every plan row needs a date and an amount") from exc
    previous = ""
    for index, (due_on, amount) in enumerate(expanded):
        try:
            if not _DATE.fullmatch(due_on):
                raise ValueError(due_on)
            date.fromisoformat(due_on)
        except ValueError as exc:
            raise FinCLError(f"plan row {index} due_on must be an ISO calendar date (YYYY-MM-DD)") from exc
        if due_on < previous:
            raise FinCLError("plan rows must be in date order")
        if amount.coefficient <= 0:
            raise FinCLError(f"plan row {index} amount must be strictly positive")
        previous = due_on
    return expanded


def outstanding(connection: sqlite3.Connection, debt_id: str) -> tuple[int, int]:
    """The principal every recorded event leaves, as (coefficient, scale)."""
    events = connection.execute(
        "SELECT signed, principal_coefficient, principal_scale FROM personal_debt_events WHERE debt_id = ?",
        (debt_id,),
    ).fetchall()
    scale = max([int(e["principal_scale"]) for e in events] + [0])
    return sum(
        int(e["signed"]) * _int_at(int(e["principal_coefficient"]), int(e["principal_scale"]), scale)
        for e in events
    ), scale


def paid_since(connection: sqlite3.Connection, debt_id: str, since: str, scale: int) -> int:
    """What the debt's decrease events dated on or after ``since`` came to, at ``scale``."""
    return sum(
        _int_at(int(e["principal_coefficient"]), int(e["principal_scale"]), scale)
        for e in connection.execute(
            "SELECT principal_coefficient, principal_scale FROM personal_debt_events "
            "WHERE debt_id = ? AND signed < 0 AND effective_on >= ?",
            (debt_id, since),
        )
    )


def until_paid_rows(
    connection: sqlite3.Connection, debt_id: str, schedule: dict[str, Any], amount: ExactMoney,
    effective_from: str,
) -> list[dict[str, str]]:
    """Rows of ``amount`` on the schedule until the debt is covered, the last one
    carrying whatever is left. Repayments dated on or after ``effective_from``
    already pay the first rows, so the rows also cover what they came to."""
    owed, owed_scale = outstanding(connection, debt_id)
    if owed <= 0:
        raise FinCLError("nothing is owed on this debt, so there is nothing to schedule")
    owed += paid_since(connection, debt_id, effective_from, owed_scale)
    scale = max(owed_scale, amount.scale)
    owed = _int_at(owed, owed_scale, scale)
    each = _int_at(amount.coefficient, amount.scale, scale)
    count = -(-owed // each)
    if count > MAX_SCHEDULE_ROWS:
        raise FinCLError(f"paying {format_decimal(owed, scale)} at that amount takes more than {MAX_SCHEDULE_ROWS} payments")
    dates = series_dates(parse_schedule(schedule), count)
    return [
        {"due_on": day.isoformat(),
         "amount": format_decimal(each if index < count - 1 else owed - each * (count - 1), scale)}
        for index, day in enumerate(dates)
    ]


def schedule_from_commitment(
    connection: sqlite3.Connection, debt_id: str, commitment: str, *, reason: str
) -> list[dict[str, Any]]:
    """The draft that turns a recurring commitment into this debt's schedule.

    Repayments already recorded against the debt stay where they are: the
    schedule starts the day after the latest one. Its rows are the commitment's
    own dates from then on, at each date's expected amount (a varied amount
    where one is set), leaving out dates already paid or skipped, until the
    principal still owed is covered; the last row carries what is left. The
    commitment ends in the same record, so its dates are not forecast twice.
    """
    debt = connection.execute(
        "SELECT currency, opened_on FROM personal_debts WHERE debt_id = ?", (debt_id,)
    ).fetchone()
    row = connection.execute(
        "SELECT * FROM recurring_commitments WHERE commitment = ?", (commitment,)
    ).fetchone()
    if debt is None or row is None:
        raise FinCLError("that debt or commitment does not exist")
    if str(row["status"]) != "active":
        raise FinCLError("only an active commitment can become a debt's schedule")
    if str(row["currency"]) != str(debt["currency"]):
        raise FinCLError("the commitment and the debt are in different currencies")
    last = connection.execute(
        "SELECT max(effective_on) FROM personal_debt_events WHERE debt_id = ? AND signed < 0", (debt_id,)
    ).fetchone()[0]
    effective_from = (
        (date.fromisoformat(str(last)) + timedelta(days=1)).isoformat() if last else str(debt["opened_on"])
    )
    settled = {
        str(r["scheduled_on"])
        for r in connection.execute(
            "SELECT scheduled_on FROM recurring_occurrences WHERE commitment = ? AND bound = 1", (commitment,)
        )
    }
    varied = {
        str(r["scheduled_on"]): (int(r["amount_coefficient"]), int(r["amount_scale"]))
        for r in connection.execute(
            "SELECT scheduled_on, amount_coefficient, amount_scale FROM recurring_occurrence_amounts "
            "WHERE commitment = ?",
            (commitment,),
        )
    }
    owed, owed_scale = outstanding(connection, debt_id)
    if owed <= 0:
        raise FinCLError("nothing is owed on this debt, so there is nothing to schedule")
    expected = (int(row["expected_coefficient"]), int(row["expected_scale"]))
    scale = max([owed_scale, expected[1]] + [s for _, s in varied.values()])
    owed = _int_at(owed, owed_scale, scale)
    schedule = json.loads(row["schedule_json"])
    rows: list[dict[str, str]] = []
    left = owed
    start = date.fromisoformat(effective_from)
    while left > 0 and len(rows) < MAX_SCHEDULE_ROWS:
        batch = series_dates(schedule, MAX_SCHEDULE_ROWS, start)
        if not batch:
            break
        for day in batch:
            iso = day.isoformat()
            if iso in settled:
                continue
            coefficient, amount_scale = varied.get(iso, expected)
            amount = min(_int_at(coefficient, amount_scale, scale), left)
            rows.append({"due_on": iso, "amount": format_decimal(amount, scale)})
            left -= amount
            if left == 0 or len(rows) == MAX_SCHEDULE_ROWS:
                break
        start = batch[-1] + timedelta(days=1)
    if not rows:
        raise FinCLError("the commitment has no dates left to schedule")
    bound_dates = sorted(settled)
    day_before = (date.fromisoformat(effective_from) - timedelta(days=1)).isoformat()
    ended_on = max([day_before, *bound_dates[-1:]])
    return [
        {"kind": "PERSONAL_DEBT_SCHEDULE_SET", "anchors": {"debt": debt_id},
         "payload": {"effective_from": effective_from, "plan": {"kind": "rows", "rows": rows}, "reason": reason}},
        {"kind": "RECURRING_COMMITMENT_SET", "anchors": {"commitment": commitment},
         "payload": {
             "name": str(row["name"]), "currency": str(row["currency"]), "merchant_key": str(row["merchant_key"]),
             "account_ids": json.loads(row["account_ids_json"]),
             "seed_entry_anchors": json.loads(row["seed_anchors_json"]),
             "schedule": schedule,
             "expected_amount": format_decimal(*expected),
             "amount_tolerance": format_decimal(int(row["tolerance_coefficient"]), int(row["tolerance_scale"])),
             "status": "ended", "ended_on": ended_on, "reason": reason,
         }},
    ]
