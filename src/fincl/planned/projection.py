"""State checks and replay for the ``PLANNED_*`` kinds (schema 31).

One checker serves the command boundary, batches and replay, so a decision the
API accepts always replays and replay refuses exactly what the API would.
Checks that need the live ledger — a transaction still resolving, an account or
trip still existing, a category still current — run only when a decision is
about to be written: replay must not fail because a later import re-anchored a
transaction or a trip was removed afterwards. A match therefore stores the
entry *anchor*, resolved again on every read.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    DIRECTION_OUTFLOW,
    LINK_TRIP,
    OUTCOME_CANCELLED,
    PLANNED_DECISION_KINDS,
    PLANNED_FLOW_RETIRE,
    PLANNED_FLOW_SET,
    PLANNED_MATCH_RETIRE,
    PLANNED_MATCH_SET,
    PLANNED_OCCURRENCE_RESOLVE_RETIRE,
    PLANNED_OCCURRENCE_RESOLVE_SET,
)
from .validation import parse_amount, validate_planned


@dataclass(frozen=True)
class EntryFacts:
    currency: str
    direction: str  # the ledger's economic direction, IN or OUT
    amount: Decimal  # magnitude


@dataclass(frozen=True)
class WriteChecks:
    """The live-ledger checks applied only when a decision is about to be written."""

    entry: Callable[[str], EntryFacts | None]
    account_exists: Callable[[str], bool]
    trip_exists: Callable[[str], bool]
    category: Callable[[str], None]


@dataclass
class _Flow:
    direction: str
    currency: str
    link_kind: str | None
    keys: set[str] = field(default_factory=set)


def _decimal(coefficient: int, scale: int) -> Decimal:
    return Decimal(int(coefficient)).scaleb(-int(scale))


@dataclass
class PlannedState:
    flows: dict[str, _Flow]
    matches: dict[tuple[str, str, str], Decimal]
    resolutions: dict[tuple[str, str], str]

    @classmethod
    def load(cls, connection: sqlite3.Connection) -> PlannedState:
        flows = {
            str(row["flow"]): _Flow(str(row["direction"]), str(row["currency"]), row["link_kind"])
            for row in connection.execute(
                "SELECT flow, direction, currency, link_kind FROM planned_flows"
            )
        }
        for row in connection.execute("SELECT flow, occurrence FROM planned_occurrences"):
            flows[str(row["flow"])].keys.add(str(row["occurrence"]))
        matches = {
            (str(row["flow"]), str(row["occurrence"]), str(row["entry_anchor"])):
                _decimal(row["amount_coefficient"], row["amount_scale"])
            for row in connection.execute(
                "SELECT flow, occurrence, entry_anchor, amount_coefficient, amount_scale "
                "FROM planned_matches"
            )
        }
        resolutions = {
            (str(row["flow"]), str(row["occurrence"])): str(row["outcome"])
            for row in connection.execute("SELECT flow, occurrence, outcome FROM planned_resolutions")
        }
        return cls(flows, matches, resolutions)

    def _flow(self, flow: str) -> _Flow:
        found = self.flows.get(flow)
        if found is None:
            raise FinCLError("That planned item does not exist")
        return found

    def _occurrence(self, flow: str, key: str) -> _Flow:
        found = self._flow(flow)
        if key not in found.keys:
            raise FinCLError(f"Occurrence {key} does not exist on this planned item")
        return found

    def _matched_keys(self, flow: str) -> set[str]:
        return {key for (owner, key, _) in self.matches if owner == flow}

    def check(
        self, kind: str, anchors: dict[str, str], payload: dict[str, Any],
        writes: WriteChecks | None = None,
    ) -> None:
        """Refuse a decision that cannot apply to this state, then apply it."""
        validate_planned(kind, anchors, payload)
        flow = anchors["flow"]
        if kind == PLANNED_FLOW_SET:
            keys = {str(item["key"]) for item in payload["occurrences"]}
            link = payload["link"]
            existing = self.flows.get(flow)
            if existing is not None:
                if existing.currency != payload["currency"]:
                    raise FinCLError(f"This planned item is in {existing.currency} and cannot change currency")
                if existing.direction != payload["direction"]:
                    raise FinCLError("A planned item cannot change between spending and income")
                matched = self._matched_keys(flow)
                if link is not None and matched:
                    raise FinCLError("Retire this item's matched payments before linking it to a trip")
                held = sorted((matched | {k for (owner, k) in self.resolutions if owner == flow}) - keys)
                if held:
                    raise FinCLError(
                        f"Occurrence(s) {', '.join(held)} have matches or an outcome; "
                        "retire those before removing the occurrence"
                    )
            if writes is not None:
                if payload["account_id"] is not None and not writes.account_exists(payload["account_id"]):
                    raise FinCLError("The selected account no longer exists")
                if link is not None and link["kind"] == LINK_TRIP and not writes.trip_exists(link["id"]):
                    raise FinCLError("That trip does not exist")
                if payload["category"] is not None:
                    writes.category(payload["category"])
            self.flows[flow] = _Flow(
                payload["direction"], payload["currency"], link["kind"] if link else None, keys
            )
        elif kind == PLANNED_FLOW_RETIRE:
            self._flow(flow)
            left = self._matched_keys(flow)
            if left:
                count = sum(1 for (owner, _, _) in self.matches if owner == flow)
                raise FinCLError(
                    f"Retiring this planned item leaves {count} matched payment(s) pointing at it; "
                    "retire them in the same batch"
                )
            del self.flows[flow]
            for resolved in [pair for pair in self.resolutions if pair[0] == flow]:
                del self.resolutions[resolved]
        elif kind == PLANNED_MATCH_SET:
            key, entry = anchors["occurrence"], anchors["entry"]
            owner = self._occurrence(flow, key)
            if owner.link_kind == LINK_TRIP:
                raise FinCLError("A trip budget is matched from the trip's own transactions, not by hand")
            if (flow, key) in self.resolutions:
                raise FinCLError("This occurrence already has an outcome; reopen it first")
            amount = parse_amount(payload["amount"])
            value = _decimal(amount.coefficient, amount.scale)
            if writes is not None:
                facts = writes.entry(entry)
                if facts is None:
                    raise FinCLError("The transaction no longer resolves")
                if facts.currency != owner.currency:
                    raise FinCLError(f"Only a {owner.currency} transaction can settle this item")
                wanted = "OUT" if owner.direction == DIRECTION_OUTFLOW else "IN"
                if facts.direction != wanted:
                    raise FinCLError(
                        "Only money going out can settle planned spending"
                        if wanted == "OUT" else "Only money coming in can settle planned income"
                    )
                elsewhere = sum(
                    (matched for (f, k, e), matched in self.matches.items()
                     if e == entry and (f, k) != (flow, key)),
                    Decimal(0),
                )
                if elsewhere + value > facts.amount:
                    raise FinCLError(
                        f"That transaction has {facts.amount - elsewhere} left to match, less than {value}"
                    )
            self.matches[(flow, key, entry)] = value
        elif kind == PLANNED_MATCH_RETIRE:
            if (flow, anchors["occurrence"], anchors["entry"]) not in self.matches:
                raise FinCLError("That match is not active")
            del self.matches[(flow, anchors["occurrence"], anchors["entry"])]
        elif kind == PLANNED_OCCURRENCE_RESOLVE_SET:
            key = anchors["occurrence"]
            self._occurrence(flow, key)
            if payload["outcome"] == OUTCOME_CANCELLED and any(
                (f, k) == (flow, key) for (f, k, _) in self.matches
            ):
                raise FinCLError("This occurrence has matched payments; mark it done instead")
            self.resolutions[(flow, key)] = str(payload["outcome"])
        elif kind == PLANNED_OCCURRENCE_RESOLVE_RETIRE:
            if (flow, anchors["occurrence"]) not in self.resolutions:
                raise FinCLError("This occurrence has no outcome to reopen")
            del self.resolutions[(flow, anchors["occurrence"])]


def write_checks(
    connection: sqlite3.Connection,
    resolve_anchor: Callable[[str], str | None],
    custom_categories: frozenset[str] = frozenset(),
) -> WriteChecks:
    from ..common import resolve_category

    def entry(anchor: str) -> EntryFacts | None:
        entry_id = resolve_anchor(anchor)
        if entry_id is None:
            return None
        row = connection.execute(
            "SELECT currency_pocket, economic_direction, amount_coefficient, amount_scale "
            "FROM ledger_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        return EntryFacts(
            str(row["currency_pocket"]), str(row["economic_direction"]),
            abs(_decimal(row["amount_coefficient"], row["amount_scale"])),
        )

    def category(name: str) -> None:
        resolve_category(name, custom_categories)

    return WriteChecks(
        entry=entry,
        account_exists=lambda account: connection.execute(
            "SELECT 1 FROM accounts WHERE id = ?", (account,)
        ).fetchone() is not None,
        trip_exists=lambda trip: connection.execute(
            "SELECT 1 FROM trips WHERE trip_id = ?", (trip,)
        ).fetchone() is not None,
        category=category,
    )


def check_planned_sequence(
    connection: sqlite3.Connection,
    items: Iterable[tuple[str, dict[str, str], dict[str, Any]]],
    resolve_anchor: Callable[[str], str | None],
    custom_categories: frozenset[str] = frozenset(),
) -> None:
    """Check planned decisions recorded together, in order, before they are written."""
    state = PlannedState.load(connection)
    writes = write_checks(connection, resolve_anchor, custom_categories)
    for kind, anchors, payload in items:
        if kind in PLANNED_DECISION_KINDS:
            state.check(kind, anchors, payload, writes)


def apply_planned(
    connection: sqlite3.Connection, *, kind: str, anchors: dict[str, str],
    payload: dict[str, Any], journal_seq: int,
) -> None:
    PlannedState.load(connection).check(kind, anchors, payload)
    flow = anchors["flow"]
    if kind == PLANNED_FLOW_SET:
        link = payload["link"]
        connection.execute(
            "INSERT OR REPLACE INTO planned_flows(flow, name, direction, currency, account_id, "
            "category, certainty, baseline_treatment, link_kind, link_id, reason, journal_seq) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (flow, payload["name"].strip(), payload["direction"], payload["currency"],
             payload["account_id"], payload["category"], payload["certainty"],
             payload["baseline_treatment"], link["kind"] if link else None,
             link["id"] if link else None, payload["reason"], journal_seq),
        )
        connection.execute("DELETE FROM planned_occurrences WHERE flow = ?", (flow,))
        for position, item in enumerate(payload["occurrences"]):
            amount = ExactMoney.parse(str(item["amount"]))
            connection.execute(
                "INSERT INTO planned_occurrences(flow, occurrence, position, earliest, latest, "
                "amount_coefficient, amount_scale) VALUES (?,?,?,?,?,?,?)",
                (flow, item["key"], position, item["earliest"], item["latest"],
                 amount.coefficient, amount.scale),
            )
    elif kind == PLANNED_FLOW_RETIRE:
        for table in ("planned_resolutions", "planned_occurrences", "planned_flows"):
            connection.execute(f"DELETE FROM {table} WHERE flow = ?", (flow,))
    elif kind == PLANNED_MATCH_SET:
        amount = ExactMoney.parse(str(payload["amount"]))
        connection.execute(
            "INSERT OR REPLACE INTO planned_matches(flow, occurrence, entry_anchor, "
            "amount_coefficient, amount_scale, reason, journal_seq) VALUES (?,?,?,?,?,?,?)",
            (flow, anchors["occurrence"], anchors["entry"], amount.coefficient, amount.scale,
             payload["reason"], journal_seq),
        )
    elif kind == PLANNED_MATCH_RETIRE:
        connection.execute(
            "DELETE FROM planned_matches WHERE flow = ? AND occurrence = ? AND entry_anchor = ?",
            (flow, anchors["occurrence"], anchors["entry"]),
        )
    elif kind == PLANNED_OCCURRENCE_RESOLVE_SET:
        connection.execute(
            "INSERT OR REPLACE INTO planned_resolutions(flow, occurrence, outcome, reason, journal_seq) "
            "VALUES (?,?,?,?,?)",
            (flow, anchors["occurrence"], payload["outcome"], payload["reason"], journal_seq),
        )
    elif kind == PLANNED_OCCURRENCE_RESOLVE_RETIRE:
        connection.execute(
            "DELETE FROM planned_resolutions WHERE flow = ? AND occurrence = ?",
            (flow, anchors["occurrence"]),
        )


__all__ = [
    "EntryFacts",
    "PlannedState",
    "WriteChecks",
    "apply_planned",
    "check_planned_sequence",
    "write_checks",
]
