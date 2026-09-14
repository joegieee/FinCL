"""v0.5 setup, inventory, maintenance, and review read models.

Editable metadata remains in the decision journal.  This module deliberately
reconstructs the small metadata maps from that journal instead of introducing
authoritative mutable tables; ledger/import facts continue to come from SQLite.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .categorization import CATEGORIZATION_DECISION_KINDS, validate_payload
from .custom_categories import custom_categories
from .db import connect_readonly
from .decision_journal import JournalRecord, default_journal_path, read_records
from .errors import FinCLError
from .generation import current_generation
from .report import coverage_report_data

V05_DECISION_KINDS = frozenset(
    {
        "CATEGORY_DETAILS_SET",
        "CATEGORY_ARCHIVE_SET",
        "ACCOUNT_SCOPE_SET",
        "ACCOUNT_DETAILS_SET",
        "ACCOUNT_ALIAS_SET",
        "ACCOUNT_ALIAS_RETIRE",
        "REVIEW_DEFER_SET",
        "REVIEW_DEFER_RETIRE",
    }
)

_RESERVED_GROUPS = {"internal", "unclassified"}

_SAFE_LABEL = re.compile(r"^[^/\\\x00-\x1f]{1,80}$")


def _reason(payload: dict[str, Any]) -> str:
    value = payload.get("reason")
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise FinCLError("A reason of 1–2,000 characters is required")
    return value.strip()


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise FinCLError(f"{label} fields must be exactly {sorted(expected)}")


def validate_v05(
    kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    *,
    database: Path,
) -> None:
    """Validate v0.5 commands both at authoring and replay boundaries."""
    if kind in CATEGORIZATION_DECISION_KINDS:
        validate_payload(kind, anchors, payload, custom_categories(database))
        return
    reason = _reason(payload)
    del reason
    if kind == "CATEGORY_DETAILS_SET":
        _exact_keys(anchors, {"category"}, "category detail anchors")
        _exact_keys(payload, {"display_group", "display_name", "reason"}, "category details")
        group, name = str(payload["display_group"]), str(payload["display_name"])
        if not _SAFE_LABEL.fullmatch(group):
            raise FinCLError("Category display group must contain 1–80 safe characters")
        if not _SAFE_LABEL.fullmatch(name):
            raise FinCLError("Category display name must contain 1–80 safe characters")
        if group.strip().casefold() in _RESERVED_GROUPS:
            raise FinCLError("Internal and Unclassified are reserved system groups")
        custom = custom_categories(database)
        if anchors["category"] not in custom:
            raise FinCLError("Only a user-created category can be renamed")
        full = f"{group.strip()} / {name.strip()}".casefold()
        for other in custom:
            if other != anchors["category"] and other.casefold() == full:
                raise FinCLError("Another active category already uses that group and name")
    elif kind == "CATEGORY_ARCHIVE_SET":
        _exact_keys(anchors, {"category"}, "category archive anchors")
        _exact_keys(payload, {"archived", "reason"}, "category archive")
        if not isinstance(payload["archived"], bool):
            raise FinCLError("archived must be a boolean")
        if anchors["category"] not in custom_categories(database):
            raise FinCLError("Only a user-created category can be archived or restored")
    elif kind == "ACCOUNT_SCOPE_SET":
        _exact_keys(anchors, {"account", "pocket"}, "account scope anchors")
        _exact_keys(payload, {"expected_start", "expected_end", "reason"}, "account scope")
        if not str(anchors["pocket"]).strip():
            raise FinCLError("A currency pocket is required for account scope")
        start, end = str(payload["expected_start"]), str(payload["expected_end"])
        try:
            from datetime import date

            date.fromisoformat(start)
            if end:
                date.fromisoformat(end)
        except ValueError as exc:
            raise FinCLError("Account scope dates must be YYYY-MM-DD") from exc
        if end and start > end:
            raise FinCLError("Account scope end cannot precede its start")
    elif kind == "ACCOUNT_DETAILS_SET":
        _exact_keys(anchors, {"account"}, "account detail anchors")
        _exact_keys(payload, {"display_name", "archived_on", "reason"}, "account details")
        if not _SAFE_LABEL.fullmatch(str(payload["display_name"])):
            raise FinCLError("Account display name must contain 1–80 safe characters")
    elif kind == "ACCOUNT_ALIAS_SET":
        _exact_keys(anchors, {"source_identity"}, "account alias anchors")
        _exact_keys(payload, {"account_id", "reason"}, "account alias")
        if not str(payload["account_id"]).strip():
            raise FinCLError("An owned account ID is required")
    elif kind == "ACCOUNT_ALIAS_RETIRE":
        _exact_keys(anchors, {"source_identity"}, "account alias anchors")
        _exact_keys(payload, {"reason"}, "account alias")
    elif kind == "REVIEW_DEFER_SET":
        _exact_keys(anchors, {"issue"}, "review defer anchors")
        _exact_keys(payload, {"fingerprint", "reason"}, "review defer")
        if not str(payload["fingerprint"]).strip():
            raise FinCLError("An issue fingerprint is required")
    elif kind == "REVIEW_DEFER_RETIRE":
        _exact_keys(anchors, {"issue"}, "review defer anchors")
        _exact_keys(payload, {"reason"}, "review defer")
    else:
        raise FinCLError(f"Unsupported v0.5 decision kind {kind}")


def _latest(records: list[JournalRecord], set_kind: str, retire_kind: str, key: str) -> dict[str, JournalRecord]:
    state: dict[str, JournalRecord] = {}
    for record in records:
        identity = record.anchors.get(key)
        if identity is None:
            continue
        if record.decision_kind == set_kind:
            state[identity] = record
        elif record.decision_kind == retire_kind:
            state.pop(identity, None)
    return state


def _latest_by(records: list[JournalRecord], set_kind: str, keys: tuple[str, ...]) -> dict[tuple[str, ...], JournalRecord]:
    """Latest set-record per composite anchor tuple. Used for pocket-scoped kinds."""
    state: dict[tuple[str, ...], JournalRecord] = {}
    for record in records:
        if record.decision_kind != set_kind:
            continue
        identity = tuple(record.anchors.get(part, "") for part in keys)
        if all(identity):
            state[identity] = record
    return state


def generation_of(database: Path) -> str:
    connection = connect_readonly(database)
    try:
        return generation_of_from(connection)
    finally:
        connection.close()


def generation_of_from(connection: sqlite3.Connection) -> str:
    row = current_generation(connection)
    if row is None:
        raise FinCLError("The ledger has no completed generation; refresh it first")
    return str(row["generation_token"])


def setup_options(database: Path) -> dict[str, Any]:
    return {
        "state": "ready",
        "current_ledger": database.name,
        "storage_profile": "plaintext-local",
        "workspace_root": str(database.parent),
        "can_create": True,
        "can_open": True,
    }


def imports_page(database: Path) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        rows = connection.execute(
            """SELECT i.id,i.started_at,i.completed_at,i.status,i.evidence_sha256,
                      i.extraction_run_id,i.extraction_reused,i.error_message,
                      s.byte_size,s.media_type,x.status AS extraction_status,
                      x.selection_state,x.source_profile_version,x.parser_version,
                      coalesce((SELECT count(*) FROM extraction_issues q
                                WHERE q.extraction_run_id=i.extraction_run_id
                                  AND q.severity='BLOCKING'),0) AS blockers
               FROM import_runs i
               LEFT JOIN source_content s ON s.sha256=i.evidence_sha256
               LEFT JOIN extraction_runs x ON x.id=i.extraction_run_id
               ORDER BY i.started_at DESC,i.id DESC LIMIT 200"""
        ).fetchall()
        return {"generation": generation_of(database), "imports": [dict(row) for row in rows]}
    finally:
        connection.close()


def import_detail(database: Path, import_id: str) -> dict[str, Any]:
    page = imports_page(database)
    item = next((row for row in page["imports"] if row["id"] == import_id), None)
    if item is None:
        raise FinCLError("Import operation not found")
    connection = connect_readonly(database)
    try:
        issues = connection.execute(
            """SELECT severity,code,message,context_json FROM extraction_issues
               WHERE extraction_run_id=? ORDER BY id""",
            (item["extraction_run_id"],),
        ).fetchall()
        item["issues"] = [
            {**dict(row), "context": json.loads(row["context_json"])} for row in issues
        ]
        return item
    finally:
        connection.close()


def accounts_page(database: Path) -> dict[str, Any]:
    records = read_records(default_journal_path(database))
    details = _latest(records, "ACCOUNT_DETAILS_SET", "ACCOUNT_DETAILS_RETIRE", "account")
    scopes = _latest_by(records, "ACCOUNT_SCOPE_SET", ("account", "pocket"))
    aliases = _latest(records, "ACCOUNT_ALIAS_SET", "ACCOUNT_ALIAS_RETIRE", "source_identity")
    connection = connect_readonly(database)
    try:
        rows = connection.execute(
            """SELECT a.id,a.institution,a.source_type,a.display_name,a.masked_identifier,
                      e.currency_pocket AS pocket,
                      min(substr(e.effective_at,1,10)) AS observed_start,
                      max(substr(e.effective_at,1,10)) AS observed_end,
                      count(e.id) AS entry_count
               FROM accounts a LEFT JOIN ledger_entries e ON e.account_id=a.id
               GROUP BY a.id,e.currency_pocket ORDER BY a.institution,a.display_name,a.id"""
        ).fetchall()
        by_account: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = by_account.setdefault(
                row["id"],
                {
                    "id": row["id"],
                    "institution": row["institution"],
                    "source_type": row["source_type"],
                    "display_name": row["display_name"],
                    "masked_identifier": row["masked_identifier"],
                    "entry_count": 0,
                    "pockets": [],
                },
            )
            detail = details.get(row["id"])
            item["display_name"] = detail.payload["display_name"] if detail else row["display_name"]
            item["archived_on"] = detail.payload["archived_on"] if detail else ""
            item["entry_count"] += row["entry_count"] or 0
            pocket = row["pocket"]
            if pocket is None:
                continue
            scope = scopes.get((row["id"], pocket))
            expected_start = str(scope.payload["expected_start"]) if scope else ""
            expected_end = str(scope.payload["expected_end"]) if scope else ""
            observed_start = str(row["observed_start"]) if row["observed_start"] else ""
            observed_end = str(row["observed_end"]) if row["observed_end"] else ""
            status = (
                "unconfigured" if scope is None
                else "gap" if not observed_start or observed_start > expected_start
                or (expected_end and observed_end < expected_end)
                else "covered"
            )
            item["pockets"].append(
                {
                    "pocket": pocket,
                    "expected_start": expected_start or None,
                    "expected_end": expected_end or None,
                    "observed_start": observed_start or None,
                    "observed_end": observed_end or None,
                    "entry_count": row["entry_count"] or 0,
                    "coverage_status": status,
                }
            )
        _RANK = {"gap": 0, "unconfigured": 1, "covered": 2}
        result: list[dict[str, Any]] = []
        for item in by_account.values():
            item["pockets"].sort(key=lambda p: p["pocket"])
            item["currencies"] = [p["pocket"] for p in item["pockets"]]
            item["coverage_status"] = (
                "unconfigured" if not item["pockets"]
                else min((p["coverage_status"] for p in item["pockets"]), key=lambda s: _RANK[s])
            )
            item["expected_start"] = next(
                (p["expected_start"] for p in item["pockets"] if p["expected_start"]), None
            )
            item["expected_end"] = next(
                (p["expected_end"] for p in item["pockets"] if p["expected_end"]), None
            )
            result.append(item)
        return {
            "generation": generation_of(database),
            "accounts": result,
            "aliases": [
                {"source_identity": key, **record.payload} for key, record in sorted(aliases.items())
            ],
            "global_coverage": coverage_report_data(database),
        }
    finally:
        connection.close()


def categories_page(database: Path) -> dict[str, Any]:
    records = read_records(default_journal_path(database))
    details = _latest(records, "CATEGORY_DETAILS_SET", "CATEGORY_DETAILS_RETIRE", "category")
    archived = _latest(records, "CATEGORY_ARCHIVE_SET", "CATEGORY_ARCHIVE_RETIRE", "category")
    custom = custom_categories(database)
    connection = connect_readonly(database)
    try:
        rule_counts = {
            f"{row['group_name']} / {row['category']}": row["n"]
            for row in connection.execute(
                "SELECT group_name, category, count(*) AS n FROM category_rules GROUP BY group_name, category"
            )
        }
    finally:
        connection.close()
    categories: list[dict[str, Any]] = []
    for key in sorted(custom):
        group, name = key.split(" / ", 1) if " / " in key else ("", key)
        detail = details.get(key)
        categories.append(
            {
                "key": key,
                "display_group": detail.payload["display_group"] if detail else group,
                "display_name": detail.payload["display_name"] if detail else name,
                "archived": bool(archived[key].payload["archived"]) if key in archived else False,
                "active_rule_count": rule_counts.get(key, 0),
                "custom": True,
            }
        )
    return {"generation": generation_of(database), "categories": categories}


def rules_page(database: Path) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        aliases = [dict(row) for row in connection.execute(
            "SELECT canonical,patterns_json,display_name,reason,journal_seq FROM merchant_aliases ORDER BY canonical"
        )]
        rules = [dict(row) for row in connection.execute(
            "SELECT rule_id,match_json,group_name,category,reason,journal_seq FROM category_rules ORDER BY journal_seq DESC"
        )]
        for row in aliases:
            row["patterns"] = json.loads(row.pop("patterns_json"))
        for row in rules:
            row["match"] = json.loads(row.pop("match_json"))
        # Current observed impact, not a claim about every possible future
        # description.  Reuse the categorization evaluator so row metrics and
        # the actual projection share precedence semantics.
        from .engine import _Rule, _rule_matches_entry, evaluate_current_entries
        evaluated = evaluate_current_entries(connection)
        override_anchors = {
            override["entry_anchor"] for override in connection.execute(
                "SELECT entry_anchor FROM category_overrides WHERE bound=1"
            )
        }
        for row in rules:
            rule = _Rule(row["rule_id"], json.dumps(row["match"]), row["group_name"], row["category"])
            predicate_matches = [item for item in evaluated if _rule_matches_entry(rule, item.entry)]
            effective = [item for item in evaluated if item.basis.endswith(row["rule_id"])]
            row["metrics"] = {
                "predicate_matches": len(predicate_matches),
                "effective_assignments": len(effective),
                "overridden": sum(item.entry["natural_anchor"] in override_anchors for item in predicate_matches),
                "direction_conflicts": sum(item.direction_conflict for item in predicate_matches),
                "first_match": min((item.entry["effective_at"][:10] for item in predicate_matches), default=None),
                "last_match": max((item.entry["effective_at"][:10] for item in predicate_matches), default=None),
            }
        for row in aliases:
            canonical = row["canonical"]
            observed = [item for item in evaluated if item.entry["merchant_canonical"] == canonical]
            effective = [item for item in evaluated if item.basis.startswith(f"alias:{canonical}:")]
            row["metrics"] = {
                "observed_matches": len(observed),
                "effective_assignments": len(effective),
                "first_match": min((item.entry["effective_at"][:10] for item in observed), default=None),
                "last_match": max((item.entry["effective_at"][:10] for item in observed), default=None),
            }
        active_ids = {row["rule_id"] for row in rules}
        latest: dict[str, tuple[str, dict[str, Any], int]] = {}
        for record in read_records(default_journal_path(database)):
            children = record.payload.get("items", []) if record.decision_kind == "DECISION_BATCH" else []
            candidates = children if isinstance(children, list) else []
            if record.decision_kind in {"CATEGORY_RULE_SET", "CATEGORY_RULE_RETIRE"}:
                candidates = [{"kind": record.decision_kind, "anchors": record.anchors, "payload": record.payload}]
            for child in candidates:
                if not isinstance(child, dict) or child.get("kind") not in {"CATEGORY_RULE_SET", "CATEGORY_RULE_RETIRE"}:
                    continue
                anchors = child.get("anchors", {})
                payload = child.get("payload", {})
                if isinstance(anchors, dict) and isinstance(payload, dict) and isinstance(anchors.get("rule_id"), str):
                    latest[anchors["rule_id"]] = (str(child["kind"]), payload, record.seq)
        inactive_rules = []
        for rule_id, (kind, payload, seq) in latest.items():
            if kind != "CATEGORY_RULE_RETIRE" or rule_id in active_ids:
                continue
            # The last set record carries the durable predicate/outcome needed
            # for an inspectable inactive row and later reactivation.
            history = rule_history(database, rule_id)["history"]
            prior_set = next((item for item in reversed(history) if item["kind"] == "CATEGORY_RULE_SET"), None)
            if prior_set is None or not isinstance(prior_set["payload"], dict):
                continue
            set_payload = prior_set["payload"]
            category = set_payload.get("category")
            if not isinstance(category, str) or " / " not in category:
                continue
            group_name, category_name = category.split(" / ", 1)
            inactive_rules.append({"rule_id": rule_id, "match": set_payload.get("match", {}), "group_name": group_name,
                                   "category": category_name, "reason": payload.get("reason"), "journal_seq": seq,
                                   "metrics": {"predicate_matches": 0, "effective_assignments": 0, "overridden": 0,
                                               "direction_conflicts": 0, "first_match": None, "last_match": None}})
        return {"generation": generation_of(database), "aliases": aliases, "rules": rules, "inactive_rules": inactive_rules}
    finally:
        connection.close()


def _match_label(match: dict[str, Any]) -> str:
    if "merchant_canonical" in match:
        return f"Merchant: {match['merchant_canonical']}"
    if "paynow_to" in match:
        return f"Recipient: {match['paynow_to']}"
    if "description_prefix" in match:
        return f"Description starts with: {match['description_prefix']}"
    label = f"Description contains: {match['description_contains']}"
    if "account" in match:
        label += f" (account {match['account']})"
    return label


def grouped_rules_page(database: Path) -> dict[str, Any]:
    """One row per ``rule_id``, joining category/role/inbound rules that share
    a match predicate (§0.1) — the Rules page's grouped read model."""
    from .financial_roles import _matches
    from .queries import _ENTRY_VIEW

    connection = connect_readonly(database)
    try:
        category_rows = {row["rule_id"]: dict(row) for row in connection.execute(
            "SELECT rule_id, match_json, group_name, category, reason, journal_seq FROM category_rules"
        )}
        role_rows = {row["rule_id"]: dict(row) for row in connection.execute(
            "SELECT rule_id, match_json, cost_behavior, need, reason, journal_seq FROM role_rules"
        )}
        inbound_rows = {row["rule_id"]: dict(row) for row in connection.execute(
            "SELECT rule_id, match_json, classification, reason, journal_seq FROM inbound_rules"
        )}
        rule_ids = set(category_rows) | set(role_rows) | set(inbound_rows)
        entries = []
        for row in connection.execute(f"SELECT * FROM ({_ENTRY_VIEW})").fetchall():
            raw = dict(row)
            entries.append({**raw, "account_id": raw["role_account_id"], "description_text": raw["role_description_text"],
                             "paynow_to": raw["role_paynow_to"], "merchant_match": raw["role_merchant_match"]})
    finally:
        connection.close()
    seq_time = {record.seq: record.decided_at for record in read_records(default_journal_path(database))}
    grouped: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        cat, role, inbound = category_rows.get(rule_id), role_rows.get(rule_id), inbound_rows.get(rule_id)
        source = cat or role or inbound
        assert source is not None
        match = json.loads(source["match_json"])
        kinds = [name for name, row in (("category", cat), ("role", role), ("inbound", inbound)) if row is not None]
        last_seq = max(row["journal_seq"] for row in (cat, role, inbound) if row is not None)
        grouped.append({
            "rule_id": rule_id,
            "match": match,
            "match_label": _match_label(match),
            "category": f"{cat['group_name']} / {cat['category']}" if cat else None,
            "cost_behavior": role["cost_behavior"] if role else None,
            "need": role["need"] if role else None,
            "incoming_type": inbound["classification"] if inbound else None,
            "kinds": kinds,
            "matches": sum(1 for entry in entries if _matches(entry, match)),
            "last_changed": seq_time.get(last_seq),
            "reason": source["reason"],
        })
    grouped.sort(key=lambda item: (item["last_changed"] or "", item["rule_id"]), reverse=True)
    return {"generation": generation_of(database), "grouped": grouped}


