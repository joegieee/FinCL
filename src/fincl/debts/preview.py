"""Full-state pre-append check for the ``PERSONAL_DEBT_*`` / residual kinds.

``validate_debt_payload`` covers shape; this module covers everything that needs
the current projection: the debt exists (or, for OPEN, does not), what a retire
names is there, every source anchor resolves, principal direction / currency is
right, the debt chronology still holds, and no bank component ends up
over-claimed. It works by applying the pending decision inside a SAVEPOINT and
running the real projection pass, so the preview can never diverge from replay.

``draft_preview`` does the same for a draft the Debts screens are about to
save, and reports what it would do to each debt's balances. It also completes
the amounts a draft leaves to the server: an event's principal as the total of
its sources, and the owner's share as whatever the draft leaves of the bill.
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Any

from ..analysis.allocations import claimed_amount
from ..common import INTERNAL_GROUP, resolve_category
from ..errors import FinCLError
from ..money import MAX_SCALE, ExactMoney, exact_sum, format_decimal
from .models import (
    DEBT_DECISION_KINDS,
    ENTRY_RESIDUAL_RETIRE,
    ENTRY_RESIDUAL_SET,
    INBOUND_CLASSIFICATION_RETIRE,
    PERSONAL_DEBT_CLOSE,
    PERSONAL_DEBT_DETAILS_SET,
    PERSONAL_DEBT_EVENT_RETIRE,
    PERSONAL_DEBT_EVENT_SET,
    PERSONAL_DEBT_OPEN,
    PERSONAL_DEBT_REOPEN,
)
from .projection import _entry_row, apply_debt, resolve_debts
from .queries import principal_timeline
from .validation import validate_debt_payload

_PREVIEW_SEQ = 1 << 40


def preview_debt(
    connection: sqlite3.Connection,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    *,
    resolve_anchor: Any,
    custom_categories: frozenset[str] = frozenset(),
) -> None:
    """Raise :class:`FinCLError` if appending this decision would not fully replay."""
    validate_debt_payload(decision_kind, anchors, payload)
    check_expense_category(decision_kind, payload, custom_categories)
    check_references(connection, decision_kind, anchors, payload, resolve_anchor=resolve_anchor)

    connection.execute("SAVEPOINT debt_preview")
    try:
        apply_debt(
            connection,
            decision_kind=decision_kind,
            anchors=anchors,
            payload=payload,
            journal_seq=_PREVIEW_SEQ,
            batch_index=None,
            resolve_anchor=resolve_anchor,
        )
        resolve_debts(connection)
    finally:
        connection.execute("ROLLBACK TO debt_preview")
        connection.execute("RELEASE debt_preview")


_NEEDS_DEBT = frozenset({
    PERSONAL_DEBT_DETAILS_SET,
    PERSONAL_DEBT_EVENT_SET,
    PERSONAL_DEBT_EVENT_RETIRE,
    PERSONAL_DEBT_CLOSE,
    PERSONAL_DEBT_REOPEN,
})


def check_references(
    connection: sqlite3.Connection,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    *,
    resolve_anchor: Any,
) -> None:
    """What the decision names exists in the current projection.

    Replay would accept a retire of something that is not there as a no-op, so
    this is the only place a mistyped or stale reference is refused. A batch
    calls it per item against the state its earlier items left behind.
    """
    debt = (
        connection.execute(
            "SELECT opened_on FROM personal_debts WHERE debt_id = ?", (anchors["debt"],)
        ).fetchone()
        if "debt" in anchors
        else None
    )
    if decision_kind == PERSONAL_DEBT_OPEN and debt is not None:
        raise FinCLError("that debt id is already open")
    if decision_kind in _NEEDS_DEBT and debt is None:
        raise FinCLError("that debt does not exist")
    if (
        decision_kind == PERSONAL_DEBT_DETAILS_SET
        and debt is not None
        and payload["due_on"] is not None
        and str(payload["due_on"]) < str(debt["opened_on"])
    ):
        raise FinCLError("due_on may not precede opened_on")
    if decision_kind == PERSONAL_DEBT_EVENT_RETIRE and connection.execute(
        "SELECT 1 FROM personal_debt_events WHERE debt_id = ? AND event_id = ?",
        (anchors["debt"], anchors["event"]),
    ).fetchone() is None:
        raise FinCLError("that debt event does not exist")
    if decision_kind == ENTRY_RESIDUAL_SET and resolve_anchor(anchors["entry"]) is None:
        raise FinCLError("the source transaction does not resolve to one selected entry")
    if decision_kind == ENTRY_RESIDUAL_RETIRE and connection.execute(
        "SELECT 1 FROM entry_residuals WHERE entry_anchor = ?", (anchors["entry"],)
    ).fetchone() is None:
        raise FinCLError("that transaction has no recorded owner share")
    if decision_kind == INBOUND_CLASSIFICATION_RETIRE and connection.execute(
        "SELECT 1 FROM inbound_classifications WHERE anchor = ?", (anchors["entry"],)
    ).fetchone() is None:
        raise FinCLError("that transaction has no recorded incoming type")


def check_expense_category(
    decision_kind: str, payload: dict[str, Any], custom_categories: frozenset[str]
) -> None:
    """A debt's expense category is a spending category from the shared taxonomy.

    Replay stays shape-only, so journals written while the field was free text
    still load; only new decisions are held to this. The owner's share of a split
    and an expense someone paid for me are reported as spending under this key,
    so it must be one the category views know, and neither ``Internal`` (not
    spending) nor ``Unclassified`` (says nothing about where the money went).
    """
    if decision_kind != ENTRY_RESIDUAL_SET and not (
        decision_kind == PERSONAL_DEBT_EVENT_SET and payload.get("expense_category") is not None
    ):
        return
    category = str(payload["expense_category"])
    group, _, _ = resolve_category(category, custom_categories)
    if group in {INTERNAL_GROUP, "Unclassified"}:
        raise FinCLError(f"{category!r} is not a spending category; choose where the expense belongs")


def _exact_text(value: Fraction, scale: int) -> str:
    for candidate in range(scale, MAX_SCALE + 1):
        scaled = value * 10**candidate
        if scaled.denominator == 1:
            return format_decimal(int(scaled), candidate)
    raise FinCLError("the amount cannot be represented as exact money")


def _complete(
    connection: sqlite3.Connection, kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Fill the amounts a draft leaves open; ``None`` drops an owner share of nothing."""
    if kind == PERSONAL_DEBT_EVENT_SET and payload.get("principal_amount") is None:
        sources = payload.get("entry_allocations")
        if not isinstance(sources, list) or not sources:
            raise FinCLError("enter the event principal")
        try:
            parts = [ExactMoney.parse(str(item["principal_amount"])) for item in sources]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise FinCLError("every source needs a principal amount") from exc
        return {**payload, "principal_amount": format_decimal(*exact_sum(*parts))}
    if kind == ENTRY_RESIDUAL_SET and payload.get("expense_amount") is None:
        entry = _entry_row(connection, anchors["entry"])
        if entry is None:
            raise FinCLError("the source transaction does not resolve to one selected entry")
        gross = Fraction(abs(int(entry["amount_coefficient"])), 10 ** int(entry["amount_scale"]))
        own = connection.execute(
            "SELECT expense_coefficient, expense_scale FROM entry_residuals "
            "WHERE entry_anchor = ? AND bound = 1",
            (anchors["entry"],),
        ).fetchone()
        claimed = claimed_amount(connection, anchors["entry"]) - (
            Fraction(int(own["expense_coefficient"]), 10 ** int(own["expense_scale"]))
            if own is not None else Fraction(0)
        )
        left = gross - claimed
        scale = int(entry["amount_scale"])
        if left < 0:
            raise FinCLError(
                f"the shares come to {_exact_text(-left, scale)} more than the "
                f"{_exact_text(gross, scale)} transaction"
            )
        if left == 0:
            return None
        if not payload.get("expense_category"):
            raise FinCLError("choose a category for your own share")
        return {**payload, "expense_amount": _exact_text(left, scale)}
    return payload


