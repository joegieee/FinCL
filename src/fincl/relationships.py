"""Manual relationship decisions: duplicates now, refund allocations next.

These are decision-journal kinds (DESIGN-relationships-and-cost.md §§1-2). Replay
projects each effective record into ``relationship_decisions`` (latest journal seq
per identity wins; a RETIRE leaves no row), then :func:`resolve` folds the bound
DUPLICATE confirmations into ``duplicate_suppressions`` with the global checks the
design requires — exactly one unsuppressed primary per group, no cycles, no
conflicting primary designation. A suppressed entry contributes no component to
any analysis.

The atomic ``DECISION_BATCH`` engine (Operations §1) lives in
:func:`fincl.application.record_batch`; this module only validates and projects
the individual kinds a batch may carry.
"""

from __future__ import annotations

import json
import sqlite3
from fractions import Fraction
from typing import Any

from .common import canonical_json
from .debts.models import DEBT_DECISION_KINDS
from .errors import FinCLError
from .financial_roles import FINANCIAL_ROLE_DECISION_KINDS
from .inbound_rules import INBOUND_RULE_DECISION_KINDS
from .money import ExactMoney, format_decimal

DECISION_BATCH = "DECISION_BATCH"
DUPLICATE_CONFIRM = "DUPLICATE_CONFIRM"
DUPLICATE_REJECT = "DUPLICATE_REJECT"
DUPLICATE_RETIRE = "DUPLICATE_RETIRE"
CATEGORY_OVERRIDE_RETIRE = "CATEGORY_OVERRIDE_RETIRE"
REFUND_ALLOCATION_SET = "REFUND_ALLOCATION_SET"
REFUND_ALLOCATION_RETIRE = "REFUND_ALLOCATION_RETIRE"
REFUND_LINK_REJECT = "REFUND_LINK_REJECT"

DUPLICATE_KINDS = frozenset({DUPLICATE_CONFIRM, DUPLICATE_REJECT, DUPLICATE_RETIRE})
REFUND_KINDS = frozenset(
    {REFUND_ALLOCATION_SET, REFUND_ALLOCATION_RETIRE, REFUND_LINK_REJECT}
)
RELATIONSHIP_DECISION_KINDS = (
    DUPLICATE_KINDS | REFUND_KINDS | {CATEGORY_OVERRIDE_RETIRE}
)

# Kinds a DECISION_BATCH may carry. Coupled corrections only: a batch can retire
# an incompatible classification and author its replacement. It cannot nest, and
# it cannot carry originating definitions (CATEGORY_CREATE, PLAN_SET) or the
# transfer-link kinds, whose own flows own their previews.
BATCH_CHILD_KINDS = frozenset(
    {
        "CATEGORY_OVERRIDE",
        CATEGORY_OVERRIDE_RETIRE,
        "INBOUND_CLASSIFICATION",
        "MERCHANT_ALIAS_SET", "MERCHANT_ALIAS_RETIRE",
        "CATEGORY_RULE_SET", "CATEGORY_RULE_RETIRE",
        *DUPLICATE_KINDS,
        # v0.3: a coupled correction batch opens a debt, its initial event and
        # shared residual atomically, and may retire a conflicting inbound
        # classification in the same record.
        *DEBT_DECISION_KINDS,
        *FINANCIAL_ROLE_DECISION_KINDS,
        *INBOUND_RULE_DECISION_KINDS,
    }
)

# M4 permits one category and one role child per each of its 200 frozen rows.
_MAX_BATCH_ITEMS = 400
_MAX_BATCH_BYTES = 1 << 20
_REASON_MAX = 2000


def _reason(payload: dict[str, Any]) -> str:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX:
        raise FinCLError("a reason of 1-2,000 characters is required")
    return reason


def duplicate_identity(a: str, b: str) -> str:
    if a == b:
        raise FinCLError("a duplicate pair needs two distinct transactions")
    lo, hi = sorted((a, b))
    return f"{lo}\x1f{hi}"


