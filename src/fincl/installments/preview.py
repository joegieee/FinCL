"""Full-state pre-append check for the ``INSTALLMENT_*`` kinds.

``validate_installment_payload`` covers shape; this applies the pending decision
inside a SAVEPOINT and runs the real ``resolve_installments`` pass, so a durable
record is never made for a decision that could not fully replay (plan exists,
occurrence anchors resolve, one active plan per occurrence, component partition).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import FinCLError
from .models import (
    INSTALLMENT_ALLOCATION_RETIRE,
    INSTALLMENT_ALLOCATION_SET,
    INSTALLMENT_PLAN_RETIRE,
    INSTALLMENT_PLAN_SET,
    INSTALLMENT_SETTLEMENT_RETIRE,
    INSTALLMENT_SETTLEMENT_SET,
)
from .projection import apply_installment, resolve_installments
from .validation import validate_installment_payload

_PREVIEW_SEQ = 1 << 40

_NEEDS_PLAN = frozenset(
    {
        INSTALLMENT_PLAN_RETIRE,
        INSTALLMENT_ALLOCATION_SET,
        INSTALLMENT_ALLOCATION_RETIRE,
        INSTALLMENT_SETTLEMENT_SET,
        INSTALLMENT_SETTLEMENT_RETIRE,
    }
)


def preview_installment(
    connection: sqlite3.Connection,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    *,
    resolve_anchor: Any,
) -> None:
    validate_installment_payload(decision_kind, anchors, payload)

    plan_exists = bool(
        "plan" in anchors
        and connection.execute(
            "SELECT 1 FROM installment_plans WHERE plan_id = ?", (anchors["plan"],)
        ).fetchone()
    )
    if decision_kind == INSTALLMENT_PLAN_SET and plan_exists:
        current = connection.execute(
            "SELECT currency FROM installment_plans WHERE plan_id = ?",
            (anchors["plan"],),
        ).fetchone()
        if current is not None and str(current["currency"]) != payload["currency"]:
            raise FinCLError("an installment plan's currency cannot change")
    if decision_kind in _NEEDS_PLAN and not plan_exists:
        raise FinCLError("that installment plan does not exist")

    if decision_kind == INSTALLMENT_PLAN_SET:
        for item in payload["occurrences"]:
            if resolve_anchor(str(item["entry_anchor"])) is None:
                raise FinCLError(
                    "occurrence source "
                    f"{item['entry_anchor']} does not resolve to one selected entry"
                )
        conversion = payload.get("conversion_anchor")
        if conversion is not None:
            entry_id = resolve_anchor(str(conversion))
            if entry_id is None:
                raise FinCLError(
                    f"conversion credit {conversion} does not resolve to one selected entry"
                )
            entry = connection.execute(
                "SELECT account_id, currency_pocket, amount_coefficient "
                "FROM ledger_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if entry is None or int(entry["amount_coefficient"]) <= 0:
                raise FinCLError("the conversion credit must be a credit (an inflow) on the card")
            if (str(entry["account_id"]), str(entry["currency_pocket"])) != (
                payload["card_account_id"],
                payload["currency"],
            ):
                raise FinCLError(
                    "the conversion credit must be on the plan's own card and currency"
                )
    if decision_kind == INSTALLMENT_ALLOCATION_SET:
        for item in payload["allocations"]:
            if resolve_anchor(str(item["entry_anchor"])) is None:
                raise FinCLError(
                    "purchase allocation source "
                    f"{item['entry_anchor']} does not resolve to one selected entry"
                )
    if (
        decision_kind == INSTALLMENT_SETTLEMENT_SET
        and resolve_anchor(str(payload["source_entry_anchor"])) is None
    ):
        raise FinCLError(
            "the settlement source repayment does not resolve to one selected entry"
        )

    connection.execute("SAVEPOINT installment_preview")
    try:
        apply_installment(
            connection,
            decision_kind=decision_kind,
            anchors=anchors,
            payload=payload,
            journal_seq=_PREVIEW_SEQ,
            batch_index=None,
            resolve_anchor=resolve_anchor,
        )
        resolve_installments(connection)
    finally:
        connection.execute("ROLLBACK TO installment_preview")
        connection.execute("RELEASE installment_preview")
