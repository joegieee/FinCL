from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from .analysis.components import build_components
from .coverage import coverage_contract as _coverage_contract
from .db import connect_readonly
from .engine import summarize_run
from .errors import FinCLError
from .intervals import (
    COVERAGE_POLICY_VERSION,
    DateInterval,
    bounding,
    contains_range,
    uncovered_dates,
)
from .money import ExactMoney, exact_sum, format_decimal
from .selection import needs_reextraction
from .valuation import (
    LATEST_FIRST_IMPORT_V1,
    ConsolidationStatus,
    EntryPrecision,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    consolidate,
    quantize,
)

_REPORTING_CURRENCY = "SGD"


@contextmanager
def _reading(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the ledger read-only for a query that always closes (plan M1a).

    Reports read whatever the last ``fincl refresh`` committed. Schema
    initialization, journal replay and categorization are preparation steps and
    never run on this path — the connection is pinned ``query_only``.
    """
    connection = connect_readonly(database_path)
    try:
        yield connection
    finally:
        connection.close()


def _dangling_decisions(connection: sqlite3.Connection) -> int:
    """Count journal decisions that no longer bind to a live anchor."""
    return int(
        connection.execute(
            "SELECT (SELECT count(*) FROM link_decisions WHERE bound = 0) "
            "     + (SELECT count(*) FROM invariant_escapes WHERE bound = 0) "
            "     + (SELECT count(*) FROM inbound_classifications WHERE bound = 0) "
            "     + (SELECT count(*) FROM category_overrides WHERE bound = 0)"
        ).fetchone()[0]
    )


def _category_warnings(connection: sqlite3.Connection) -> dict[str, Any]:
    """Category coverage plus the counts surfaced as warnings (§7, §14.3).

    A pure read: it reports whatever the last ``fincl refresh`` / ``fincl
    categorize`` committed. ``NOT_RUN`` until a categorization run exists.
    """
    run = connection.execute(
        "SELECT id FROM categorization_runs ORDER BY completed_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        return {"status": "NOT_RUN"}
    summary = summarize_run(connection, run["id"])
    summary["warnings"] = {
        key: summary[key]
        for key in (
            "unclassified", "ambiguous", "direction_conflict",
            "migrated", "missing_description", "alias_ambiguous",
        )
        if summary[key]
    }
    return summary


def build_report(database_path: Path) -> str:
    with _reading(database_path) as connection:
        return _build_report(connection)


def _build_report(connection: sqlite3.Connection) -> str:
    extraction = connection.execute(
        """
        SELECT * FROM extraction_runs
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if extraction is None:
        return "No extraction runs."

    document_statement = connection.execute(
        "SELECT * FROM statements WHERE extraction_run_id = ?",
        (extraction["id"],),
    ).fetchone()
    if document_statement is not None:
        return _build_document_report(connection, extraction, document_statement)

    issue_counts = Counter(
        {
            row["severity"]: row["count"]
            for row in connection.execute(
                """
                SELECT severity, count(*) AS count
                FROM extraction_issues
                WHERE extraction_run_id = ?
                GROUP BY severity
                """,
                (extraction["id"],),
            )
        }
    )
    checks = connection.execute(
        """
        SELECT count(*) AS total, coalesce(sum(passed), 0) AS passed
        FROM reconciliation_checks WHERE extraction_run_id = ?
        """,
        (extraction["id"],),
    ).fetchone()
    lines = [
        f"extraction: {extraction['id']}",
        f"status: {extraction['status']}",
        f"profile: {extraction['source_profile_version']}",
        f"evidence_sha256: {extraction['evidence_sha256']}",
        (
            "coverage: "
            f"physical_rows={extraction['physical_data_row_count'] or 0} "
            f"raw_records={extraction['raw_record_count'] or 0} "
            f"ledger_entries={extraction['canonical_entry_count'] or 0}"
        ),
        (
            "issues: "
            f"blocking={issue_counts['BLOCKING']} "
            f"warning={issue_counts['WARNING']} info={issue_counts['INFO']}"
        ),
        f"balance_checks: passed={checks['passed']} total={checks['total']}",
        f"dangling_decisions: {_dangling_decisions(connection)}",
        *_category_lines(_category_warnings(connection)),
    ]

    currencies = [
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT currency_pocket FROM ledger_entries
            WHERE extraction_run_id = ? AND source_family = 'CSV'
            ORDER BY currency_pocket
            """,
            (extraction["id"],),
        )
    ]
    for currency in currencies:
        rows = connection.execute(
            """
            SELECT c.*, r.extraction_ordinal
            FROM ledger_entries c
            JOIN raw_records r ON r.id = c.raw_record_id
            WHERE c.extraction_run_id = ? AND c.source_family = 'CSV'
              AND c.currency_pocket = ?
            ORDER BY r.extraction_ordinal
            """,
            (extraction["id"], currency),
        ).fetchall()
        states = Counter(row["lifecycle_state"] for row in rows)
        completed = [row for row in rows if row["lifecycle_state"] == "COMPLETED"]
        amount_text = _sum_rows(completed, "amount")
        fee_text = _sum_rows(completed, "fee")
        balances = [row["balance_text"] for row in completed if row["balance_text"] is not None]
        pocket_checks = connection.execute(
            """
            SELECT count(*) AS total, coalesce(sum(passed), 0) AS passed
            FROM reconciliation_checks
            WHERE extraction_run_id = ? AND currency_pocket = ?
            """,
            (extraction["id"], currency),
        ).fetchone()
        lines.append(

                f"pocket {currency}: COMPLETED={states['COMPLETED']} "
                f"PENDING={states['PENDING']} REVERTED={states['REVERTED']} "
                f"completed_amount={amount_text} completed_fee={fee_text} "
                f"first_balance={balances[0] if balances else 'unavailable'} "
                f"last_balance={balances[-1] if balances else 'unavailable'} "
                f"checks={pocket_checks['passed']}/{pocket_checks['total']}"

        )
    return "\n".join(lines)


def latest_report_data(database_path: Path) -> dict[str, Any]:
    with _reading(database_path) as connection:
        return _latest_report_data(connection)


def _latest_report_data(connection: sqlite3.Connection) -> dict[str, Any]:
    extraction = connection.execute(
        "SELECT * FROM extraction_runs ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if extraction is None:
        return {"status": "NO_DATA"}
    statement = connection.execute(
        "SELECT * FROM statements WHERE extraction_run_id = ?", (extraction["id"],)
    ).fetchone()
    if statement is None:
        data: dict[str, Any] = {
            "kind": "REVOLUT_PDF",
            "extraction_id": extraction["id"],
            "status": extraction["status"],
            "profile": extraction["source_profile_version"],
            "physical_rows": extraction["physical_data_row_count"] or 0,
            "raw_records": extraction["raw_record_count"] or 0,
            "ledger_entries": extraction["canonical_entry_count"] or 0,
        }
    else:
        pages = connection.execute(
            "SELECT count(*) AS total, sum(status = 'ACCOUNTED') AS accounted, sum(transaction_rows) AS rows FROM page_coverage WHERE extraction_run_id = ?",
            (extraction["id"],),
        ).fetchone()
        checks = connection.execute(
            "SELECT count(*) AS total, sum(passed = 1) AS passed FROM document_reconciliation_checks WHERE extraction_run_id = ? AND blocking = 1",
            (extraction["id"],),
        ).fetchone()
        data = {
            "kind": statement["source_type"],
            "extraction_id": extraction["id"],
            "status": extraction["status"],
            "profile": extraction["source_profile_version"],
            "statement_date": statement["statement_date"],
            "period_start": statement["period_start"],
            "period_end": statement["period_end"],
            "selection_state": statement["selection_state"],
            "pages": pages["total"] or 0,
            "accounted_pages": pages["accounted"] or 0,
            "transaction_rows": pages["rows"] or 0,
            "document_records": extraction["raw_record_count"] or 0,
            "reconciliation_passed": checks["passed"] or 0,
            "reconciliation_total": checks["total"] or 0,
        }
    issue_counts = {
        row["severity"]: row["count"]
        for row in connection.execute(
            "SELECT severity, count(*) AS count FROM extraction_issues WHERE extraction_run_id = ? GROUP BY severity",
            (extraction["id"],),
        )
    }
    data["issues"] = issue_counts
    data["confirmed_transfer_links"] = connection.execute(
        "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
    ).fetchone()[0]
    data["dangling_decisions"] = _dangling_decisions(connection)
    data["categories"] = _category_warnings(connection)
    return data


def economic_flow_report_data(
    database_path: Path,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Build exact economic flow for the covered or explicit analysis window."""
    with _reading(database_path) as connection:
        return _economic_flow(connection, start=start, end=end)


def _economic_flow(
    connection: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    coverage = _economic_flow_coverage(connection)
    dangling = _dangling_decisions(connection)

    contract = _coverage_contract(connection)
    covered_intervals = contract.covered_intervals
    covered_bound = bounding(covered_intervals)
    covered_window = None if covered_bound is None else covered_bound.as_dict()
    analysis_intervals, analysis_window_kind = _analysis_intervals(
        covered_intervals, start=start, end=end
    )
    analysis_bound = bounding(analysis_intervals)
    analysis_window = None if analysis_bound is None else analysis_bound.as_dict()

    # The economic-flow totals and the contributor rows behind them are the same
    # shared component projection (v0.2 analysis §2); this report just folds it
    # into per-currency sums.
    projection = build_components(
        connection,
        covered_intervals=covered_intervals,
        analysis_intervals=analysis_intervals,
    )
    excluded_entry_count = projection.excluded_entry_count
    period_precision_excluded = projection.period_precision_excluded
    netting = projection.netting()

    currencies = []
    for currency in sorted(projection.per_currency):
        values = projection.per_currency[currency]
        spending = projection.bucket_total(currency, "spending")
        income = projection.bucket_total(currency, "income")
        refunds = projection.bucket_total(currency, "refunds")
        reimbursements = projection.bucket_total(currency, "reimbursements")
        non_income_inflow = projection.bucket_total(currency, "non_income_inflow")
        fees = projection.bucket_total(currency, "fees")
        exceptional_inflow = projection.bucket_total(currency, "exceptional_inflow")
        exceptional_loss = projection.bucket_total(currency, "exceptional_loss")
        currencies.append(
            {
                "currency": currency,
                "entries": values.entries,
                "spending": spending,
                "income": income,
                "refunds": refunds,
                "reimbursements": reimbursements,
                "non_income_inflow": non_income_inflow,
                "exceptional_inflow": exceptional_inflow,
                "exceptional_loss": exceptional_loss,
                "pending_review": projection.bucket_total(currency, "pending_review"),
                "other_classified_inbound": projection.bucket_total(currency, "other"),
                "fees": fees,
                "net_economic_flow": _net_money(
                    [income, refunds, reimbursements, non_income_inflow, exceptional_inflow],
                    [spending, fees, exceptional_loss],
                ),
                "excluded_confirmed_internal_entries": (
                    values.excluded_confirmed_internal_entries
                ),
                "excluded_self_transfer_entries": values.excluded_self_transfer_entries,
                "entries_outside_covered_window": values.outside_covered_window,
                "entries_outside_analysis_window": values.outside_analysis_window,
                "coverage_complete": (
                    analysis_window_kind == "EXPLICIT"
                    or (
                        contract.status == "COVERED"
                        and values.outside_covered_window == 0
                    )
                ),
                "covered_window": covered_window,
                "analysis_window": analysis_window,
            }
        )
    any_incomplete = any(not currency["coverage_complete"] for currency in currencies)
    # A GAPPED or UNAVAILABLE intersection withholds the consolidated total even
    # when no entry happens to fall outside it (e.g. an empty export): a missing
    # period must read as null, never as a measured zero (§2).
    coverage_withheld = (
        analysis_window_kind != "EXPLICIT" and contract.status != "COVERED"
    )
    fx_inputs: dict[tuple[str, str], dict[str, str]] = {}
    if any_incomplete or coverage_withheld:
        consolidated_total, consolidated_status = None, "UNAVAILABLE_COVERAGE_INCOMPLETE"
    else:
        consolidated_total, consolidated_status = _consolidate_sgd(
            connection, netting, fx_inputs, analysis_window=analysis_window
        )

    return {
        "kind": "ECONOMIC_FLOW",
        "fx_selection_policy": LATEST_FIRST_IMPORT_V1,
        "fx_inputs": [fx_inputs[key] for key in sorted(fx_inputs)],
        "reporting_currency": _REPORTING_CURRENCY,
        "consolidated_sgd_total": consolidated_total,
        "consolidated_sgd_total_status": consolidated_status,
        "coverage_complete": not any_incomplete and not coverage_withheld,
        "analysis_window": analysis_window,
        "analysis_window_kind": analysis_window_kind,
        "coverage_policy_version": COVERAGE_POLICY_VERSION,
        "coverage_status": contract.status,
        "coverage_status_reasons": contract.reasons,
        "required_sources": contract.required_sources,
        "source_intervals": contract.source_intervals,
        "covered_intervals": [interval.as_dict() for interval in covered_intervals],
        "requested_window": (
            {"start": start, "end": end} if start is not None and end is not None else None
        ),
        "analysis_intervals": [interval.as_dict() for interval in analysis_intervals],
        "excluded_entry_count": excluded_entry_count,
        "period_precision_excluded": period_precision_excluded,
        "currencies": currencies,
        "coverage": coverage,
        "dangling_decisions": dangling,
    }


def _consolidate_sgd(
    connection: sqlite3.Connection, netting: list[tuple[str, int, int, str]],
    fx_inputs: dict[tuple[str, str], dict[str, str]],
    *, analysis_window: dict[str, str] | None,
) -> tuple[str | None, str]:
    """Value every netting entry into SGD at its month's loaded rate.

    Returns ``(total_text, "AVAILABLE")`` only when a rate is loaded for every
    (foreign currency, month) that appears; otherwise the total is ``None`` and
    the status names why. The sum is exact until a single final rounding to the
    cent, half away from zero.

    This is a thin adapter over the M2 valuation seam (``valuation.py``,
    §3.3/§3.6): it builds one ``FLOW``-purpose ``ValuationItem`` per netting
    row and delegates arithmetic, rate selection, and quantization to
    ``consolidate``. This report predates the money-flow call graph (§5.1) and
    is not in scope for the M1 wall-clock threading, so ``valuation_as_of`` is
    resolved from the already-computed analysis window rather than a request
    parameter; it is never used to select a rate here because every FX lookup
    below is an exact-period lookup keyed by each row's own month, matching
    the pre-seam behaviour exactly. ``date.min`` is a deterministic fallback
    for the (netting-empty) case where no analysis window exists at all.

    The policy name is this report's own ``fx_selection_policy``, and the seam
    honours it as a direct-only ladder (no reciprocal leg). That is deliberate:
    §3.2's ``direct-or-inverse-v1`` would price a currency from a reporting ->
    native row this report has always ignored, turning an
    ``UNAVAILABLE_FX_RATES_NOT_IMPORTED`` status into a total. Moving this
    behind the seam must not change the figure it publishes (§8 M2); reporting
    surfaces built on the new ladder ask for ``flow-rate-v1`` instead.
    """
    valuation_as_of = (
        date.fromisoformat(analysis_window["end"]) if analysis_window else date.min
    )
    context = ValuationContext(
        reporting_currency=_REPORTING_CURRENCY,
        purpose=ValuationPurpose.FLOW,
        valuation_as_of=valuation_as_of,
        rate_selection_policy=LATEST_FIRST_IMPORT_V1,
    )
    items = [
        ValuationItem(
            native=ExactMoney(coefficient, scale, format_decimal(coefficient, scale)),
            native_currency=currency,
            purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH,
            period_month=period,
        )
        for currency, coefficient, scale, period in netting
    ]
    result = consolidate(connection, items, context=context)
    # Reproduce the pre-seam ``fx_inputs`` payload exactly: this report walked
    # `netting` in order and returned on the *first* row whose rate was
    # missing, so it published provenance only for the rows it had already
    # priced. `consolidate` prices every row (partial totals need that), so
    # stop the walk where the old loop would have returned.
    for (currency, _coefficient, _scale, period), value in zip(
        netting, result.values, strict=True
    ):
        if value.unavailable is not None:
            break
        for rate_input in value.rate_path:
            fx_inputs[(currency, period)] = rate_input.legacy_provenance_dict()
    if result.status != ConsolidationStatus.AVAILABLE:
        return None, "UNAVAILABLE_FX_RATES_NOT_IMPORTED"
    assert result.total is not None
    quantized = quantize(result.total, scale=2)
    return format_decimal(quantized.coefficient, quantized.scale), "AVAILABLE"


def _analysis_intervals(
    covered: list[DateInterval],
    *,
    start: str | None,
    end: str | None,
) -> tuple[list[DateInterval], str]:
    """Resolve the analysis interval set from an optional explicit window (§2).

    Without ``--start``/``--end`` the analysis set is the covered intersection.
    An explicit window must lie wholly inside a single covered interval; one
    that crosses a coverage gap is refused with the uncovered dates named.
    """
    if (start is None) != (end is None):
        raise FinCLError("economic-flow requires --start and --end together")
    if start is None:
        return covered, "COVERAGE_INTERSECTION"
    assert end is not None
    try:
        parsed_start, parsed_end = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise FinCLError("economic-flow dates must use YYYY-MM-DD") from exc
    if parsed_start > parsed_end:
        raise FinCLError("economic-flow --start must not be after --end")
    if not covered:
        raise FinCLError("economic-flow has no non-empty cross-source covered window")
    if not contains_range(covered, start, end):
        gaps = uncovered_dates(covered, start, end)
        raise FinCLError(
            "economic-flow analysis window is not inside a single covered window; "
            "uncovered dates: "
            + ", ".join(f"{gap.start}..{gap.end}" for gap in gaps)
        )
    return [DateInterval(start, end)], "EXPLICIT"


def build_economic_flow_report(
    database_path: Path,
    *,
    start: str | None = None,
    end: str | None = None,
) -> str:
    data = economic_flow_report_data(database_path, start=start, end=end)
    if data["consolidated_sgd_total"] is None:
        consolidated_line = (
            f"consolidated_sgd_total: unavailable ({data['consolidated_sgd_total_status']})"
        )
    else:
        consolidated_line = f"consolidated_sgd_total: {data['consolidated_sgd_total']} SGD"
    covered_intervals = ", ".join(
        f"{interval['start']}..{interval['end']}" for interval in data["covered_intervals"]
    )
    lines = [
        "economic_flow: per-currency exact totals",
        consolidated_line,
        f"coverage_complete: {data['coverage_complete']}",
        f"coverage_status: {data['coverage_status']} [{covered_intervals}]"
        f" ({data['coverage_policy_version']})",
        f"excluded_entry_count: {data['excluded_entry_count']} "
        f"(period_precision_excluded={data['period_precision_excluded']})",
        "analysis_window: "
        f"{data['analysis_window']} ({data['analysis_window_kind']})",
        f"dangling_decisions: {data['dangling_decisions']}",
    ]
    for currency in data["currencies"]:
        lines.append(
            "{currency}: spending={spending} income={income} "
            "refunds={refunds} reimbursements={reimbursements} non_income_inflow={non_income_inflow} "
            "exceptional_inflow={exceptional_inflow} exceptional_loss={exceptional_loss} "
            "pending_review={pending_review} "
            "other_classified_inbound={other_classified_inbound} "
            "fees={fees} net={net_economic_flow} "
            "excluded_confirmed_internal_entries={excluded_confirmed_internal_entries} "
            "excluded_self_transfer_entries={excluded_self_transfer_entries} "
            "coverage_complete={coverage_complete} "
            "entries_outside_covered_window={entries_outside_covered_window} "
            "entries_outside_analysis_window={entries_outside_analysis_window}".format(**currency)
        )
    for source in data["coverage"]:
        lines.append(
            "coverage: {source_type} {period_start}..{period_end} "
            "status={status} selection={selection_state}".format(**source)
        )
    return "\n".join(lines)


def coverage_report_data(database_path: Path) -> dict[str, Any]:
    with _reading(database_path) as connection:
        statements = []
        for row in connection.execute(
            """
            SELECT s.*, x.status AS extraction_status,
                   count(p.id) AS pages,
                   coalesce(sum(p.transaction_rows), 0) AS transaction_rows,
                   coalesce(sum(p.status = 'UNRESOLVED'), 0) AS unresolved_pages
            FROM statements s
            JOIN extraction_runs x ON x.id = s.extraction_run_id
            LEFT JOIN page_coverage p ON p.statement_id = s.id
            GROUP BY s.id
            ORDER BY s.statement_date, s.source_type
            """
        ):
            statements.append(
                {
                    "source_type": row["source_type"],
                    "statement_date": row["statement_date"],
                    "period_start": row["period_start"],
                    "period_end": row["period_end"],
                    "status": row["extraction_status"],
                    "selection_state": row["selection_state"],
                    "pages": row["pages"],
                    "transaction_rows": row["transaction_rows"],
                    "unresolved_pages": row["unresolved_pages"],
                }
            )
        contract = _coverage_contract(connection)
        data = {
            "statements": statements,
            "confirmed_transfer_links": connection.execute(
                "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
            ).fetchone()[0],
            "dangling_decisions": _dangling_decisions(connection),
            "needs_reextraction": needs_reextraction(connection),
            "categories": _category_warnings(connection),
            "coverage_policy_version": COVERAGE_POLICY_VERSION,
            "coverage_status": contract.status,
            "coverage_status_reasons": contract.reasons,
            "required_sources": contract.required_sources,
            "source_intervals": contract.source_intervals,
            "covered_intervals": [
                interval.as_dict() for interval in contract.covered_intervals
            ],
        }
    return data


def profile_report_data(database_path: Path) -> dict[str, Any]:
    """Return repeatable corpus observations without asserting their values."""
    with _reading(database_path) as connection:
        profiles = [
            dict(row)
            for row in connection.execute(
                """
                SELECT source_profile_version, parser_version, extraction_policy_version,
                       status, count(*) AS extractions,
                       coalesce(sum(raw_record_count), 0) AS raw_records,
                       coalesce(sum(canonical_entry_count), 0) AS ledger_entries,
                       min(started_at) AS first_started_at, max(completed_at) AS last_completed_at
                FROM extraction_runs
                GROUP BY source_profile_version, parser_version, extraction_policy_version, status
                ORDER BY source_profile_version, parser_version, extraction_policy_version, status
                """
            )
        ]
        source_types = [
            dict(row)
            for row in connection.execute(
                """
                SELECT source_type, count(*) AS statements,
                       min(period_start) AS first_period_start, max(period_end) AS last_period_end
                FROM statements
                GROUP BY source_type
                ORDER BY source_type
                """
            )
        ]
        categories = _category_warnings(connection)
    return {"profiles": profiles, "source_types": source_types, "categories": categories}


def build_profile_report(database_path: Path) -> str:
    data = profile_report_data(database_path)
    if not data["profiles"]:
        return "No extraction profiles."
    lines = [
        "profile parser policy status extractions raw_records ledger_entries first_started last_completed"
    ]
    for profile in data["profiles"]:
        lines.append(
            "{source_profile_version} {parser_version} {extraction_policy_version} {status} "
            "{extractions} {raw_records} {ledger_entries} {first_started_at} {last_completed_at}".format(
                **profile
            )
        )
    for source_type in data["source_types"]:
        lines.append(
            "source_type={source_type} statements={statements} period={first_period_start}..{last_period_end}".format(
                **source_type
            )
        )
    lines.extend(_category_lines(data["categories"]))
    return "\n".join(lines)


def build_coverage_report(database_path: Path) -> str:
    data = coverage_report_data(database_path)
    statements = data["statements"]
    stale = data["needs_reextraction"]
    if not statements:
        return (
            f"No document statements.\ndangling_decisions={data['dangling_decisions']}\n"
            f"needs_reextraction={len(stale)}"
        )
    lines = ["source_type statement_date period status selection pages rows unresolved_pages"]
    for statement in statements:
        period = f"{statement['period_start']}..{statement['period_end']}"
        lines.append(
            f"{statement['source_type']} {statement['statement_date']} {period} "
            f"{statement['status']} {statement['selection_state']} {statement['pages']} "
            f"{statement['transaction_rows']} {statement['unresolved_pages']}"
        )
    covered_intervals = ", ".join(
        f"{interval['start']}..{interval['end']}" for interval in data["covered_intervals"]
    )
    lines.append(
        f"coverage_status={data['coverage_status']} covered_intervals=[{covered_intervals}] "
        f"required_sources={','.join(data['required_sources'])}"
    )
    lines.append(f"confirmed_transfer_links={data['confirmed_transfer_links']}")
    lines.append(f"dangling_decisions={data['dangling_decisions']}")
    lines.append(f"needs_reextraction={len(stale)}")
    for blob in stale:
        lines.append(
            f"  stale-version evidence {blob['evidence_sha256'][:12]} "
            f"({blob['profiles']}) {blob['original_path']}"
        )
    lines.extend(_category_lines(data["categories"]))
    return "\n".join(lines)


def _category_lines(categories: dict[str, Any]) -> list[str]:
    if categories.get("status") == "NOT_RUN":
        return ["categories: not yet run (fincl categorize)"]
    warnings = categories.get("warnings", {})
    line = "categories: " + " ".join(
        f"{group}={count}" for group, count in sorted(categories["by_group"].items())
    )
    out = [line]
    if warnings:
        out.append(
            "category_warnings: "
            + " ".join(f"{key}={value}" for key, value in sorted(warnings.items()))
        )
    return out


def json_report(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def _build_document_report(
    connection: sqlite3.Connection, extraction: sqlite3.Row, statement: sqlite3.Row
) -> str:
    pages = connection.execute(
        "SELECT count(*) AS total, sum(status = 'ACCOUNTED') AS accounted, sum(transaction_rows) AS rows, sum(rotated_ignored_count) AS rotated FROM page_coverage WHERE extraction_run_id = ?",
        (extraction["id"],),
    ).fetchone()
    checks = connection.execute(
        "SELECT count(*) AS total, sum(passed = 1) AS passed FROM document_reconciliation_checks WHERE extraction_run_id = ? AND blocking = 1",
        (extraction["id"],),
    ).fetchone()
    issue_counts = Counter(
        {
            row["severity"]: row["count"]
            for row in connection.execute(
                "SELECT severity, count(*) AS count FROM extraction_issues WHERE extraction_run_id = ? GROUP BY severity",
                (extraction["id"],),
            )
        }
    )
    sections = [
        f"{row['currency'] or row['section_kind']}@{row['start_page']}-{row['end_page']}"
        for row in connection.execute(
            "SELECT * FROM statement_sections WHERE statement_id = ? ORDER BY section_ordinal",
            (statement["id"],),
        )
    ]
    links = connection.execute(
        "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
    ).fetchone()[0]
    return "\n".join(
        (
            f"extraction: {extraction['id']}",
            f"status: {extraction['status']}",
            f"source_type: {statement['source_type']}",
            f"profile: {extraction['source_profile_version']}",
            f"statement_date: {statement['statement_date']}",
            f"period: {statement['period_start']} to {statement['period_end']}",
            f"selection: {statement['selection_state']} conflict={statement['conflict_state']}",
            f"coverage: pages={pages['accounted'] or 0}/{pages['total'] or 0} transaction_rows={pages['rows'] or 0} rotated_ignored={pages['rotated'] or 0}",
            f"records: raw={extraction['raw_record_count'] or 0} ledger={extraction['canonical_entry_count'] or 0}",
            f"issues: blocking={issue_counts['BLOCKING']} warning={issue_counts['WARNING']} info={issue_counts['INFO']}",
            f"reconciliation: passed={checks['passed'] or 0} total={checks['total'] or 0}",
            f"sections: {', '.join(sections)}",
            f"confirmed_transfer_links: {links}",
            f"dangling_decisions: {_dangling_decisions(connection)}",
            *_category_lines(_category_warnings(connection)),
        )
    )


def _sum_rows(rows: list[sqlite3.Row], prefix: str) -> str:
    values = [
        ExactMoney(
            row[f"{prefix}_coefficient"],
            row[f"{prefix}_scale"],
            row[f"{prefix}_text"],
        )
        for row in rows
    ]
    coefficient, scale = exact_sum(*values)
    return format_decimal(coefficient, scale)


def _net_money(positives: list[str], negatives: list[str]) -> str:
    terms = [ExactMoney.parse(text) for text in positives]
    for text in negatives:
        value = ExactMoney.parse(text)
        terms.append(ExactMoney(-value.coefficient, value.scale, text))
    coefficient, scale = exact_sum(*terms)
    return format_decimal(coefficient, scale)


def _economic_flow_coverage(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Expose source periods individually; statement cycles are not unionable."""
    coverage = [
        dict(row)
        for row in connection.execute(
            """
            SELECT s.source_type, s.period_start, s.period_end, x.status,
                   s.selection_state
            FROM statements s JOIN extraction_runs x ON x.id = s.extraction_run_id
            ORDER BY s.source_type, s.period_start, s.period_end, s.id
            """
        )
    ]
    return coverage