def rule_history(database: Path, rule_id: str) -> dict[str, Any]:
    """Journal-backed history, including rule changes authored in a batch."""
    rows: list[dict[str, Any]] = []
    for record in read_records(default_journal_path(database)):
        children = record.payload.get("items", []) if record.decision_kind == "DECISION_BATCH" else []
        candidates = children if isinstance(children, list) else []
        if record.decision_kind in {"CATEGORY_RULE_SET", "CATEGORY_RULE_RETIRE"}:
            candidates = [{"kind": record.decision_kind, "anchors": record.anchors, "payload": record.payload}]
        for index, child in enumerate(candidates):
            if not isinstance(child, dict) or child.get("kind") not in {"CATEGORY_RULE_SET", "CATEGORY_RULE_RETIRE"}:
                continue
            anchors = child.get("anchors", {})
            if isinstance(anchors, dict) and anchors.get("rule_id") == rule_id:
                payload = child.get("payload", {})
                rows.append({"journal_seq": record.seq, "batch_index": index if record.decision_kind == "DECISION_BATCH" else None,
                             "recorded_at": record.decided_at, "kind": child["kind"],
                             "reason": payload.get("reason", "") if isinstance(payload, dict) else "",
                             "payload": payload})
    return {"generation": generation_of(database), "rule_id": rule_id, "history": rows}


