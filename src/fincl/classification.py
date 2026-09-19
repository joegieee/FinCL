"""Unified transaction classification: shared draft, combined impact preview,
and command generation (PLAN-unified-transaction-classification.md Step 1).

A :class:`ClassificationDraft` carries one explicit keep/set intent per field
(category, spending pattern, spending need, incoming-money type) plus a bulk
fill-missing/replace policy and one shared reason. ``evaluate_classification_draft``
resolves it against the current projection for a frozen anchor list and returns
the exact commands a subsequent ``POST /api/v1/batch`` would submit, together
with the per-field/per-currency impact a caller previews before committing.

Applicability follows the resolved contract in
``docs/PLAN-unified-transaction-classification.md`` §5.1: spending pattern/need
apply only to the ``spending`` bucket; incoming type applies to every IN-direction
bucket except a confirmed-relationship-linked internal movement; category applies
everywhere except both kinds of ``internal`` row. This module never merges the
spending trio onto an incoming row or vice versa — a transaction's applicable
field set is bucket-driven, not user-selected.

No command is ever emitted for a field left at ``keep``, for a no-op ``set``
(the resolved value already matches), or for a fill-missing ``set`` on an
already-resolved field. The domain/application validation and idempotent batch
lifecycle this module feeds are unchanged; this module only decides which
commands a draft implies and discloses that decision before it is submitted.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from .baseline.outliers import outlier_anchor_set
from .common import resolve_category
from .errors import FinCLError
from .financial_roles import _BEHAVIOURS, _NEEDS
from .generation import current_generation
from .inbound_rules import _CLASSIFICATIONS as _INCOMING_CLASSIFICATIONS
from .money import ExactMoney, exact_sum, format_decimal
from .queries import _ENTRY_VIEW, _rule_tables, _shape

FieldIntent = Literal["keep", "set"]
BulkPolicy = Literal["fill_missing", "replace"]

FIELD_NAMES = ("category", "cost_behavior", "need", "incoming_type")

# The batch cap (400) is exactly 2x the anchor cap (200): every transaction
# generates at most one category-ish command and one role-or-incoming command
# (PLAN-unified-transaction-classification.md §5.1's Step 0 finding). Enforcing
# this here, not just at the anchor list length, keeps preview and commit
# agreeing about what fits before the user invests in an oversized selection.
_MAX_COMMANDS = 400


@dataclass(frozen=True, slots=True)
class FieldEdit:
    intent: FieldIntent = "keep"
    value: str | None = None


_KEEP: FieldEdit = FieldEdit()


@dataclass(frozen=True, slots=True)
class ClassificationDraft:
    """One shared typed edit intent, applied across a frozen anchor selection."""

    anchors: tuple[str, ...]
    category: FieldEdit = _KEEP
    cost_behavior: FieldEdit = _KEEP
    need: FieldEdit = _KEEP
    incoming_type: FieldEdit = _KEEP
    clear_category: bool = False
    clear_roles: bool = False
    policy: BulkPolicy = "replace"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class FieldOutcome:
    field: str
    applicable: bool
    reason: str | None
    before: str | None
    after: str | None
    changed: bool
    derived: bool = False
    manual_replacement: bool = False
    skipped_fill_missing: bool = False


@dataclass(frozen=True, slots=True)
class AnchorPlan:
    anchor: str
    currency: str
    bucket: str
    amount: str
    fields: dict[str, FieldOutcome] = field(default_factory=dict)
    commands: list[dict[str, Any]] = field(default_factory=list)


_NOT_ACTIVE = "This transaction is not active in the current selection scope"
# A movement a personal debt fully explains is principal (plus any interest,
# fee and owner residual the debt records), not spending or income -- the debt
# owns it, the way a confirmed transfer link owns an internal movement.
_DEBT_OWNED = "This transaction is fully allocated to a personal debt, which decides how it counts"
_NOT_APPLICABLE = {
    "category": {
        "internal_linked": "Category is set by the linked transfer decision",
        "internal_self_transfer": "Category is derived from the transfer classification",
    },
    "cost_behavior": {
        "__default__": "Spending pattern applies only to external outflows",
    },
    "need": {
        "__default__": "Spending need applies only to external outflows",
    },
    "incoming_type": {
        "__default__": "Incoming type applies only to inbound transactions",
        "internal_linked": "The linked relationship owns this movement",
    },
}


def _detail_bucket(row: dict[str, Any]) -> str:
    if row["bucket"] != "internal":
        return row["bucket"]
    return "internal_linked" if row["linked"] else "internal_self_transfer"


def _applicability(detail_bucket: str) -> dict[str, bool]:
    if detail_bucket in ("excluded", "debt"):
        return dict.fromkeys(FIELD_NAMES, False)
    return {
        "category": detail_bucket not in ("internal_linked", "internal_self_transfer"),
        "cost_behavior": detail_bucket == "spending",
        "need": detail_bucket == "spending",
        "incoming_type": detail_bucket
        in (
            "internal_self_transfer", "income", "reimbursements",
            "non_income_inflow", "refunds", "pending_review", "other",
        ),
    }


def _not_applicable_reason(name: str, detail_bucket: str) -> str:
    if detail_bucket == "excluded":
        return _NOT_ACTIVE
    if detail_bucket == "debt":
        return _DEBT_OWNED
    table = _NOT_APPLICABLE[name]
    return table.get(detail_bucket, table.get("__default__", "This field does not apply to this transaction"))


def _is_unresolved(name: str, value: str | None) -> bool:
    if name == "category":
        return value == "Unclassified / Unclassified"
    if name == "incoming_type":
        return not value
    return value == "unresolved"


def _validate_value(name: str, value: str, *, custom_categories: frozenset[str]) -> None:
    if name == "category":
        resolve_category(value, custom_categories)
    elif name == "cost_behavior":
        if value not in _BEHAVIOURS:
            raise FinCLError(f"Unsupported spending pattern {value!r}")
    elif name == "need":
        if value not in _NEEDS:
            raise FinCLError(f"Unsupported spending need {value!r}")
    elif name == "incoming_type" and value not in _INCOMING_CLASSIFICATIONS:
        raise FinCLError(f"Unsupported incoming type {value!r}")


def _absolute_money(value: str) -> ExactMoney:
    whole, _, fraction = value.lstrip("+-").partition(".")
    return ExactMoney(abs(int((whole or "0") + fraction)), len(fraction), "")


def _plan_field(
    name: str, edit: FieldEdit, *, applicable: bool, not_applicable_reason: str | None,
    current: str | None, current_manual: bool, policy: BulkPolicy,
    custom_categories: frozenset[str],
) -> FieldOutcome:
    if not applicable:
        return FieldOutcome(name, False, not_applicable_reason, current, current, False)
    if edit.intent == "keep":
        return FieldOutcome(name, True, None, current, current, False)
    value = edit.value
    if value is None:
        raise FinCLError(f"A {name} edit needs a value when set")
    _validate_value(name, value, custom_categories=custom_categories)
    unresolved = _is_unresolved(name, current)
    if policy == "fill_missing" and not unresolved:
        return FieldOutcome(name, True, None, current, current, False, skipped_fill_missing=True)
    changed = value != current
    return FieldOutcome(
        name, True, None, current, value if changed else current, changed,
        manual_replacement=changed and current_manual,
    )


def _role_source(row: dict[str, Any], outliers: set[str]) -> dict[str, Any]:
    """The entry shape ``effective_financial_role`` expects, built the same way
    ``queries._shape`` builds it internally, so a hypothetical re-evaluation
    (e.g. under a proposed new category) uses the identical precedence ladder."""
    return {
        **row,
        "account_id": row["role_account_id"], "description_text": row["role_description_text"],
        "paynow_to": row["role_paynow_to"], "merchant_match": row["role_merchant_match"],
        "outlier_irregular": bool(outliers and row["natural_anchor"] in outliers),
    }


def _derive_role_under_category(
    role_source: dict[str, Any], category: str, role_rules: list[dict[str, Any]],
) -> dict[str, str]:
    """What ``cost_behavior``/``need`` would resolve to for this entry if its
    category were ``category`` and no manual role override existed — the
    category-derived effect a category change discloses (§3.2), computed with
    the real precedence evaluator rather than a re-implementation of it."""
    from .financial_roles import effective_financial_role

    hypothetical = {**role_source, "category": category, "cost_behavior": None, "need": None}
    result = effective_financial_role(hypothetical, role_rules)
    return {"cost_behavior": result["cost_behavior"], "need": result["need"]}


def evaluate_classification_draft(
    connection: sqlite3.Connection, draft: ClassificationDraft, *,
    custom_categories: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Resolve ``draft`` for its frozen anchors; never writes.

    Returns the exact ``commands`` a caller submits via the existing
    ``POST /api/v1/batch`` (all of them are already-supported
    ``BATCH_CHILD_KINDS``), plus the combined per-field/per-currency impact.
    Raises :class:`FinCLError` for an invalid field value, an anchor that is no
    longer active, a missing reason a generated command requires, or a command
    count exceeding the batch limit.
    """
    if not draft.anchors:
        raise FinCLError("Choose at least one transaction")
    if draft.clear_category and draft.category.intent == "set":
        raise FinCLError("Choose either a new category or Remove manual category, not both")
    if draft.clear_roles and (draft.cost_behavior.intent == "set" or draft.need.intent == "set"):
        raise FinCLError("Choose either new spending values or Remove manual spending classifications, not both")
    if (
        draft.category.intent == "keep" and draft.cost_behavior.intent == "keep"
        and draft.need.intent == "keep" and draft.incoming_type.intent == "keep"
        and not draft.clear_category and not draft.clear_roles
    ):
        raise FinCLError("Choose at least one field to change")

    marks = ",".join("?" for _ in draft.anchors)
    rows = connection.execute(
        f"SELECT * FROM ({_ENTRY_VIEW}) WHERE natural_anchor IN ({marks})",
        tuple(draft.anchors),
    ).fetchall()
    by_anchor = {row["natural_anchor"]: dict(row) for row in rows}
    missing = [a for a in draft.anchors if a not in by_anchor]
    if missing:
        raise FinCLError(f"One or more selected transactions are no longer active: {missing[0]}")

    role_rules, inbound_rules = _rule_tables(connection)
    current = current_generation(connection)
    outliers = outlier_anchor_set(connection, current["generation_token"]) if current else set()

    plans: list[AnchorPlan] = []
    for anchor in draft.anchors:
        row = by_anchor[anchor]
        if row["lifecycle_state"] != "COMPLETED" or not row["eligible"]:
            raise FinCLError(f"Transaction {anchor} is inactive and cannot be classified")
        detail_bucket = _detail_bucket(row)
        applicable = _applicability(detail_bucket)
        shaped = _shape(row, role_rules, inbound_rules, outliers)

        category_field = _plan_field(
            "category", draft.category, applicable=applicable["category"],
            not_applicable_reason=None if applicable["category"] else _not_applicable_reason("category", detail_bucket),
            current=shaped["category"], current_manual=shaped["basis"] == "override",
            policy=draft.policy, custom_categories=custom_categories,
        )
        if draft.clear_category and applicable["category"] and shaped["basis"] == "override":
            category_field = FieldOutcome(
                "category", True, None, shaped["category"], shaped["category"], True,
            )

        cost_behavior_field = _plan_field(
            "cost_behavior", draft.cost_behavior, applicable=applicable["cost_behavior"],
            not_applicable_reason=None if applicable["cost_behavior"] else _not_applicable_reason("cost_behavior", detail_bucket),
            current=shaped["cost_behavior"], current_manual=shaped["cost_behavior_basis"] == "manual",
            policy=draft.policy, custom_categories=custom_categories,
        )
        need_field = _plan_field(
            "need", draft.need, applicable=applicable["need"],
            not_applicable_reason=None if applicable["need"] else _not_applicable_reason("need", detail_bucket),
            current=shaped["need"], current_manual=shaped["need_basis"] == "manual",
            policy=draft.policy, custom_categories=custom_categories,
        )
        role_clear = draft.clear_roles and applicable["cost_behavior"] and (
            shaped["cost_behavior_basis"] == "manual" or shaped["need_basis"] == "manual"
        )
        role_source = _role_source(row, outliers)
        if role_clear:
            fallback = _derive_role_under_category(role_source, shaped["category"], role_rules)
            cost_behavior_field = FieldOutcome(
                "cost_behavior", True, None, shaped["cost_behavior"], fallback["cost_behavior"],
                shaped["cost_behavior"] != fallback["cost_behavior"],
            )
            need_field = FieldOutcome(
                "need", True, None, shaped["need"], fallback["need"], shaped["need"] != fallback["need"],
            )

        # Category-derived role effect: disclosed even without an explicit role
        # edit, but never turned into a command of its own (§3.2). Computed with
        # the real precedence evaluator, so a bound recurring occurrence or an
        # outlier suggestion is respected exactly as it would be on replay.
        if applicable["cost_behavior"] and category_field.changed and not role_clear:
            new_category = category_field.after or shaped["category"]
            derived = _derive_role_under_category(role_source, new_category, role_rules)
            if draft.cost_behavior.intent == "keep" and shaped["cost_behavior_basis"] != "manual" and derived["cost_behavior"] != cost_behavior_field.before:
                cost_behavior_field = FieldOutcome(
                    "cost_behavior", True, None, cost_behavior_field.before, derived["cost_behavior"],
                    False, derived=True,
                )
            if applicable["need"] and draft.need.intent == "keep" and shaped["need_basis"] != "manual" and derived["need"] != need_field.before:
                need_field = FieldOutcome(
                    "need", True, None, need_field.before, derived["need"], False, derived=True,
                )

        incoming_field = _plan_field(
            "incoming_type", draft.incoming_type, applicable=applicable["incoming_type"],
            not_applicable_reason=None if applicable["incoming_type"] else _not_applicable_reason("incoming_type", detail_bucket),
            current=row.get("classification"),
            current_manual=row.get("classification") is not None,
            policy=draft.policy, custom_categories=custom_categories,
        )

        commands: list[dict[str, Any]] = []
        reason = draft.reason.strip()
        entry_anchor = shaped["anchor"]
        if draft.clear_category and applicable["category"] and shaped["basis"] == "override":
            commands.append({
                "kind": "CATEGORY_OVERRIDE_RETIRE", "anchors": {"entry": entry_anchor},
                "payload": {"reason": reason},
            })
        elif category_field.changed:
            commands.append({
                "kind": "CATEGORY_OVERRIDE", "anchors": {"entry": entry_anchor},
                "payload": {"category": category_field.after, "reason": reason},
            })
        if role_clear:
            commands.append({
                "kind": "ENTRY_ROLE_RETIRE", "anchors": {"entry": entry_anchor},
                "payload": {"reason": reason},
            })
        else:
            role_payload: dict[str, Any] = {}
            if cost_behavior_field.changed and not cost_behavior_field.derived:
                role_payload["cost_behavior"] = cost_behavior_field.after
            if need_field.changed and not need_field.derived:
                role_payload["need"] = need_field.after
            if role_payload:
                commands.append({
                    "kind": "ENTRY_ROLE_SET", "anchors": {"entry": entry_anchor},
                    "payload": {**role_payload, "reason": reason},
                })
        if incoming_field.changed:
            commands.append({
                "kind": "INBOUND_CLASSIFICATION", "anchors": {"entry": entry_anchor},
                "payload": {"classification": incoming_field.after, "reason": reason},
            })

        plans.append(AnchorPlan(
            anchor=entry_anchor, currency=shaped["currency"], bucket=detail_bucket,
            amount=shaped["amount"],
            fields={
                "category": category_field, "cost_behavior": cost_behavior_field,
                "need": need_field, "incoming_type": incoming_field,
            },
            commands=commands,
        ))

    all_commands = [c for plan in plans for c in plan.commands]
    if len(all_commands) > _MAX_COMMANDS:
        raise FinCLError(
            f"This selection would generate {len(all_commands)} decisions; "
            f"narrow the selection to fit the {_MAX_COMMANDS}-command limit."
        )
    if any(c["kind"] != "CATEGORY_OVERRIDE" for c in all_commands) and not draft.reason.strip():
        raise FinCLError("A reason is required for this decision")

    return _summarize(plans)


