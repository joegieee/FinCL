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
from typing import Any

from .common import canonical_json
from .errors import FinCLError

DECISION_BATCH = "DECISION_BATCH"
DUPLICATE_CONFIRM = "DUPLICATE_CONFIRM"
DUPLICATE_REJECT = "DUPLICATE_REJECT"
DUPLICATE_RETIRE = "DUPLICATE_RETIRE"
CATEGORY_OVERRIDE_RETIRE = "CATEGORY_OVERRIDE_RETIRE"

DUPLICATE_KINDS = frozenset({DUPLICATE_CONFIRM, DUPLICATE_REJECT, DUPLICATE_RETIRE})
RELATIONSHIP_DECISION_KINDS = DUPLICATE_KINDS | {CATEGORY_OVERRIDE_RETIRE}

# Kinds a DECISION_BATCH may carry. Coupled corrections only: a batch can retire
# an incompatible classification and author its replacement. It cannot nest, and
# it cannot carry originating definitions (CATEGORY_CREATE, PLAN_SET) or the
# transfer-link kinds, whose own flows own their previews.
BATCH_CHILD_KINDS = frozenset(
    {
        "CATEGORY_OVERRIDE",
        CATEGORY_OVERRIDE_RETIRE,
        "INBOUND_CLASSIFICATION",
        *DUPLICATE_KINDS,
    }
)

_MAX_BATCH_ITEMS = 200
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


def validate_relationship_payload(
    kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if kind in DUPLICATE_KINDS:
        validate_duplicate(kind, anchors, payload)
    elif kind == CATEGORY_OVERRIDE_RETIRE:
        validate_override_retire(anchors, payload)
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
    _reason(payload)
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
    only projects as dangling. Runs read-only. Returns a small preview summary.
    Raises :class:`FinCLError` on the first invalid item so a durable append is
    never made for a batch that cannot fully replay.
    """
    from .categorization import CATEGORIZATION_DECISION_KINDS, validate_payload
    from .common import resolve_category
    from .decisions import resolve_anchor

    def _entry(anchor: str) -> str:
        entry_id = resolve_anchor(connection, anchor)
        if entry_id is None:
            raise FinCLError(f"transaction {anchor} no longer resolves")
        return entry_id

    suppress_pairs: list[tuple[str, str]] = []
    for index, item in enumerate(items):
        kind, anchors, payload = item["kind"], dict(item["anchors"]), dict(item["payload"])
        try:
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
                _reason(payload)
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


def resolve(connection: sqlite3.Connection) -> None:
    """Fold effective DUPLICATE confirmations into ``duplicate_suppressions``.

    Group-wide checks: an anchor designated primary must not itself be suppressed
    elsewhere, and a candidate must not be claimed by two primaries. A pair that
    fails is left un-applied (its ``relationship_decisions`` row already records
    the operator decision and stays inspectable); it never silently promotes a
    different row.
    """
    connection.execute("DELETE FROM duplicate_suppressions")
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
