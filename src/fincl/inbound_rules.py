"""Reusable inbound-classification rules (cash-flow usability M5).

An inbound rule is the "save for future matches" half of the income-candidate
workflow (PLAN-cashflow-usability.md §3.3). Confirming a candidate's history is
one atomic batch of ``INBOUND_CLASSIFICATION`` records; a rule is the optional,
separately journalled decision that a *future* inflow matching the same closed
predicate should carry the same classification.

A rule only ever produces a suggestion. It is applied in the read model as
``suggested_classification`` for an inflow that has no explicit
``INBOUND_CLASSIFICATION`` of its own; it never rewrites the effective bucket
and never reaches a forecast on its own (product contract §2.7, §3.3).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .categorization import canonical_match, rule_id_for
from .errors import FinCLError
from .financial_roles import _matches

INBOUND_RULE_SET = "INBOUND_RULE_SET"
INBOUND_RULE_RETIRE = "INBOUND_RULE_RETIRE"
INBOUND_RULE_DECISION_KINDS = frozenset({INBOUND_RULE_SET, INBOUND_RULE_RETIRE})

_CLASSIFICATIONS = {
    "income", "reimbursement", "gift", "family_transfer", "self_transfer", "other",
}


def validate_inbound_rule(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if kind == INBOUND_RULE_SET:
        if set(anchors) != {"rule_id"} or set(payload) != {"match", "classification", "reason"}:
            raise FinCLError("An inbound rule needs match, classification, and reason")
        if not payload["reason"]:
            raise FinCLError("An inbound rule reason is required")
        if payload["classification"] not in _CLASSIFICATIONS:
            raise FinCLError("Choose a supported inflow classification")
        canonical = canonical_match(payload["match"])
        if anchors["rule_id"] != rule_id_for(canonical):
            raise FinCLError("The inbound rule id does not match its predicate")
    elif kind == INBOUND_RULE_RETIRE:
        if set(anchors) != {"rule_id"} or set(payload) != {"reason"} or not payload["reason"]:
            raise FinCLError("Retiring an inbound rule needs a reason")


def apply_inbound_rule(connection: sqlite3.Connection, *, kind: str, anchors: dict[str, str], payload: dict[str, Any], journal_seq: int) -> None:
    validate_inbound_rule(kind, anchors, payload)
    if kind == INBOUND_RULE_RETIRE:
        connection.execute("DELETE FROM inbound_rules WHERE rule_id=?", (anchors["rule_id"],))
        return
    match = canonical_match(payload["match"])
    connection.execute(
        "INSERT OR REPLACE INTO inbound_rules(rule_id,match_json,classification,reason,journal_seq) VALUES(?,?,?,?,?)",
        (anchors["rule_id"], json.dumps(match, sort_keys=True), payload["classification"], payload["reason"], journal_seq),
    )


def effective_inbound_classification(entry: dict[str, Any], rules: list[dict[str, Any]]) -> str:
    """The classification a reusable rule suggests for an unresolved inflow.

    Returns ``""`` unless the entry is an inbound row with no explicit
    classification of its own and an active rule matches it. The latest matching
    rule by journal sequence wins, mirroring the role-rule evaluator.
    """
    if entry.get("economic_direction") != "IN" or entry.get("classification") is not None:
        return ""
    if entry.get("bucket") != "pending_review":
        return ""
    suggestion = ""
    for rule in sorted(rules, key=lambda item: int(item["journal_seq"])):
        if _matches(entry, json.loads(rule["match_json"])):
            suggestion = rule["classification"]
    return suggestion
