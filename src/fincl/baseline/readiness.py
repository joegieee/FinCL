"""Stage 1A — is the imported evidence good enough to derive a baseline?

Pure read. For every ``(account_id, currency_pocket)`` this walks the calendar
months of the pocket's expected/observed span and classifies each:

``established``
    The whole month lies inside a ``SELECTED`` accepted statement period for
    the account. Only these months can back a *true* zero — an
    ``established`` month with no entries is genuinely a no-spend month.
``provisional``
    The month has imported activity, but the export boundary was never
    independently proven (a CSV's first/last transaction dates are an observed
    span, not the export period). Usable as an explicitly observed-only view,
    never merged silently into an ``established`` total.
``missing``
    Inside the pocket's span but neither covered by a statement period nor
    backed by any entry — a real gap the operator must import, not a zero.

From the grid it recommends the latest run of consecutive comparable months
(prefer six, three for a *normal* baseline; one or two is a *limited-history*
estimate; zero is *none*) and lists the missing-evidence tasks that stand
between the operator and a baseline. There is no "enter the baseline instead"
escape here — every task resolves by importing data or reviewing evidence.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..coverage import account_statement_coverage
from ..db import connect_readonly, database_path
from ..decision_journal import default_journal_path, read_records
from ..intervals import DateInterval, month_span
from ..money import ExactMoney, exact_sum, format_decimal
from ..read_cache import per_request
from ..v05 import generation_of_from

_COMPLETENESS_RANK = {"missing": 0, "provisional": 1, "established": 2}
_PREFERRED_MONTHS = 6
_NORMAL_MIN_MONTHS = 3


def _month_of(day: str) -> str:
    return day[:7]


def _add_month(year_month: str) -> str:
    year, month = (int(part) for part in year_month.split("-"))
    month += 1
    if month == 13:
        year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


def _month_range(start_month: str, end_month: str) -> list[str]:
    if start_month > end_month:
        return []
    months = [start_month]
    while months[-1] < end_month:
        months.append(_add_month(months[-1]))
    return months


def _merge_missing_runs(months: list[str]) -> list[DateInterval]:
    """Collapse a sorted list of missing ``YYYY-MM`` into calendar intervals."""
    runs: list[DateInterval] = []
    for month in months:
        span = month_span(month)
        if runs and _month_of(runs[-1].end) == _prev_month(month):
            runs[-1] = DateInterval(runs[-1].start, span.end)
        else:
            runs.append(span)
    return runs


def _prev_month(year_month: str) -> str:
    year, month = (int(part) for part in year_month.split("-"))
    month -= 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"



def _account_scopes_from(
    connection: sqlite3.Connection,
) -> tuple[dict[tuple[str, str], dict[str, str]], dict[str, str]]:
    """Latest ``ACCOUNT_SCOPE_SET`` expected window per ``(account, pocket)``,
    and the latest non-empty ``ACCOUNT_DETAILS_SET`` archive date per account."""
    scopes: dict[tuple[str, str], dict[str, str]] = {}
    archived: dict[str, str] = {}
    for record in read_records(default_journal_path(database_path(connection))):
        if record.decision_kind == "ACCOUNT_DETAILS_SET":
            archived[record.anchors["account"]] = str(record.payload.get("archived_on") or "")
            continue
        if record.decision_kind != "ACCOUNT_SCOPE_SET":
            continue
        key = (record.anchors["account"], record.anchors["pocket"])
        scopes[key] = {
            "expected_start": str(record.payload["expected_start"]),
            "expected_end": str(record.payload["expected_end"]),
        }
    return scopes, {account: day for account, day in archived.items() if day}


def _pocket_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT a.id AS account_id, a.institution AS institution,
               a.source_type AS source_type, a.display_name AS display_name,
               e.currency_pocket AS currency_pocket,
               min(substr(e.effective_at, 1, 10)) AS observed_start,
               max(substr(e.effective_at, 1, 10)) AS observed_end,
               count(e.id) AS entry_count
        FROM accounts a
        JOIN ledger_entries e ON e.account_id = a.id
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE x.selection_state = 'SELECTED' AND x.status = 'ACCEPTED'
        GROUP BY a.id, e.currency_pocket
        ORDER BY a.institution, a.display_name, a.id, e.currency_pocket
        """
    ).fetchall()