def alias_history(database: Path, canonical: str) -> dict[str, Any]:
    """Journal-backed merchant-alias history, including atomic batches."""
    rows: list[dict[str, Any]] = []
    for record in read_records(default_journal_path(database)):
        children = record.payload.get("items", []) if record.decision_kind == "DECISION_BATCH" else []
        candidates = children if isinstance(children, list) else []
        if record.decision_kind in {"MERCHANT_ALIAS_SET", "MERCHANT_ALIAS_RETIRE"}:
            candidates = [{"kind": record.decision_kind, "anchors": record.anchors, "payload": record.payload}]
        for index, child in enumerate(candidates):
            if not isinstance(child, dict) or child.get("kind") not in {"MERCHANT_ALIAS_SET", "MERCHANT_ALIAS_RETIRE"}:
                continue
            anchors = child.get("anchors", {})
            if isinstance(anchors, dict) and anchors.get("canonical") == canonical:
                payload = child.get("payload", {})
                rows.append({"journal_seq": record.seq, "batch_index": index if record.decision_kind == "DECISION_BATCH" else None,
                             "recorded_at": record.decided_at, "kind": child["kind"],
                             "reason": payload.get("reason", "") if isinstance(payload, dict) else "",
                             "payload": payload})
    return {"generation": generation_of(database), "canonical": canonical, "history": rows}


