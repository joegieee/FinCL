"""Replayable per-entry financial-role decisions (cash-flow usability M3)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

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
        if not isinstance(payload["match"], dict) or not payload["reason"]:
            raise FinCLError("A role rule match and reason are required")
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
        connection.execute("INSERT OR REPLACE INTO role_rules(rule_id,match_json,cost_behavior,need,reason,journal_seq) VALUES(?,?,?,?,?,?)", (anchors["rule_id"], json.dumps(payload["match"], sort_keys=True), payload["cost_behavior"], payload["need"], payload["reason"], journal_seq))
        return
    entry_id = resolve_anchor(anchors["entry"])
    if kind == ENTRY_ROLE_RETIRE:
        if entry_id:
            connection.execute("DELETE FROM entry_financial_roles WHERE entry_id=?", (entry_id,))
        return
    if entry_id:
        old = connection.execute("SELECT cost_behavior,need FROM entry_financial_roles WHERE entry_id=?", (entry_id,)).fetchone()
        connection.execute("INSERT OR REPLACE INTO entry_financial_roles(entry_id,cost_behavior,cost_behavior_basis,need,need_basis,review_state,journal_seq) VALUES(?,?,?,?,?,?,?)", (entry_id, payload.get("cost_behavior") if payload.get("cost_behavior") is not None else (old[0] if old else None), "manual" if payload.get("cost_behavior") is not None else ("manual" if old else None), payload.get("need") if payload.get("need") is not None else (old[1] if old else None), "manual" if payload.get("need") is not None else ("manual" if old else None), "confirmed", journal_seq))