def _entries_per_month(
    connection: sqlite3.Connection, account_id: str, currency_pocket: str
) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT substr(e.effective_at, 1, 7) AS month, count(e.id) AS n
        FROM ledger_entries e
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE e.account_id = ? AND e.currency_pocket = ?
          AND x.selection_state = 'SELECTED' AND x.status = 'ACCEPTED'
        GROUP BY month
        """,
        (account_id, currency_pocket),
    ).fetchall()
    return {row["month"]: row["n"] for row in rows}


def _reconciliation(
    connection: sqlite3.Connection, account_id: str
) -> dict[str, int]:
    csv_row = connection.execute(
        """
        SELECT count(*) AS total, coalesce(sum(rc.passed = 1), 0) AS passed
        FROM reconciliation_checks rc
        JOIN raw_records r ON r.id = rc.prior_raw_record_id
        JOIN ledger_entries e ON e.raw_record_id = r.id
        WHERE e.account_id = ?
        """,
        (account_id,),
    ).fetchone()
    doc_row = connection.execute(
        """
        SELECT count(*) AS total, coalesce(sum(d.passed = 1), 0) AS passed
        FROM document_reconciliation_checks d
        JOIN extraction_runs x ON x.id = d.extraction_run_id
        JOIN ledger_entries e ON e.extraction_run_id = x.id
        WHERE e.account_id = ? AND d.blocking = 1
        """,
        (account_id,),
    ).fetchone()
    total = (csv_row["total"] or 0) + (doc_row["total"] or 0)
    passed = (csv_row["passed"] or 0) + (doc_row["passed"] or 0)
    return {"passed": passed, "total": total}


def _unclassified_load(
    connection: sqlite3.Connection, window: DateInterval | None
) -> list[dict[str, Any]]:
    """Unresolved-categorization pressure inside the window, ranked by money."""
    if window is None:
        return []
    rows = connection.execute(
        """
        SELECT e.currency_pocket AS currency_pocket,
               e.amount_coefficient AS coefficient, e.amount_scale AS scale
        FROM entry_categories ec
        JOIN ledger_entries e ON e.id = ec.entry_id
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE x.selection_state = 'SELECTED' AND x.status = 'ACCEPTED'
          AND e.analytics_eligible = 1 AND e.lifecycle_state = 'COMPLETED'
          AND ec.category = 'Unclassified'
          AND ec.basis NOT LIKE 'override'
          AND ec.basis NOT LIKE 'rule:%' AND ec.basis NOT LIKE 'alias:%'
          AND ec.basis NOT LIKE 'link:%' AND ec.basis NOT LIKE 'inbound:%'
          AND substr(e.effective_at, 1, 10) BETWEEN ? AND ?
        """,
        (window.start, window.end),
    ).fetchall()
    by_currency: dict[str, list[ExactMoney]] = {}
    for row in rows:
        by_currency.setdefault(row["currency_pocket"], []).append(
            ExactMoney(coefficient=abs(row["coefficient"]), scale=row["scale"], text="")
        )
    tasks: list[dict[str, Any]] = []
    for currency, amounts in by_currency.items():
        coefficient, scale = exact_sum(*amounts)
        tasks.append(
            {
                "kind": "resolve-categorization",
                "currency_pocket": currency,
                "entry_count": len(amounts),
                "absolute_amount": format_decimal(coefficient, scale),
                "note": (
                    f"{len(amounts)} unclassified entries in the baseline window "
                    f"(|{format_decimal(coefficient, scale)}| {currency}); "
                    "unknown role is not discretionary and not zero"
                ),
            }
        )
    tasks.sort(key=lambda task: ExactMoney.parse(task["absolute_amount"]).coefficient, reverse=True)
    return tasks


def _classify_month(
    month: str,
    periods: list[DateInterval],
    entry_count: int,
) -> tuple[str, str]:
    span = month_span(month)
    if any(period.contains_interval(span) for period in periods):
        return "established", "statement-period"
    if entry_count > 0:
        return "provisional", "observed-entry-span"
    return "missing", "no-evidence"


def baseline_readiness(database: Path, *, today: date) -> dict[str, Any]:
    connection = connect_readonly(database)
    try:
        return baseline_readiness_from(connection, today=today)
    finally:
        connection.close()


@per_request
def baseline_readiness_from(connection: sqlite3.Connection, *, today: date) -> dict[str, Any]:
    """The evidence-coverage-backed baseline window.

    ``today`` is required (§5.3): this reader sits on the money-flow call
    graph and must never consult the wall clock itself — the caller resolves
    one request date and threads it down.
    """
    scopes, archived = _account_scopes_from(connection)
    periods_by_account = account_statement_coverage(connection)
    pocket_rows = _pocket_rows(connection)
    # An account with no stated end is expected to stay active as long as any
    # other account has evidence. Without this, an account whose statements
    # stop simply leaves the grid, and later months look complete without it.
    evidence_end = max(
        (spans[-1].end for spans in periods_by_account.values() if spans), default=None
    )
    pockets: list[dict[str, Any]] = []
    span_starts: list[str] = []
    span_ends: list[str] = []
    currencies: set[str] = set()

    for row in pocket_rows:
        account_id = row["account_id"]
        currency_pocket = row["currency_pocket"]
        currencies.add(currency_pocket)
        scope = scopes.get((account_id, currency_pocket))
        observed_start = row["observed_start"]
        observed_end = row["observed_end"]
        periods = periods_by_account.get(account_id, [])
        # A SELECTED statement with no transactions is still a covered month
        # (a genuine no-spend month), so the grid spans the statement
        # periods as well as the observed entries and the expected window.
        span_start = min(
            filter(
                None,
                [
                    observed_start,
                    scope["expected_start"] if scope else None,
                    periods[0].start if periods else None,
                ],
            )
        )
        stated_end = (scope["expected_end"] if scope else "") or archived.get(account_id)
        span_end = max(
            filter(
                None,
                [
                    observed_end,
                    stated_end,
                    periods[-1].end if periods else None,
                    None if stated_end else evidence_end,
                ],
            )
        )
        span_starts.append(span_start)
        span_ends.append(span_end)
        last_evidence = periods[-1].end if periods else observed_end
        stopped = bool(
            not stated_end and evidence_end and last_evidence
            and _month_of(last_evidence) < _month_of(evidence_end)
        )

        per_month = _entries_per_month(connection, account_id, currency_pocket)
        months: list[dict[str, Any]] = []
        missing_months: list[str] = []
        for month in _month_range(_month_of(span_start), _month_of(span_end)):
            completeness, basis = _classify_month(
                month, periods, per_month.get(month, 0)
            )
            if completeness == "missing":
                missing_months.append(month)
            months.append(
                {
                    "month": month,
                    "completeness": completeness,
                    "basis": basis,
                    "entry_count": per_month.get(month, 0),
                }
            )

        warnings: list[str] = []
        if not periods:
            warnings.append(
                "no independently proven statement period; every month is "
                "observed-only until an export period is confirmed"
            )
        if scope and observed_start and observed_start > scope["expected_start"]:
            warnings.append(
                f"evidence starts {observed_start}, after the expected "
                f"{scope['expected_start']} (opened late or missing early statements)"
            )
        if scope and observed_end and observed_end < scope["expected_end"]:
            warnings.append(
                f"evidence ends {observed_end}, before the expected "
                f"{scope['expected_end']} (closed early or missing recent statements)"
            )

        pockets.append(
            {
                "account_id": account_id,
                "institution": row["institution"],
                "display_name": row["display_name"],
                "source_type": row["source_type"],
                "currency_pocket": currency_pocket,
                "expected_span": scope,
                "observed_span": {"start": observed_start, "end": observed_end},
                "span": {"start": span_start, "end": span_end},
                "entry_count": row["entry_count"],
                "reconciliation": _reconciliation(connection, account_id),
                "months": months,
                "missing_intervals": [
                    interval.as_dict() for interval in _merge_missing_runs(missing_months)
                ],
                "stopped": stopped,
                "warnings": warnings,
            }
        )

    analysis_window = (
        {"start": min(span_starts), "end": max(span_ends)} if span_starts else None
    )
    recommendation = _recommend_window(pockets, analysis_window)
    window_interval = (
        DateInterval(
            month_span(recommendation["start_month"]).start,
            month_span(recommendation["end_month"]).end,
        )
        if recommendation["quality"] != "none"
        else None
    )
    tasks = _tasks(pockets) + _unclassified_load(connection, window_interval)

    return {
        "generation": generation_of_from(connection),
        "generated_for": today.isoformat(),
        "scope": {
            "accounts": sorted({p["account_id"] for p in pockets}),
            "currencies": sorted(currencies),
            "analysis_window": analysis_window,
        },
        "pockets": pockets,
        "baseline_window": recommendation,
        "tasks": tasks,
    }


def _month_completeness(pockets: list[dict[str, Any]], month: str) -> str | None:
    """Worst completeness across every pocket that is in-span for ``month``.

    ``None`` when no pocket is expected to be active that month.
    """
    ranks: list[int] = []
    for pocket in pockets:
        if not (_month_of(pocket["span"]["start"]) <= month <= _month_of(pocket["span"]["end"])):
            continue
        cell = next((m for m in pocket["months"] if m["month"] == month), None)
        if cell is not None:
            ranks.append(_COMPLETENESS_RANK[cell["completeness"]])
    if not ranks:
        return None
    worst = min(ranks)
    for name, rank in _COMPLETENESS_RANK.items():
        if rank == worst:
            return name
    return None  # pragma: no cover


def _recommend_window(
    pockets: list[dict[str, Any]], analysis_window: dict[str, str] | None
) -> dict[str, Any]:
    if analysis_window is None:
        return {
            "quality": "none",
            "reason": "no imported activity in any account",
            "start_month": None,
            "end_month": None,
            "month_count": 0,
            "basis": None,
        }
    months = _month_range(
        _month_of(analysis_window["start"]), _month_of(analysis_window["end"])
    )
    labels = [(month, _month_completeness(pockets, month)) for month in months]

    def _latest_run(predicate: Any) -> list[str]:
        best: list[str] = []
        current: list[str] = []
        for month, label in labels:
            if label is not None and predicate(label):
                current.append(month)
            else:
                current = []
            if len(current) >= len(best):
                best = list(current)
        return best

    established_run = _latest_run(lambda label: label == "established")
    usable_run = _latest_run(lambda label: label in ("established", "provisional"))

    if len(established_run) >= _NORMAL_MIN_MONTHS:
        window = established_run[-_PREFERRED_MONTHS:]
        return {
            "quality": "normal",
            "reason": (
                f"{len(established_run)} consecutive fully-established months; "
                f"using the latest {len(window)}"
            ),
            "start_month": window[0],
            "end_month": window[-1],
            "month_count": len(window),
            "basis": "all-established",
        }
    if established_run:
        return {
            "quality": "limited-history",
            "reason": (
                f"only {len(established_run)} consecutive established month(s); "
                "a limited-history estimate, not a normal baseline"
            ),
            "start_month": established_run[0],
            "end_month": established_run[-1],
            "month_count": len(established_run),
            "basis": "all-established",
        }
    if usable_run:
        window = usable_run[-_PREFERRED_MONTHS:]
        return {
            "quality": "limited-history",
            "reason": (
                "no independently proven statement period; "
                f"{len(window)} observed-only month(s) can support a labelled "
                "limited-history estimate"
            ),
            "start_month": window[0],
            "end_month": window[-1],
            "month_count": len(window),
            "basis": "some-provisional",
        }
    return {
        "quality": "none",
        "reason": "no run of comparable complete months; import or reconcile first",
        "start_month": None,
        "end_month": None,
        "month_count": 0,
        "basis": None,
    }


def _tasks(pockets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for pocket in pockets:
        for interval in pocket["missing_intervals"]:
            tasks.append(
                {
                    "kind": "import-missing",
                    "account_id": pocket["account_id"],
                    "display_name": pocket["display_name"],
                    "currency_pocket": pocket["currency_pocket"],
                    "interval": interval,
                    "note": (
                        f"no statement or activity for {pocket['display_name']} "
                        f"{pocket['currency_pocket']} over {interval['start']}..{interval['end']}"
                    ),
                }
            )
        has_provisional = any(m["completeness"] == "provisional" for m in pocket["months"])
        if has_provisional:
            tasks.append(
                {
                    "kind": "confirm-export-period",
                    "account_id": pocket["account_id"],
                    "display_name": pocket["display_name"],
                    "currency_pocket": pocket["currency_pocket"],
                    "interval": pocket["observed_span"],
                    "note": (
                        f"{pocket['display_name']} {pocket['currency_pocket']} is "
                        "observed-only; add an export-period statement to make its "
                        "months established"
                    ),
                }
            )
        recon = pocket["reconciliation"]
        if recon["total"] and recon["passed"] < recon["total"]:
            tasks.append(
                {
                    "kind": "resolve-reconciliation",
                    "account_id": pocket["account_id"],
                    "display_name": pocket["display_name"],
                    "currency_pocket": pocket["currency_pocket"],
                    "note": (
                        f"{recon['total'] - recon['passed']} of {recon['total']} "
                        f"reconciliation checks fail for {pocket['display_name']}"
                    ),
                }
            )
    return tasks
