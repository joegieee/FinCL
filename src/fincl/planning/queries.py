"""Read models for v0.6 planning surfaces.

Budget actuals are gross recognised purchase components for the budget's month
and currency (DESIGN-personal-finance §5) — principal repayment and installment
settlement handling arrive with v0.3/v0.4. An incomplete month shows observed
spending so far with no confident remaining allowance.
"""

from __future__ import annotations

import calendar
import json
from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..intervals import DateInterval
from ..queries import _ENTRY_VIEW
from ..report import _coverage_contract


def _generation(connection: Any) -> str:
    row = current_generation(connection)
    if row is None:
        raise FinCLError("The ledger has no completed generation; refresh it first")
    return str(row["generation_token"])


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def budgets_page(database: Path, *, month: str, currency: str) -> dict[str, Any]:
    if len(month) != 7 or month[4] != "-":
        raise FinCLError("month must be YYYY-MM")
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        generation = _generation(connection)
        contract = _coverage_contract(connection)
        start, end = _month_bounds(month)
        month_covered = any(i.contains_interval(DateInterval(start, end)) for i in contract.covered_intervals)
        month_past = end < date.today().isoformat()
        complete = month_covered and month_past

        revisions = connection.execute(
            "SELECT plan, revision, effective_month, name, currency, category_key, "
            "limit_coefficient, limit_scale FROM budget_revisions ORDER BY plan, effective_month, journal_seq"
        ).fetchall()
        archives = connection.execute(
            "SELECT plan, effective_month, archived FROM budget_archive_periods "
            "ORDER BY plan, effective_month, journal_seq"
        ).fetchall()

        by_plan: dict[str, list[Any]] = {}
        for row in revisions:
            by_plan.setdefault(row["plan"], []).append(row)
        archived_for: dict[str, bool] = {}
        for row in archives:
            if row["effective_month"] <= month:
                archived_for[row["plan"]] = bool(row["archived"])

        budgets: list[dict[str, Any]] = []
        for plan, rows in by_plan.items():
            effective = None
            for row in rows:
                if row["effective_month"] <= month:
                    effective = row
            if effective is None:
                continue  # budget starts after this month
            limit_cents = effective["limit_coefficient"] * (10 ** (2 - effective["limit_scale"]))
            actual_cents = _budget_actual_cents(
                connection, category_key=effective["category_key"],
                currency=effective["currency"], start=start, end=end,
            )
            archived = archived_for.get(plan, False)
            budgets.append({
                "plan": plan,
                "name": effective["name"],
                "currency": effective["currency"],
                "category_key": effective["category_key"],
                "effective_month": effective["effective_month"],
                "limit": _money(limit_cents),
                "actual": _money(actual_cents),
                "remaining": _money(limit_cents - actual_cents) if complete else None,
                "archived": archived,
                "status": (
                    "archived" if archived
                    else "settled" if complete
                    else "observed_so_far" if month_covered
                    else "coverage_unknown"
                ),
            })
        budgets.sort(key=lambda b: (b["archived"], b["name"].lower()))
        return {
            "generation": generation, "month": month, "currency": currency,
            "month_covered": month_covered, "month_complete": complete,
            "budgets": [b for b in budgets if b["currency"] == currency],
        }
    finally:
        connection.close()


def _budget_actual_cents(connection: Any, *, category_key: str | None, currency: str, start: str, end: str) -> int:
    params: dict[str, Any] = {"currency": currency, "start": start, "end": end}
    where = [
        "currency_pocket = :currency",
        "substr(effective_at, 1, 10) BETWEEN :start AND :end",
        "amount_scale <= 2",
        "eligible",
    ]
    if category_key is None:
        where.append("bucket IN ('spending', 'fees')")
        amount = "abs(amount_coefficient) * (CASE amount_scale WHEN 0 THEN 100 WHEN 1 THEN 10 ELSE 1 END) " \
                 "+ abs(coalesce(fee_coefficient, 0)) * (CASE coalesce(fee_scale, 0) WHEN 0 THEN 100 WHEN 1 THEN 10 ELSE 1 END)"
    else:
        where.append("bucket = 'spending'")
        where.append("category = :category")
        params["category"] = category_key
        amount = "abs(amount_coefficient) * (CASE amount_scale WHEN 0 THEN 100 WHEN 1 THEN 10 ELSE 1 END)"
    row = connection.execute(
        f"SELECT coalesce(SUM({amount}), 0) AS cents FROM ({_ENTRY_VIEW}) WHERE {' AND '.join(where)}",
        params,
    ).fetchone()
    return int(row["cents"])


