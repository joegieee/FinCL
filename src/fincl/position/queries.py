"""Evidence-backed liquid position and historical cash bridge.

Printed balances, reconciled statement closes, and journalled observations are
balance boundaries. Silence after a boundary is missing evidence, never zero.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..money import ExactMoney, exact_sum, format_decimal
from .models import STALE_AFTER_DAYS

_LIQUID_SOURCE_TYPES = frozenset({
    "REVOLUT_CSV", "DBS_CONSOLIDATED", "DBS_PAYLAH",
    "MARI_SAVINGS", "TRUST_SAVINGS",
})


def _money(coefficient: int, scale: int) -> ExactMoney:
    return ExactMoney(int(coefficient), int(scale), "")


def _sum(values: list[ExactMoney]) -> tuple[int, int]:
    return exact_sum(*values) if values else (0, 0)


def _selected_entries(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT e.* FROM ledger_entries e
        JOIN extraction_runs x ON x.id=e.extraction_run_id
        LEFT JOIN statements s ON s.id=e.statement_id
        WHERE x.status='ACCEPTED' AND x.selection_state='SELECTED'
          AND (e.source_family='CSV' OR s.selection_state='SELECTED')
          AND e.lifecycle_state='COMPLETED' AND e.analytics_eligible=1
        ORDER BY e.effective_at, e.natural_anchor, e.id
        """
    ).fetchall()


