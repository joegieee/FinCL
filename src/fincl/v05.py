"""v0.5 setup, inventory, maintenance, and review read models.

Editable metadata remains in the decision journal.  This module deliberately
reconstructs the small metadata maps from that journal instead of introducing
authoritative mutable tables; ledger/import facts continue to come from SQLite.
"""

from __future__ import annotations

import json
import re
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
        row = current_generation(connection)
        if row is None:
            raise FinCLError("The ledger has no completed generation; refresh it first")
        return str(row["generation_token"])
    finally:
        connection.close()


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
        return {"generation": generation_of(database), "aliases": aliases, "rules": rules}
    finally:
        connection.close()


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
