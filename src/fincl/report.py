from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, TypedDict

from . import fx
from .db import connect_sqlite, initialize_schema
from .decisions import replay_journal
from .errors import FinCLError
from .money import ExactMoney, exact_sum, format_decimal

_REPORTING_CURRENCY = "SGD"


@contextmanager
def _reading(database_path: Path, *, replay: bool = True) -> Iterator[sqlite3.Connection]:
    """Open an initialized connection for a report that always closes."""
    connection = connect_sqlite(database_path)
    try:
        initialize_schema(connection)
        if replay:
            replay_journal(connection)
        yield connection
    finally:
        connection.close()


def _dangling_decisions(connection: sqlite3.Connection) -> int:
    """Count journal decisions that no longer bind to a live anchor."""
    return int(
        connection.execute(
            "SELECT (SELECT count(*) FROM link_decisions WHERE bound = 0) "
            "     + (SELECT count(*) FROM invariant_escapes WHERE bound = 0) "
            "     + (SELECT count(*) FROM inbound_classifications WHERE bound = 0)"
        ).fetchone()[0]
    )


class _EconomicFlowBucket(TypedDict):
    spending: list[ExactMoney]
    income: list[ExactMoney]
    reimbursements: list[ExactMoney]
    non_income_inflow: list[ExactMoney]
    pending_review: list[ExactMoney]
    other_classified_inbound: list[ExactMoney]
    fees: list[ExactMoney]
    excluded_linked: int
    excluded_self_transfer: int
    entries: int
    outside_covered_window: int
    outside_analysis_window: int


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
            "kind": "REVOLUT_CSV",
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
    rows = connection.execute(
        """
        WITH entries AS (
            SELECT e.source_family AS entry_kind, e.id, e.currency_pocket AS currency,
                   substr(e.effective_at, 1, 10) AS posted_date,
                   e.amount_text, e.amount_coefficient, e.amount_scale,
                   COALESCE(e.fee_text, '0') AS fee_text,
                   COALESCE(e.fee_coefficient, 0) AS fee_coefficient,
                   COALESCE(e.fee_scale, 0) AS fee_scale,
                   e.economic_direction, e.event_kind,
                   e.natural_anchor AS entry_anchor
            FROM ledger_entries e
            JOIN extraction_runs x ON x.id = e.extraction_run_id
            LEFT JOIN statements s ON s.id = e.statement_id
            WHERE e.analytics_eligible = 1 AND e.lifecycle_state = 'COMPLETED'
              AND x.status = 'ACCEPTED'
              AND (e.source_family = 'CSV' OR s.selection_state = 'SELECTED')
        ), linked AS (
            SELECT left_entry_id AS id FROM transfer_links WHERE status = 'CONFIRMED'
            UNION
            SELECT right_entry_id AS id FROM transfer_links WHERE status = 'CONFIRMED'
        )
        SELECT e.*, CASE WHEN l.id IS NULL THEN 0 ELSE 1 END AS is_linked,
               i.classification AS inbound_classification
        FROM entries e
        LEFT JOIN linked l ON l.id = e.id
        LEFT JOIN inbound_classifications i ON i.bound = 1 AND i.anchor = e.entry_anchor
        ORDER BY e.currency, e.posted_date, e.entry_kind, e.id
        """
    ).fetchall()
    coverage = _economic_flow_coverage(connection)
    dangling = _dangling_decisions(connection)

    covered_start, covered_end = _covered_window(coverage)
    covered_window = (
        None if covered_start is None else {"start": covered_start, "end": covered_end}
    )
    analysis_start, analysis_end, analysis_window_kind = _analysis_window(
        covered_start, covered_end, start=start, end=end
    )
    analysis_window = (
        None
        if analysis_start is None
        else {"start": analysis_start, "end": analysis_end}
    )

    by_currency: dict[str, _EconomicFlowBucket] = {}
    netting: list[tuple[str, int, int, str]] = []
    for row in rows:
        bucket = by_currency.setdefault(row["currency"], _empty_bucket())
        bucket["entries"] += 1
        if covered_start is not None and not (
            covered_start <= row["posted_date"] <= covered_end
        ):
            bucket["outside_covered_window"] += 1
        if analysis_start is not None and not (
            analysis_start <= row["posted_date"] <= analysis_end
        ):
            bucket["outside_analysis_window"] += 1
            continue
        period = row["posted_date"][:7]
        if row["is_linked"]:
            bucket["excluded_linked"] += 1
        elif row["economic_direction"] == "OUT":
            bucket["spending"].append(_magnitude(row, "amount"))
            netting.append(
                (row["currency"], -abs(int(row["amount_coefficient"])), int(row["amount_scale"]), period)
            )
        elif row["economic_direction"] == "IN" and _classify_inbound(bucket, row):
            netting.append(
                (row["currency"], abs(int(row["amount_coefficient"])), int(row["amount_scale"]), period)
            )
        if row["fee_coefficient"]:
            bucket["fees"].append(_magnitude(row, "fee"))
            netting.append(
                (row["currency"], -abs(int(row["fee_coefficient"])), int(row["fee_scale"]), period)
            )

    currencies = []
    for currency, values in sorted(by_currency.items()):
        income = _format_money_list(values["income"])
        reimbursements = _format_money_list(values["reimbursements"])
        non_income_inflow = _format_money_list(values["non_income_inflow"])
        spending = _format_money_list(values["spending"])
        fees = _format_money_list(values["fees"])
        currencies.append(
            {
                "currency": currency,
                "entries": values["entries"],
                "spending": spending,
                "income": income,
                "reimbursements": reimbursements,
                "non_income_inflow": non_income_inflow,
                "pending_review": _format_money_list(values["pending_review"]),
                "other_classified_inbound": _format_money_list(
                    values["other_classified_inbound"]
                ),
                "fees": fees,
                "net_economic_flow": _net_money(
                    [income, reimbursements, non_income_inflow], [spending, fees]
                ),
                "excluded_confirmed_internal_entries": values["excluded_linked"],
                "excluded_self_transfer_entries": values["excluded_self_transfer"],
                "entries_outside_covered_window": values["outside_covered_window"],
                "entries_outside_analysis_window": values["outside_analysis_window"],
                "coverage_complete": (
                    analysis_window_kind == "EXPLICIT"
                    or values["outside_covered_window"] == 0
                ),
                "covered_window": covered_window,
                "analysis_window": analysis_window,
            }
        )
    any_incomplete = any(not currency["coverage_complete"] for currency in currencies)
    if any_incomplete:
        consolidated_total, consolidated_status = None, "UNAVAILABLE_COVERAGE_INCOMPLETE"
    else:
        consolidated_total, consolidated_status = _consolidate_sgd(connection, netting)

    return {
        "kind": "ECONOMIC_FLOW",
        "reporting_currency": _REPORTING_CURRENCY,
        "consolidated_sgd_total": consolidated_total,
        "consolidated_sgd_total_status": consolidated_status,
        "coverage_complete": not any_incomplete,
        "analysis_window": analysis_window,
        "analysis_window_kind": analysis_window_kind,
        "currencies": currencies,
        "coverage": coverage,
        "dangling_decisions": dangling,
    }