def _statement_boundaries(
    connection: sqlite3.Connection, entries: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    """Return statement opens/closes only when a blocking equation passed."""
    by_statement: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for entry in entries:
        if entry["statement_id"]:
            by_statement[(entry["statement_id"], entry["currency_pocket"])].append(entry)
    rows = connection.execute(
        """
        SELECT s.id statement_id, s.account_id, s.period_start, s.period_end,
               c.currency_pocket, c.actual_coefficient, c.actual_scale, c.check_type
        FROM statements s JOIN extraction_runs x ON x.id=s.extraction_run_id
        JOIN document_reconciliation_checks c ON c.statement_id=s.id
        WHERE x.status='ACCEPTED' AND x.selection_state='SELECTED'
          AND s.selection_state='SELECTED' AND c.passed=1 AND c.blocking=1
          AND c.actual_coefficient IS NOT NULL
          AND c.check_type IN ('STATEMENT_BALANCE','SECTION_CLOSING_BALANCE')
          AND s.period_start IS NOT NULL AND s.period_end IS NOT NULL
        ORDER BY s.period_end,
          CASE c.check_type WHEN 'STATEMENT_BALANCE' THEN 0 ELSE 1 END
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["statement_id"], row["currency_pocket"])
        if key in seen:
            continue
        seen.add(key)
        movements = by_statement.get(key, [])
        if any(item["effective_precision"] != "DAY" for item in movements):
            continue
        close = _money(row["actual_coefficient"], row["actual_scale"])
        delta_coefficient, delta_scale = _sum([
            _money(item["amount_coefficient"], item["amount_scale"])
            for item in movements
        ])
        scale = max(close.scale, delta_scale)
        opening = close.rescaled_coefficient(scale) - (
            delta_coefficient * 10 ** (scale - delta_scale)
        )
        opening_day = (
            date.fromisoformat(row["period_start"]) - timedelta(days=1)
        ).isoformat()
        common = {"account_id": row["account_id"], "currency_pocket": row["currency_pocket"]}
        result.extend((
            {**common, "as_of": opening_day, "sort_at": opening_day + "T23:59:59",
             "coefficient": opening, "scale": scale,
             "balance": format_decimal(opening, scale),
             "basis": "statement-derived-opening", "statement_id": row["statement_id"]},
            {**common, "as_of": row["period_end"], "sort_at": row["period_end"] + "T23:59:59",
             "coefficient": close.coefficient, "scale": close.scale,
             "balance": format_decimal(close.coefficient, close.scale),
             "basis": "statement-closing-balance", "statement_id": row["statement_id"]},
        ))
    return result


def _boundaries(
    connection: sqlite3.Connection, entries: list[sqlite3.Row]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in entries:
        if row["balance_coefficient"] is None or row["effective_precision"] != "DAY":
            continue
        item = {
            "account_id": row["account_id"], "currency_pocket": row["currency_pocket"],
            "as_of": row["effective_at"][:10], "sort_at": row["effective_at"],
            "coefficient": row["balance_coefficient"], "scale": row["balance_scale"],
            "balance": format_decimal(row["balance_coefficient"], row["balance_scale"]),
            "basis": "statement-running-balance", "entry_id": row["id"],
            "date_precision": row["effective_precision"],
        }
        result[(row["account_id"], row["currency_pocket"])].append(item)
    for item in _statement_boundaries(connection, entries):
        result[(item["account_id"], item["currency_pocket"])].append(item)
    for row in connection.execute(
        """SELECT * FROM balance_observations WHERE retired=0
           ORDER BY observed_on, journal_seq, observation"""
    ):
        item = {
            "account_id": row["account_id"], "currency_pocket": row["currency_pocket"],
            "as_of": row["observed_on"], "sort_at": row["observed_on"] + "T23:59:59",
            "coefficient": row["balance_coefficient"], "scale": row["balance_scale"],
            "balance": format_decimal(row["balance_coefficient"], row["balance_scale"]),
            "basis": "manual-observation", "observation": row["observation"],
        }
        result[(row["account_id"], row["currency_pocket"])].append(item)
    for values in result.values():
        priority = {"statement-derived-opening": 0, "statement-running-balance": 1,
                    "statement-closing-balance": 2, "manual-observation": 3}
        values.sort(key=lambda item: (item["sort_at"], priority[item["basis"]]))
    return result


def _paired_liquid_entries(
    connection: sqlite3.Connection, liquid_scopes: set[tuple[str, str]]
) -> tuple[set[str], set[str]]:
    entries: set[str] = set()
    links: set[str] = set()
    for row in connection.execute(
        """SELECT t.id, t.left_entry_id, t.right_entry_id,
                  l.account_id la, l.currency_pocket lc,
                  r.account_id ra, r.currency_pocket rc
           FROM transfer_links t JOIN ledger_entries l ON l.id=t.left_entry_id
           JOIN ledger_entries r ON r.id=t.right_entry_id
           WHERE t.status='CONFIRMED'"""
    ):
        if (row["la"], row["lc"]) in liquid_scopes and (row["ra"], row["rc"]) in liquid_scopes:
            entries.update((row["left_entry_id"], row["right_entry_id"]))
            links.add(row["id"])
    return entries, links


def _bridge(
    key: tuple[str, str], boundaries: list[dict[str, Any]],
    entries: list[sqlite3.Row], paired: set[str], connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    if len(boundaries) < 2:
        return None
    opening, closing = boundaries[0], boundaries[-1]
    scope_rows = [row for row in entries if (row["account_id"], row["currency_pocket"]) == key]
    rows = [row for row in scope_rows
            if row["effective_at"] > opening["sort_at"] and row["effective_at"] <= closing["sort_at"]]
    ambiguous = opening["basis"] == "manual-observation" and any(
        row["effective_at"][:10] == opening["as_of"] for row in scope_rows
    )
    same_certified_statement = (
        opening.get("statement_id") is not None
        and opening.get("statement_id") == closing.get("statement_id")
        and opening["basis"] == "statement-derived-opening"
        and closing["basis"] == "statement-closing-balance"
    )
    unchecked_rows = [
        row for row in rows
        if row["source_family"] == "CSV" and not connection.execute(
            "SELECT 1 FROM reconciliation_checks WHERE current_raw_record_id=? AND passed=1",
            (row["raw_record_id"],),
        ).fetchone()
    ]
    evidence_complete = same_certified_statement or (
        bool(rows) and not unchecked_rows
        and all(row["source_family"] == "CSV" for row in rows)
    )
    buckets: dict[str, list[ExactMoney]] = defaultdict(list)
    net: list[ExactMoney] = []
    for row in rows:
        amount = _money(row["amount_coefficient"], row["amount_scale"])
        fee = _money(abs(row["fee_coefficient"] or 0), row["fee_scale"] or 0)
        net.append(amount)
        if fee.coefficient:
            net.append(_money(-fee.coefficient, fee.scale))
            buckets["external_outflows"].append(fee)
        if row["id"] in paired:
            name = "owned_transfer_inflows" if amount.coefficient >= 0 else "owned_transfer_outflows"
        else:
            name = "external_inflows" if amount.coefficient >= 0 else "external_outflows"
        buckets[name].append(_money(abs(amount.coefficient), amount.scale))
    delta, delta_scale = _sum(net)
    scale = max(opening["scale"], closing["scale"], delta_scale)
    calculated = opening["coefficient"] * 10 ** (scale - opening["scale"])
    calculated += delta * 10 ** (scale - delta_scale)
    actual = closing["coefficient"] * 10 ** (scale - closing["scale"])
    reconciled = calculated == actual and not ambiguous and evidence_complete

    def total(name: str) -> str:
        return format_decimal(*_sum(buckets[name]))

    ids = {row["id"] for row in rows}
    paired_links = 0
    if ids:
        marks = ",".join("?" for _ in ids)
        paired_links = connection.execute(
            f"SELECT count(*) FROM transfer_links WHERE status='CONFIRMED' "
            f"AND left_entry_id IN ({marks}) AND right_entry_id IN ({marks})",
            tuple(ids) + tuple(ids),
        ).fetchone()[0]
    reason = None
    if ambiguous:
        reason = "same-day movement order is ambiguous at a manual observation boundary"
    elif not evidence_complete:
        reason = "intervening movement completeness is not supported"
    elif not reconciled:
        reason = "intervening movements do not reconcile the balance boundaries"
    return {
        "account_id": key[0], "currency_pocket": key[1],
        "opening": opening["balance"], "opening_as_of": opening["as_of"],
        "external_inflows": total("external_inflows"),
        "external_outflows": total("external_outflows"),
        "owned_transfer_inflows": total("owned_transfer_inflows"),
        "owned_transfer_outflows": total("owned_transfer_outflows"),
        "closing": closing["balance"], "closing_as_of": closing["as_of"],
        "calculated_closing": format_decimal(calculated, scale),
        "reconciled": reconciled, "movement_count": len(rows),
        "paired_transfer_link_count": paired_links, "unsupported_reason": reason,
    }


def position_page(database: Path, *, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        accounts = {row["id"]: row for row in connection.execute(
            "SELECT id,institution,source_type,display_name,masked_identifier FROM accounts"
        )}
        card_ids = {row[0] for row in connection.execute(
            "SELECT DISTINCT account_id FROM card_facility_accounts WHERE bound=1"
        )}
        entries = _selected_entries(connection)
        boundary_map = _boundaries(connection, entries)
        scopes = set(boundary_map) | {
            (row["account_id"], row["currency_pocket"]) for row in entries
        }
        liquid_scopes: set[tuple[str, str]] = set()
        exclusions: dict[tuple[str, str], str] = {}
        for key in scopes:
            account = accounts.get(key[0])
            source_type = str(account["source_type"]).upper() if account else ""
            if key[0] in card_ids or source_type.endswith("_CARD"):
                exclusions[key] = "credit-card facility balance is an obligation, not cash"
            elif source_type not in _LIQUID_SOURCE_TYPES:
                exclusions[key] = "account product is not supported as liquid cash"
            else:
                liquid_scopes.add(key)
        paired, transfer_links = _paired_liquid_entries(connection, liquid_scopes)
        pockets: list[dict[str, Any]] = []
        bridges: list[dict[str, Any]] = []
        missing: list[dict[str, str]] = []
        latest_dates: set[str] = set()
        latest_instants: set[str] = set()
        chosen_boundaries: dict[tuple[str, str], dict[str, Any]] = {}
        for key in sorted(scopes):
            account = accounts.get(key[0])
            boundaries = [
                boundary for boundary in boundary_map.get(key, [])
                if boundary["as_of"] <= today.isoformat()
            ]
            chosen = boundaries[-1] if boundaries else None
            if chosen:
                chosen_boundaries[key] = chosen
            liquid = key in liquid_scopes
            if liquid and chosen:
                latest_dates.add(chosen["as_of"])
                latest_instants.add(chosen["sort_at"])
            unavailable = None if chosen else "no supported balance boundary for this pocket"
            if liquid and chosen and chosen["as_of"] < today.isoformat():
                missing.append({
                    "account_id": key[0], "currency_pocket": key[1],
                    "start": (date.fromisoformat(chosen["as_of"]) + timedelta(days=1)).isoformat(),
                    "end": today.isoformat(),
                    "reason": "no complete actual-movement interval after the last supported balance",
                })
            elif liquid and not chosen:
                missing.append({"account_id": key[0], "currency_pocket": key[1],
                                "start": "unknown", "end": today.isoformat(),
                                "reason": unavailable or "unknown"})
            age = (today - date.fromisoformat(chosen["as_of"])).days if chosen else None
            bridge = _bridge(key, boundaries, entries, paired, connection) if liquid else None
            if bridge:
                bridges.append(bridge)
                if not bridge["reconciled"]:
                    missing.append({"account_id": key[0], "currency_pocket": key[1],
                                    "start": bridge["opening_as_of"], "end": bridge["closing_as_of"],
                                    "reason": bridge["unsupported_reason"]})
            pockets.append({
                "account_id": key[0], "institution": account["institution"] if account else None,
                "display_name": account["display_name"] if account else None,
                "source_type": account["source_type"] if account else None,
                "currency_pocket": key[1], "liquid": liquid,
                "excluded_reason": exclusions.get(key),
                "basis": chosen["basis"] if chosen else "unavailable",
                "balance": chosen["balance"] if chosen else None,
                "as_of": chosen["as_of"] if chosen else None,
                "date_precision": chosen.get("date_precision") if chosen else None,
                "stale": bool(age is not None and age > STALE_AFTER_DAYS),
                "staleness_days": age, "unavailable_reason": unavailable,
                "statement_running_balance": next((b for b in reversed(boundaries)
                    if b["basis"] == "statement-running-balance"), None),
                "manual_observation": next((b for b in reversed(boundaries)
                    if b["basis"] == "manual-observation"), None),
            })
        included = [p for p in pockets if p["liquid"]]
        cutoff = (next(iter(latest_dates)) if included and len(latest_dates) == 1
                  and len(latest_instants) == 1
                  and all(p["balance"] is not None for p in included) else None)
        totals: dict[str, list[ExactMoney]] = defaultdict(list)
        if cutoff:
            for pocket in included:
                chosen = chosen_boundaries[(pocket["account_id"], pocket["currency_pocket"])]
                totals[pocket["currency_pocket"]].append(_money(chosen["coefficient"], chosen["scale"]))
        combined = {currency: format_decimal(*_sum(values))
                    for currency, values in sorted(totals.items())}
        reasons: list[str] = []
        if not included:
            reasons.append("no supported liquid account pockets were found")
        elif not cutoff:
            reasons.append("included liquid pockets do not share a supported balance cutoff")
        if cutoff != today.isoformat():
            reasons.append("complete actual movements from the supported cutoff through today are unavailable")
        if any(not bridge["reconciled"] for bridge in bridges):
            reasons.append("one or more balance bridges do not reconcile")
        combined_bridge: dict[str, dict[str, Any]] = {}
        for currency in sorted({bridge["currency_pocket"] for bridge in bridges}):
            members = [bridge for bridge in bridges if bridge["currency_pocket"] == currency]
            def bridge_total(field: str, selected: list[dict[str, Any]] = members) -> str:
                return format_decimal(*_sum([
                    ExactMoney.parse(member[field]) for member in selected
                ]))
            combined_bridge[currency] = {
                "opening": bridge_total("opening"),
                "external_inflows": bridge_total("external_inflows"),
                "external_outflows": bridge_total("external_outflows"),
                "closing": bridge_total("closing"),
                "reconciled": all(member["reconciled"] for member in members),
                "owned_transfer_pairs_cancelled": len(transfer_links),
            }
        return {
            "generated_for": today.isoformat(), "stale_after_days": STALE_AFTER_DAYS,
            "supported_cutoff": cutoff,
            "current_position_supported": cutoff == today.isoformat() and not missing,
            "current_position_reasons": list(dict.fromkeys(reasons)),
            "included_pockets": included, "pockets": pockets,
            "combined_liquid_position": combined, "liquid_total": combined,
            "liquid_total_withheld": {} if cutoff else dict.fromkeys(sorted({p["currency_pocket"] for p in included}), "withheld: no common supported cutoff"),
            "stale": any(p["stale"] for p in included), "missing_intervals": missing,
            "cash_bridge": {"pockets": bridges, "combined": {
                "by_currency": combined_bridge,
                "owned_transfer_pairs_cancelled": len(transfer_links),
                "policy": "each confirmed pair is removed once only when both legs are included liquid pockets",
            }},
        }
    finally:
        connection.close()
