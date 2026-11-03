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
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .. import clock
from ..coverage import pocket_covered_intervals
from ..db import connect_readonly
from ..decision_journal import default_journal_path, read_records
from ..errors import FinCLError
from ..generation import current_generation
from ..intervals import DateInterval
from ..money import ExactMoney
from ..queries import _ENTRY_VIEW
from ..read_cache import per_request
from ..report import _coverage_contract
from .budgets import _MONTH
from .detection import candidates as detect_candidates


def _to_cents(text: str) -> int:
    money = ExactMoney.parse(text)
    if money.scale > 2:
        raise FinCLError("Amounts allow at most two decimal places")
    return money.coefficient * (10 ** (2 - money.scale))


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
    if not _MONTH.fullmatch(month):
        raise FinCLError("month must be YYYY-MM")
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        generation = _generation(connection)
        start, end = _month_bounds(month)
        month_past = end < clock.today().isoformat()

        def pocket_covered(pocket: str) -> bool:
            return any(i.contains_interval(DateInterval(start, end))
                       for i in pocket_covered_intervals(connection, pocket))

        month_covered = pocket_covered(currency)
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
        legacy = {}
        for record in read_records(default_journal_path(database)):
            if record.decision_kind == "PLAN_SET" and record.payload.get("type") == "budget":
                legacy[record.anchors["plan"]] = record.payload
        for plan, payload in legacy.items():
            if plan in by_plan or payload["currency"] != currency:
                continue
            legacy_category = payload.get("category")
            category_key = legacy_category if isinstance(legacy_category, str) and legacy_category else None
            actual = _budget_actual_cents(connection, category_key=category_key,
                                          currency=currency, start=start, end=end)
            limit = _to_cents(str(payload["target"]))
            budgets.append({"plan": plan, "name": payload["name"], "currency": currency,
                            "category_key": category_key, "effective_month": None,
                            "limit": _money(limit), "actual": _money(actual),
                            "remaining": _money(limit-actual) if complete else None,
                            "over_limit": actual > limit if complete else None, "archived": False,
                            "legacy_undated": True, "status": "settled" if complete else "observed_so_far" if month_covered else "coverage_unknown"})
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
            plan_covered = pocket_covered(effective["currency"])
            plan_complete = plan_covered and month_past
            budgets.append({
                "plan": plan,
                "name": effective["name"],
                "currency": effective["currency"],
                "category_key": effective["category_key"],
                "effective_month": effective["effective_month"],
                "limit": _money(limit_cents),
                "actual": _money(actual_cents),
                "remaining": _money(limit_cents - actual_cents) if plan_complete else None,
                "over_limit": actual_cents > limit_cents if plan_complete else None,
                "archived": archived,
                "legacy_undated": False,
                "status": (
                    "archived" if archived
                    else "settled" if plan_complete
                    else "observed_so_far" if plan_covered
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
        "eligible",
    ]
    if category_key is None:
        where.append("bucket IN ('spending', 'fees')")
    else:
        where.append("bucket = 'spending'")
        where.append("category = :category")
        params["category"] = category_key
    rows = connection.execute(
        "SELECT amount_coefficient, amount_scale, coalesce(fee_coefficient, 0) AS fee_coefficient, "
        f"coalesce(fee_scale, 0) AS fee_scale FROM ({_ENTRY_VIEW}) WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    total = sum((Decimal(abs(row["amount_coefficient"])).scaleb(-row["amount_scale"]) for row in rows), Decimal(0))
    if category_key is None:
        total += sum((Decimal(abs(row["fee_coefficient"])).scaleb(-row["fee_scale"]) for row in rows), Decimal(0))
    return int((total * 100).to_integral_value(rounding=ROUND_HALF_UP))


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


def _month_add(month: str, delta: int) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _iter_months(start: str, end: str) -> list[str]:
    months, cur = [], start
    while cur <= end:
        months.append(cur)
        cur = _month_add(cur, 1)
    return months


def budget_backdate_preview(
    database: Path, plan: str, *, effective_month: str, limit: str,
    category_key: str | None, currency: str,
) -> dict[str, Any]:
    """Every month a new revision effective from ``effective_month`` would change,
    with the limit and remaining before and after. A revision never silently
    rewrites a previously displayed historical limit — this is what the caller
    shows first (DESIGN-personal-finance §5)."""
    if not _MONTH.fullmatch(effective_month):
        raise FinCLError("effective_month must be YYYY-MM")
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        _generation(connection)
        pocket_intervals = pocket_covered_intervals(connection, currency)
        rows = connection.execute(
            "SELECT effective_month, category_key, limit_coefficient, limit_scale, currency "
            "FROM budget_revisions WHERE plan = ? ORDER BY effective_month, journal_seq", (plan,)
        ).fetchall()
        new_cents = _to_cents(limit)
        today_month = clock.today().strftime("%Y-%m")
        later = [r["effective_month"] for r in rows if r["effective_month"] > effective_month]
        window_end = _month_add(min(later), -1) if later else today_month
        if effective_month > window_end:
            return {"plan": plan, "affected_months": [], "note": "future revision — no historical month is affected"}

        def effective_at(month: str) -> tuple[int, str | None] | None:
            active = None
            for r in rows:
                if r["effective_month"] <= month:
                    active = r
            if active is None:
                return None
            return active["limit_coefficient"] * (10 ** (2 - active["limit_scale"])), active["category_key"]

        affected: list[dict[str, Any]] = []
        for month in _iter_months(effective_month, window_end):
            prev = effective_at(month)
            prev_limit = prev[0] if prev else None
            prev_cat = prev[1] if prev else None
            if prev_limit == new_cents and prev_cat == category_key:
                continue
            start, end = _month_bounds(month)
            covered = any(i.contains_interval(DateInterval(start, end)) for i in pocket_intervals)
            actual = _budget_actual_cents(connection, category_key=category_key, currency=currency, start=start, end=end)
            affected.append({
                "month": month,
                "previous_limit": _money(prev_limit) if prev_limit is not None else None,
                "new_limit": _money(new_cents),
                "actual": _money(actual),
                "previous_remaining": _money(prev_limit - actual) if (prev_limit is not None and covered and end < clock.today().isoformat()) else None,
                "new_remaining": _money(new_cents - actual) if (covered and end < clock.today().isoformat()) else None,
            })
        return {"plan": plan, "affected_months": affected}
    finally:
        connection.close()


def goals_page(database: Path, *, as_of: str | None = None) -> dict[str, Any]:
    as_of = as_of or clock.today().isoformat()
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
        legacy = {}
        for record in read_records(default_journal_path(database)):
            if record.decision_kind == "PLAN_SET" and record.payload.get("type") == "goal":
                legacy[record.anchors["plan"]] = record.payload
        for plan, payload in legacy.items():
            if plan not in details:
                target = _to_cents(str(payload["target"]))
                goals.append({"plan": plan, "name": payload["name"], "currency": payload["currency"],
                              "target": _money(target), "target_on": None, "observed_on": None,
                              "saved": None, "remaining": None, "dated_progress_known": False,
                              "legacy_undated_saved": str(payload.get("saved", "0")),
                              "archived": False, "overdue": False})
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
                "legacy_undated_saved": None,
                "archived": archived.get(plan, False),
                "overdue": bool(detail["target_on"] and detail["target_on"] < as_of and (saved_cents is None or saved_cents < target_cents)),
            })
        goals.sort(key=lambda g: (g["archived"], g["name"].lower()))
        return {"generation": generation, "as_of": as_of, "goals": goals}
    finally:
        connection.close()


def goal_detail(database: Path, plan: str, *, as_of: str | None = None) -> dict[str, Any]:
    page = goals_page(database, as_of=as_of)
    goal = next((g for g in page["goals"] if g["plan"] == plan), None)
    if goal is None:
        raise FinCLError("Goal not found")
    return {"generation": page["generation"], "as_of": page["as_of"], **goal}


def goal_observations(database: Path, plan: str) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        generation = _generation(connection)
        rows = connection.execute("SELECT observation, observed_on, saved_coefficient, saved_scale, journal_seq FROM goal_observations WHERE plan=? ORDER BY observed_on, journal_seq", (plan,)).fetchall()
        return {"generation": generation, "plan": plan, "observations": [
            {"observation": r["observation"], "observed_on": r["observed_on"],
             "saved": _money(r["saved_coefficient"] * 10 ** (2-r["saved_scale"])), "journal_seq": r["journal_seq"]}
            for r in rows]}
    finally:
        connection.close()


def goal_scenario(
    database: Path, plan: str, *, as_of: str | None, contribution_dates: list[str],
) -> dict[str, Any]:
    """"Monthly amount to reach target": the positive remainder divided by the
    count of user-selected contribution dates after ``as_of`` and on/before the
    target date, rounded up to the currency display scale. No interest or
    investment return is assumed; this creates no transfer or reservation
    (DESIGN-personal-finance §6)."""
    as_of = as_of or clock.today().isoformat()
    connection = connect_readonly(database)
    try:
        generation = _generation(connection)
        detail = connection.execute("SELECT * FROM goal_details WHERE plan = ?", (plan,)).fetchone()
        if detail is None:
            raise FinCLError("Goal not found")
        observation = connection.execute(
            "SELECT saved_coefficient, saved_scale FROM goal_observations "
            "WHERE plan = ? AND observed_on <= ? ORDER BY observed_on DESC, journal_seq DESC LIMIT 1",
            (plan, as_of),
        ).fetchone()
        target_cents = detail["target_coefficient"] * (10 ** (2 - detail["target_scale"]))
        saved_cents = (
            observation["saved_coefficient"] * (10 ** (2 - observation["saved_scale"]))
            if observation is not None else 0
        )
        remaining_cents = max(0, target_cents - saved_cents)
        target_on = detail["target_on"]
        eligible = sorted(
            d for d in contribution_dates
            if len(d) == 10 and d > as_of and (target_on is None or d <= target_on)
        )
        if remaining_cents == 0:
            status = "reached"
        elif not eligible:
            status = "overdue" if (target_on and target_on <= as_of) else "no_future_contribution_date"
        else:
            status = "scheduled"
        monthly = _money(-(-remaining_cents // len(eligible))) if eligible and remaining_cents else None
        return {
            "generation": generation, "plan": plan, "as_of": as_of, "currency": detail["currency"],
            "remaining": _money(remaining_cents),
            "contribution_dates_considered": eligible,
            "monthly_amount": monthly,
            "status": status,
            "assumes_no_investment_return": True,
        }
    finally:
        connection.close()


def recurring_page(database: Path) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return recurring_page_from(connection)
    finally:
        connection.close()


@per_request
def recurring_page_from(connection: Any) -> dict[str, Any]:
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
    found = detect_candidates(connection, dismissed=frozenset(dismissed))
    bills = found["bill"]
    income = found["income"]
    return {
        "generation": generation,
        # Bill candidates keep the historical key so an existing dismissal
        # still matches; income streams are a separate list because they are
        # confirmed against different evidence (Stage 2B requirement 2).
        "candidates": bills,
        "bill_candidates": bills,
        "income_candidates": income,
        "income_exclusions": found["income_exclusions"],
        "confirmed": [c for c in commitments if c["status"] == "active"],
        "paused_or_ended": [c for c in commitments if c["status"] != "active"],
        "dismissed_candidate_keys": sorted(dismissed),
    }


def recurring_occurrences(database: Path, commitment: str, *, start: str, end: str) -> dict[str, Any]:
    from .recurring import occurrence_amounts
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
        usual = _money(row["expected_coefficient"] * 10 ** (2 - row["expected_scale"]))
        varied = occurrence_amounts(connection, commitment)
        today = clock.today().isoformat()
        contract = _coverage_contract(connection)
        claimed = {r["entry_anchor"]: r["commitment"] for r in connection.execute(
            "SELECT commitment, entry_anchor FROM recurring_occurrences WHERE entry_anchor IS NOT NULL")}
        items: list[dict[str, Any]] = []
        for occ in scheduled:
            iso = occ.isoformat()
            expected_amount = varied[iso]["amount"] if iso in varied else usual
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
                window = 1 if schedule["kind"] == "weekly" else 3
                lo, hi = date.fromordinal(occ.toordinal()-window).isoformat(), date.fromordinal(occ.toordinal()+window).isoformat()
                if not any(i.contains_interval(DateInterval(lo, hi)) for i in contract.covered_intervals):
                    state, entry = "unverifiable_coverage", None
                else:
                    expected = int(Decimal(expected_amount) * 100)
                    tolerance = row["tolerance_coefficient"] * 10 ** (2-row["tolerance_scale"])
                    accounts = json.loads(row["account_ids_json"])
                    matches = connection.execute(
                        f"SELECT natural_anchor FROM ({_ENTRY_VIEW}) WHERE eligible AND bucket='spending' AND currency_pocket=? AND merchant=? AND substr(effective_at,1,10) BETWEEN ? AND ? AND (?=0 OR account_id IN (SELECT value FROM json_each(?))) AND abs(abs(amount_coefficient) * (CASE amount_scale WHEN 0 THEN 100 WHEN 1 THEN 10 ELSE 1 END)-?)<=?",
                        (row["currency"], row["merchant_key"], lo, hi, len(accounts), json.dumps(accounts), expected, tolerance)).fetchall()
                    available = [m["natural_anchor"] for m in matches if claimed.get(m["natural_anchor"], commitment) == commitment]
                    conflicted = [m["natural_anchor"] for m in matches if claimed.get(m["natural_anchor"]) not in (None, commitment)]
                    if conflicted:
                        state, entry = "conflict", None
                    elif len(available) == 1:
                        state, entry = "observed", available[0]
                    elif len(available) > 1:
                        state, entry = "ambiguous", None
                    else:
                        state, entry = "missing_in_covered_window", None
            items.append({
                "scheduled_on": iso, "state": state, "entry": entry,
                "expected_amount": expected_amount,
                "amount_reason": varied[iso]["reason"] if iso in varied else None,
            })
        return {"generation": generation, "commitment": commitment, "occurrences": items}
    finally:
        connection.close()
