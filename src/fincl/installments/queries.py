"""Snapshot-only reads for the Installments screens (v0.4 slice 3).

Known and unknown principal / term are both shown; an unknown principal yields
no invented remaining liability. The projected schedule is presented distinctly
from the actual observed occurrences. "Recognized purchase cost" and "spread
allocation" are explicit alternative lenses, never blended into one number.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..money import format_decimal
from .models import LENS_RECOGNIZED_PURCHASE_COST, LENS_SPREAD_ALLOCATION


def _check_as_of(as_of: str | None) -> str:
    if as_of is None:
        return date.today().isoformat()
    try:
        return date.fromisoformat(as_of).isoformat()
    except ValueError as exc:
        raise FinCLError("as_of must be an ISO calendar date (YYYY-MM-DD)") from exc


def _generation(connection: Any, expected: str | None) -> str:
    generation = current_generation(connection)
    if generation is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    token = str(generation["generation_token"])
    if expected and expected != token:
        raise FinCLError("STALE_GENERATION: reload before continuing")
    return token


def _sum_scaled(rows: list[Any], coeff_key: str, scale_key: str) -> tuple[int, int]:
    scale = max((int(r[scale_key]) for r in rows), default=0)
    total = sum(
        int(r[coeff_key]) * 10 ** (scale - int(r[scale_key])) for r in rows
    )
    return total, scale


def _money(coefficient: int, scale: int, currency: str) -> dict[str, Any]:
    return {"value": format_decimal(coefficient, scale), "currency": currency}


def _plan_progress(connection: Any, plan: Any) -> dict[str, Any]:
    currency = str(plan["currency"])
    occurrences = connection.execute(
        "SELECT amount_coefficient, amount_scale, bound FROM installment_occurrences "
        "WHERE plan_id = ?",
        (plan["plan_id"],),
    ).fetchall()
    schedule = connection.execute(
        "SELECT principal_coefficient, principal_scale, fee_coefficient, fee_scale "
        "FROM installment_schedule WHERE plan_id = ?",
        (plan["plan_id"],),
    ).fetchall()
    settlements = connection.execute(
        "SELECT principal_coefficient, principal_scale FROM installment_settlements "
        "WHERE plan_id = ? AND bound = 1",
        (plan["plan_id"],),
    ).fetchall()

    observed_c, observed_s = _sum_scaled(
        occurrences, "amount_coefficient", "amount_scale"
    )
    scheduled_c, scheduled_s = _sum_scaled(
        schedule, "principal_coefficient", "principal_scale"
    )
    settled_c, settled_s = _sum_scaled(
        settlements, "principal_coefficient", "principal_scale"
    )

    known_principal: dict[str, Any] | None = None
    remaining: dict[str, Any] | None = None
    if plan["principal_coefficient"] is not None:
        known_principal = _money(
            int(plan["principal_coefficient"]), int(plan["principal_scale"]), currency
        )
        # Remaining obligation is determinable only with a known principal: the
        # recognized principal less the principal observed on the card and less
        # any settled principal.
        common = max(int(plan["principal_scale"]), observed_s, settled_s)
        principal_minor = int(plan["principal_coefficient"]) * 10 ** (
            common - int(plan["principal_scale"])
        )
        observed_minor = observed_c * 10 ** (common - observed_s)
        settled_minor = settled_c * 10 ** (common - settled_s)
        remaining_minor = principal_minor - observed_minor - settled_minor
        remaining = _money(max(remaining_minor, 0), common, currency)

    return {
        "known_principal": known_principal,
        "term_months": plan["term_months"],
        "observed_principal_paid": _money(observed_c, observed_s, currency),
        "scheduled_principal": _money(scheduled_c, scheduled_s, currency),
        "settled_principal": _money(settled_c, settled_s, currency),
        "remaining_obligation": remaining,
        "occurrence_count": len(occurrences),
        "occurrences_bound": sum(int(o["bound"]) for o in occurrences),
    }


def _plan_shape(connection: Any, plan: Any) -> dict[str, Any]:
    settled = (
        connection.execute(
            "SELECT count(*) FROM installment_settlements WHERE plan_id = ? AND bound = 1",
            (plan["plan_id"],),
        ).fetchone()[0]
        > 0
    )
    return {
        "plan_id": str(plan["plan_id"]),
        "card_account_id": (
            str(plan["card_account_id"])
            if plan["card_account_id"] is not None
            else None
        ),
        "currency": str(plan["currency"]),
        "plan_reference": plan["plan_reference"],
        "merchant_label": str(plan["merchant_label"]),
        "opened_on": str(plan["opened_on"]),
        "status": str(plan["status"]),
        "effective_status": "SETTLED" if settled else str(plan["status"]),
        "interpretation_status": (
            "confirmed" if str(plan["status"]) == "CONFIRMED" else "proposed"
        ),
        "bound": bool(plan["bound"]),
        "dangling_reason": plan["dangling_reason"],
        "progress": _plan_progress(connection, plan),
    }


def installments_page(
    database: Path,
    *,
    card: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    expected: str | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    if status is not None and status not in {"PROPOSED", "CONFIRMED", "SETTLED"}:
        raise FinCLError("status must be PROPOSED, CONFIRMED or SETTLED")
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        clauses: list[str] = []
        params: list[Any] = []
        if card is not None:
            clauses.append("card_account_id = ?")
            params.append(card)
        if cursor is not None:
            clauses.append("plan_id > ?")
            params.append(cursor)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        plans = connection.execute(
            f"SELECT * FROM installment_plans{where} ORDER BY plan_id LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        next_cursor = str(plans[limit]["plan_id"]) if len(plans) > limit else None
        plans = plans[:limit]
        shapes = [_plan_shape(connection, p) for p in plans]
        if status is not None:
            shapes = [s for s in shapes if s["effective_status"] == status]
        return {
            "generation": token,
            "plans": shapes,
            "next_cursor": next_cursor,
        }
    finally:
        connection.close()


def installment_detail(
    database: Path,
    plan_id: str,
    *,
    as_of: str | None = None,
    expected: str | None = None,
) -> dict[str, Any]:
    as_of = _check_as_of(as_of)
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        plan = connection.execute(
            "SELECT * FROM installment_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise FinCLError("that installment plan does not exist")
        currency = str(plan["currency"])
        shape = _plan_shape(connection, plan)

        occurrences = [
            {
                "occurrence_id": str(r["occurrence_id"]),
                "occurrence_index": r["occurrence_index"],
                "occurrence_count": r["occurrence_count"],
                "entry_anchor": str(r["entry_anchor"]),
                "effective_on": str(r["effective_on"]),
                "date_precision": str(r["date_precision"]),
                "amount": _money(
                    int(r["amount_coefficient"]), int(r["amount_scale"]), currency
                ),
                "bound": bool(r["bound"]),
            }
            for r in connection.execute(
                "SELECT * FROM installment_occurrences WHERE plan_id = ? "
                "ORDER BY effective_on, occurrence_id",
                (plan_id,),
            )
        ]
        schedule = [
            {
                "schedule_index": int(r["schedule_index"]),
                "due_on": str(r["due_on"]),
                "principal": _money(
                    int(r["principal_coefficient"]), int(r["principal_scale"]), currency
                ),
                "fee": _money(int(r["fee_coefficient"]), int(r["fee_scale"]), currency),
                "rounding_remainder": _money(
                    int(r["rounding_remainder_coefficient"]),
                    int(r["rounding_remainder_scale"]),
                    currency,
                ),
                "is_projected": bool(r["is_projected"]),
            }
            for r in connection.execute(
                "SELECT * FROM installment_schedule WHERE plan_id = ? ORDER BY schedule_index",
                (plan_id,),
            )
        ]
        allocations = [
            {
                "entry_anchor": str(r["entry_anchor"]),
                "component_role": str(r["component_role"]),
                "amount": _money(
                    int(r["amount_coefficient"]), int(r["amount_scale"]), currency
                ),
                "bound": bool(r["bound"]),
            }
            for r in connection.execute(
                "SELECT * FROM installment_purchase_allocations WHERE plan_id = ? "
                "ORDER BY seq_in_plan",
                (plan_id,),
            )
        ]
        settlements = [
            {
                "settlement_id": str(r["settlement_id"]),
                "settled_on": str(r["settled_on"]),
                "source_entry_anchor": str(r["source_entry_anchor"]),
                "principal": _money(
                    int(r["principal_coefficient"]), int(r["principal_scale"]), currency
                ),
                "fee": _money(int(r["fee_coefficient"]), int(r["fee_scale"]), currency),
                "waiver": _money(
                    int(r["waiver_coefficient"]), int(r["waiver_scale"]), currency
                ),
                "bound": bool(r["bound"]),
            }
            for r in connection.execute(
                "SELECT * FROM installment_settlements WHERE plan_id = ? ORDER BY settled_on",
                (plan_id,),
            )
        ]

        financing_fee = (
            _money(
                int(plan["financing_fee_coefficient"]),
                int(plan["financing_fee_scale"]),
                currency,
            )
            if plan["financing_fee_coefficient"] is not None
            else None
        )
        views = {
            # Economic-cost lens: the recognized principal at plan open plus the
            # separately evidenced financing fee, counted once.
            LENS_RECOGNIZED_PURCHASE_COST: {
                "recognized_principal": shape["progress"]["known_principal"],
                "financing_fee": financing_fee,
                "note": (
                    "Recognition components belong only to economic-cost views; a "
                    "plan purchase in card closing liability is not an additional "
                    "net-worth liability."
                ),
            },
            # Cash-flow lens: each observed occurrence as it posts to the card.
            LENS_SPREAD_ALLOCATION: {
                "observed_occurrences": occurrences,
                "projected_schedule": schedule,
                "note": (
                    "Projected schedule rows are distinct from actual observed "
                    "installments even after their due date passes."
                ),
            },
        }

        return {
            "generation": token,
            "as_of": as_of,
            "plan": shape,
            "source_occurrences": occurrences,
            "purchase_allocations": allocations,
            "schedule": schedule,
            "settlements": settlements,
            "views": views,
        }
    finally:
        connection.close()


__all__ = ["installments_page", "installment_detail"]
