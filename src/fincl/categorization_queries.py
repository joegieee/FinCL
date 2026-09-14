"""Generation-pinned, derived read models for the categorization inbox."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .categorization import MIN_LITERAL_LENGTH
from .common import canonical_json
from .db import connect_readonly
from .errors import FinCLError
from .generation import current_generation
from .money import ExactMoney, exact_sum, format_decimal

CANDIDATE_GROUPING_VERSION = "categorization-candidates-2026-v2"

# Quick discovery paths (PLAN-unified-transaction-classification.md Step 5).
# "category" is the original, unchanged inbox (category still Unclassified).
# The other three group by an *effective* dimension instead of raw category
# text, reusing the same bucket/applicability/effective-role evaluator
# `evaluate_classification_draft` uses, so a group here can never disagree
# with what the shared classification editor would show for the same anchor.
FOCUS_KINDS = ("category", "spending_details", "incoming", "conflicts")


_MERGE_RULE_TABLES = (
    ("category_rules", "CATEGORY_RULE_SET", "CATEGORY_RULE_RETIRE", "category"),
    ("role_rules", "ROLE_RULE_SET", "ROLE_RULE_RETIRE", "role"),
    ("inbound_rules", "INBOUND_RULE_SET", "INBOUND_RULE_RETIRE", "inbound"),
)


def _merge_rule_value(table: str, row: Any) -> Any:
    if table == "category_rules":
        return f"{row['group_name']} / {row['category']}"
    if table == "role_rules":
        return {"cost_behavior": row["cost_behavior"], "need": row["need"]}
    return row["classification"]


def merchant_merge_preview(database: Path, *, generation: str, sources: list[str], target: str,
                           display_name: str | None, reason: str,
                           resolutions: dict[str, str] | None = None) -> dict[str, Any]:
    """Return the frozen journal children for a deliberate merchant merge.

    This is deliberately a read-only compiler: the caller previews these exact
    set-new/retire-old children and submits them as one existing idempotent
    batch. No projection table is edited directly.

    Category, role and inbound rules pointing at any source all collapse onto
    the same new ``rule_id`` (one match, the target). When sources disagree on
    the outcome for one of those three kinds (e.g. one source's category rule
    says "Food", another says "Transport"), neither the new rule nor the old
    rules for that kind are emitted -- an unresolved conflict must not
    silently pick a winner or drop a source's categorization. The caller
    resolves it by naming which source's value to keep in ``resolutions``
    (keyed by "category" / "role" / "inbound") and re-previewing.
    """
    from .categorization import canonical_match, rule_id_for, validate_alias_patterns
    resolutions = resolutions or {}
    if not sources or len(set(sources)) != len(sources) or target in sources:
        raise FinCLError("Choose distinct source merchant identities and a different target")
    if not target.strip() or not reason.strip():
        raise FinCLError("A target merchant identity and reason are required")
    connection = _pinned_connection(database, generation)
    try:
        aliases = {row["canonical"]: dict(row) for row in connection.execute(
            "SELECT canonical,patterns_json,display_name FROM merchant_aliases"
        )}
        patterns: list[dict[str, str]] = []
        for canonical in [*sources, target]:
            if canonical in aliases:
                patterns.extend(json.loads(aliases[canonical]["patterns_json"]))
        # Source identities without an alias, and a target without one, are
        # still observable statement names -- preserve them as exact patterns
        # in the new canonical so nothing that used to resolve here stops
        # matching once this alias becomes authoritative.
        normalized_sources = list(sources) if target in aliases else [*sources, target]
        marks = ",".join("?" for _ in normalized_sources)
        for row in connection.execute(
            f"SELECT DISTINCT merchant_normalized FROM merchant_observations WHERE merchant_canonical IN ({marks})",
            normalized_sources,
        ):
            if row["merchant_normalized"]:
                patterns.append({"exact": row["merchant_normalized"]})
        patterns = validate_alias_patterns(list({canonical_json(p): p for p in patterns}.values()))
        resolved_display_name = display_name or aliases.get(target, {}).get("display_name")
        if not resolved_display_name:
            resolved_display_name = next(
                (aliases[source]["display_name"] for source in sources
                 if source in aliases and aliases[source].get("display_name")),
                None,
            )
        commands: list[dict[str, Any]] = [{
            "kind": "MERCHANT_ALIAS_SET", "anchors": {"canonical": target},
            "payload": {"patterns": patterns, "replace_patterns": True,
                        "display_name": resolved_display_name, "reason": reason},
        }]
        conflicts: list[dict[str, Any]] = []
        new_match = canonical_match({"merchant_canonical": target})
        new_rule_id = rule_id_for(new_match)
        for table, set_kind, retire_kind, kind_label in _MERGE_RULE_TABLES:
            matched_rows = [
                row for row in connection.execute(f"SELECT * FROM {table}").fetchall()
                if json.loads(row["match_json"]).get("merchant_canonical") in sources
            ]
            if not matched_rows:
                continue
            groups: dict[str, list[tuple[str, Any, Any]]] = {}
            for row in matched_rows:
                from_merchant = json.loads(row["match_json"])["merchant_canonical"]
                value = _merge_rule_value(table, row)
                value_key = value if isinstance(value, str) else canonical_json(value)
                groups.setdefault(value_key, []).append((from_merchant, value, row))
            if len(groups) > 1:
                chosen_from = resolutions.get(kind_label)
                winner = next((g for g in groups.values() if g[0][0] == chosen_from), None) if chosen_from else None
                if winner is None:
                    conflicts.append({
                        "kind": kind_label,
                        "candidates": [{"from_merchant": g[0][0], "value": g[0][1]} for g in groups.values()],
                    })
                    continue
                value = winner[0][1]
            else:
                value = next(iter(groups.values()))[0][1]
            payload: dict[str, Any] = {"match": new_match, "reason": reason}
            if table == "category_rules":
                payload["category"] = value
            elif table == "role_rules":
                payload.update(value)
            else:
                payload["classification"] = value
            commands.append({"kind": set_kind, "anchors": {"rule_id": new_rule_id}, "payload": payload})
            for row in matched_rows:
                commands.append({"kind": retire_kind, "anchors": {"rule_id": row["rule_id"]}, "payload": {"reason": reason}})
        for source in sources:
            if source in aliases:
                commands.append({"kind": "MERCHANT_ALIAS_RETIRE", "anchors": {"canonical": source}, "payload": {"reason": reason}})
        affected_transactions = connection.execute(
            f"SELECT COUNT(*) AS n FROM merchant_observations WHERE merchant_canonical IN ({','.join('?' for _ in [*sources, target])})",
            [*sources, target],
        ).fetchone()["n"]
        return {"generation": generation, "sources": sources, "target": target, "patterns": patterns,
                "commands": commands, "conflicts": conflicts, "affected_transactions": affected_transactions,
                "dependent_rules": sum(1 for item in commands if item["kind"].endswith("_RETIRE") and item["kind"] != "MERCHANT_ALIAS_RETIRE")}
    finally:
        connection.close()


def merchant_search(database: Path, *, generation: str, query: str, limit: int) -> dict[str, Any]:
    """Case-insensitive substring search over merchant key, display name and
    observed statement-name variants (Plan Step 4). Read-only."""
    connection = _pinned_connection(database, generation)
    try:
        merchants = _merchant_index(connection)
    finally:
        connection.close()
    needle = query.strip().casefold()
    if needle:
        merchants = [
            item for item in merchants
            if needle in item["merchant_key"].casefold()
            or needle in (item["display_name"] or "").casefold()
            or any(needle in variant.casefold() for variant in item["variants"])
        ]
    return {"generation": generation, "merchants": merchants[:limit]}


def merchant_similar(database: Path, *, generation: str, merchant_key: str) -> dict[str, Any]:
    """Suggestion-only near-duplicate merchants for ``merchant_key`` (Plan Step 4)."""
    from .merchant_similarity import merchants_similar
    connection = _pinned_connection(database, generation)
    try:
        merchants = _merchant_index(connection)
    finally:
        connection.close()
    if not any(item["merchant_key"] == merchant_key for item in merchants):
        raise FinCLError("Unknown merchant identity")
    suggestions = sorted(
        (item for item in merchants if item["merchant_key"] != merchant_key and merchants_similar(merchant_key, item["merchant_key"])),
        key=lambda item: (-item["transaction_count"], item["merchant_key"]),
    )
    return {"generation": generation, "merchant_key": merchant_key, "suggestions": suggestions[:20]}


_RULE_SUGGESTION_MIN_OVERRIDES = 3


def rule_suggestions(database: Path, *, generation: str) -> dict[str, Any]:
    """Merchants with >= 3 manual overrides that agree on one category and
    have no category rule yet (Step 6c) -- a prompt, never an automatic rule.
    """
    from .categorization import rule_id_for

    connection = _pinned_connection(database, generation)
    try:
        rows = connection.execute(
            """
            SELECT COALESCE(o.merchant_canonical, o.merchant_normalized) AS merchant_key,
                   co.group_name, co.category, count(*) AS occurrences
            FROM category_overrides co
            JOIN ledger_entries e ON e.natural_anchor = co.entry_anchor
            JOIN merchant_observations o ON o.entry_id = e.id
            WHERE co.bound = 1 AND COALESCE(o.merchant_canonical, o.merchant_normalized) IS NOT NULL
            GROUP BY merchant_key, co.group_name, co.category
            HAVING count(*) >= ?
            """,
            (_RULE_SUGGESTION_MIN_OVERRIDES,),
        ).fetchall()
        suggestions = []
        for row in rows:
            match = {"merchant_canonical": row["merchant_key"]}
            rule_id = rule_id_for(match)
            if connection.execute("SELECT 1 FROM category_rules WHERE rule_id=?", (rule_id,)).fetchone():
                continue
            category = f'{row["group_name"]} / {row["category"]}'
            suggestions.append({
                "merchant_key": row["merchant_key"], "category": category, "occurrences": row["occurrences"],
                "match": match,
                "label": f'You categorized {row["merchant_key"]} as {category} {row["occurrences"]} times. Make it a rule?',
            })
        suggestions.sort(key=lambda item: (-item["occurrences"], item["merchant_key"]))
        return {"generation": generation, "suggestions": suggestions}
    finally:
        connection.close()


def _merchant_index(connection: Any) -> list[dict[str, Any]]:
    """Every observed merchant identity, aggregated across ledger history.

    Not focus/date/currency scoped -- a merge or its suggestions must consider
    every statement variant ever seen, the same all-time posture as the rest
    of the categorization inbox (§0, F1).
    """
    rows = connection.execute(
        """SELECT o.merchant_canonical AS key, o.merchant_normalized AS variant, e.effective_at
           FROM merchant_observations o JOIN ledger_entries e ON e.id = o.entry_id
           WHERE o.merchant_canonical IS NOT NULL"""
    ).fetchall()
    aliases = {row["canonical"]: row["display_name"] for row in connection.execute(
        "SELECT canonical, display_name FROM merchant_aliases"
    )}
    rule_keys: set[str] = set()
    for table, *_rest in _MERGE_RULE_TABLES:
        for row in connection.execute(f"SELECT match_json FROM {table}"):
            match = json.loads(row["match_json"])
            if "merchant_canonical" in match:
                rule_keys.add(match["merchant_canonical"])
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = grouped.setdefault(row["key"], {"variants": set(), "transaction_count": 0, "last_seen": None})
        if row["variant"]:
            item["variants"].add(row["variant"])
        item["transaction_count"] += 1
        if item["last_seen"] is None or row["effective_at"] > item["last_seen"]:
            item["last_seen"] = row["effective_at"]
    return [
        {
            "merchant_key": key, "display_name": aliases.get(key),
            "variants": sorted(item["variants"]), "transaction_count": item["transaction_count"],
            "last_seen": item["last_seen"], "has_rules": key in rule_keys,
        }
        for key, item in sorted(grouped.items(), key=lambda kv: (-kv[1]["transaction_count"], kv[0]))
    ]


def merchant_rename_preview(database: Path, *, generation: str, merchant_key: str, display_name: str,
                            reason: str) -> dict[str, Any]:
    """Return the one `MERCHANT_ALIAS_SET` that changes only the *display*
    name for `merchant_key`, never the canonical it matches against.

    The canonical always stays `merchant_key`: when no alias exists yet, the
    new alias's canonical equals the un-aliased `merchant_normalized` value,
    so every existing tier-3 rule keeps matching (now through tier 4 —
    `engine.py:506-514` resolves the same match value at whichever tier is
    active). This is deliberately read-only; the caller submits the returned
    command through the existing `/batch` endpoint.
    """
    from .categorization import validate_alias_patterns
    display_name = display_name.strip()
    if not display_name:
        raise FinCLError("A display name is required")
    if len(display_name) > 80:
        raise FinCLError("Display name must be 80 characters or fewer")
    if not merchant_key.strip() or not reason.strip():
        raise FinCLError("A merchant key and reason are required")
    connection = _pinned_connection(database, generation)
    try:
        existing = connection.execute(
            "SELECT patterns_json, display_name FROM merchant_aliases WHERE canonical = ?", (merchant_key,),
        ).fetchone()
        current_value = (existing["display_name"] if existing else None) or merchant_key
        if display_name == current_value:
            raise FinCLError("The new display name must differ from the current value")
        patterns = json.loads(existing["patterns_json"]) if existing is not None else [{"exact": merchant_key}]
        patterns = validate_alias_patterns(patterns)
        command = {
            "kind": "MERCHANT_ALIAS_SET", "anchors": {"canonical": merchant_key},
            "payload": {"patterns": patterns, "replace_patterns": False, "display_name": display_name, "reason": reason},
        }
        affected_transactions = connection.execute(
            "SELECT COUNT(*) AS n FROM merchant_observations WHERE merchant_canonical = ?", (merchant_key,),
        ).fetchone()["n"]
        return {
            "generation": generation, "merchant_key": merchant_key, "display_name": display_name,
            "commands": [command], "affected_transactions": affected_transactions,
        }
    finally:
        connection.close()


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
            "combined_completeness": _combined_completeness(connection),
        }
    finally:
        connection.close()


def _combined_completeness(connection: Any) -> dict[str, int]:
    """A separately named measure of *every* applicable classification
    dimension being resolved — category, plus spending pattern/need for a
    spending row or incoming type for an eligible incoming row — not just
    category assignment (PLAN §10: "Do not imply that category assignment
    alone completes all classification work"). An incoming receipt is
    complete without Essential/Discretionary, since those do not apply to it."""
    from .baseline.outliers import outlier_anchor_set
    from .classification import _applicability, _detail_bucket, _is_unresolved
    from .queries import _ENTRY_VIEW, _rule_tables, _shape

    rows = connection.execute(f"SELECT * FROM ({_ENTRY_VIEW}) WHERE selection_state='SELECTED'").fetchall()
    role_rules, inbound_rules = _rule_tables(connection)
    current = current_generation(connection)
    outliers = outlier_anchor_set(connection, current["generation_token"]) if current else set()
    denominator = 0
    numerator = 0
    for row in rows:
        raw = dict(row)
        detail_bucket = _detail_bucket(raw)
        if detail_bucket == "excluded":
            continue
        denominator += 1
        applicable = _applicability(detail_bucket)
        shaped = _shape(row, role_rules, inbound_rules, outliers)
        complete = (
            (not applicable["category"] or not _is_unresolved("category", shaped["category"]))
            and (not applicable["cost_behavior"] or not _is_unresolved("cost_behavior", shaped["cost_behavior"]))
            and (not applicable["need"] or not _is_unresolved("need", shaped["need"]))
            and (not applicable["incoming_type"] or not _is_unresolved("incoming_type", raw.get("classification")))
        )
        if complete:
            numerator += 1
    return {"numerator": numerator, "denominator": denominator}


CANDIDATE_SORTS = ("active", "amount")


def _card_total_decimal(card: dict[str, Any]) -> Decimal:
    values = card["total_abs_amount"].values()
    return max((Decimal(value) for value in values), default=Decimal(0))


def categorization_candidates(
    database: Path, *, generation: str, cursor: str | None = None, limit: int = 50,
    account: str = "", currency: str = "", direction: str = "", lifecycle: str = "", kind: str = "",
    focus: str = "category", sort: str = "active",
) -> dict[str, Any]:
    """Group unclassified records, or another quick-discovery focus, without
    persisting a second projection."""
    if sort not in CANDIDATE_SORTS:
        raise FinCLError(f"Unknown categorization sort {sort!r}")
    connection = _pinned_connection(database, generation)
    try:
        limit = max(1, min(int(limit), 100))
        groups = _focus_groups(
            connection, focus=focus, account=account, currency=currency, direction=direction, lifecycle=lifecycle,
        )
        cards = [_candidate_card(group_kind, key, members, focus=focus) for (group_kind, key), members in groups.items()
                 if not kind or group_kind == kind]
        # Stable passes make the requested priority explicit: actionable rows,
        # then total matching rows, then the newest occurrence and stable id
        # -- or, under `sort="amount"` (Step 6g), the largest observed total
        # first, so high-value groups are handled before high-count ones.
        cards.sort(key=lambda card: card["candidate_id"])
        cards.sort(key=lambda card: card["date_range"]["last"], reverse=True)
        if sort == "amount":
            cards.sort(key=_card_total_decimal, reverse=True)
        else:
            cards.sort(key=lambda card: card["transaction_count"], reverse=True)
            cards.sort(key=lambda card: card["active_count"], reverse=True)
        offset = _decode_cursor(cursor)
        page = cards[offset:offset + limit]
        next_cursor = _encode_cursor(offset + limit) if offset + limit < len(cards) else None
        return {
            "generation": generation,
            "candidate_version": CANDIDATE_GROUPING_VERSION,
            "candidates": page,
            "next_cursor": next_cursor,
            "total": len(cards),
            "focus": focus,
            "sort": sort,
        }
    finally:
        connection.close()


def _merchant_usual_categories(connection: Any, merchant_keys: set[str]) -> dict[str, str]:
    """Each merchant's most-observed effective category, for read-only refund
    context (Step 6e) -- never used to set a category, bucket or incoming type."""
    if not merchant_keys:
        return {}
    placeholders = ",".join("?" for _ in merchant_keys)
    rows = connection.execute(
        f"""
        SELECT COALESCE(o.merchant_canonical, o.merchant_normalized) AS merchant_key,
               ec.group_name, ec.category, count(*) AS occurrences
        FROM entry_categories ec
        JOIN ledger_entries e ON e.id = ec.entry_id
        JOIN merchant_observations o ON o.entry_id = e.id
        WHERE COALESCE(o.merchant_canonical, o.merchant_normalized) IN ({placeholders})
          AND ec.group_name != 'Unclassified'
        GROUP BY merchant_key, ec.group_name, ec.category
        """,
        list(merchant_keys),
    ).fetchall()
    best: dict[str, tuple[int, str]] = {}
    for row in rows:
        category = f'{row["group_name"]} / {row["category"]}'
        if row["merchant_key"] not in best or row["occurrences"] > best[row["merchant_key"]][0]:
            best[row["merchant_key"]] = (row["occurrences"], category)
    return {key: category for key, (_occurrences, category) in best.items()}


def categorization_candidate_detail(
    database: Path, *, generation: str, candidate_id: str, cursor: str | None = None, limit: int = 50,
    account: str = "", currency: str = "", direction: str = "", lifecycle: str = "", kind: str = "",
    focus: str = "category",
) -> dict[str, Any]:
    """Return one current candidate and its bounded, inspectable members."""
    connection = _pinned_connection(database, generation)
    try:
        for (group_kind, key), members in _focus_groups(
            connection, focus=focus, account=account, currency=currency, direction=direction, lifecycle=lifecycle,
        ).items():
            if kind and group_kind != kind:
                continue
            card = _candidate_card(group_kind, key, members, focus=focus)
            if card["candidate_id"] != candidate_id:
                continue
            limit = max(1, min(int(limit), 100))
            ordered = sorted(
                members,
                key=lambda member: (member[0]["effective_at"], member[0]["natural_anchor"]),
                reverse=True,
            )
            offset = _decode_cursor(cursor)
            # The inbox is a read model, but its row applicability and current
            # values must use the same evaluator as classification preview.
            from .baseline.outliers import outlier_anchor_set
            from .classification import _applicability, _detail_bucket
            from .queries import _ENTRY_VIEW, _rule_tables, _shape
            page_members = ordered[offset:offset + limit]
            anchors = [row["natural_anchor"] for row, _label in page_members]
            marks = ",".join("?" for _ in anchors)
            effective_rows = connection.execute(
                f"SELECT * FROM ({_ENTRY_VIEW}) WHERE natural_anchor IN ({marks})", anchors,
            ).fetchall() if anchors else []
            effective_by_anchor = {item["natural_anchor"]: item for item in effective_rows}
            role_rules, inbound_rules = _rule_tables(connection)
            current = current_generation(connection)
            outliers = outlier_anchor_set(connection, current["generation_token"]) if current else set()
            shaped_members = []
            for row, _label in page_members:
                effective = effective_by_anchor[row["natural_anchor"]]
                raw = dict(effective)
                bucket = _detail_bucket(raw)
                shaped = _shape(effective, role_rules, inbound_rules, outliers)
                merchant_key = row["merchant_canonical"] or row["merchant_normalized"]
                shaped_members.append((row, raw, bucket, shaped, merchant_key))
            refund_merchants = {
                merchant_key for _row, _raw, bucket, _shaped, merchant_key in shaped_members
                if bucket == "refunds" and merchant_key
            }
            usual_categories = _merchant_usual_categories(connection, refund_merchants)
            records = []
            for row, raw, bucket, shaped, merchant_key in shaped_members:
                record = {
                    "anchor": row["natural_anchor"], "account": row["account_id"],
                    "currency": row["currency_pocket"], "amount": row["amount_text"],
                    "date": row["effective_at"][:10], "direction": row["economic_direction"],
                    "lifecycle": row["lifecycle_state"], "active": bool(row["analytics_eligible"]) and row["lifecycle_state"] == "COMPLETED",
                    "description": row["description_text"], "merchant": merchant_key,
                    "merchant_raw": row["merchant_raw"], "merchant_normalized": row["merchant_normalized"],
                    "paynow_to": row["paynow_to"],
                    "detail_bucket": bucket, "applicable_fields": _applicability(bucket),
                    "effective": {
                        "category": shaped["category"], "category_basis": shaped["basis"],
                        "cost_behavior": shaped["cost_behavior"], "cost_behavior_basis": shaped["cost_behavior_basis"],
                        "need": shaped["need"], "need_basis": shaped["need_basis"],
                        "incoming_type": raw.get("classification"),
                    },
                    "conflicts": {
                        "ambiguous": bool(raw.get("ambiguous")),
                        "direction": bool(raw.get("direction_conflict")),
                    },
                }
                # Step 6e: read-only context only -- never changes category,
                # bucket or incoming type for a refund (user decision).
                if bucket == "refunds" and merchant_key and merchant_key in usual_categories:
                    record["refund_context"] = f"Refund from {row['display_name'] or merchant_key} (usually {usual_categories[merchant_key]})"
                records.append(record)
            return {"generation": generation, "candidate_version": CANDIDATE_GROUPING_VERSION,
                    "candidate": card, "records": records, "focus": focus,
                    "next_cursor": _encode_cursor(offset + limit) if offset + limit < len(ordered) else None}
        raise FinCLError("Categorization candidate not found")
    finally:
        connection.close()


_BAND_MIN_TRANSACTIONS = 4
_BAND_MIN_SIDE = 2
_BAND_MIN_GAP_RATIO = Decimal("0.2")


_AmountPart = dict[str, int]  # {"coefficient": int, "scale": int}
_BandBound = dict[str, _AmountPart]  # {"min": <part>, "max": <part>}


def _decimal_value(coefficient: int, scale: int) -> Decimal:
    return Decimal(coefficient).scaleb(-scale)


def _amount_band_split(
    magnitudes: list[_AmountPart],
) -> tuple[_BandBound, _BandBound] | None:
    """Split observed amounts into two bands at their widest relative gap.

    Returns the (lower, upper) cluster bounds as ``{coefficient, scale}``
    amount parts taken straight from the observed rows, or ``None`` when the
    amounts do not separate into two clusters with a clearly wider gap
    between them than within them.
    """
    if len(magnitudes) < _BAND_MIN_TRANSACTIONS:
        return None
    ordered = sorted(magnitudes, key=lambda part: _decimal_value(part["coefficient"], part["scale"]))
    values = [_decimal_value(part["coefficient"], part["scale"]) for part in ordered]
    span = values[-1] - values[0]
    if span <= 0:
        return None
    gap, split_at = max((values[i + 1] - values[i], i) for i in range(len(values) - 1))
    if gap / span < _BAND_MIN_GAP_RATIO:
        return None
    lower, upper = ordered[: split_at + 1], ordered[split_at + 1 :]
    if len(lower) < _BAND_MIN_SIDE or len(upper) < _BAND_MIN_SIDE:
        return None
    return ({"min": lower[0], "max": lower[-1]}, {"min": upper[0], "max": upper[-1]})


def _amount_band_options(connection: Any, *, base_match: dict[str, str], where: str, params: tuple) -> list[dict[str, Any]]:
    """Offer an ``amount_band`` refinement of ``base_match`` when its observed amounts cluster."""
    rows = connection.execute(
        f"""
        SELECT abs(e.amount_coefficient) AS coefficient, e.amount_scale AS scale, e.currency_pocket AS currency
        FROM ledger_entries e
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        JOIN merchant_observations o ON o.entry_id = e.id
        WHERE x.selection_state='SELECTED' AND {where}
        """,
        params,
    ).fetchall()
    currencies = {row["currency"] for row in rows}
    if len(currencies) != 1:
        return []
    split = _amount_band_split([{"coefficient": row["coefficient"], "scale": row["scale"]} for row in rows])
    if split is None:
        return []
    currency = next(iter(currencies))
    options = []
    for bound in split:
        low = format_decimal(bound["min"]["coefficient"], bound["min"]["scale"])
        high = format_decimal(bound["max"]["coefficient"], bound["max"]["scale"])
        options.append({
            "label": f"Only amounts between {currency} {low} and {currency} {high}",
            "match": {**base_match, "amount_band": {"currency": currency, **bound}},
        })
    return options


def _longest_common_prefix(strings: set[str]) -> str:
    items = list(strings)
    prefix = items[0]
    for other in items[1:]:
        limit = min(len(prefix), len(other))
        cut = 0
        while cut < limit and prefix[cut] == other[cut]:
            cut += 1
        prefix = prefix[:cut]
        if not prefix:
            break
    return prefix


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
            merchant_key = next(iter(merchants))
            options.append({"label": "All transactions from this merchant", "match": {"merchant_canonical": merchant_key}})
            options.extend(_amount_band_options(
                connection, base_match={"merchant_canonical": merchant_key},
                where="COALESCE(o.merchant_canonical, o.merchant_normalized)=?", params=(merchant_key,),
            ))
        payees = {row["paynow_to"] for row in rows}
        if len(payees) == 1 and None not in payees:
            options.append({"label": "All transactions to this PayNow recipient", "match": {"paynow_to": next(iter(payees))}})
        descriptions = {row["description_text"] for row in rows}
        accounts = {row["account_id"] for row in rows}
        if len(descriptions) == 1 and None not in descriptions:
            match: dict[str, str] = {"description_contains": next(iter(descriptions))}
            if len(accounts) == 1:
                match["account"] = next(iter(accounts))
            options.append({"label": "Transactions whose description contains this text", "match": match})
        elif len(descriptions) > 1 and None not in descriptions:
            prefix = _longest_common_prefix(descriptions)
            if len(prefix) >= MIN_LITERAL_LENGTH:
                options.append({
                    "label": f'Transactions whose description starts with "{prefix}"',
                    "match": {"description_prefix": prefix},
                })
        return {"generation": generation, "anchors": sorted(anchors), "options": options,
                "unavailable_reason": "The selected transactions do not share a safe reusable predicate" if not options else None}
    finally:
        connection.close()


def _candidate_groups(
    connection: Any, *, account: str = "", currency: str = "", direction: str = "", lifecycle: str = "",
) -> dict[tuple[str, str], list[Any]]:
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    clauses = ["ec.group_name='Unclassified'", "ec.category='Unclassified'"]
    params: list[str] = []
    for field, value in (("e.account_id", account), ("e.currency_pocket", currency), ("e.economic_direction", direction), ("e.lifecycle_state", lifecycle)):
        if value:
            clauses.append(f"{field}=?")
            params.append(value)
    rows = connection.execute(
        f"""
        SELECT e.natural_anchor,e.account_id,e.currency_pocket,e.effective_at,e.economic_direction,
               e.amount_text,e.analytics_eligible,e.lifecycle_state,o.merchant_canonical,ma.display_name,
               o.merchant_raw,o.merchant_normalized,o.paynow_to,o.description_text
        FROM entry_categories ec JOIN ledger_entries e ON e.id=ec.entry_id
        JOIN extraction_runs x ON x.id=e.extraction_run_id AND x.selection_state='SELECTED'
        JOIN merchant_observations o ON o.entry_id=e.id
        LEFT JOIN merchant_aliases ma ON ma.canonical=o.merchant_canonical
        WHERE {' AND '.join(clauses)}
        """, params,
    ).fetchall()
    for row in rows:
        kind, key, label = _candidate_key(row)
        groups[(kind, key)].append((row, label))
    return groups


def _focus_groups(
    connection: Any, *, focus: str, account: str = "", currency: str = "", direction: str = "",
    lifecycle: str = "",
) -> dict[tuple[str, str], list[Any]]:
    if focus == "category":
        return _candidate_groups(
            connection, account=account, currency=currency, direction=direction, lifecycle=lifecycle,
        )
    if focus not in FOCUS_KINDS:
        raise FinCLError(f"Unknown categorization focus {focus!r}")
    return _derived_focus_groups(
        connection, focus, account=account, currency=currency, direction=direction, lifecycle=lifecycle,
    )


def _derived_focus_groups(
    connection: Any, focus: str, *, account: str = "", currency: str = "", direction: str = "",
    lifecycle: str = "",
) -> dict[tuple[str, str], list[Any]]:
    """Group entries by the same merchant/PayNow/description identity as the
    category inbox (§10's "authoritative filters", not a frontend-only
    re-filter), but selected by an *effective* dimension -- unresolved spending
    pattern/need, unresolved incoming type, or an ambiguous/direction-conflict
    flag -- rather than by raw category text. This reuses the exact bucket,
    applicability, and effective-role evaluator `evaluate_classification_draft`
    already uses, so a group here can never disagree with what the shared
    classification editor would show for the same anchor. Grouping keys come
    from the closest equivalent fields exposed by that shared shaping
    (`role_merchant_match`/`role_paynow_to`/`role_description_text`), which is
    a deliberately close but not byte-identical approximation of the
    category-focus grouping's own dedicated merchant-observation columns."""
    from .baseline.outliers import outlier_anchor_set
    from .classification import _applicability, _detail_bucket
    from .queries import _ENTRY_VIEW, _rule_tables, _shape

    clauses = ["selection_state='SELECTED'"]
    params: list[str] = []
    for field, value in (("account_id", account), ("currency_pocket", currency), ("economic_direction", direction), ("lifecycle_state", lifecycle)):
        if value:
            clauses.append(f"{field}=?")
            params.append(value)
    rows = connection.execute(
        f"SELECT * FROM ({_ENTRY_VIEW}) WHERE {' AND '.join(clauses)}", params,
    ).fetchall()
    role_rules, inbound_rules = _rule_tables(connection)
    current = current_generation(connection)
    outliers = outlier_anchor_set(connection, current["generation_token"]) if current else set()
    # Looked up once, not per row: focus queues (unlike the category inbox's
    # own dedicated query) don't already join `merchant_aliases`, so without
    # this every merchant here shows its raw key instead of its display name
    # (Plan §Step 2 F2.5).
    display_names = {
        row["canonical"]: row["display_name"]
        for row in connection.execute("SELECT canonical, display_name FROM merchant_aliases")
        if row["display_name"]
    }
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in rows:
        raw = dict(row)
        detail_bucket = _detail_bucket(raw)
        if focus == "spending_details":
            if detail_bucket != "spending":
                continue
            shaped = _shape(row, role_rules, inbound_rules, outliers)
            if "unresolved" not in (shaped["cost_behavior"], shaped["need"]):
                continue
        elif focus == "incoming":
            if not _applicability(detail_bucket)["incoming_type"] or raw.get("classification") is not None:
                continue
        else:  # conflicts
            if not (raw["ambiguous"] or raw["direction_conflict"]):
                continue
        adapted = {
            "natural_anchor": raw["natural_anchor"], "account_id": raw["account_id"],
            "currency_pocket": raw["currency_pocket"], "effective_at": raw["effective_at"],
            "economic_direction": raw["economic_direction"], "amount_text": raw["amount_text"],
            "analytics_eligible": raw["analytics_eligible"], "lifecycle_state": raw["lifecycle_state"],
            "merchant_canonical": raw["role_merchant_match"], "merchant_raw": raw["role_merchant_match"],
            "merchant_normalized": raw["role_merchant_match"],
            "display_name": display_names.get(raw["role_merchant_match"]), "paynow_to": raw["role_paynow_to"],
            "description_text": raw["role_description_text"],
        }
        kind, key, label = _candidate_key(adapted)
        groups[(kind, key)].append((adapted, label))
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
        return "merchant", str(merchant), str(row["display_name"] or merchant)
    if row["paynow_to"]:
        return "paynow_recipient", str(row["paynow_to"]), str(row["paynow_to"])
    description = (row["description_text"] or "").strip()
    if description:
        return "description", description.casefold(), description
    return "missing_merchant", str(row["account_id"]), f"Missing merchant — {row['account_id']}"


def _candidate_card(kind: str, key: str, members: list[tuple[Any, str]], *, focus: str = "category") -> dict[str, Any]:
    rows = [member[0] for member in members]
    label = members[0][1]
    # `focus` is folded into the id so the same merchant under two different
    # focuses (e.g. "category" and "spending_details") never collides.
    canonical = canonical_json([CANDIDATE_GROUPING_VERSION, focus, kind, key])
    candidate_id = "catcand_" + hashlib.sha256(canonical.encode()).hexdigest()[:20]
    currencies = Counter(row["currency_pocket"] for row in rows)
    directions = Counter(row["economic_direction"] for row in rows)
    active_count = sum(bool(row["analytics_eligible"]) and row["lifecycle_state"] == "COMPLETED" for row in rows)
    examples = list(dict.fromkeys((row["description_text"] or "").strip() for row in rows if row["description_text"]))[:3]
    by_currency: dict[str, list[ExactMoney]] = defaultdict(list)
    for row in rows:
        amount = ExactMoney.parse(row["amount_text"])
        by_currency[row["currency_pocket"]].append(ExactMoney(abs(amount.coefficient), amount.scale, amount.text))
    total_abs_amount = {currency: format_decimal(*exact_sum(*values)) for currency, values in by_currency.items()}
    card = {
        "candidate_id": candidate_id, "kind": kind, "label": label,
        "transaction_count": len(rows), "active_count": active_count,
        "date_range": {"first": min(row["effective_at"][:10] for row in rows), "last": max(row["effective_at"][:10] for row in rows)},
        "currencies": dict(sorted(currencies.items())), "directions": dict(sorted(directions.items())),
        "accounts": sorted({str(row["account_id"]) for row in rows}), "examples": examples,
        "total_abs_amount": dict(sorted(total_abs_amount.items())),
    }
    # `candidate_id` already hashes `key`; `merchant_key` exposes the raw
    # canonical/normalized value itself so a rename action has something to
    # preview and submit against without re-deriving it from the label (which
    # is the *display* name once one exists — Plan §Step 2 F2.3).
    if kind == "merchant":
        card["merchant_key"] = key
    return card


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
