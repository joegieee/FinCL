"""The planned-flows read: every planned item, where each occurrence stands, and
what the forecast carries for it.

Amounts are native decimal strings in the item's own currency; the reported
mirror annotates them like every other workspace read.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..db import read_snapshot
from ..decisions import resolve_anchor
from ..errors import FinCLError
from ..generation import current_generation
from ..read_cache import request_cache
from .lifecycle import Assessment, Occurrence, Slice, assess, envelope
from .models import DIRECTION_OUTFLOW, LINK_TRIP, STATE_OVERDUE
from .trip_spend import TripSpend, trip_spend_to_date

_CENT = Decimal("0.01")
_ZERO = Decimal(0)
# How far either side of an occurrence's window a payment may fall and still be
# offered as its match.
CANDIDATE_DAYS = 14
_CANDIDATE_LIMIT = 25


def money(value: Decimal) -> str:
    return str(value.quantize(_CENT))


def _decimal(coefficient: int, scale: int) -> Decimal:
    return Decimal(int(coefficient)).scaleb(-int(scale))


@dataclass(frozen=True)
class Horizon:
    reference: str
    cutoff: str | None
    end: str | None


def horizon_for(connection: sqlite3.Connection, today: date) -> Horizon:
    from ..forecast.engine import HORIZON_MONTHS, add_months
    from ..position.queries import position_page_from

    cutoff = position_page_from(connection, today=today).get("supported_cutoff")
    if cutoff is None:
        return Horizon(today.isoformat(), None, None)
    end = add_months(date.fromisoformat(str(cutoff)), HORIZON_MONTHS).isoformat()
    return Horizon(str(cutoff), str(cutoff), end)


@dataclass(frozen=True)
class AssessedOccurrence:
    flow: dict[str, Any]
    occurrence: Occurrence
    slices: list[Slice]
    assessment: Assessment


def _match_rows(connection: sqlite3.Connection) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT flow, occurrence, entry_anchor, amount_coefficient, amount_scale, reason "
        "FROM planned_matches ORDER BY journal_seq"
    ):
        entry_id = resolve_anchor(connection, str(row["entry_anchor"]))
        entry = connection.execute(
            "SELECT e.effective_at, a.display_name AS account, "
            "coalesce(r.description_raw, d.description_raw, '') AS description "
            "FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
            "LEFT JOIN raw_records r ON r.id = e.raw_record_id "
            "LEFT JOIN document_records d ON d.id = e.document_record_id WHERE e.id = ?",
            (entry_id,),
        ).fetchone() if entry_id else None
        rows.setdefault((str(row["flow"]), str(row["occurrence"])), []).append({
            "entry": str(row["entry_anchor"]),
            "on": str(entry["effective_at"])[:10] if entry else None,
            "amount": money(_decimal(row["amount_coefficient"], row["amount_scale"])),
            "description": str(entry["description"]) if entry else None,
            "account": str(entry["account"]) if entry else None,
            "dangling": entry is None,
            "reason": str(row["reason"]),
        })
    return rows


def assessed(
    connection: sqlite3.Connection,
    horizon: Horizon,
    *,
    trip_spend: TripSpend = trip_spend_to_date,
    today: date,
) -> tuple[list[tuple[dict[str, Any], list[AssessedOccurrence], Decimal | None]], dict[tuple[str, str], list[dict[str, Any]]]]:
    """Every flow with its assessed occurrences; trip flows also report spend beyond the plan."""
    flows = [dict(row) for row in connection.execute("SELECT * FROM planned_flows ORDER BY name, flow")]
    outcomes = {
        (str(row["flow"]), str(row["occurrence"])): (str(row["outcome"]), str(row["reason"]))
        for row in connection.execute("SELECT flow, occurrence, outcome, reason FROM planned_resolutions")
    }
    matches = _match_rows(connection)
    result: list[tuple[dict[str, Any], list[AssessedOccurrence], Decimal | None]] = []
    for flow in flows:
        occurrences = [
            Occurrence(
                str(row["occurrence"]), str(row["earliest"]), str(row["latest"]),
                _decimal(row["amount_coefficient"], row["amount_scale"]),
                outcomes.get((str(flow["flow"]), str(row["occurrence"])), (None, None))[0],
            )
            for row in connection.execute(
                "SELECT * FROM planned_occurrences WHERE flow = ? ORDER BY earliest, position",
                (flow["flow"],),
            )
        ]
        over: Decimal | None = None
        if flow["link_kind"] == LINK_TRIP:
            spend = trip_spend(connection, str(flow["link_id"]), str(flow["currency"]), today.isoformat())
            parts = [Slice(str(part["on"]), Decimal(str(part["amount"]))) for part in spend.get("parts", [])]
            by_key, over = envelope(occurrences, parts)
        else:
            by_key = {
                item.key: [
                    Slice(str(match["on"]), Decimal(match["amount"]))
                    for match in matches.get((str(flow["flow"]), item.key), [])
                    if not match["dangling"]
                ]
                for item in occurrences
            }
        rows = [
            AssessedOccurrence(
                flow, item, by_key[item.key],
                assess(
                    item, by_key[item.key], direction=str(flow["direction"]),
                    certainty=str(flow["certainty"]), reference=horizon.reference,
                    horizon_end=horizon.end,
                ),
            )
            for item in occurrences
        ]
        result.append((flow, rows, over))
    return result, matches


def _forecast_cell(assessment: Assessment) -> dict[str, Any] | None:
    if not assessment.placements:
        return None
    cases = sorted({case for placement in assessment.placements for case in placement.cases})
    return {
        "cases": cases,
        "amount": money(sum((placement.amount for placement in assessment.placements), _ZERO)),
        "placements": [
            {
                "earliest": placement.earliest, "latest": placement.latest,
                "amount": money(placement.amount), "cases": list(placement.cases),
                "overdue": placement.overdue,
            }
            for placement in assessment.placements
        ],
    }


def _occurrence_cell(
    row: AssessedOccurrence, matches: list[dict[str, Any]], outcome: tuple[str, str] | None,
) -> dict[str, Any]:
    item, assessment = row.occurrence, row.assessment
    forecast = _forecast_cell(assessment)
    return {
        "key": item.key,
        "earliest": item.earliest,
        "latest": item.latest,
        "on": item.earliest if item.earliest == item.latest else None,
        "month": item.earliest[:7],
        "planned": money(item.planned),
        "matched": money(assessment.matched),
        "remaining": money(assessment.remaining),
        "state": assessment.state,
        "overdue": assessment.overdue,
        "outcome": {"outcome": outcome[0], "reason": outcome[1]} if outcome else None,
        "in_forecast": forecast is not None,
        "forecast": forecast,
        "forecast_note": assessment.note,
        "matches": matches,
        "actual_parts": (
            [{"on": part.on, "amount": money(part.amount)} for part in row.slices]
            if row.flow["link_kind"] == LINK_TRIP else []
        ),
    }


def _question(flow: dict[str, Any]) -> str:
    return (
        "Did this payment happen?" if flow["direction"] == DIRECTION_OUTFLOW
        else "Did this money arrive?"
    )


def planned_page_from(
    connection: sqlite3.Connection, *, today: date, trip_spend: TripSpend = trip_spend_to_date,
) -> dict[str, Any]:
    generation = current_generation(connection)
    if generation is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    horizon = horizon_for(connection, today)
    flows, matches = assessed(connection, horizon, trip_spend=trip_spend, today=today)
    outcomes = {
        (str(row["flow"]), str(row["occurrence"])): (str(row["outcome"]), str(row["reason"]))
        for row in connection.execute("SELECT flow, occurrence, outcome, reason FROM planned_resolutions")
    }
    accounts = {str(row[0]): str(row[1] or row[0]) for row in connection.execute("SELECT id, display_name FROM accounts")}
    trips = {str(row[0]): str(row[1]) for row in connection.execute("SELECT trip_id, name FROM trips")}

    flow_cells: list[dict[str, Any]] = []
    months: dict[str, dict[str, Any]] = {}
    attention: list[dict[str, Any]] = []
    for flow, rows, over in flows:
        flow_id = str(flow["flow"])
        occurrences = [
            _occurrence_cell(row, matches.get((flow_id, row.occurrence.key), []), outcomes.get((flow_id, row.occurrence.key)))
            for row in rows
        ]
        link = None
        if flow["link_kind"]:
            link = {
                "kind": flow["link_kind"], "id": flow["link_id"],
                "name": trips.get(str(flow["link_id"])), "exists": str(flow["link_id"]) in trips,
            }
        upcoming = [cell for cell in occurrences if cell["state"] not in ("done", "cancelled", "matched")]
        flow_cells.append({
            "flow": flow_id,
            "name": flow["name"],
            "direction": flow["direction"],
            "currency": flow["currency"],
            "account_id": flow["account_id"],
            "account_name": accounts.get(str(flow["account_id"])) if flow["account_id"] else None,
            "category": flow["category"],
            "certainty": flow["certainty"],
            "baseline_treatment": flow["baseline_treatment"],
            "link": link,
            "reason": flow["reason"],
            "planned": money(sum((row.occurrence.planned for row in rows), _ZERO)),
            "matched": money(sum((row.assessment.matched for row in rows), _ZERO)),
            "remaining": money(sum((row.assessment.remaining for row in rows), _ZERO)),
            "over_plan": money(over) if over else None,
            "next_due": upcoming[0]["earliest"] if upcoming else None,
            "occurrences": occurrences,
        })
        for row, cell in zip(rows, occurrences, strict=True):
            month = months.setdefault(cell["month"], {"month": cell["month"], "items": [], "totals": {}})
            month["items"].append({"flow": flow_id, "occurrence": cell["key"]})
            if cell["state"] not in ("done", "cancelled", "matched"):
                totals = month["totals"].setdefault(flow["currency"], {"outflows": _ZERO, "inflows": _ZERO})
                totals[f"{flow['direction']}s"] += row.assessment.remaining
            if cell["state"] == STATE_OVERDUE:
                attention.append({
                    "flow": flow_id, "occurrence": cell["key"], "name": flow["name"],
                    "direction": flow["direction"], "currency": flow["currency"],
                    "latest": cell["latest"], "remaining": cell["remaining"],
                    "question": _question(flow), "trip_linked": flow["link_kind"] == LINK_TRIP,
                })
    for month in months.values():
        month["totals"] = {
            currency: {direction: money(amount) for direction, amount in cell.items()}
            for currency, cell in sorted(month["totals"].items())
        }
    attention.sort(key=lambda item: (item["latest"], item["name"]))
    return {
        "generation": str(generation["generation_token"]),
        "as_of": today.isoformat(),
        "cutoff": horizon.cutoff,
        "reference_day": horizon.reference,
        "horizon": (
            {"start": (date.fromisoformat(horizon.reference) + timedelta(days=1)).isoformat(), "end": horizon.end}
            if horizon.end else None
        ),
        "flows": flow_cells,
        "months": [months[key] for key in sorted(months)],
        "attention": attention,
        "rules": [
            "an occurrence is overdue once its last possible day is on or before the cash cutoff",
            "overdue spending stays in the forecast on the first forecast day until matched or resolved",
            "overdue income is left out of the forecast until it is matched",
            "spending you are only maybe going to do counts in the cautious case only; "
            "expected income counts in the base case only; maybe income is never counted",
        ],
    }


def planned_page(database: Path, *, today: date) -> dict[str, Any]:
    with read_snapshot(database) as connection, request_cache(connection):
        return planned_page_from(connection, today=today)


def match_candidates(database: Path, *, flow: str, occurrence: str) -> dict[str, Any]:
    """Transactions that could settle one occurrence, nearest first."""
    from ..queries import _ENTRY_VIEW

    with read_snapshot(database) as connection:
        generation = current_generation(connection)
        if generation is None:
            raise FinCLError("Ledger is not ready. Run fincl refresh first.")
        owner = connection.execute("SELECT * FROM planned_flows WHERE flow = ?", (flow,)).fetchone()
        if owner is None:
            raise FinCLError("That planned item does not exist")
        if owner["link_kind"] == LINK_TRIP:
            raise FinCLError("A trip budget is matched from the trip's own transactions, not by hand")
        item = connection.execute(
            "SELECT * FROM planned_occurrences WHERE flow = ? AND occurrence = ?", (flow, occurrence)
        ).fetchone()
        if item is None:
            raise FinCLError(f"Occurrence {occurrence} does not exist on this planned item")
        planned = _decimal(item["amount_coefficient"], item["amount_scale"])
        claimed: dict[str, Decimal] = {}
        here: dict[str, Decimal] = {}
        for row in connection.execute(
            "SELECT flow, occurrence, entry_anchor, amount_coefficient, amount_scale FROM planned_matches"
        ):
            amount = _decimal(row["amount_coefficient"], row["amount_scale"])
            if (row["flow"], row["occurrence"]) == (flow, occurrence):
                here[str(row["entry_anchor"])] = amount
            else:
                claimed[str(row["entry_anchor"])] = claimed.get(str(row["entry_anchor"]), _ZERO) + amount
        remaining = planned - sum(here.values(), _ZERO)
        low = (date.fromisoformat(item["earliest"]) - timedelta(days=CANDIDATE_DAYS)).isoformat()
        high = (date.fromisoformat(item["latest"]) + timedelta(days=CANDIDATE_DAYS)).isoformat()
        rows = connection.execute(
            f"SELECT natural_anchor, effective_at, merchant, description, account, amount_coefficient, "
            f"amount_scale FROM ({_ENTRY_VIEW}) WHERE eligible AND bucket NOT IN ('internal', 'excluded') "
            "AND currency_pocket = ? AND economic_direction = ? AND substr(effective_at, 1, 10) BETWEEN ? AND ?",
            (owner["currency"], "OUT" if owner["direction"] == DIRECTION_OUTFLOW else "IN", low, high),
        ).fetchall()
        target = date.fromisoformat(item["earliest"])
        ranked: list[tuple[tuple[bool, int, Decimal], dict[str, Any]]] = []
        for row in rows:
            anchor = str(row["natural_anchor"])
            amount = abs(_decimal(row["amount_coefficient"], row["amount_scale"]))
            available = amount - claimed.get(anchor, _ZERO)
            if available <= 0 and anchor not in here:
                continue
            on = str(row["effective_at"])[:10]
            days = 0 if item["earliest"] <= on <= item["latest"] else abs((date.fromisoformat(on) - target).days)
            ranked.append(((anchor not in here, days, abs(available - remaining)), {
                "entry": anchor, "on": on, "merchant": row["merchant"], "description": row["description"],
                "account": row["account"], "amount": money(amount), "available": money(available),
                "suggested_amount": money(min(available, remaining)) if anchor not in here
                else money(here[anchor]),
                "matched_here": money(here[anchor]) if anchor in here else None,
            }))
        ranked.sort(key=lambda pair: pair[0])
        candidates = [cell for _, cell in ranked if cell["matched_here"] or Decimal(cell["suggested_amount"]) > 0]
        return {
            "generation": str(generation["generation_token"]),
            "flow": flow, "occurrence": occurrence, "currency": owner["currency"],
            "planned": money(planned), "remaining": money(max(remaining, _ZERO)),
            "window": {"from": low, "to": high},
            "candidates": candidates[:_CANDIDATE_LIMIT],
        }


__all__ = [
    "CANDIDATE_DAYS",
    "AssessedOccurrence",
    "Horizon",
    "assessed",
    "horizon_for",
    "match_candidates",
    "money",
    "planned_page",
    "planned_page_from",
]
