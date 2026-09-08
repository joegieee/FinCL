"""Generation-pinned, derived read models for the categorization inbox."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import canonical_json
from .db import connect_readonly
from .errors import FinCLError
from .generation import current_generation

CANDIDATE_GROUPING_VERSION = "categorization-candidates-2026-v1"


def categorization_summary(database: Path, *, generation: str) -> dict[str, Any]:
    """Return disjoint effective-category buckets plus the attention overlay."""
    connection = _pinned_connection(database, generation)
    try:
        rows = connection.execute(
            """
            SELECT ec.group_name, ec.category, ec.basis, ec.ambiguous, ec.direction_conflict,
                   e.analytics_eligible, e.lifecycle_state
            FROM entry_categories ec JOIN ledger_entries e ON e.id=ec.entry_id
            JOIN extraction_runs x ON x.id=e.extraction_run_id AND x.selection_state='SELECTED'
            """
        ).fetchall()
        buckets: Counter[str] = Counter()
        attention = 0
        for row in rows:
            if row["ambiguous"] or row["direction_conflict"]:
                attention += 1
            active = bool(row["analytics_eligible"]) and row["lifecycle_state"] == "COMPLETED"
            basis = str(row["basis"])
            if basis == "override":
                bucket = "manual_override"
            elif basis.startswith(("rule:", "alias:")):
                bucket = "rule_assigned"
            elif basis.startswith("link:"):
                bucket = "internal"
            elif basis.startswith("inbound:"):
                bucket = "inbound_classified"
            elif row["category"] == "Unclassified":
                bucket = "unclassified_active" if active else "unclassified_inactive"
            else:  # Defensive: category engine should not produce another basis.
                bucket = "other_system_assigned"
            buckets[bucket] += 1
        denominator = sum(
            1 for row in rows
            if bool(row["analytics_eligible"]) and row["lifecycle_state"] == "COMPLETED"
        )
        numerator = buckets["rule_assigned"] + buckets["manual_override"]
        return {
            "generation": generation,
            "counts": dict(buckets),
            "total_selected": len(rows),
            "active_categorization_denominator": denominator,
            "progress_numerator": numerator,
            "needs_attention": attention,
        }
    finally:
        connection.close()


def categorization_candidates(
    database: Path, *, generation: str, cursor: str | None = None, limit: int = 50,
    account: str = "", currency: str = "", direction: str = "", lifecycle: str = "", kind: str = "",
    start: str = "", end: str = "",
) -> dict[str, Any]:
    """Group unclassified records without persisting a second projection."""
    connection = _pinned_connection(database, generation)
    try:
        limit = max(1, min(int(limit), 100))
        groups = _candidate_groups(
            connection, account=account, currency=currency, direction=direction, lifecycle=lifecycle, start=start, end=end,
        )
        cards = [_candidate_card(group_kind, key, members) for (group_kind, key), members in groups.items()
                 if not kind or group_kind == kind]
        cards.sort(key=lambda card: (-card["transaction_count"], card["label"], card["candidate_id"]))
        offset = _decode_cursor(cursor)
        page = cards[offset:offset + limit]
        next_cursor = _encode_cursor(offset + limit) if offset + limit < len(cards) else None
        return {
            "generation": generation,
            "candidate_version": CANDIDATE_GROUPING_VERSION,
            "candidates": page,
            "next_cursor": next_cursor,
            "total": len(cards),
        }
    finally:
        connection.close()


def categorization_candidate_detail(
    database: Path, *, generation: str, candidate_id: str, cursor: str | None = None, limit: int = 50,
    account: str = "", currency: str = "", direction: str = "", lifecycle: str = "", kind: str = "",
    start: str = "", end: str = "",
) -> dict[str, Any]:
    """Return one current candidate and its bounded, inspectable members."""
    connection = _pinned_connection(database, generation)
    try:
        for (group_kind, key), members in _candidate_groups(
            connection, account=account, currency=currency, direction=direction, lifecycle=lifecycle, start=start, end=end,
        ).items():
            if kind and group_kind != kind:
                continue
            card = _candidate_card(group_kind, key, members)
            if card["candidate_id"] != candidate_id:
                continue
            limit = max(1, min(int(limit), 100))
            ordered = sorted(
                members,
                key=lambda member: (member[0]["effective_at"], member[0]["natural_anchor"]),
                reverse=True,
            )
            offset = _decode_cursor(cursor)
            records = []
            for row, _label in ordered[offset:offset + limit]:
                records.append({
                    "anchor": row["natural_anchor"], "account": row["account_id"],
                    "currency": row["currency_pocket"], "amount": row["amount_text"],
                    "date": row["effective_at"][:10], "direction": row["economic_direction"],
                    "lifecycle": row["lifecycle_state"], "active": bool(row["analytics_eligible"]) and row["lifecycle_state"] == "COMPLETED",
                    "description": row["description_text"], "merchant": row["merchant_canonical"] or row["merchant_normalized"],
                    "merchant_raw": row["merchant_raw"], "merchant_normalized": row["merchant_normalized"],
                    "paynow_to": row["paynow_to"],
                })
            return {"generation": generation, "candidate_version": CANDIDATE_GROUPING_VERSION,
                    "candidate": card, "records": records,
                    "next_cursor": _encode_cursor(offset + limit) if offset + limit < len(ordered) else None}
        raise FinCLError("Categorization candidate not found")
    finally:
        connection.close()


def derive_rule_options(database: Path, *, generation: str, anchors: list[str]) -> dict[str, Any]:
    """Derive only closed predicates supported by the categorization engine."""
    if not anchors or len(set(anchors)) != len(anchors):
        raise FinCLError("Choose one or more distinct transaction anchors")
    connection = _pinned_connection(database, generation)
    try:
        placeholders = ",".join("?" for _ in anchors)
        rows = connection.execute(
            f"""
            SELECT e.natural_anchor,e.account_id,o.merchant_canonical,o.merchant_normalized,
                   o.paynow_to,o.description_text
            FROM ledger_entries e JOIN extraction_runs x ON x.id=e.extraction_run_id
            JOIN merchant_observations o ON o.entry_id=e.id
            WHERE x.selection_state='SELECTED' AND e.natural_anchor IN ({placeholders})
            """, anchors,
        ).fetchall()
        if len(rows) != len(anchors):
            raise FinCLError("One or more selected transactions are no longer available")
        options: list[dict[str, Any]] = []
        merchants = {row["merchant_canonical"] or row["merchant_normalized"] for row in rows}
        if len(merchants) == 1 and None not in merchants:
            options.append({"label": "All transactions from this merchant", "match": {"merchant_canonical": next(iter(merchants))}})
        payees = {row["paynow_to"] for row in rows}
        if len(payees) == 1 and None not in payees:
            options.append({"label": "All transactions to this PayNow recipient", "match": {"paynow_to": next(iter(payees))}})
        descriptions = {row["description_text"] for row in rows}
        accounts = {row["account_id"] for row in rows}
        if len(descriptions) == 1 and None not in descriptions:
            match: dict[str, str] = {"description_contains": next(iter(descriptions))}
            if len(accounts) == 1:
                match["account"] = next(iter(accounts))
            options.append({"label": "Transactions with this exact description", "match": match})
        return {"generation": generation, "anchors": sorted(anchors), "options": options,
                "unavailable_reason": "The selected transactions do not share a safe reusable predicate" if not options else None}
    finally:
        connection.close()


def _candidate_groups(
    connection: Any, *, account: str = "", currency: str = "", direction: str = "", lifecycle: str = "",
    start: str = "", end: str = "",
) -> dict[tuple[str, str], list[Any]]:
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    clauses = ["ec.group_name='Unclassified'", "ec.category='Unclassified'"]
    params: list[str] = []
    for field, value in (("e.account_id", account), ("e.currency_pocket", currency), ("e.economic_direction", direction), ("e.lifecycle_state", lifecycle)):
        if value:
            clauses.append(f"{field}=?")
            params.append(value)
    for operator, value in ((">=", start), ("<=", end)):
        if value:
            _validated_date(value)
            clauses.append(f"substr(e.effective_at,1,10){operator}?")
            params.append(value)
    rows = connection.execute(
        f"""
        SELECT e.natural_anchor,e.account_id,e.currency_pocket,e.effective_at,e.economic_direction,
               e.amount_text,e.analytics_eligible,e.lifecycle_state,o.merchant_canonical,
               o.merchant_raw,o.merchant_normalized,o.paynow_to,o.description_text
        FROM entry_categories ec JOIN ledger_entries e ON e.id=ec.entry_id
        JOIN extraction_runs x ON x.id=e.extraction_run_id AND x.selection_state='SELECTED'
        JOIN merchant_observations o ON o.entry_id=e.id
        WHERE {' AND '.join(clauses)}
        """, params,
    ).fetchall()
    for row in rows:
        kind, key, label = _candidate_key(row)
        groups[(kind, key)].append((row, label))
    return groups


def _validated_date(value: str) -> None:
    from datetime import date
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise FinCLError("Candidate dates must be YYYY-MM-DD") from exc


def _candidate_key(row: Any) -> tuple[str, str, str]:
    merchant = row["merchant_canonical"] or row["merchant_normalized"]
    if merchant:
        return "merchant", str(merchant), str(merchant)
    if row["paynow_to"]:
        return "paynow_recipient", str(row["paynow_to"]), str(row["paynow_to"])
    description = (row["description_text"] or "").strip()
    if description:
        return "description", description.casefold(), description
    return "missing_merchant", str(row["account_id"]), f"Missing merchant — {row['account_id']}"


def _candidate_card(kind: str, key: str, members: list[tuple[Any, str]]) -> dict[str, Any]:
    rows = [member[0] for member in members]
    label = members[0][1]
    canonical = canonical_json([CANDIDATE_GROUPING_VERSION, kind, key])
    candidate_id = "catcand_" + hashlib.sha256(canonical.encode()).hexdigest()[:20]
    currencies = Counter(row["currency_pocket"] for row in rows)
    directions = Counter(row["economic_direction"] for row in rows)
    active_count = sum(bool(row["analytics_eligible"]) and row["lifecycle_state"] == "COMPLETED" for row in rows)
    examples = list(dict.fromkeys((row["description_text"] or "").strip() for row in rows if row["description_text"]))[:3]
    return {
        "candidate_id": candidate_id, "kind": kind, "label": label,
        "transaction_count": len(rows), "active_count": active_count,
        "date_range": {"first": min(row["effective_at"][:10] for row in rows), "last": max(row["effective_at"][:10] for row in rows)},
        "currencies": dict(sorted(currencies.items())), "directions": dict(sorted(directions.items())),
        "accounts": sorted({str(row["account_id"]) for row in rows}), "examples": examples,
    }


def _pinned_connection(database: Path, generation: str) -> Any:
    connection = connect_readonly(database)
    connection.execute("BEGIN")
    current = current_generation(connection)
    if current is None or generation != current["generation_token"]:
        connection.close()
        raise FinCLError("STALE_GENERATION: reload categorization")
    return connection


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        offset = int(value["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (KeyError, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise FinCLError("Invalid candidate cursor") from exc
