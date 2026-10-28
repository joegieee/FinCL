"""Stage 5A — issuing and reading back a forecast snapshot.

:func:`snapshot_payload` is the *only* way a Stage 3B timeline becomes a stored
snapshot, and it is an explicit request: it takes a snapshot id the caller
chose, runs the timeline once, and hands back a journal payload.  It writes
nothing.  Every read in this codebase that recomputes a timeline — the Stage 3B
engine itself, the Stage 4 reads that compose it, any future presentation layer
— therefore cannot produce a snapshot as a side effect, and cannot touch one
that exists.  That is the immutability guarantee stated as a call graph rather
than as a promise: recalculation has no write path to
:mod:`fincl.variance.projection`, and the projection refuses a second issue of
an id that is already taken.

What is captured
----------------
Everything needed to hold the forecast to account later and nothing that would
have to be recomputed to read it: the inputs' generation and basis dates, the
scenario settings, the reserve and currency, the horizon, the identity of every
cash event that was issued, and the outputs that were shown.  A limitation the
forecast carried is captured too — a forecast issued with known gaps must never
read back later as though it had been unqualified.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from .. import clock
from ..common import utc_now
from ..db import connect_readonly
from ..errors import FinCLError
from ..forecast.engine import (
    ENGINE_VERSION,
    LIKELY_EXCLUDE,
    SCENARIO_BASE,
    SCENARIO_CONSERVATIVE,
    cash_timeline,
)
from .models import (
    SNAPSHOT_CONTRACT_VERSION,
    canonical_document,
    snapshot_digest,
)
from .validation import validate_snapshot_document

SNAPSHOT_REQUEST_RULE = (
    "a snapshot is issued only by an explicit request that names its id; no "
    "read, refresh or recalculation creates or alters one"
)


def _event_document(item: dict[str, Any]) -> dict[str, Any]:
    """One issued cash event, reduced to what a later comparison needs.

    The provenance blob is not carried: it is a description of where a number
    came from at issue time, it is not comparable against anything, and copying
    it would make two snapshots of the same forecast differ over commentary.
    ``confidence`` and ``recurring_salary`` are lifted out of it because the
    Likely-income realization report is answered from them.
    """
    provenance = item.get("provenance") or {}
    timing = item["timing"]
    return {
        "event_id": str(item["event_id"]),
        "source_id": str(item["source_id"]),
        "flow_class": str(item["column"]),
        "direction": str(item["direction"]),
        "on": str(item["on"]),
        "amount": str(item["amount"]),
        "label": str(item["label"]),
        "timing": {
            "basis": str(timing["basis"]),
            "earliest": str(timing["earliest"]),
            "latest": str(timing["latest"]),
            "precision": str(timing["precision"]),
        },
        # A modelled residual slice carries no sourced confidence; saying so is
        # better than borrowing "confirmed" from a neighbour.
        "confidence": str(provenance.get("confidence") or "modelled"),
        "recurring_salary": bool(provenance.get("recurring_salary", False)),
        "conditional": bool(item.get("conditional", False)),
        "condition": item.get("condition"),
    }


def build_snapshot_document(
    timeline: dict[str, Any],
    *,
    currency: str,
    case: str,
    issued_at: str,
    issued_as_of: str,
    likely_income_policy: str,
    strong_income_interruption: bool,
) -> dict[str, Any]:
    """Reduce a Stage 3B result to the document a snapshot stores."""
    if case not in (SCENARIO_BASE, SCENARIO_CONSERVATIVE):
        raise FinCLError(f"a snapshot case is {SCENARIO_BASE!r} or {SCENARIO_CONSERVATIVE!r}")
    cell = timeline.get("by_currency", {}).get(currency)
    if cell is None:
        raise FinCLError(
            f"the forecast states no timeline for {currency}; there is nothing to issue"
        )
    scenario = cell.get(case)
    if scenario is None:
        reason = cell.get("reason") or "the scenario produced no cash timeline"
        raise FinCLError(
            f"the {case} case for {currency} produced no issued outputs: {reason}"
        )
    reserve = cell.get("reserve_readiness", {})
    horizon = timeline.get("horizon", {})
    document = {
        "contract_version": SNAPSHOT_CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "issued_at": issued_at,
        "generation": str(timeline["generation"]),
        "currency": currency,
        "scale": int(cell["scale"]),
        "case": case,
        "settings": {
            "likely_income_policy": likely_income_policy,
            "strong_income_interruption": strong_income_interruption,
        },
        "basis": {
            "supported_cash_cutoff": horizon.get("supported_cash_cutoff"),
            "manifest_as_of": timeline.get("manifest_as_of"),
            "issued_as_of": issued_as_of,
        },
        "horizon": {
            "start": str(scenario["horizon"]["start"]),
            "first_modelled_day": str(scenario["horizon"]["first_modelled_day"]),
            "end": str(scenario["horizon"]["end"]),
            "months": int(horizon.get("months") or len(scenario["months"])),
        },
        "reserve": {
            "available": bool(reserve.get("available", False)),
            "target": reserve.get("target"),
            "reason": reserve.get("reason"),
        },
        "opening_balance": str(scenario["starting_balance"]),
        "events": [_event_document(item) for item in scenario["events"]["items"]],
        "outputs": {
            "months": scenario["months"],
            "totals": scenario["totals"],
            "closing_balance": scenario["closing_balance"],
            "lowest_balance": scenario["lowest_balance"],
            "first_zero": scenario["first_zero"],
            "first_reserve_breach": scenario["first_reserve_breach"],
        },
        "limitations": [
            *(str(item) for item in timeline.get("limitations", [])),
            *(str(item) for item in scenario.get("limitations", [])),
        ],
    }
    # Round-trip through the canonical form so the document that is validated,
    # digested and stored is exactly the JSON the journal will carry.
    normalized: dict[str, Any] = json.loads(canonical_document(document))
    validate_snapshot_document(normalized)
    return normalized


def snapshot_payload(
    database: Path,
    *,
    snapshot: str,
    currency: str,
    reason: str,
    case: str = SCENARIO_BASE,
    today: date | None = None,
    issued_at: str | None = None,
    likely_income_policy: str = LIKELY_EXCLUDE,
    strong_income_interruption: bool = False,
) -> tuple[dict[str, str], dict[str, Any]]:
    """``(anchors, payload)`` for one ``FORECAST_SNAPSHOT_ISSUE`` record.

    Explicitly requested and side-effect free: this runs the timeline and
    returns a journal payload. The caller appends it (through
    ``fincl.application.record_decision``) when it means to issue the forecast.
    """
    today = today or clock.today()
    timeline = cash_timeline(
        database,
        today=today,
        likely_income_policy=likely_income_policy,
        strong_income_interruption=strong_income_interruption,
    )
    document = build_snapshot_document(
        timeline,
        currency=currency,
        case=case,
        issued_at=issued_at or utc_now(),
        issued_as_of=today.isoformat(),
        likely_income_policy=likely_income_policy,
        strong_income_interruption=strong_income_interruption,
    )
    return (
        {"snapshot": snapshot},
        {
            "document": document,
            "digest": snapshot_digest(document),
            "reason": reason,
        },
    )


def stored_snapshot(connection: sqlite3.Connection, snapshot: str) -> dict[str, Any]:
    """The snapshot as issued, with its digest re-checked against its bytes."""
    row = connection.execute(
        "SELECT snapshot, issued_at, document_json, document_digest, journal_seq "
        "FROM forecast_snapshots WHERE snapshot = ?",
        (snapshot,),
    ).fetchone()
    if row is None:
        raise FinCLError(f"no forecast snapshot has been issued under the id {snapshot}")
    document = json.loads(row["document_json"])
    recomputed = snapshot_digest(document)
    return {
        "snapshot": str(row["snapshot"]),
        "issued_at": str(row["issued_at"]),
        "journal_seq": int(row["journal_seq"]),
        "digest": str(row["document_digest"]),
        "digest_verified": recomputed == str(row["document_digest"]),
        "document": document,
        "immutability": SNAPSHOT_REQUEST_RULE,
    }


def snapshot_index(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every issued snapshot, newest issue first."""
    return [
        {
            "snapshot": str(row["snapshot"]),
            "issued_at": str(row["issued_at"]),
            "digest": str(row["document_digest"]),
            "journal_seq": int(row["journal_seq"]),
        }
        for row in connection.execute(
            "SELECT snapshot, issued_at, document_digest, journal_seq "
            "FROM forecast_snapshots ORDER BY issued_at DESC, snapshot"
        )
    ]


def forecast_snapshots_page(database: Path) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return {
            "snapshots": snapshot_index(connection),
            "contract_version": SNAPSHOT_CONTRACT_VERSION,
            "immutability": SNAPSHOT_REQUEST_RULE,
        }
    finally:
        connection.close()


__all__ = [
    "SNAPSHOT_REQUEST_RULE",
    "build_snapshot_document",
    "forecast_snapshots_page",
    "snapshot_index",
    "snapshot_payload",
    "stored_snapshot",
]