def _summarize(plans: list[AnchorPlan]) -> dict[str, Any]:
    by_currency: dict[str, list[AnchorPlan]] = {}
    for plan in plans:
        by_currency.setdefault(plan.currency, []).append(plan)

    field_summary: dict[str, dict[str, int]] = {
        name: {
            "applicable": 0, "not_applicable": 0, "changed": 0,
            "skipped_fill_missing": 0, "manual_replacements": 0, "derived": 0,
        }
        for name in FIELD_NAMES
    }
    for plan in plans:
        for name, outcome in plan.fields.items():
            summary = field_summary[name]
            if outcome.applicable:
                summary["applicable"] += 1
            else:
                summary["not_applicable"] += 1
            if outcome.changed:
                summary["changed"] += 1
            if outcome.skipped_fill_missing:
                summary["skipped_fill_missing"] += 1
            if outcome.manual_replacement:
                summary["manual_replacements"] += 1
            if outcome.derived:
                summary["derived"] += 1

    currency_totals = []
    for currency, values in sorted(by_currency.items()):
        changed = [p for p in values if p.commands]
        currency_totals.append({
            "currency": currency,
            "count": len(values),
            "changed": len(changed),
            "absolute_amount": format_decimal(*exact_sum(*[_absolute_money(p.amount) for p in values])) if values else "0",
            "category_changed": sum(p.fields["category"].changed for p in values),
            "behavior_changed": sum(p.fields["cost_behavior"].changed for p in values),
            "need_changed": sum(p.fields["need"].changed for p in values),
            "incoming_changed": sum(p.fields["incoming_type"].changed for p in values),
        })

    commands = [c for plan in plans for c in plan.commands]
    return {
        "changed_transactions": sum(1 for plan in plans if plan.commands),
        "command_count": len(commands),
        "commands": commands,
        "by_currency": currency_totals,
        "fields": field_summary,
        "rows": [
            {
                "anchor": plan.anchor, "currency": plan.currency, "bucket": plan.bucket,
                "fields": {
                    name: {
                        "applicable": outcome.applicable, "reason": outcome.reason,
                        "before": outcome.before, "after": outcome.after,
                        "changed": outcome.changed, "derived": outcome.derived,
                        "manual_replacement": outcome.manual_replacement,
                        "skipped_fill_missing": outcome.skipped_fill_missing,
                    }
                    for name, outcome in plan.fields.items()
                },
            }
            for plan in plans
        ],
    }
