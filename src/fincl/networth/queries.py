"""Stage 5B — assemble a tracked net-worth subtotal from existing reads.

The composition rule that keeps this honest: every contributing figure is taken
from exactly one upstream read.  Cash comes from the position read, personal
debts and receivables from the obligations read, and existing card liability
from the card cash-conversion contract.  The obligations read also carries card
statement dues and installment rows — those are ignored here, because card
liability already has one authority and an installment that settles inside a
statement due is inside that liability once already.

``assemble_components`` and ``summarise`` are pure so the double-count rules can
be tested without a ledger; ``net_worth_page`` is the I/O wrapper.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from ..cards.cash_conversion import card_cash_conversion_from
from ..db import connect_readonly
from ..errors import FinCLError
from ..obligations.queries import obligations_page_from
from ..position.queries import position_page_from

_CENT = Decimal("0.01")

_SUBTOTAL_NOTE = (
    "tracked subtotal only — the positions this ledger can support, not a "
    "complete statement of personal wealth"
)
_MANUAL_NOTE = (
    "manual values keep their own basis and are reported here separately; they "
    "are not part of the evidence-only tracked subtotal"
)


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _as_of(value: str | None, today: date) -> str:
    if value is None:
        return today.isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise FinCLError("as_of must be an ISO calendar date (YYYY-MM-DD)") from exc


def _is_card_pocket(pocket: dict[str, Any]) -> bool:
    reason = (pocket.get("excluded_reason") or "").lower()
    source = (pocket.get("source_type") or "").upper()
    return "credit-card" in reason or "card" in reason or source.endswith("_CARD")


def assemble_components(
    *,
    position: dict[str, Any],
    obligations: dict[str, Any],
    cards: dict[str, Any],
    debt_flags: dict[str, dict[str, bool]],
) -> dict[str, Any]:
    """Flatten the three reads into one list of net-worth components.

    Each component names its side (asset / liability), its currency, its basis
    (evidence / manual), whether it is counted into a subtotal line, and — when
    it is not — why.  A missing figure is ``amount = None`` and never a zero.
    """
    components: list[dict[str, Any]] = []

    # --- cash pockets, from the position read -------------------------------
    for pocket in position["pockets"]:
        currency = pocket["currency_pocket"]
        component_id = f"cash:{pocket['account_id']}:{currency}"
        label = pocket.get("display_name") or pocket["account_id"]
        if not pocket["liquid"]:
            if _is_card_pocket(pocket):
                # Card liability has one authority, below.
                continue
            components.append({
                "component_id": component_id,
                "kind": "cash",
                "side": "asset",
                "currency": currency,
                "amount": None,
                "as_of": pocket.get("as_of"),
                "basis": "evidence",
                "origin": "position · excluded pocket",
                "label": label,
                "included": False,
                "confidence": None,
                "note": (
                    "not supported as a tracked asset: "
                    + (pocket.get("excluded_reason") or "unsupported account product")
                ),
            })
            continue
        if pocket["balance"] is None:
            components.append({
                "component_id": component_id,
                "kind": "cash",
                "side": "asset",
                "currency": currency,
                "amount": None,
                "as_of": pocket.get("as_of"),
                "basis": "evidence",
                "origin": "position · no supported boundary",
                "label": label,
                "included": False,
                "confidence": None,
                "note": pocket.get("unavailable_reason") or "no supported balance boundary",
            })
            continue
        manual = pocket["basis"] == "manual-observation"
        components.append({
            "component_id": component_id,
            "kind": "cash",
            "side": "asset",
            "currency": currency,
            "amount": pocket["balance"],
            "as_of": pocket.get("as_of"),
            "basis": "manual" if manual else "evidence",
            "origin": f"position · {pocket['basis']}",
            "label": label,
            "included": True,
            "confidence": None,
            "note": "stored manual balance observation" if manual else None,
        })

    # --- personal debts and receivables, from the obligations read ---------
    for obligation in obligations["obligations"]:
        if obligation["origin"] != "personal_debt":
            continue
        debt_id = str(obligation["source"]["id"])
        flags = debt_flags.get(debt_id, {})
        side = "liability" if obligation["direction"] == "outflow" else "asset"
        amount = (
            obligation["remaining_amount"]["value"]
            if obligation["remaining_amount"] is not None
            else None
        )
        basis = "manual" if flags.get("manual") else "evidence"
        confidence = None
        if side == "asset":
            confidence = f"{obligation['confidence']} · {obligation['payment_status']}"
        included = True
        note: str | None = None
        if flags.get("card_linked"):
            included = False
            note = "linked to a tracked card facility; counted once in card liability"
        elif amount is None:
            included = False
            note = obligation.get("remaining_unknown_reason") or "amount is not evidenced"
        elif not obligation["stays_visible"]:
            included = False
            note = (
                f"excluded — {obligation['status'].replace('_', ' ')} "
                f"({obligation['basis']})"
            )
        components.append({
            "component_id": f"debt:{debt_id}",
            "kind": "debt",
            "side": side,
            "currency": obligation["currency"],
            "amount": amount,
            "as_of": obligation["date_basis"].get("value"),
            "basis": basis,
            "origin": "obligations · personal_debt",
            "label": obligation["label"],
            "included": included,
            "confidence": confidence,
            "note": note,
        })

    # --- existing card liability, from the cash-conversion contract --------
    for card in cards["cards"]:
        currency = card["currency"]
        partitions = {
            item["partition"]: item
            for item in card["partitions"]["partitions"]
        }
        card_as_of = card["partitions"].get("as_of")
        for name in card["partitions"]["existing_liability_partitions"]:
            partition = partitions[name]
            component_id = f"card:{card['account_id']}:{currency}:{name}"
            pretty = name.replace("_", " ")
            if partition["known"]:
                components.append({
                    "component_id": component_id,
                    "kind": "card_liability",
                    "side": "liability",
                    "currency": currency,
                    "amount": partition["amount"],
                    "as_of": card_as_of,
                    "basis": "evidence",
                    "origin": f"card cash-conversion · {name}",
                    "label": f"{card['label']} · {pretty}",
                    "included": True,
                    "confidence": None,
                    "note": None,
                })
            else:
                unresolved = partition.get("unresolved") or []
                reason = (
                    unresolved[0]["detail"]
                    if unresolved
                    else "card liability partition is unresolved"
                )
                components.append({
                    "component_id": component_id,
                    "kind": "card_liability",
                    "side": "liability",
                    "currency": currency,
                    "amount": None,
                    "as_of": card_as_of,
                    "basis": "evidence",
                    "origin": f"card cash-conversion · {name}",
                    "label": f"{card['label']} · {pretty}",
                    "included": False,
                    "confidence": None,
                    "note": reason,
                })

    notes = [
        "cash is taken from the position read, debts and receivables from the "
        "obligations read, and card liability from the card cash-conversion "
        "contract — each figure from exactly one source, so nothing is added twice",
        "the obligations read's card statement dues and installment rows are not "
        "included here: card liability has one authority and an installment that "
        "settles inside a statement due is already inside it",
        "receivables are reported as assets with their confidence disclosed while "
        "staying excluded from liquid cash",
    ]
    return {"components": components, "notes": notes}


def summarise(components: list[dict[str, Any]], *, as_of: str) -> dict[str, dict[str, Any]]:
    """Per-currency asset, liability and tracked net-worth lines.

    Evidence-basis components make the tracked subtotal; manual-basis components
    are summed onto their own line and offered as a clearly labelled combined
    figure.  A component with no amount is a named missing position, and the
    currency it belongs to is reported as an incomplete (known, not complete)
    subtotal.
    """
    per: dict[str, dict[str, Any]] = {}

    def slot(currency: str) -> dict[str, Any]:
        if currency not in per:
            per[currency] = {
                "_tracked_assets": Decimal(0),
                "_tracked_liabilities": Decimal(0),
                "_manual_assets": Decimal(0),
                "_manual_liabilities": Decimal(0),
                "missing_components": [],
                "excluded_components": [],
                "valuation_dates": set(),
            }
        return per[currency]

    for component in components:
        current = slot(component["currency"])
        if not component["included"]:
            record = {
                "component_id": component["component_id"],
                "kind": component["kind"],
                "side": component["side"],
                "reason": component["note"],
            }
            if component["amount"] is None:
                current["missing_components"].append(record)
            else:
                current["excluded_components"].append({**record, "amount": component["amount"]})
            continue
        value = Decimal(component["amount"])
        if component["as_of"]:
            current["valuation_dates"].add(component["as_of"])
        prefix = "_manual" if component["basis"] == "manual" else "_tracked"
        if component["side"] == "asset":
            current[f"{prefix}_assets"] += value
        else:
            current[f"{prefix}_liabilities"] += value

    result: dict[str, dict[str, Any]] = {}
    for currency, data in sorted(per.items()):
        tracked_assets = data["_tracked_assets"].quantize(_CENT, rounding=ROUND_HALF_UP)
        tracked_liabilities = data["_tracked_liabilities"].quantize(_CENT, rounding=ROUND_HALF_UP)
        manual_assets = data["_manual_assets"].quantize(_CENT, rounding=ROUND_HALF_UP)
        manual_liabilities = data["_manual_liabilities"].quantize(_CENT, rounding=ROUND_HALF_UP)
        tracked_net_worth = tracked_assets - tracked_liabilities
        net_worth_including_manual = (
            tracked_net_worth + manual_assets - manual_liabilities
        )
        result[currency] = {
            "tracked_assets": str(tracked_assets),
            "tracked_liabilities": str(tracked_liabilities),
            "tracked_net_worth": str(tracked_net_worth),
            "manual_assets": str(manual_assets),
            "manual_liabilities": str(manual_liabilities),
            "net_worth_including_manual": str(net_worth_including_manual),
            "manual_basis_note": _MANUAL_NOTE,
            "subtotal_note": _SUBTOTAL_NOTE,
            "missing_components": data["missing_components"],
            "excluded_components": data["excluded_components"],
            "complete": not data["missing_components"],
            "valuation_dates": sorted(data["valuation_dates"]),
            "as_of": as_of,
        }
    return result


def _debt_flags(
    database: Path, debt_ids: list[str]
) -> dict[str, dict[str, bool]]:
    connection = connect_readonly(database)
    try:
        return _debt_flags_from(connection, debt_ids)
    finally:
        connection.close()


def _debt_flags_from(
    connection: sqlite3.Connection, debt_ids: list[str]
) -> dict[str, dict[str, bool]]:
    """Per debt: is it a manual (opening-balance / unbound) record, and is it

    linked through a bound source allocation to a tracked card account?
    """
    flags: dict[str, dict[str, bool]] = {}
    if not debt_ids:
        return flags
    for debt_id in debt_ids:
        rows = connection.execute(
            "SELECT opening_balance, bound FROM personal_debt_events WHERE debt_id = ?",
            (debt_id,),
        ).fetchall()
        has_opening = any(int(row["opening_balance"]) for row in rows)
        any_unbound = any(not int(row["bound"]) for row in rows)
        try:
            linked = connection.execute(
                """
                SELECT 1
                FROM personal_debt_allocations a
                JOIN ledger_entries e ON e.natural_anchor = a.entry_anchor
                JOIN accounts acc ON acc.id = e.account_id
                LEFT JOIN card_facility_accounts c
                    ON c.account_id = e.account_id AND c.bound = 1
                WHERE a.debt_id = ? AND a.bound = 1
                  AND (c.account_id IS NOT NULL
                       OR upper(acc.source_type) LIKE '%\\_CARD' ESCAPE '\\')
                LIMIT 1
                """,
                (debt_id,),
            ).fetchone() is not None
        except sqlite3.OperationalError:
            linked = False
        flags[debt_id] = {
            "manual": has_opening or any_unbound,
            "card_linked": linked,
        }
    return flags


def _trend(prior: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    def scope(page: dict[str, Any]) -> set[str]:
        return {c["component_id"] for c in page["components"] if c["included"]}

    prior_scope, current_scope = scope(prior), scope(current)
    scope_changes = [
        {"component_id": component_id, "change": "added_since_prior"}
        for component_id in sorted(current_scope - prior_scope)
    ] + [
        {"component_id": component_id, "change": "absent_now"}
        for component_id in sorted(prior_scope - current_scope)
    ]
    currencies = sorted(set(prior["per_currency"]) | set(current["per_currency"]))
    change: dict[str, Any] = {}
    for currency in currencies:
        before = prior["per_currency"].get(currency)
        after = current["per_currency"].get(currency)
        before_value = Decimal(before["tracked_net_worth"]) if before else Decimal(0)
        after_value = Decimal(after["tracked_net_worth"]) if after else Decimal(0)
        change[currency] = {
            "prior_tracked_net_worth": before["tracked_net_worth"] if before else None,
            "current_tracked_net_worth": after["tracked_net_worth"] if after else None,
            "delta": _money(after_value - before_value),
            "both_complete": bool(
                before and after and before["complete"] and after["complete"]
            ),
        }
    return {
        "prior_as_of": prior["as_of"],
        "current_as_of": current["as_of"],
        "change_by_currency": change,
        "scope_changed": bool(scope_changes),
        "scope_changes": scope_changes,
        "comparability": (
            "the tracked scope changed between the two observations, so the delta "
            "spans a different set of components"
            if scope_changes
            else "the two observations cover the same tracked components"
        ),
    }


def net_worth_page(
    database: Path,
    *,
    as_of: str | None = None,
    prior_as_of: str | None = None,
    today: date,
) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return net_worth_page_from(
            connection, as_of=as_of, prior_as_of=prior_as_of, today=today
        )
    finally:
        connection.close()


def net_worth_page_from(
    connection: sqlite3.Connection,
    *,
    as_of: str | None = None,
    prior_as_of: str | None = None,
    today: date,
) -> dict[str, Any]:
    """The tracked net-worth read.

    ``prior_as_of`` adds a second dated observation and a basic trend between the
    two, labelling any change of tracked scope.  The figure is always a
    subtotal: it is never presented as a complete personal-wealth total.
    ``today`` is required (§5.3): resolved once by the caller, never here.
    """
    if not connection.in_transaction:
        connection.execute("BEGIN")
    resolved = _as_of(as_of, today)
    position = position_page_from(connection, today=date.fromisoformat(resolved))
    obligations = obligations_page_from(connection, as_of=resolved, today=today)
    cards = card_cash_conversion_from(connection, as_of=resolved)
    debt_ids = [
        str(obligation["source"]["id"])
        for obligation in obligations["obligations"]
        if obligation["origin"] == "personal_debt"
    ]
    built = assemble_components(
        position=position,
        obligations=obligations,
        cards=cards,
        debt_flags=_debt_flags_from(connection, debt_ids),
    )
    page: dict[str, Any] = {
        "as_of": resolved,
        "generation": obligations["generation"],
        "label": "tracked net worth",
        "is_total_personal_wealth": False,
        "disclaimer": (
            "A per-currency subtotal of the dated cash, debt, receivable and "
            "card positions this ledger can support. Components it cannot value "
            "are listed as missing, never as zero. This is not proof of total "
            "personal wealth and is not labelled as such."
        ),
        "components": built["components"],
        "notes": built["notes"],
        "per_currency": summarise(built["components"], as_of=resolved),
    }
    if prior_as_of is not None:
        prior_page = net_worth_page_from(connection, as_of=prior_as_of, today=today)
        page["trend"] = _trend(prior_page, page)
    return page


__all__ = ["assemble_components", "net_worth_page", "summarise"]
