"""Evidence-backed liquid position and historical cash bridge.

Printed balances, reconciled statement closes, and journalled observations are
balance boundaries. Silence after a boundary is missing evidence, never zero.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..money import ExactMoney, exact_sum, format_decimal
from .models import STALE_AFTER_DAYS

_LIQUID_SOURCE_TYPES = frozenset({
    "REVOLUT_PDF", "DBS_CONSOLIDATED", "DBS_PAYLAH",
    "MARI_SAVINGS", "TRUST_SAVINGS",
})


def _money(coefficient: int, scale: int) -> ExactMoney:
    return ExactMoney(int(coefficient), int(scale), "")


def _sum(values: list[ExactMoney]) -> tuple[int, int]:
    return exact_sum(*values) if values else (0, 0)


def _selected_entries(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT e.* FROM ledger_entries e
        JOIN extraction_runs x ON x.id=e.extraction_run_id
        LEFT JOIN statements s ON s.id=e.statement_id
        WHERE x.status='ACCEPTED' AND x.selection_state='SELECTED'
          AND (e.source_family='CSV' OR s.selection_state='SELECTED')
          AND e.lifecycle_state='COMPLETED' AND e.analytics_eligible=1
        ORDER BY e.effective_at, e.natural_anchor, e.id
        """
    ).fetchall()