def _consolidate_sgd(
    connection: sqlite3.Connection, netting: list[tuple[str, int, int, str]]
) -> tuple[str | None, str]:
    """Value every netting entry into SGD at its month's loaded rate.

    Returns ``(total_text, "AVAILABLE")`` only when a rate is loaded for every
    (foreign currency, month) that appears; otherwise the total is ``None`` and
    the status names why. The sum is exact until a single final rounding to the
    cent, half away from zero.
    """
    total = Fraction(0)
    for currency, coefficient, scale, period in netting:
        amount = Fraction(coefficient, 10**scale)
        if currency == _REPORTING_CURRENCY:
            total += amount
            continue
        rate = fx.rate_fraction(connection, currency, _REPORTING_CURRENCY, period)
        if rate is None:
            return None, "UNAVAILABLE_FX_RATES_NOT_IMPORTED"
        total += amount * rate
    cents = total * 100
    rounded = int(cents + Fraction(1, 2)) if cents >= 0 else -int(-cents + Fraction(1, 2))
    return format_decimal(rounded, 2), "AVAILABLE"


def _empty_bucket() -> _EconomicFlowBucket:
    return {
        "spending": [], "income": [], "reimbursements": [], "non_income_inflow": [],
        "pending_review": [], "other_classified_inbound": [], "fees": [],
        "excluded_linked": 0, "excluded_self_transfer": 0, "entries": 0,
        "outside_covered_window": 0, "outside_analysis_window": 0,
    }


def _classify_inbound(bucket: _EconomicFlowBucket, row: sqlite3.Row) -> bool:
    """Route an inbound entry by its journaled classification (§15.1).

    Returns True when the entry nets into ``net_economic_flow`` (income,
    reimbursement, gift, family_transfer) and False otherwise.
    """
    classification = row["inbound_classification"]
    if classification == "income":
        bucket["income"].append(_magnitude(row, "amount"))
        return True
    if classification == "self_transfer":
        bucket["excluded_self_transfer"] += 1
        return False
    if classification == "reimbursement":
        bucket["reimbursements"].append(_magnitude(row, "amount"))
        return True
    if classification in ("gift", "family_transfer"):
        bucket["non_income_inflow"].append(_magnitude(row, "amount"))
        return True
    if classification == "other":
        bucket["other_classified_inbound"].append(_magnitude(row, "amount"))
        return False
    bucket["pending_review"].append(_magnitude(row, "amount"))
    return False


