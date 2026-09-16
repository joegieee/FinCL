"""Snapshot-only reads for the Installments screens (v0.4 slice 3).

Known and unknown principal / term are both shown; an unknown principal yields
no invented remaining liability. The projected schedule is presented distinctly
from the actual observed occurrences. "Recognized purchase cost" and "spread
allocation" are explicit alternative lenses, never blended into one number.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import date
from fractions import Fraction
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


def _decode_entry_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if {"at", "anchor"} <= set(payload) and all(
            isinstance(payload[key], str) for key in ("at", "anchor")
        ):
            return payload["at"], payload["anchor"]
    except (binascii.Error, ValueError, TypeError):
        pass
    raise FinCLError("The page cursor is not valid; reload the list")


def _encode_entry_cursor(effective_at: str, entry_anchor: str) -> str:
    payload = {"at": effective_at, "anchor": entry_anchor}
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def _open_decimal(remaining: Fraction, scale: int) -> str:
    scaled = remaining * (10**scale)
    return format_decimal(int(scaled), scale) if scaled.denominator == 1 else str(remaining)


def entry_candidates(
    database: Path,
    *,
    plan_id: str,
    purpose: str = "purchase",
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
    expected: str | None = None,
) -> dict[str, Any]:
    """Selected entries that could back a purchase attribution or a settlement.

    Both purposes want an outflow in the plan's currency that some other domain
    has not already claimed, so they share one query and differ only in scope.

    ``purchase`` is the original charge the plan splits, which by definition sat
    on the plan's own card, so it is scoped to that account. ``settlement`` is
    whatever paid the plan off, which is commonly a transfer from a bank account
    rather than a charge on the card, so it is not account-scoped -- narrowing it
    to the card would hide the usual case.

    Each row carries the amount still unclaimed for both components, from the
    same ``analysis.allocations`` registry every other domain answers to, so a
    source already spent elsewhere cannot be offered here as if it were free.

    Instalment lines are withheld from both purposes: those already bound as an
    occurrence of a plan, and those the statement itself prints as an instalment
    ("Split Payment 7/12 Instalment") whose chain nobody has confirmed yet. An
    instalment repays a purchase; it is never the purchase, and never an early
    settlement of one. The claim registry does not track occurrences, so such a
    line still reads as fully unclaimed, and the write path does not refuse it --
    without this the same statement line could be counted once as an instalment
    and again as the cost it repays. The printed block is the same evidence
    `installments.candidates` trusts to propose a chain, applied consistently.
    """
    from ..analysis.allocations import open_residual

    if purpose not in {"purchase", "settlement"}:
        raise FinCLError("purpose must be 'purchase' or 'settlement'")
    limit = max(1, min(int(limit), 200))
    connection = connect_readonly(database)
    try:
        connection.execute("BEGIN")
        token = _generation(connection, expected)
        plan = connection.execute(
            "SELECT card_account_id, currency, opened_on FROM installment_plans "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if plan is None:
            raise FinCLError("that installment plan does not exist")

        params: list[Any] = [str(plan["currency"])]
        clauses = ""
        if purpose == "purchase" and plan["card_account_id"]:
            clauses += " AND e.account_id = ?"
            params.append(str(plan["card_account_id"]))
        if search:
            clauses += (
                " AND instr(lower(COALESCE(r.description_raw, d.description_raw, '')),"
                " lower(?)) > 0"
            )
            params.append(search)
        keyset = _decode_entry_cursor(cursor)
        if keyset is not None:
            clauses += " AND (e.effective_at < ? OR (e.effective_at = ? AND e.natural_anchor > ?))"
            params.extend([keyset[0], keyset[0], keyset[1]])

        rows = connection.execute(
            f"""
            SELECT e.natural_anchor, e.effective_at, e.account_id, e.economic_direction,
                   e.amount_coefficient, e.amount_scale, e.currency_pocket,
                   COALESCE(r.description_raw, d.description_raw) AS description_raw
            FROM ledger_entries e
            JOIN extraction_runs x
              ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
            LEFT JOIN raw_records r ON r.id = e.raw_record_id
            LEFT JOIN document_records d ON d.id = e.document_record_id
            LEFT JOIN entry_categories c ON c.entry_id = e.id
            WHERE e.currency_pocket = ? AND e.economic_direction = 'OUT'
              AND (c.group_name IS NULL OR c.group_name != 'Internal'){clauses}
            ORDER BY e.effective_at DESC, e.natural_anchor ASC
            LIMIT ?
            """,
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            _encode_entry_cursor(
                str(rows[-1]["effective_at"]), str(rows[-1]["natural_anchor"])
            )
            if has_more and rows
            else None
        )

        occurrence_anchors = {
            str(row["entry_anchor"])
            for row in connection.execute(
                "SELECT entry_anchor FROM installment_occurrences"
            ).fetchall()
        }
        occurrence_anchors.update(
            str(row["natural_anchor"])
            for row in connection.execute(
                """
                SELECT e.natural_anchor
                FROM ledger_entries e
                JOIN document_records r ON r.id = e.document_record_id
                JOIN extraction_runs x
                  ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
                WHERE r.raw_fields_json LIKE '%"installment"%'
                """
            ).fetchall()
        )

        candidates = []
        for row in rows:
            scale = int(row["amount_scale"])
            anchor = str(row["natural_anchor"])
            if anchor in occurrence_anchors:
                continue
            open_amount = open_residual(connection, anchor, role="amount")
            open_fee = open_residual(connection, anchor, role="fee")
            if open_amount <= 0 and open_fee <= 0:
                continue  # every component already claimed; not a usable source
            candidates.append(
                {
                    "entry_anchor": anchor,
                    "effective_at": str(row["effective_at"]),
                    "account_id": str(row["account_id"]),
                    "currency": str(row["currency_pocket"]),
                    "amount": format_decimal(abs(int(row["amount_coefficient"])), scale),
                    "open_amount": _open_decimal(open_amount, scale),
                    "open_fee": _open_decimal(open_fee, scale),
                    "description": row["description_raw"],
                }
            )
        return {
            "generation": token,
            "plan_id": plan_id,
            "purpose": purpose,
            "currency": str(plan["currency"]),
            "card_account_id": plan["card_account_id"],
            "candidates": candidates,
            "next_cursor": next_cursor,
        }
    finally:
        connection.close()


__all__ = ["installments_page", "installment_detail", "entry_candidates"]