def _statement_boundaries(
    connection: sqlite3.Connection, entries: list[sqlite3.Row]
) -> list[dict[str, Any]]:
    """Return statement opens/closes only when a blocking equation passed."""
    by_statement: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for entry in entries:
        if entry["statement_id"]:
            by_statement[(entry["statement_id"], entry["currency_pocket"])].append(entry)
    rows = connection.execute(
        """
        SELECT s.id statement_id, s.account_id, s.period_start, s.period_end,
               c.currency_pocket, c.actual_coefficient, c.actual_scale, c.check_type
        FROM statements s JOIN extraction_runs x ON x.id=s.extraction_run_id
        JOIN document_reconciliation_checks c ON c.statement_id=s.id
        WHERE x.status='ACCEPTED' AND x.selection_state='SELECTED'
          AND s.selection_state='SELECTED' AND c.passed=1 AND c.blocking=1
          AND c.actual_coefficient IS NOT NULL
          AND c.check_type IN ('STATEMENT_BALANCE','SECTION_CLOSING_BALANCE')
          AND s.period_start IS NOT NULL AND s.period_end IS NOT NULL
        ORDER BY s.period_end,
          CASE c.check_type WHEN 'STATEMENT_BALANCE' THEN 0 ELSE 1 END
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (row["statement_id"], row["currency_pocket"])
        if key in seen:
            continue
        seen.add(key)
        movements = by_statement.get(key, [])
        if any(item["effective_precision"] != "DAY" for item in movements):
            continue
        close = _money(row["actual_coefficient"], row["actual_scale"])
        delta_coefficient, delta_scale = _sum([
            _money(item["amount_coefficient"], item["amount_scale"])
            for item in movements
        ])
        scale = max(close.scale, delta_scale)
        opening = close.rescaled_coefficient(scale) - (
            delta_coefficient * 10 ** (scale - delta_scale)
        )
        opening_day = (
            date.fromisoformat(row["period_start"]) - timedelta(days=1)
        ).isoformat()
        common = {"account_id": row["account_id"], "currency_pocket": row["currency_pocket"]}
        result.extend((
            {**common, "as_of": opening_day, "sort_at": opening_day + "T23:59:59",
             "coefficient": opening, "scale": scale,
             "balance": format_decimal(opening, scale),
             "basis": "statement-derived-opening", "statement_id": row["statement_id"]},
            {**common, "as_of": row["period_end"], "sort_at": row["period_end"] + "T23:59:59",
             "coefficient": close.coefficient, "scale": close.scale,
             "balance": format_decimal(close.coefficient, close.scale),
             "basis": "statement-closing-balance", "statement_id": row["statement_id"]},
        ))
    return result


def _boundaries(
    connection: sqlite3.Connection, entries: list[sqlite3.Row]
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in entries:
        if row["balance_coefficient"] is None or row["effective_precision"] != "DAY":
            continue
        item = {
            "account_id": row["account_id"], "currency_pocket": row["currency_pocket"],
            "as_of": row["effective_at"][:10], "sort_at": row["effective_at"],
            "coefficient": row["balance_coefficient"], "scale": row["balance_scale"],
            "balance": format_decimal(row["balance_coefficient"], row["balance_scale"]),
            "basis": "statement-running-balance", "entry_id": row["id"],
            "date_precision": row["effective_precision"],
        }
        result[(row["account_id"], row["currency_pocket"])].append(item)
    for item in _statement_boundaries(connection, entries):
        result[(item["account_id"], item["currency_pocket"])].append(item)
    for row in connection.execute(
        """SELECT * FROM balance_observations WHERE retired=0
           ORDER BY observed_on, journal_seq, observation"""
    ):
        item = {
            "account_id": row["account_id"], "currency_pocket": row["currency_pocket"],
            "as_of": row["observed_on"], "sort_at": row["observed_on"] + "T23:59:59",
            "coefficient": row["balance_coefficient"], "scale": row["balance_scale"],
            "balance": format_decimal(row["balance_coefficient"], row["balance_scale"]),
            "basis": "manual-observation", "observation": row["observation"],
        }
        result[(row["account_id"], row["currency_pocket"])].append(item)
    for values in result.values():
        priority = {"statement-derived-opening": 0, "statement-running-balance": 1,
                    "statement-closing-balance": 2, "manual-observation": 3}
        values.sort(key=lambda item: (item["sort_at"], priority[item["basis"]]))
    return result


def _paired_liquid_entries(
    connection: sqlite3.Connection, liquid_scopes: set[tuple[str, str]]
) -> tuple[set[str], set[str]]:
    entries: set[str] = set()
    links: set[str] = set()
    for row in connection.execute(
        """SELECT t.id, t.left_entry_id, t.right_entry_id,
                  l.account_id la, l.currency_pocket lc,
                  r.account_id ra, r.currency_pocket rc
           FROM transfer_links t JOIN ledger_entries l ON l.id=t.left_entry_id
           JOIN ledger_entries r ON r.id=t.right_entry_id
           WHERE t.status='CONFIRMED'"""
    ):
        if (row["la"], row["lc"]) in liquid_scopes and (row["ra"], row["rc"]) in liquid_scopes:
            entries.update((row["left_entry_id"], row["right_entry_id"]))
            links.add(row["id"])
    return entries, links


def _certified_periods(
    connection: sqlite3.Connection, key: tuple[str, str], *, through: str,
) -> list[tuple[str, str]]:
    """Periods that prove a pocket's movement coverage.

    A transaction CSV can reconcile its running balances but its first and last
    rows do not prove that no rows were omitted.  It therefore cannot certify a
    silent day for a cash bridge.  A selected, accepted statement with passing
    blocking checks can: its stated period is the coverage evidence.

    A non-retired ``movement_coverage_certifications`` row (schema 24) is the
    second accepted proof: an operator who inspected the account's own
    transaction list for a range is attesting that the ledger's silence there is
    real.  It proves completeness only — it carries no amount — so it can bridge
    a quiet interval without ever supplying a balance.  Both kinds are returned
    in one sorted list because ``_interval_covered`` asks only "was this day
    proven", and the two answers are interchangeable for that question.
    """
    rows = connection.execute(
        """
        SELECT s.period_start AS start, s.period_end AS end
        FROM statements s
        JOIN extraction_runs x ON x.id=s.extraction_run_id
        WHERE s.account_id=? AND x.status='ACCEPTED'
          AND x.selection_state='SELECTED' AND s.selection_state='SELECTED'
          AND s.period_start IS NOT NULL AND s.period_end IS NOT NULL
          AND s.period_start <= ?
          AND EXISTS (
              SELECT 1 FROM document_reconciliation_checks ok
              WHERE ok.statement_id=s.id AND ok.currency_pocket=?
                AND ok.blocking=1 AND ok.passed=1
                AND ok.check_type IN ('STATEMENT_BALANCE', 'SECTION_CLOSING_BALANCE')
          )
          AND NOT EXISTS (
              SELECT 1 FROM document_reconciliation_checks failed
              WHERE failed.statement_id=s.id AND failed.currency_pocket=?
                AND failed.blocking=1 AND failed.passed<>1
          )
        ORDER BY s.period_start, s.period_end
        """,
        (key[0], through, key[1], key[1]),
    ).fetchall()
    periods = [(str(row["start"][:10]), str(row["end"][:10])) for row in rows]
    periods.extend(_certification_periods(connection, key, through=through))
    periods.sort()
    return periods


def _certification_periods(
    connection: sqlite3.Connection, key: tuple[str, str], *, through: str,
) -> list[tuple[str, str]]:
    """Non-retired operator attestations of complete movement evidence."""
    rows = connection.execute(
        """
        SELECT period_start AS start, period_end AS end
        FROM movement_coverage_certifications
        WHERE account_id=? AND currency_pocket=? AND retired=0 AND period_start<=?
        ORDER BY period_start, period_end
        """,
        (key[0], key[1], through),
    ).fetchall()
    return [(str(row["start"][:10]), str(row["end"][:10])) for row in rows]


def _interval_covered(start: str, end: str, periods: list[tuple[str, str]]) -> bool:
    """Whether adjacent certified statement periods cover every calendar day."""
    if start > end:
        return True
    cursor = date.fromisoformat(start)
    final = date.fromisoformat(end)
    for period_start, period_end in periods:
        lo, hi = date.fromisoformat(period_start), date.fromisoformat(period_end)
        if hi < cursor:
            continue
        if lo > cursor:
            return False
        cursor = max(cursor, hi + timedelta(days=1))
        if cursor > final:
            return True
    return cursor > final


def _derived_at(
    key: tuple[str, str], target: str, boundaries: list[dict[str, Any]],
    entries: list[sqlite3.Row], periods: list[tuple[str, str]],
) -> dict[str, Any]:
    """Derive one pocket through ``target`` from actual, complete movements."""
    eligible = [boundary for boundary in boundaries if boundary["as_of"] <= target]
    if not eligible:
        return {"supported": False, "reason": "no trusted balance boundary on or before this date"}
    boundary = eligible[-1]
    if boundary["as_of"] == target:
        # A source balance is itself evidence at its recorded as-of date.  Do
        # not demand a second source merely because a running balance carries
        # an intraday timestamp.
        return {
            "supported": True,
            "balance": boundary["balance"], "coefficient": boundary["coefficient"], "scale": boundary["scale"],
            "as_of": target, "basis": boundary["basis"],
            "opening": boundary["balance"], "opening_as_of": boundary["as_of"],
            "movement_count": 0, "coverage_periods": [],
        }
    # A balance captured at end-of-day needs coverage from the following day.
    # A timestamped running balance can have later same-day movements, so that
    # calendar day must itself be certified.
    start = (
        (date.fromisoformat(boundary["as_of"]) + timedelta(days=1)).isoformat()
        if boundary["sort_at"].endswith("T23:59:59")
        else boundary["as_of"]
    )
    if not _interval_covered(start, target, periods):
        return {
            "supported": False,
            "reason": "complete actual-movement coverage is not established through this date",
            "gap_start": start,
        }
    rows = [
        row for row in entries
        if (row["account_id"], row["currency_pocket"]) == key
        and row["effective_at"] > boundary["sort_at"]
        and row["effective_at"][:10] <= target
    ]
    if any(row["effective_precision"] != "DAY" for row in rows):
        return {"supported": False, "reason": "movement date precision is insufficient for an exact daily balance"}
    delta, delta_scale = _sum([
        _money(row["amount_coefficient"], row["amount_scale"]) for row in rows
    ])
    scale = max(boundary["scale"], delta_scale)
    coefficient = boundary["coefficient"] * 10 ** (scale - boundary["scale"])
    coefficient += delta * 10 ** (scale - delta_scale)
    return {
        "supported": True,
        "balance": format_decimal(coefficient, scale), "coefficient": coefficient, "scale": scale,
        "as_of": target, "basis": "actual-movement-bridge",
        "opening": boundary["balance"], "opening_as_of": boundary["as_of"],
        "movement_count": len(rows), "coverage_periods": [
            {"start": start, "end": end} for start, end in periods if end >= start
        ],
    }


def _bridge(
    key: tuple[str, str], boundaries: list[dict[str, Any]],
    entries: list[sqlite3.Row], paired: set[str], connection: sqlite3.Connection,
) -> dict[str, Any] | None:
    if len(boundaries) < 2:
        return None
    opening, closing = boundaries[0], boundaries[-1]
    scope_rows = [row for row in entries if (row["account_id"], row["currency_pocket"]) == key]
    rows = [row for row in scope_rows
            if row["effective_at"] > opening["sort_at"] and row["effective_at"] <= closing["sort_at"]]
    ambiguous = opening["basis"] == "manual-observation" and any(
        row["effective_at"][:10] == opening["as_of"] for row in scope_rows
    )
    same_certified_statement = (
        opening.get("statement_id") is not None
        and opening.get("statement_id") == closing.get("statement_id")
        and opening["basis"] == "statement-derived-opening"
        and closing["basis"] == "statement-closing-balance"
    )
    # A single statement can certify its own span, but a boundary pair further
    # apart than one statement needs a chain of adjacent, passing statements
    # covering every day in between -- the same coverage proof the as-of read
    # uses in `_derived_at`. Nothing populates a row-level CSV check anymore
    # (that ingestion path is retired), so this chain is the only remaining
    # way to certify movement completeness across multiple statements.
    coverage_start = (
        (date.fromisoformat(opening["as_of"]) + timedelta(days=1)).isoformat()
        if opening["sort_at"].endswith("T23:59:59")
        else opening["as_of"]
    )
    certified_periods = _certified_periods(connection, key, through=closing["as_of"])
    evidence_complete = same_certified_statement or _interval_covered(
        coverage_start, closing["as_of"], certified_periods
    )
    buckets: dict[str, list[ExactMoney]] = defaultdict(list)
    net: list[ExactMoney] = []
    for row in rows:
        amount = _money(row["amount_coefficient"], row["amount_scale"])
        fee = _money(abs(row["fee_coefficient"] or 0), row["fee_scale"] or 0)
        net.append(amount)
        if fee.coefficient:
            net.append(_money(-fee.coefficient, fee.scale))
            buckets["external_outflows"].append(fee)
        if row["id"] in paired:
            name = "owned_transfer_inflows" if amount.coefficient >= 0 else "owned_transfer_outflows"
        else:
            name = "external_inflows" if amount.coefficient >= 0 else "external_outflows"
        buckets[name].append(_money(abs(amount.coefficient), amount.scale))
    delta, delta_scale = _sum(net)
    scale = max(opening["scale"], closing["scale"], delta_scale)
    calculated = opening["coefficient"] * 10 ** (scale - opening["scale"])
    calculated += delta * 10 ** (scale - delta_scale)
    actual = closing["coefficient"] * 10 ** (scale - closing["scale"])
    reconciled = calculated == actual and not ambiguous and evidence_complete

    def total(name: str) -> str:
        return format_decimal(*_sum(buckets[name]))

    ids = {row["id"] for row in rows}
    paired_links = 0
    if ids:
        marks = ",".join("?" for _ in ids)
        paired_links = connection.execute(
            f"SELECT count(*) FROM transfer_links WHERE status='CONFIRMED' "
            f"AND left_entry_id IN ({marks}) AND right_entry_id IN ({marks})",
            tuple(ids) + tuple(ids),
        ).fetchone()[0]
    reason = None
    if ambiguous:
        reason = "same-day movement order is ambiguous at a manual observation boundary"
    elif not evidence_complete:
        reason = "intervening movement completeness is not supported"
    elif not reconciled:
        reason = "intervening movements do not reconcile the balance boundaries"
    return {
        "account_id": key[0], "currency_pocket": key[1],
        "opening": opening["balance"], "opening_as_of": opening["as_of"],
        "external_inflows": total("external_inflows"),
        "external_outflows": total("external_outflows"),
        "owned_transfer_inflows": total("owned_transfer_inflows"),
        "owned_transfer_outflows": total("owned_transfer_outflows"),
        "closing": closing["balance"], "closing_as_of": closing["as_of"],
        "calculated_closing": format_decimal(calculated, scale),
        "reconciled": reconciled, "movement_count": len(rows),
        "paired_transfer_link_count": paired_links, "unsupported_reason": reason,
    }


def _partial_position(
    included: list[dict[str, Any]],
    attempts: dict[str, dict[tuple[str, str], dict[str, Any]]],
    *,
    strict_cutoff: str | None,
) -> dict[str, Any]:
    """The most current cash figure the evidence supports, however partial.

    ``combined_liquid_position`` answers "what is the complete total", and to
    stay complete it falls back to the latest date *every* pocket can prove —
    which can be months old, or nothing at all. Withholding on that basis leaves
    the operator with no current figure, while silently reducing the total would
    be a lie. This is the third option, and it is deliberately the complementary
    read: the **latest** date at which any pocket is proven, totalled over
    exactly the pockets proven there, with the missing ones named beside it.

    So the two reads answer different questions — complete but older, versus
    current but partial — and coincide exactly when every pocket is proven
    today. This is never presented as the position; ``is_complete`` is true only
    when the strict cutoff exists and nothing is short.
    """
    if not included:
        return {
            "as_of": None, "by_currency": {}, "contributing_pockets": [],
            "short_pockets": [], "is_complete": False,
            "reason": "no supported liquid account pockets were found",
        }
    keys = [(pocket["account_id"], pocket["currency_pocket"]) for pocket in included]
    best_date: str | None = None
    best_count = 0
    for candidate in sorted(attempts, reverse=True):
        count = sum(1 for key in keys if attempts[candidate][key]["supported"])
        if count:
            best_date, best_count = candidate, count
            break
    if best_date is None or best_count == 0:
        return {
            "as_of": None, "by_currency": {}, "contributing_pockets": [],
            "short_pockets": [
                {"account_id": key[0], "currency_pocket": key[1]} for key in keys
            ],
            "is_complete": False,
            "reason": "no liquid pocket has a coverage-backed balance on any candidate date",
        }
    attempt = attempts[best_date]
    totals: dict[str, list[ExactMoney]] = defaultdict(list)
    contributing: list[dict[str, Any]] = []
    short: list[dict[str, Any]] = []
    for pocket, key in zip(included, keys, strict=True):
        result = attempt[key]
        if result["supported"]:
            totals[key[1]].append(_money(result["coefficient"], result["scale"]))
            contributing.append({
                "account_id": key[0], "currency_pocket": key[1],
                "display_name": pocket["display_name"], "balance": result["balance"],
                "basis": result["basis"],
            })
        else:
            short.append({
                "account_id": key[0], "currency_pocket": key[1],
                "display_name": pocket["display_name"],
                "reason": str(result["reason"]),
                "best_supported_cutoff": pocket.get("best_supported_cutoff"),
            })
    return {
        "as_of": best_date,
        "by_currency": {
            currency: format_decimal(*_sum(values))
            for currency, values in sorted(totals.items())
        },
        "contributing_pockets": contributing,
        "short_pockets": short,
        "is_complete": bool(strict_cutoff) and not short,
        "reason": None if not short else (
            f"{len(short)} of {len(keys)} liquid pockets have no coverage-backed "
            f"balance at {best_date}"
        ),
    }


def _coverage_summary(
    included: list[dict[str, Any]],
    partial: dict[str, Any],
    *,
    strict_cutoff: str | None,
) -> dict[str, Any]:
    """One compact answer to "how much of my cash can this figure see?".

    ``covered_pockets / included_pockets`` is a count, not a weight: a ratio by
    balance would need the very balances the short pockets cannot supply.
    """
    total = len(included)
    covered = len(partial["contributing_pockets"])
    return {
        "included_pockets": total,
        "covered_pockets": covered,
        "short_pockets": total - covered,
        "ratio": (covered / total) if total else 0.0,
        "basis": "share of included liquid pockets with a coverage-backed balance",
        "status": (
            "complete" if strict_cutoff and covered == total
            else "partial" if covered
            else "unavailable"
        ),
    }


def position_page(database: Path, *, today: date) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return position_page_from(connection, today=today)
    finally:
        connection.close()


def position_page_from(connection: sqlite3.Connection, *, today: date) -> dict[str, Any]:
    accounts = {row["id"]: row for row in connection.execute(
        "SELECT id,institution,source_type,display_name,masked_identifier FROM accounts"
    )}
    card_ids = {row[0] for row in connection.execute(
        "SELECT DISTINCT account_id FROM card_facility_accounts WHERE bound=1"
    )}
    entries = _selected_entries(connection)
    boundary_map = _boundaries(connection, entries)
    scopes = set(boundary_map) | {
        (row["account_id"], row["currency_pocket"]) for row in entries
    }
    liquid_scopes: set[tuple[str, str]] = set()
    exclusions: dict[tuple[str, str], str] = {}
    for key in scopes:
        account = accounts.get(key[0])
        source_type = str(account["source_type"]).upper() if account else ""
        if key[0] in card_ids or source_type.endswith("_CARD"):
            exclusions[key] = "credit-card facility balance is an obligation, not cash"
        elif source_type not in _LIQUID_SOURCE_TYPES:
            exclusions[key] = "account product is not supported as liquid cash"
        else:
            liquid_scopes.add(key)
    paired, transfer_links = _paired_liquid_entries(connection, liquid_scopes)
    pockets: list[dict[str, Any]] = []
    bridges: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for key in sorted(scopes):
        account = accounts.get(key[0])
        boundaries = [
            boundary for boundary in boundary_map.get(key, [])
            if boundary["as_of"] <= today.isoformat()
        ]
        chosen = boundaries[-1] if boundaries else None
        liquid = key in liquid_scopes
        unavailable = None if chosen else "no supported balance boundary for this pocket"
        age = (today - date.fromisoformat(chosen["as_of"])).days if chosen else None
        bridge = _bridge(key, boundaries, entries, paired, connection) if liquid else None
        if bridge:
            bridges.append(bridge)
            if not bridge["reconciled"]:
                missing.append({"account_id": key[0], "currency_pocket": key[1],
                                "start": bridge["opening_as_of"], "end": bridge["closing_as_of"],
                                "reason": bridge["unsupported_reason"]})
        pockets.append({
            "account_id": key[0], "institution": account["institution"] if account else None,
            "display_name": account["display_name"] if account else None,
            "source_type": account["source_type"] if account else None,
            "currency_pocket": key[1], "liquid": liquid,
            "excluded_reason": exclusions.get(key),
            "basis": chosen["basis"] if chosen else "unavailable",
            "balance": chosen["balance"] if chosen else None,
            "as_of": chosen["as_of"] if chosen else None,
            "date_precision": chosen.get("date_precision") if chosen else None,
            "stale": bool(age is not None and age > STALE_AFTER_DAYS),
            "staleness_days": age, "unavailable_reason": unavailable,
            "statement_running_balance": next((b for b in reversed(boundaries)
                if b["basis"] == "statement-running-balance"), None),
            "manual_observation": next((b for b in reversed(boundaries)
                if b["basis"] == "manual-observation"), None),
            # Filled in below for liquid pockets only. An excluded pocket (a
            # card's obligation balance) is never derived into a cash cutoff,
            # but readers still expect every pocket to carry these keys.
            "derived_balance": None, "derived_as_of": None, "derived_basis": None,
            "coverage_status": "unsupported", "best_supported_cutoff": None,
            "best_supported_balance": None, "best_supported_basis": None,
            "blocking_reason": None,
        })
    included = [p for p in pockets if p["liquid"]]
    periods_by_key = {
        (pocket["account_id"], pocket["currency_pocket"]): _certified_periods(
            connection, (pocket["account_id"], pocket["currency_pocket"]), through=today.isoformat(),
        )
        for pocket in included
    }
    candidate_dates = {today.isoformat()}
    for pocket in included:
        key = (pocket["account_id"], pocket["currency_pocket"])
        candidate_dates.update(boundary["as_of"] for boundary in boundary_map.get(key, []) if boundary["as_of"] <= today.isoformat())
        candidate_dates.update(end for _start, end in periods_by_key[key] if end <= today.isoformat())
    cutoff: str | None = None
    derived: dict[tuple[str, str], dict[str, Any]] = {}
    # Every candidate is evaluated, not just those up to the first full hit:
    # the strict cutoff still stops at the latest date all pockets support, but
    # the partial read below needs the whole table to find the best-supported
    # date and each pocket's own furthest supported date. Candidate dates are
    # bounded by the pockets' own boundaries and statement ends, so this stays
    # the same order of work the strict search already did.
    attempts: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for candidate in sorted(candidate_dates, reverse=True):
        attempt = {
            (pocket["account_id"], pocket["currency_pocket"]): _derived_at(
                (pocket["account_id"], pocket["currency_pocket"]), candidate,
                boundary_map.get((pocket["account_id"], pocket["currency_pocket"]), []),
                entries, periods_by_key[(pocket["account_id"], pocket["currency_pocket"])],
            )
            for pocket in included
        }
        attempts[candidate] = attempt
        if cutoff is None and included and all(result["supported"] for result in attempt.values()):
            cutoff, derived = candidate, attempt
    today_attempt = {
        (pocket["account_id"], pocket["currency_pocket"]): _derived_at(
            (pocket["account_id"], pocket["currency_pocket"]), today.isoformat(),
            boundary_map.get((pocket["account_id"], pocket["currency_pocket"]), []),
            entries, periods_by_key[(pocket["account_id"], pocket["currency_pocket"])],
        )
        for pocket in included
    }
    for pocket in included:
        key = (pocket["account_id"], pocket["currency_pocket"])
        result = today_attempt[key]
        if not result["supported"]:
            missing.append({
                "account_id": key[0], "currency_pocket": key[1],
                "start": str(result.get("gap_start") or "unknown"), "end": today.isoformat(),
                "reason": str(result["reason"]),
            })
        if cutoff and key in derived:
            pocket["derived_balance"] = derived[key]["balance"]
            pocket["derived_as_of"] = cutoff
            pocket["derived_basis"] = derived[key]["basis"]
            pocket["coverage_status"] = "supported"
        else:
            pocket["derived_balance"] = None
            pocket["derived_as_of"] = None
            pocket["derived_basis"] = None
            pocket["coverage_status"] = "unsupported"
        # This pocket's own furthest supported date, independent of what the
        # others can prove. A pocket that is fine through July is described as
        # such instead of disappearing because a sibling pocket is short.
        own = next(
            (
                date_
                for date_ in sorted(attempts, reverse=True)
                if attempts[date_][key]["supported"]
            ),
            None,
        )
        pocket["best_supported_cutoff"] = own
        pocket["best_supported_balance"] = attempts[own][key]["balance"] if own else None
        pocket["best_supported_basis"] = attempts[own][key]["basis"] if own else None
        pocket["blocking_reason"] = (
            None if own == today.isoformat() else str(today_attempt[key].get("reason") or "")
        ) or None
    totals: dict[str, list[ExactMoney]] = defaultdict(list)
    if cutoff:
        for pocket in included:
            result = derived[(pocket["account_id"], pocket["currency_pocket"])]
            totals[pocket["currency_pocket"]].append(_money(result["coefficient"], result["scale"]))
    combined = {currency: format_decimal(*_sum(values))
                for currency, values in sorted(totals.items())}
    partial = _partial_position(included, attempts, strict_cutoff=cutoff)
    reasons: list[str] = []
    if not included:
        reasons.append("no supported liquid account pockets were found")
    elif not cutoff:
        reasons.append("included liquid pockets do not share a coverage-backed balance cutoff")
    if cutoff != today.isoformat():
        reasons.append("complete actual movements from the supported cutoff through today are unavailable")
    combined_bridge: dict[str, dict[str, Any]] = {}
    for currency in sorted({bridge["currency_pocket"] for bridge in bridges}):
        members = [bridge for bridge in bridges if bridge["currency_pocket"] == currency]
        def bridge_total(field: str, selected: list[dict[str, Any]] = members) -> str:
            return format_decimal(*_sum([
                ExactMoney.parse(member[field]) for member in selected
            ]))
        combined_bridge[currency] = {
            "opening": bridge_total("opening"),
            "external_inflows": bridge_total("external_inflows"),
            "external_outflows": bridge_total("external_outflows"),
            "closing": bridge_total("closing"),
            "reconciled": all(member["reconciled"] for member in members),
            "owned_transfer_pairs_cancelled": len(transfer_links),
        }
    return {
        "generated_for": today.isoformat(), "stale_after_days": STALE_AFTER_DAYS,
        "supported_cutoff": cutoff,
        "current_position_supported": cutoff == today.isoformat() and not missing,
        "current_position_reasons": list(dict.fromkeys(reasons)),
        "included_pockets": included, "pockets": pockets,
        "combined_liquid_position": combined, "liquid_total": combined,
        "partial_position": partial,
        "coverage_summary": _coverage_summary(included, partial, strict_cutoff=cutoff),
        "liquid_total_withheld": {} if cutoff else dict.fromkeys(sorted({p["currency_pocket"] for p in included}), "withheld: no common supported cutoff"),
        "stale": any(p["stale"] for p in included), "missing_intervals": missing,
        "cash_bridge": {"pockets": bridges, "combined": {
            "by_currency": combined_bridge,
            "owned_transfer_pairs_cancelled": len(transfer_links),
            "policy": "each confirmed pair is removed once only when both legs are included liquid pockets",
        }},
    }