def _compare(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    """One debt's events after the draft, each marked with how the draft touched it."""
    earlier = {event["event_id"]: event for event in (before or {}).get("events", [])}
    events = []
    for event in after["events"]:
        prior = earlier.get(event["event_id"])
        if prior is None:
            change = "added"
        elif any(prior[key] != event[key] for key in ("event_kind", "effective_on", "principal")):
            change = "changed"
        elif ExactMoney.parse(prior["balance"]) != ExactMoney.parse(event["balance"]):
            change = "balance"
        else:
            change = None
        events.append({**event, "change": change,
                       "balance_before": prior["balance"] if prior is not None else None})
    kept = {event["event_id"] for event in after["events"]}
    return {
        "debt_id": after["debt_id"],
        "counterparty_label": after["counterparty_label"],
        "direction": after["direction"],
        "currency": after["currency"],
        "before": None if before is None else {
            key: before[key] for key in ("status", "closed_on", "remaining")
        },
        "after": {key: after[key] for key in ("status", "closed_on", "remaining")},
        "events": events,
        "removed": [event for event in (before or {}).get("events", []) if event["event_id"] not in kept],
    }


def draft_preview(
    connection: sqlite3.Connection,
    items: list[dict[str, Any]],
    *,
    resolve_anchor: Any,
    custom_categories: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Check a debt draft as one batch and say what it does to each debt.

    Returns the items exactly as they should be saved (amounts completed, an
    empty owner share dropped) and, per debt the draft names, its events before
    and after with the principal standing after each. Nothing is kept.
    """
    debt_ids = list(dict.fromkeys(
        str(item["anchors"]["debt"]) for item in items if "debt" in item["anchors"]
    ))
    before = {debt_id: principal_timeline(connection, debt_id) for debt_id in debt_ids}
    completed: list[dict[str, Any]] = []
    connection.execute("SAVEPOINT debt_draft")
    try:
        for index, item in enumerate(items):
            kind, anchors = str(item["kind"]), dict(item["anchors"])
            if kind not in DEBT_DECISION_KINDS:
                raise FinCLError(f"{kind} is not a debt decision")
            payload = _complete(connection, kind, anchors, dict(item["payload"]))
            if payload is None:
                continue
            validate_debt_payload(kind, anchors, payload)
            check_expense_category(kind, payload, custom_categories)
            check_references(connection, kind, anchors, payload, resolve_anchor=resolve_anchor)
            apply_debt(
                connection, decision_kind=kind, anchors=anchors, payload=payload,
                journal_seq=_PREVIEW_SEQ + index, batch_index=index, resolve_anchor=resolve_anchor,
            )
            completed.append({"kind": kind, "anchors": anchors, "payload": payload})
        resolve_debts(connection)
        after = {debt_id: principal_timeline(connection, debt_id) for debt_id in debt_ids}
    finally:
        connection.execute("ROLLBACK TO debt_draft")
        connection.execute("RELEASE debt_draft")
    return {
        "items": completed,
        "debts": [
            _compare(before[debt_id], timeline)
            for debt_id, timeline in after.items() if timeline is not None
        ],
    }
