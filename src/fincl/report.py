from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .db import connect_sqlite, initialize_schema
from .money import ExactMoney, exact_sum, format_decimal


def build_report(database_path: Path) -> str:
    connection = connect_sqlite(database_path)
    initialize_schema(connection)
    extraction = connection.execute(
        """
        SELECT * FROM extraction_runs
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if extraction is None:
        connection.close()
        return "No extraction runs."

    document_statement = connection.execute(
        "SELECT * FROM statements WHERE extraction_run_id = ?",
        (extraction["id"],),
    ).fetchone()
    if document_statement is not None:
        report = _build_document_report(connection, extraction, document_statement)
        connection.close()
        return report

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
            f"canonical_entries={extraction['canonical_entry_count'] or 0}"
        ),
        (
            "issues: "
            f"blocking={issue_counts['BLOCKING']} "
            f"warning={issue_counts['WARNING']} info={issue_counts['INFO']}"
        ),
        f"balance_checks: passed={checks['passed']} total={checks['total']}",
    ]

    currencies = [
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT currency_pocket FROM canonical_entries
            WHERE extraction_run_id = ? ORDER BY currency_pocket
            """,
            (extraction["id"],),
        )
    ]
    for currency in currencies:
        rows = connection.execute(
            """
            SELECT c.*, r.extraction_ordinal
            FROM canonical_entries c
            JOIN raw_records r ON r.id = c.raw_record_id
            WHERE c.extraction_run_id = ? AND c.currency_pocket = ?
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
    connection.close()
    return "\n".join(lines)


def latest_report_data(database_path: Path) -> dict[str, Any]:
    connection = connect_sqlite(database_path)
    initialize_schema(connection)
    extraction = connection.execute(
        "SELECT * FROM extraction_runs ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if extraction is None:
        connection.close()
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
            "canonical_entries": extraction["canonical_entry_count"] or 0,
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
    connection.close()
    return data


def coverage_report_data(database_path: Path) -> dict[str, Any]:
    connection = connect_sqlite(database_path)
    initialize_schema(connection)
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
    }
    connection.close()
    return data


def build_coverage_report(database_path: Path) -> str:
    data = coverage_report_data(database_path)
    statements = data["statements"]
    if not statements:
        return "No document statements."
    lines = ["source_type statement_date period status selection pages rows unresolved_pages"]
    for statement in statements:
        period = f"{statement['period_start']}..{statement['period_end']}"
        lines.append(
            f"{statement['source_type']} {statement['statement_date']} {period} "
            f"{statement['status']} {statement['selection_state']} {statement['pages']} "
            f"{statement['transaction_rows']} {statement['unresolved_pages']}"
        )
    lines.append(f"confirmed_transfer_links={data['confirmed_transfer_links']}")
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
            f"records: raw={extraction['raw_record_count'] or 0} canonical={extraction['canonical_entry_count'] or 0}",
            f"issues: blocking={issue_counts['BLOCKING']} warning={issue_counts['WARNING']} info={issue_counts['INFO']}",
            f"reconciliation: passed={checks['passed'] or 0} total={checks['total'] or 0}",
            f"sections: {', '.join(sections)}",
            f"confirmed_transfer_links: {links}",
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