def validate_duplicate(kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"primary", "candidate"}:
        raise FinCLError("a duplicate decision needs a primary and a candidate transaction")
    duplicate_identity(anchors["primary"], anchors["candidate"])
    if set(payload) != {"reason"}:
        raise FinCLError("a duplicate decision payload is exactly {reason}")
    _reason(payload)


def validate_override_retire(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if set(anchors) != {"entry"}:
        raise FinCLError("CATEGORY_OVERRIDE_RETIRE needs an entry anchor")
    if set(payload) != {"reason"}:
        raise FinCLError("CATEGORY_OVERRIDE_RETIRE payload is exactly {reason}")
    _reason(payload)


def refund_link_identity(refund: str, purchase: str) -> str:
    return f"{refund}\x1f{purchase}"


def _positive_money(text: object, label: str) -> ExactMoney:
    if not isinstance(text, str):
        raise FinCLError(f"{label} must be a decimal string")
    try:
        value = ExactMoney.parse(text)
    except (ValueError, OverflowError) as exc:
        raise FinCLError(f"{label} is not a valid amount: {text!r}") from exc
    if value.coefficient <= 0:
        raise FinCLError(f"{label} must be strictly positive")
    return value


def _fraction(coefficient: int, scale: int) -> Fraction:
    return Fraction(coefficient, 10**scale)


def validate_refund_allocation(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind == REFUND_LINK_REJECT:
        if set(anchors) != {"refund", "purchase"}:
            raise FinCLError("REFUND_LINK_REJECT needs a refund and a purchase anchor")
        if set(payload) != {"reason"}:
            raise FinCLError("REFUND_LINK_REJECT payload is exactly {reason}")
        _reason(payload)
        return
    if set(anchors) != {"refund"}:
        raise FinCLError("a refund-allocation decision needs a refund anchor")
    if kind == REFUND_ALLOCATION_RETIRE:
        if set(payload) != {"reason"}:
            raise FinCLError("REFUND_ALLOCATION_RETIRE payload is exactly {reason}")
        _reason(payload)
        return
    if set(payload) != {"allocations", "reason"}:
        raise FinCLError("REFUND_ALLOCATION_SET payload is exactly {allocations, reason}")
    _reason(payload)
    allocations = payload["allocations"]
    if not isinstance(allocations, list) or not allocations:
        raise FinCLError("a refund allocation needs at least one purchase")
    seen: set[str] = set()
    for index, item in enumerate(allocations):
        if not isinstance(item, dict) or set(item) != {
            "purchase_anchor", "refund_amount", "purchase_amount"
        }:
            raise FinCLError(
                f"allocation {index} must be exactly "
                "{purchase_anchor, refund_amount, purchase_amount}"
            )
        if not isinstance(item["purchase_anchor"], str) or not item["purchase_anchor"]:
            raise FinCLError(f"allocation {index} needs a purchase anchor")
        if item["purchase_anchor"] in seen:
            raise FinCLError(f"allocation {index} repeats a purchase anchor")
        seen.add(item["purchase_anchor"])
        _positive_money(item["refund_amount"], f"allocation {index} refund amount")
        _positive_money(item["purchase_amount"], f"allocation {index} purchase amount")


def preview_refund_allocation(
    connection: sqlite3.Connection,
    refund_anchor: str,
    allocations: list[dict[str, Any]],
    *,
    resolve_anchor: Any,
) -> dict[str, Any]:
    """Validate one REFUND_ALLOCATION_SET against the current projection.

    Enforces: refund and purchases resolve and have the right direction;
    same-currency amounts balance exactly; a refund is not dated strictly before
    its purchase when both dates are day-precise; allocated refund total does not
    exceed the refund magnitude; and no purchase receives more credit than its
    gross cost across every active refund. Cross-currency allocation is refused
    for now (slice 4 baseline). Returns the exact refund/allocated/residual
    totals.
    """
    refund = connection.execute(
        "SELECT e.natural_anchor, e.economic_direction, e.event_kind, "
        "e.currency_pocket, e.amount_coefficient, e.amount_scale, "
        "substr(e.effective_at, 1, 10) AS posted_date, e.effective_precision "
        "FROM ledger_entries e JOIN extraction_runs x "
        "  ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED' "
        "WHERE e.natural_anchor = ? AND e.economic_direction = 'IN' LIMIT 1",
        (refund_anchor,),
    ).fetchone()
    if refund is None or resolve_anchor(refund_anchor) is None:
        raise FinCLError("the refund transaction does not resolve to one inbound entry")

    refund_magnitude = _fraction(abs(int(refund["amount_coefficient"])), int(refund["amount_scale"]))
    allocated_total = Fraction(0)
    for index, item in enumerate(allocations):
        purchase_anchor = item["purchase_anchor"]
        refund_amount = _positive_money(item["refund_amount"], f"allocation {index} refund amount")
        purchase_amount = _positive_money(item["purchase_amount"], f"allocation {index} purchase amount")
        purchase = connection.execute(
            "SELECT e.currency_pocket, e.amount_coefficient, e.amount_scale, "
            "substr(e.effective_at, 1, 10) AS posted_date, e.effective_precision "
            "FROM ledger_entries e JOIN extraction_runs x "
            "  ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED' "
            "WHERE e.natural_anchor = ? AND e.economic_direction = 'OUT' LIMIT 1",
            (purchase_anchor,),
        ).fetchone()
        if purchase is None or resolve_anchor(purchase_anchor) is None:
            raise FinCLError(f"allocation {index}: the purchase does not resolve")
        if purchase["currency_pocket"] != refund["currency_pocket"]:
            raise FinCLError(
                f"allocation {index}: cross-currency refund allocation is not yet supported"
            )
        if (refund_amount.coefficient, refund_amount.scale) != (
            purchase_amount.coefficient, purchase_amount.scale
        ) and _fraction(refund_amount.coefficient, refund_amount.scale) != _fraction(
            purchase_amount.coefficient, purchase_amount.scale
        ):
            raise FinCLError(
                f"allocation {index}: same-currency refund and purchase amounts must be equal"
            )
        if (
            refund["effective_precision"] == "DAY"
            and purchase["effective_precision"] == "DAY"
            and refund["posted_date"] < purchase["posted_date"]
        ):
            raise FinCLError(f"allocation {index}: the refund is dated before its purchase")

        purchase_gross = _fraction(
            abs(int(purchase["amount_coefficient"])), int(purchase["amount_scale"])
        )
        other = connection.execute(
            "SELECT COALESCE(SUM(purchase_coefficient * 1.0 / power(10, purchase_scale)), 0) "
            "FROM refund_allocations WHERE purchase_anchor = ? AND refund_anchor <> ?",
            (purchase_anchor, refund_anchor),
        ).fetchone()
        prior_credit = Fraction(str(other[0]))
        this_credit = _fraction(purchase_amount.coefficient, purchase_amount.scale)
        if prior_credit + this_credit > purchase_gross:
            raise FinCLError(
                f"allocation {index}: total refunds would exceed the purchase cost"
            )
        allocated_total += _fraction(refund_amount.coefficient, refund_amount.scale)

    if allocated_total > refund_magnitude:
        raise FinCLError("the allocated total exceeds the refund amount")
    residual = refund_magnitude - allocated_total
    scale = int(refund["amount_scale"])

    def _fmt(value: Fraction) -> str:
        scaled = value * (10**scale)
        if scaled.denominator != 1:
            raise FinCLError("a refund amount is finer than the ledger scale")
        return format_decimal(int(scaled), scale)

    return {
        "refund_total": _fmt(refund_magnitude),
        "allocated_total": _fmt(allocated_total),
        "residual": _fmt(residual),
        "currency": refund["currency_pocket"],
        "allocations": len(allocations),
    }


def validate_relationship_payload(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind in DUPLICATE_KINDS:
        validate_duplicate(kind, anchors, payload)
    elif kind == CATEGORY_OVERRIDE_RETIRE:
        validate_override_retire(anchors, payload)
    elif kind in REFUND_KINDS:
        validate_refund_allocation(kind, anchors, payload)
    else:  # pragma: no cover - guarded by the dispatcher
        raise FinCLError(f"{kind} is not a relationship decision")


def validate_batch(anchors: dict[str, str], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Shape-check a ``DECISION_BATCH`` payload; return its item list.

    Reference and final-state validation happen against an isolated projection in
    :func:`fincl.application.record_batch`; this only enforces the structural
    limits so replay of a durable record cannot be surprised.
    """
    if set(anchors) != {"batch"}:
        raise FinCLError("a decision batch needs a single batch anchor")
    if set(payload) != {"items", "reason"}:
        raise FinCLError("a decision batch payload is exactly {items, reason}")
    items = payload["items"]
    if not isinstance(items, list) or not items:
        raise FinCLError("a decision batch needs at least one item")
    if len(items) > _MAX_BATCH_ITEMS:
        raise FinCLError(f"a decision batch is limited to {_MAX_BATCH_ITEMS} items")
    if len(canonical_json(payload).encode()) > _MAX_BATCH_BYTES:
        raise FinCLError("the decision batch record exceeds 1 MiB")
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"kind", "anchors", "payload"}:
            raise FinCLError(f"batch item {index} must be exactly {{kind, anchors, payload}}")
        kind = item["kind"]
        if kind == DECISION_BATCH:
            raise FinCLError("a decision batch cannot contain another decision batch")
        if kind not in BATCH_CHILD_KINDS:
            raise FinCLError(f"batch item {index} has an unsupported kind {kind!r}")
        if not isinstance(item["anchors"], dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in item["anchors"].items()
        ):
            raise FinCLError(f"batch item {index} anchors must be a string map")
        if not isinstance(item["payload"], dict):
            raise FinCLError(f"batch item {index} payload must be an object")
    reason = payload["reason"]
    if not isinstance(reason, str) or len(reason) > 2000:
        raise FinCLError("a decision batch reason must be text up to 2,000 characters")
    if not reason.strip() and any(item["kind"] != "CATEGORY_OVERRIDE" for item in items):
        raise FinCLError("a non-empty reason is required for this decision batch")
    return items


def preview_batch(
    connection: sqlite3.Connection,
    items: list[dict[str, Any]],
    *,
    custom_categories: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Validate every batch child against the current projection before append.

    Mirrors exactly the checks that could raise during replay (payload shape,
    category existence, inbound direction) plus anchor resolution, which replay
    only projects as dangling. Debt children are dry-run applied inside a
    SAVEPOINT and rolled back, so the projection is left untouched. Raises
    :class:`FinCLError` on the first invalid item so a durable append is never
    made for a batch that cannot fully replay.
    """
    from .categorization import CATEGORIZATION_DECISION_KINDS, validate_payload
    from .common import resolve_category
    from .debts.validation import validate_debt_payload
    from .decisions import resolve_anchor
    from .financial_roles import validate_financial_role
    from .inbound_rules import validate_inbound_rule

    def _entry(anchor: str) -> str:
        entry_id = resolve_anchor(connection, anchor)
        if entry_id is None:
            raise FinCLError(f"transaction {anchor} no longer resolves")
        return entry_id

    suppress_pairs: list[tuple[str, str]] = []
    debt_items: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    for index, item in enumerate(items):
        kind, anchors, payload = item["kind"], dict(item["anchors"]), dict(item["payload"])
        try:
            if kind in CATEGORIZATION_DECISION_KINDS and kind != "CATEGORY_OVERRIDE":
                validate_payload(kind, anchors, payload, custom_categories)
                continue
            if kind in DEBT_DECISION_KINDS:
                validate_debt_payload(kind, anchors, payload)
                debt_items.append((index, kind, anchors, payload))
                continue
            if kind in FINANCIAL_ROLE_DECISION_KINDS:
                validate_financial_role(kind, anchors, payload)
                if "entry" in anchors:
                    _entry(anchors["entry"])
                continue
            if kind in INBOUND_RULE_DECISION_KINDS:
                validate_inbound_rule(kind, anchors, payload)
                continue
            if kind in DUPLICATE_KINDS:
                validate_duplicate(kind, anchors, payload)
                _entry(anchors["primary"])
                _entry(anchors["candidate"])
                if kind == DUPLICATE_CONFIRM:
                    suppress_pairs.append((anchors["primary"], anchors["candidate"]))
            elif kind == CATEGORY_OVERRIDE_RETIRE:
                validate_override_retire(anchors, payload)
                _entry(anchors["entry"])
            elif kind == "CATEGORY_OVERRIDE":
                if set(anchors) != {"entry"} or set(payload) != {"category", "reason"}:
                    raise FinCLError("a category override is {entry} / {category, reason}")
                if not isinstance(payload["reason"], str) or len(payload["reason"]) > 2000:
                    raise FinCLError("a category override reason must be text up to 2,000 characters")
                _entry(anchors["entry"])
                group, _, _ = resolve_category(str(payload["category"]), custom_categories)
                if group == "Internal":
                    raise FinCLError("Internal categories are assigned by transfer decisions")
            elif kind == "INBOUND_CLASSIFICATION":
                if set(anchors) != {"entry"} or set(payload) != {"classification", "reason"}:
                    raise FinCLError("an inbound classification is {entry} / {classification, reason}")
                _reason(payload)
                if payload["classification"] not in {
                    "income", "reimbursement", "gift", "family_transfer", "self_transfer", "other"
                }:
                    raise FinCLError("choose a supported inflow classification")
                entry_id = _entry(anchors["entry"])
                if not connection.execute(
                    "SELECT 1 FROM ledger_entries WHERE id = ? AND economic_direction = 'IN'",
                    (entry_id,),
                ).fetchone():
                    raise FinCLError("only an inbound transaction can be classified")
                if kind in CATEGORIZATION_DECISION_KINDS:  # pragma: no cover
                    validate_payload(kind, anchors, payload, custom_categories)
        except FinCLError as exc:
            raise FinCLError(f"batch item {index}: {exc}") from exc

    # Conflicting primary designation inside this batch or against prior state.
    confirmed_primaries = {p for p, _ in suppress_pairs}
    confirmed_candidates = {c for _, c in suppress_pairs}
    if confirmed_primaries & confirmed_candidates:
        raise FinCLError("a transaction cannot be both a duplicate primary and suppressed")
    prior = {
        row[0]
        for row in connection.execute("SELECT primary_anchor FROM duplicate_suppressions")
    }
    if confirmed_candidates & prior:
        raise FinCLError("a candidate in this batch is already a duplicate primary elsewhere")

    if debt_items:
        from .debts.projection import apply_debt, resolve_debts

        connection.execute("SAVEPOINT batch_debt_preview")
        try:
            for index, kind, anchors, payload in debt_items:
                try:
                    apply_debt(
                        connection,
                        decision_kind=kind,
                        anchors=anchors,
                        payload=payload,
                        journal_seq=(1 << 40) + index,
                        batch_index=index,
                        resolve_anchor=lambda a: resolve_anchor(connection, a),
                    )
                except FinCLError as exc:
                    raise FinCLError(f"batch item {index}: {exc}") from exc
            resolve_debts(connection)
        finally:
            connection.execute("ROLLBACK TO batch_debt_preview")
            connection.execute("RELEASE batch_debt_preview")

    return {"items": len(items), "duplicate_confirms": len(suppress_pairs)}


# --- replay projection -------------------------------------------------------

def _upsert(
    connection: sqlite3.Connection,
    *,
    kind: str,
    identity: str,
    journal_seq: int,
    batch_index: int | None,
    payload: dict[str, Any],
    snapshot: str,
    bound: int,
    dangling_reason: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO relationship_decisions
            (kind, identity, journal_seq, batch_index, payload_json, snapshot,
             bound, dangling_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(kind, identity) DO UPDATE SET
            journal_seq = excluded.journal_seq,
            batch_index = excluded.batch_index,
            payload_json = excluded.payload_json,
            snapshot = excluded.snapshot,
            bound = excluded.bound,
            dangling_reason = excluded.dangling_reason
        """,
        (kind, identity, journal_seq, batch_index,
         json.dumps(payload, sort_keys=True, separators=(",", ":")),
         snapshot, bound, dangling_reason),
    )


def apply_relationship(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
    snapshot: str,
    override: str | None,
    batch_index: int | None,
    resolve_anchor: Any,
) -> None:
    """Project one relationship record into ``relationship_decisions``.

    ``resolve_anchor(anchor) -> str | None`` is the shared binding check; an
    unresolved anchor projects the decision as dangling with a reason, exactly
    like the link/inbound/override projections.
    """
    if decision_kind == CATEGORY_OVERRIDE_RETIRE:
        connection.execute(
            "DELETE FROM category_overrides WHERE entry_anchor = ?",
            (anchors["entry"],),
        )
        return

    if decision_kind in REFUND_KINDS:
        _apply_refund(
            connection, decision_kind=decision_kind, anchors=anchors, payload=payload,
            journal_seq=journal_seq, snapshot=snapshot, override=override,
            batch_index=batch_index, resolve_anchor=resolve_anchor,
        )
        return

    identity = duplicate_identity(anchors["primary"], anchors["candidate"])
    if decision_kind == DUPLICATE_RETIRE:
        connection.execute(
            "DELETE FROM relationship_decisions WHERE kind = 'DUPLICATE' AND identity = ?",
            (identity,),
        )
        return

    reasons = [
        r for r in (
            None if resolve_anchor(anchors["primary"]) else "primary no longer resolves",
            None if resolve_anchor(anchors["candidate"]) else "candidate no longer resolves",
        ) if r
    ]
    dangling_reason = override or (reasons[0] if reasons else None)
    body = {
        "decision": "CONFIRM" if decision_kind == DUPLICATE_CONFIRM else "REJECT",
        "primary": anchors["primary"],
        "candidate": anchors["candidate"],
        "reason": payload["reason"],
    }
    _upsert(
        connection, kind="DUPLICATE", identity=identity, journal_seq=journal_seq,
        batch_index=batch_index, payload=body, snapshot=snapshot,
        bound=0 if dangling_reason else 1, dangling_reason=dangling_reason,
    )


def _apply_refund(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
    snapshot: str,
    override: str | None,
    batch_index: int | None,
    resolve_anchor: Any,
) -> None:
    if decision_kind == REFUND_ALLOCATION_RETIRE:
        connection.execute(
            "DELETE FROM relationship_decisions "
            "WHERE kind = 'REFUND_ALLOCATION' AND identity = ?",
            (anchors["refund"],),
        )
        return
    if decision_kind == REFUND_LINK_REJECT:
        identity = refund_link_identity(anchors["refund"], anchors["purchase"])
        reason = (
            override
            or (None if resolve_anchor(anchors["refund"]) else "refund no longer resolves")
            or (None if resolve_anchor(anchors["purchase"]) else "purchase no longer resolves")
        )
        _upsert(
            connection, kind="REFUND_LINK_REJECT", identity=identity,
            journal_seq=journal_seq, batch_index=batch_index,
            payload={"refund": anchors["refund"], "purchase": anchors["purchase"],
                     "reason": payload["reason"]},
            snapshot=snapshot, bound=0 if reason else 1, dangling_reason=reason,
        )
        return

    allocations = payload["allocations"]
    unresolved = [
        a["purchase_anchor"] for a in allocations
        if resolve_anchor(a["purchase_anchor"]) is None
    ]
    reason = (
        override
        or (None if resolve_anchor(anchors["refund"]) else "refund no longer resolves")
        or (f"purchase {unresolved[0]} no longer resolves" if unresolved else None)
    )
    _upsert(
        connection, kind="REFUND_ALLOCATION", identity=anchors["refund"],
        journal_seq=journal_seq, batch_index=batch_index,
        payload={"refund": anchors["refund"], "allocations": allocations,
                 "reason": payload["reason"]},
        snapshot=snapshot, bound=0 if reason else 1, dangling_reason=reason,
    )


def resolve(connection: sqlite3.Connection) -> None:
    """Fold effective relationship decisions into their normalized tables.

    Duplicates → ``duplicate_suppressions`` with the group-wide checks (an anchor
    designated primary must not itself be suppressed elsewhere; a candidate must
    not be claimed by two primaries). A pair that fails is left un-applied — its
    ``relationship_decisions`` row still records the operator decision.
    Refund allocations → ``refund_allocations``, minus any pair a bound
    ``REFUND_LINK_REJECT`` suppresses.
    """
    connection.execute("DELETE FROM duplicate_suppressions")
    connection.execute("DELETE FROM refund_allocations")
    _resolve_refunds(connection)
    confirms = [
        (json.loads(row["payload_json"]), row["journal_seq"])
        for row in connection.execute(
            "SELECT payload_json, journal_seq FROM relationship_decisions "
            "WHERE kind = 'DUPLICATE' AND bound = 1 "
            "ORDER BY journal_seq"
        )
        if json.loads(row["payload_json"]).get("decision") == "CONFIRM"
    ]
    suppressed: dict[str, str] = {}
    primaries: set[str] = set()
    for body, seq in confirms:  # journal order: an earlier confirm wins a conflict
        primary, candidate = body["primary"], body["candidate"]
        if primary in suppressed:
            continue  # the named primary is itself suppressed elsewhere
        if candidate in primaries:
            continue  # the candidate already acts as a primary — conflicting
        if candidate in suppressed and suppressed[candidate] != primary:
            continue  # two primaries claim the same candidate
        suppressed[candidate] = primary
        primaries.add(primary)
        connection.execute(
            "INSERT OR REPLACE INTO duplicate_suppressions"
            "(suppressed_anchor, primary_anchor, journal_seq, reason) VALUES (?, ?, ?, ?)",
            (candidate, primary, seq, body["reason"]),
        )


def _resolve_refunds(connection: sqlite3.Connection) -> None:
    rejected = {
        row["identity"]
        for row in connection.execute(
            "SELECT identity FROM relationship_decisions "
            "WHERE kind = 'REFUND_LINK_REJECT' AND bound = 1"
        )
    }
    for row in connection.execute(
        "SELECT payload_json, journal_seq FROM relationship_decisions "
        "WHERE kind = 'REFUND_ALLOCATION' AND bound = 1 ORDER BY journal_seq"
    ):
        body = json.loads(row["payload_json"])
        refund = body["refund"]
        currency = connection.execute(
            "SELECT currency_pocket FROM ledger_entries WHERE natural_anchor = ? LIMIT 1",
            (refund,),
        ).fetchone()
        for allocation in body["allocations"]:
            purchase = allocation["purchase_anchor"]
            if refund_link_identity(refund, purchase) in rejected:
                continue
            refund_amount = ExactMoney.parse(allocation["refund_amount"])
            purchase_amount = ExactMoney.parse(allocation["purchase_amount"])
            connection.execute(
                "INSERT OR REPLACE INTO refund_allocations"
                "(refund_anchor, purchase_anchor, refund_coefficient, refund_scale,"
                " purchase_coefficient, purchase_scale, currency, journal_seq)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (refund, purchase, refund_amount.coefficient, refund_amount.scale,
                 purchase_amount.coefficient, purchase_amount.scale,
                 currency[0] if currency else "", row["journal_seq"]),
            )
