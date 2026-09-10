"""Replayable per-entry financial-role decisions (cash-flow usability M3)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .categorization import canonical_match, rule_id_for
from .errors import FinCLError

ENTRY_ROLE_SET = "ENTRY_ROLE_SET"
ENTRY_ROLE_RETIRE = "ENTRY_ROLE_RETIRE"
ROLE_RULE_SET = "ROLE_RULE_SET"
ROLE_RULE_RETIRE = "ROLE_RULE_RETIRE"
FINANCIAL_ROLE_DECISION_KINDS = frozenset({ENTRY_ROLE_SET, ENTRY_ROLE_RETIRE, ROLE_RULE_SET, ROLE_RULE_RETIRE})
_BEHAVIOURS = {"fixed_recurring", "variable_routine", "irregular", "unresolved"}
_NEEDS = {"essential", "discretionary", "unresolved"}


def validate_financial_role(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if kind == ENTRY_ROLE_SET:
        if set(anchors) != {"entry"} or set(payload) - {"cost_behavior", "need", "reason"}:
            raise FinCLError("An entry role sets only behavior and/or need with a reason")
        if not payload.get("reason") or (payload.get("cost_behavior") is None and payload.get("need") is None):
            raise FinCLError("An entry role needs at least one field and a reason")
        if payload.get("cost_behavior") not in _BEHAVIOURS | {None} or payload.get("need") not in _NEEDS | {None}:
            raise FinCLError("Unsupported financial role")
    elif kind == ENTRY_ROLE_RETIRE:
        if set(anchors) != {"entry"} or set(payload) != {"reason"} or not payload["reason"]:
            raise FinCLError("Retiring an entry role needs an entry and reason")
    elif kind == ROLE_RULE_SET:
        if set(anchors) != {"rule_id"} or set(payload) != {"match", "cost_behavior", "need", "reason"}:
            raise FinCLError("A role rule needs match, behavior/need, and reason")
        if not payload["reason"]:
            raise FinCLError("A role rule match and reason are required")
        canonical = canonical_match(payload["match"])
        if anchors["rule_id"] != rule_id_for(canonical):
            raise FinCLError("The role rule id does not match its predicate")
        if payload["cost_behavior"] not in _BEHAVIOURS | {None} or payload["need"] not in _NEEDS | {None}:
            raise FinCLError("Unsupported financial role")
    elif kind == ROLE_RULE_RETIRE:
        if set(anchors) != {"rule_id"} or set(payload) != {"reason"} or not payload["reason"]:
            raise FinCLError("Retiring a role rule needs a reason")


def apply_financial_role(connection: sqlite3.Connection, *, kind: str, anchors: dict[str, str], payload: dict[str, Any], journal_seq: int, resolve_anchor: Any) -> None:
    validate_financial_role(kind, anchors, payload)
    if kind == ROLE_RULE_RETIRE:
        connection.execute("DELETE FROM role_rules WHERE rule_id=?", (anchors["rule_id"],))
        return
    if kind == ROLE_RULE_SET:
        match = canonical_match(payload["match"])
        connection.execute("INSERT OR REPLACE INTO role_rules(rule_id,match_json,cost_behavior,need,reason,journal_seq) VALUES(?,?,?,?,?,?)", (anchors["rule_id"], json.dumps(match, sort_keys=True), payload["cost_behavior"], payload["need"], payload["reason"], journal_seq))
        return
    entry_id = resolve_anchor(anchors["entry"])
    if kind == ENTRY_ROLE_RETIRE:
        if entry_id:
            connection.execute("DELETE FROM entry_financial_roles WHERE entry_id=?", (entry_id,))
        return
    if entry_id:
        old = connection.execute("SELECT cost_behavior,need FROM entry_financial_roles WHERE entry_id=?", (entry_id,)).fetchone()
        connection.execute("INSERT OR REPLACE INTO entry_financial_roles(entry_id,cost_behavior,cost_behavior_basis,need,need_basis,review_state,journal_seq) VALUES(?,?,?,?,?,?,?)", (entry_id, payload.get("cost_behavior") if payload.get("cost_behavior") is not None else (old[0] if old else None), "manual" if payload.get("cost_behavior") is not None else ("manual" if old else None), payload.get("need") if payload.get("need") is not None else (old[1] if old else None), "manual" if payload.get("need") is not None else ("manual" if old else None), "confirmed", journal_seq))


def effective_financial_role(entry: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, str]:
    """Apply the M3 precedence ladder for a transaction read model.

    Rules are deliberately evaluated per dimension: a rule that sets only need
    cannot erase a behavior chosen by a stronger source (or another rule).
    """
    if entry["bucket"] != "spending":
        return {"cost_behavior": "N/A", "cost_behavior_basis": "N/A", "need": "N/A", "need_basis": "N/A", "role_review_state": "N/A"}
    from .baseline.roles import _role

    default = _role(entry["category"], recurring=bool(entry.get("recurring_confirmed")))
    result = {
        "cost_behavior": default["cost_pattern"],
        "cost_behavior_basis": "recurring evidence" if entry.get("recurring_confirmed") else "category default",
        "need": default["essential_status"],
        "need_basis": "category default",
        "role_review_state": default["review_status"],
    }
    # The latest matching rule wins within a dimension. This is stable because
    # journal sequence is monotonic and rules are projected from that journal.
    for rule in sorted(rules, key=lambda item: int(item["journal_seq"])):
        match = json.loads(rule["match_json"])
        if _matches(entry, match):
            if rule["cost_behavior"] is not None:
                result["cost_behavior"] = rule["cost_behavior"]
                result["cost_behavior_basis"] = "role rule"
                result["role_review_state"] = "automatic"
            if rule["need"] is not None:
                result["need"] = rule["need"]
                result["need_basis"] = "role rule"
                result["role_review_state"] = "automatic"
    # Outlier detection may only suggest `irregular`, and only where nothing
    # stronger than the category default has spoken (§3.1, §3.2). It never
    # overrides a confirmed recurring occurrence, an active rule, or a manual
    # entry override.
    if entry.get("outlier_irregular") and result["cost_behavior_basis"] == "category default":
        result["cost_behavior"] = "irregular"
        result["cost_behavior_basis"] = "outlier review"
        result["role_review_state"] = "suggested"
    if entry.get("cost_behavior") is not None:
        result["cost_behavior"] = entry["cost_behavior"]
        result["cost_behavior_basis"] = "manual"
        result["role_review_state"] = "confirmed"
    if entry.get("need") is not None:
        result["need"] = entry["need"]
        result["need_basis"] = "manual"
        result["role_review_state"] = "confirmed"
    return result


def _matches(entry: dict[str, Any], match: dict[str, Any]) -> bool:
    if "amount_band" in match:
        band = match["amount_band"]
        if band["currency"] != entry["currency_pocket"]:
            return False
        value = abs(int(entry["amount_coefficient"]))
        scale = int(entry["amount_scale"])
        for bound, comparison in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
            if bound in band:
                item = band[bound]
                common = max(scale, int(item["scale"]))
                if comparison(value * 10 ** (common - scale), int(item["coefficient"]) * 10 ** (common - int(item["scale"]))):
                    return False
    if "merchant_canonical" in match:
        return entry.get("merchant_match") == match["merchant_canonical"]
    if "paynow_to" in match:
        return entry.get("paynow_to") == match["paynow_to"]
    description = entry.get("description_text") or ""
    if "description_prefix" in match:
        return description.startswith(match["description_prefix"])
    return match["description_contains"] in description and ("account" not in match or entry["account_id"] == match["account"])