def budget_history(database: Path, plan: str) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        generation = _generation(connection)
        revisions = [
            {
                "revision": r["revision"], "effective_month": r["effective_month"],
                "name": r["name"], "currency": r["currency"], "category_key": r["category_key"],
                "limit": _money(r["limit_coefficient"] * (10 ** (2 - r["limit_scale"]))),
                "journal_seq": r["journal_seq"],
            }
            for r in connection.execute(
                "SELECT * FROM budget_revisions WHERE plan = ? ORDER BY effective_month, journal_seq", (plan,)
            )
        ]
        if not revisions:
            raise FinCLError("No dated revisions for this budget")
        archives = [
            {"effective_month": a["effective_month"], "archived": bool(a["archived"]), "journal_seq": a["journal_seq"]}
            for a in connection.execute(
                "SELECT * FROM budget_archive_periods WHERE plan = ? ORDER BY effective_month, journal_seq", (plan,)
            )
        ]
        return {"generation": generation, "plan": plan, "revisions": revisions, "archive_periods": archives}
    finally:
        connection.close()


def goals_page(database: Path, *, as_of: str | None = None) -> dict[str, Any]:
    as_of = as_of or date.today().isoformat()
    connection = connect_readonly(database)
    try:
        generation = _generation(connection)
        details = {
            d["plan"]: d for d in connection.execute("SELECT * FROM goal_details")
        }
        archived = {
            a["plan"]: bool(a["archived"]) for a in connection.execute("SELECT * FROM goal_archive")
        }
        goals: list[dict[str, Any]] = []
        for plan, detail in details.items():
            observation = connection.execute(
                "SELECT observed_on, saved_coefficient, saved_scale FROM goal_observations "
                "WHERE plan = ? AND observed_on <= ? ORDER BY observed_on DESC, journal_seq DESC LIMIT 1",
                (plan, as_of),
            ).fetchone()
            target_cents = detail["target_coefficient"] * (10 ** (2 - detail["target_scale"]))
            saved_cents = (
                observation["saved_coefficient"] * (10 ** (2 - observation["saved_scale"]))
                if observation is not None else None
            )
            goals.append({
                "plan": plan, "name": detail["name"], "currency": detail["currency"],
                "target": _money(target_cents),
                "target_on": detail["target_on"],
                "observed_on": observation["observed_on"] if observation else None,
                "saved": _money(saved_cents) if saved_cents is not None else None,
                "remaining": _money(max(0, target_cents - saved_cents)) if saved_cents is not None else None,
                "dated_progress_known": observation is not None,
                "archived": archived.get(plan, False),
                "overdue": bool(detail["target_on"] and detail["target_on"] < as_of and (saved_cents is None or saved_cents < target_cents)),
            })
        goals.sort(key=lambda g: (g["archived"], g["name"].lower()))
        return {"generation": generation, "as_of": as_of, "goals": goals}
    finally:
        connection.close()


def recurring_page(database: Path) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        generation = _generation(connection)
        commitments = [
            {
                "commitment": c["commitment"], "name": c["name"], "currency": c["currency"],
                "merchant_key": c["merchant_key"], "status": c["status"], "ended_on": c["ended_on"],
                "schedule": json.loads(c["schedule_json"]),
                "account_ids": json.loads(c["account_ids_json"]),
                "expected_amount": _money(c["expected_coefficient"] * (10 ** (2 - c["expected_scale"]))),
                "amount_tolerance": _money(c["tolerance_coefficient"] * (10 ** (2 - c["tolerance_scale"]))),
            }
            for c in connection.execute("SELECT * FROM recurring_commitments ORDER BY name")
        ]
        dismissed = [
            d["candidate"] for d in connection.execute("SELECT candidate FROM recurring_candidate_dismissals")
        ]
        return {
            "generation": generation,
            "confirmed": [c for c in commitments if c["status"] == "active"],
            "paused_or_ended": [c for c in commitments if c["status"] != "active"],
            "dismissed_candidate_keys": sorted(dismissed),
        }
    finally:
        connection.close()


def recurring_occurrences(database: Path, commitment: str, *, start: str, end: str) -> dict[str, Any]:
    from .schedules import occurrences

    connection = connect_readonly(database)
    try:
        generation = _generation(connection)
        row = connection.execute(
            "SELECT * FROM recurring_commitments WHERE commitment = ?", (commitment,)
        ).fetchone()
        if row is None:
            raise FinCLError("Commitment not found")
        schedule = json.loads(row["schedule_json"])
        scheduled = occurrences(schedule, date.fromisoformat(start), date.fromisoformat(end))
        associations = {
            a["scheduled_on"]: a["entry_anchor"]
            for a in connection.execute(
                "SELECT scheduled_on, entry_anchor FROM recurring_occurrences WHERE commitment = ?",
                (commitment,),
            )
        }
        today = date.today().isoformat()
        items: list[dict[str, Any]] = []
        for occ in scheduled:
            iso = occ.isoformat()
            if iso in associations:
                state = "explicitly_skipped" if associations[iso] is None else "associated"
                entry = associations[iso]
            elif row["status"] == "ended":
                state, entry = "ended", None
            elif row["status"] == "paused":
                state, entry = "paused", None
            elif iso > today:
                state, entry = "upcoming_estimate", None
            else:
                state, entry = "missing_in_window", None
            items.append({"scheduled_on": iso, "state": state, "entry": entry})
        return {"generation": generation, "commitment": commitment, "occurrences": items}
    finally:
        connection.close()