def review_page(database: Path) -> dict[str, Any]:
    records = read_records(default_journal_path(database))
    deferred = _latest(records, "REVIEW_DEFER_SET", "REVIEW_DEFER_RETIRE", "issue")
    connection = connect_readonly(database)
    try:
        rows = connection.execute(
            """SELECT 'source' AS queue,'extraction:'||q.id AS issue_key,
                      q.severity AS status,q.code AS title,q.message AS detail,
                      q.extraction_run_id AS owner
               FROM extraction_issues q
               UNION ALL
               SELECT 'categories','entry:'||e.natural_anchor,'review',
                      'Category needs review',c.basis,e.natural_anchor
               FROM entry_categories c JOIN ledger_entries e ON e.id=c.entry_id
               WHERE c.ambiguous=1 OR c.direction_conflict=1
               ORDER BY queue,issue_key"""
        ).fetchall()
        issues: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            fingerprint = __import__("hashlib").sha256(
                json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            item["fingerprint"] = fingerprint
            item["deferred"] = bool(
                item["issue_key"] in deferred
                and deferred[item["issue_key"]].payload["fingerprint"] == fingerprint
            )
            issues.append(item)
        return {"generation": generation_of(database), "issues": issues}
    finally:
        connection.close()


def inverse_preview(database: Path, seq: int) -> dict[str, Any]:
    record = next((row for row in read_records(default_journal_path(database)) if row.seq == seq), None)
    if record is None:
        raise FinCLError("Decision not found")
    mapping = {
        "CATEGORY_OVERRIDE": "CATEGORY_OVERRIDE_RETIRE",
        "MERCHANT_ALIAS_SET": "MERCHANT_ALIAS_RETIRE",
        "CATEGORY_RULE_SET": "CATEGORY_RULE_RETIRE",
        "ACCOUNT_ALIAS_SET": "ACCOUNT_ALIAS_RETIRE",
        "REVIEW_DEFER_SET": "REVIEW_DEFER_RETIRE",
    }
    kind = mapping.get(record.decision_kind)
    if kind is None:
        return {"supported": False, "reason": "This decision has no safe automatic inverse"}
    return {
        "supported": True,
        "generation": generation_of(database),
        "command": {"kind": kind, "anchors": record.anchors, "payload": {"reason": f"Undo decision #{seq}"}},
    }