def _covered_window(
    coverage: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Intersection of every accepted, selected source's period (§11).

    An entry outside this window is not fully cross-source observed, so it is
    excluded from the per-currency baselines. This uses one window for all
    currencies; per-pocket coverage is a tracked refinement.
    """
    spans: dict[str, tuple[str, str]] = {}
    for source in coverage:
        if source["status"] != "ACCEPTED" or source["selection_state"] != "SELECTED":
            continue
        if not source["period_start"] or not source["period_end"]:
            continue
        source_type = source["source_type"]
        start, end = source["period_start"], source["period_end"]
        if source_type in spans:
            low, high = spans[source_type]
            spans[source_type] = (min(low, start), max(high, end))
        else:
            spans[source_type] = (start, end)
    if not spans:
        return None, None
    # Same-source periods union; distinct sources must overlap to count as
    # fully cross-source observed.
    return (
        max(low for low, _ in spans.values()),
        min(high for _, high in spans.values()),
    )


def _analysis_window(
    covered_start: str | None,
    covered_end: str | None,
    *,
    start: str | None,
    end: str | None,
) -> tuple[str | None, str | None, str]:
    """Validate an explicit analysis window against the coverage intersection."""
    if (start is None) != (end is None):
        raise FinCLError("economic-flow requires --start and --end together")
    if start is None:
        return covered_start, covered_end, "COVERAGE_INTERSECTION"
    assert end is not None
    try:
        parsed_start, parsed_end = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise FinCLError("economic-flow dates must use YYYY-MM-DD") from exc
    if parsed_start > parsed_end:
        raise FinCLError("economic-flow --start must not be after --end")
    if covered_start is None or covered_end is None or covered_start > covered_end:
        raise FinCLError("economic-flow has no non-empty cross-source covered window")
    if start < covered_start or end > covered_end:
        raise FinCLError(
            "economic-flow analysis window must be inside the covered window "
            f"{covered_start}..{covered_end}"
        )
    return start, end, "EXPLICIT"


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
    lines = [
        "economic_flow: per-currency exact totals",
        consolidated_line,
        f"coverage_complete: {data['coverage_complete']}",
        "analysis_window: "
        f"{data['analysis_window']} ({data['analysis_window_kind']})",
        f"dangling_decisions: {data['dangling_decisions']}",
    ]
    for currency in data["currencies"]:
        lines.append(
            "{currency}: spending={spending} income={income} "
            "reimbursements={reimbursements} non_income_inflow={non_income_inflow} "
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
        data = {
            "statements": statements,
            "confirmed_transfer_links": connection.execute(
                "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
            ).fetchone()[0],
            "dangling_decisions": _dangling_decisions(connection),
        }
    return data


def profile_report_data(database_path: Path) -> dict[str, Any]:
    """Return repeatable corpus observations without asserting their values."""
    with _reading(database_path, replay=False) as connection:
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
    return {"profiles": profiles, "source_types": source_types}


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
    return "\n".join(lines)


def build_coverage_report(database_path: Path) -> str:
    data = coverage_report_data(database_path)
    statements = data["statements"]
    if not statements:
        return f"No document statements.\ndangling_decisions={data['dangling_decisions']}"
    lines = ["source_type statement_date period status selection pages rows unresolved_pages"]
    for statement in statements:
        period = f"{statement['period_start']}..{statement['period_end']}"
        lines.append(
            f"{statement['source_type']} {statement['statement_date']} {period} "
            f"{statement['status']} {statement['selection_state']} {statement['pages']} "
            f"{statement['transaction_rows']} {statement['unresolved_pages']}"
        )
    lines.append(f"confirmed_transfer_links={data['confirmed_transfer_links']}")
    lines.append(f"dangling_decisions={data['dangling_decisions']}")
    return "\n".join(lines)


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


def _magnitude(row: sqlite3.Row, prefix: str) -> ExactMoney:
    return ExactMoney(
        abs(int(row[f"{prefix}_coefficient"])),
        int(row[f"{prefix}_scale"]),
        str(row[f"{prefix}_text"]),
    )


def _format_money_list(values: list[ExactMoney]) -> str:
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
    # CSV does not have a statement-period table. Its accepted transaction dates
    # are its coverage fact, so retrieve them through the entry table instead.
    for row in connection.execute(
        """
        SELECT min(substr(e.effective_at, 1, 10)) AS period_start,
               max(substr(e.effective_at, 1, 10)) AS period_end, x.status
        FROM extraction_runs x JOIN ledger_entries e ON e.extraction_run_id = x.id
        WHERE e.source_family = 'CSV'
          AND x.source_profile_version LIKE 'revolut-csv-%'
        GROUP BY x.id, x.status
        ORDER BY period_start
        """
    ):
        coverage.append(
            {
                "source_type": "REVOLUT_CSV",
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "status": row["status"],
                "selection_state": "SELECTED" if row["status"] == "ACCEPTED" else "QUARANTINED",
            }
        )
    return coverage
