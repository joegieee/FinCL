from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from .association import adopt_ledger
from .backup import backup_ledger, restore_ledger
from .categorization import (
    canonical_amount_band,
    canonical_match,
    rule_id_for,
    validate_alias_patterns,
)
from .common import resolve_category
from .db import connect_sqlite, initialize_schema
from .decision_journal import append_decision, default_journal_path
from .decisions import entry_anchor, invariant_violation_is_live, replay_journal, resolve_anchor
from .document_service import DocumentImportService
from .engine import categorize as run_categorize
from .engine import summarize_run
from .errors import FinCLError
from .fx import load_fx_rates_file, load_mas_weekly_csv_file
from .locking import ledger_writer
from .migrations import apply_pending_migrations
from .money import ExactMoney
from .rebuild import rebuild_database
from .reconciliation import link_narrow_transfers
from .recovery import recover_extraction
from .refresh import refresh_ledger
from .report import (
    build_coverage_report,
    build_economic_flow_report,
    build_profile_report,
    build_report,
    coverage_report_data,
    economic_flow_report_data,
    json_report,
    latest_report_data,
    profile_report_data,
)
from .service import FinCLService

app = typer.Typer(
    name="fincl",
    help="Local evidence-backed financial statement intake.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


@contextmanager
def _ledger(database: Path) -> Iterator[sqlite3.Connection]:
    """A write session: the ledger writer lock plus an initialized connection.

    Everything a CLI write command does — validate current state, append to the
    journal, refresh the projection — happens inside one of these so it
    serializes against every other writer and against a backup (lock order:
    ledger lock -> journal file lock -> SQLite transaction).
    """
    with ledger_writer(database):
        connection = connect_sqlite(database)
        try:
            initialize_schema(connection)
            yield connection
        finally:
            connection.close()


review_app = typer.Typer(help="Review and record durable transfer decisions.")
app.add_typer(review_app, name="review")
rules_app = typer.Typer(help="Author and inspect categorization rules and merchant aliases.")
app.add_typer(rules_app, name="rules")
duplicates_app = typer.Typer(help="Review and record manual duplicate-transaction decisions.")
app.add_typer(duplicates_app, name="duplicates")
refunds_app = typer.Typer(help="Record and inspect refund allocations against purchases.")
app.add_typer(refunds_app, name="refunds")
debt_app = typer.Typer(help="Open and inspect personal debts and shared expenses (v0.3).")
app.add_typer(debt_app, name="debt")
console = Console()
error_console = Console(stderr=True)


@app.command("import-revolut")
def import_revolut(
    source: Path,
    database: Path = typer.Option(..., "--database"),
    evidence_dir: Path = typer.Option(..., "--evidence-dir"),
    plain: bool = typer.Option(False, "--plain", help="Emit stable plain text."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Capture and extract one Revolut CSV."""
    try:
        result = FinCLService.plaintext_phase1(database, evidence_dir).import_revolut(source)
        data = {
            "status": result.status,
            "import_run": result.import_run_id,
            "extraction_run": result.extraction_run_id,
            "evidence_sha256": result.evidence_sha256,
            "evidence_reused": result.evidence_reused,
            "extraction_reused": result.extraction_reused,
            "raw_records": result.raw_record_count,
            "ledger_entries": result.ledger_entry_count,
            "blocking_issues": result.blocking_issue_count,
            "warning_issues": result.warning_issue_count,
        }
        _emit_mapping(data, plain, json_output)
        if result.status != "ACCEPTED":
            raise typer.Exit(2)
    except (FinCLError, OSError) as exc:
        _fail(exc)


@app.command("import-dbs")
def import_dbs(
    source: Path,
    database: Path = typer.Option(..., "--database"),
    evidence_dir: Path = typer.Option(..., "--evidence-dir"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture and extract one DBS consolidated statement."""
    _import_document(source, database, evidence_dir, "DBS", plain, json_output)


@app.command("import-paylah")
def import_paylah(
    source: Path,
    database: Path = typer.Option(..., "--database"),
    evidence_dir: Path = typer.Option(..., "--evidence-dir"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture and extract one DBS PayLah! statement."""
    _import_document(source, database, evidence_dir, "PAYLAH", plain, json_output)


@app.command("import-maribank")
def import_maribank(
    source: Path,
    database: Path = typer.Option(..., "--database"),
    evidence_dir: Path = typer.Option(..., "--evidence-dir"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture and extract one MariBank deposit-and-investment savings statement."""
    _import_document(source, database, evidence_dir, "MARI_SAVINGS", plain, json_output)


@app.command("import-mari-card")
def import_mari_card(
    source: Path,
    database: Path = typer.Option(..., "--database"),
    evidence_dir: Path = typer.Option(..., "--evidence-dir"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture and extract one Mari credit-card e-Statement."""
    _import_document(source, database, evidence_dir, "MARI_CARD", plain, json_output)


_NO_REFRESH_HELP = (
    "Skip the implicit refresh and read whatever the last preparation committed."
)


def _prepare_for_query(database: Path, no_refresh: bool) -> None:
    """Explicit preparation before a CLI query (plan M1a, application contract §4)."""
    if no_refresh:
        return
    refresh_ledger(database)


@app.command("refresh")
def refresh_command(
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Rebuild the disposable projection the read surface serves."""
    try:
        result = refresh_ledger(database)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    _emit_mapping(
        {
            "status": "REFRESHED",
            "generation": result.generation_token,
            "decisions_bound": result.decisions_bound,
            "decisions_dangling": result.decisions_dangling,
            "confirmed_transfer_links": result.confirmed_transfer_links,
            "categorization_run": result.categorization_run_id or "none",
            "categorized_entries": result.categorized_entries,
        },
        plain,
        json_output,
    )
    if result.decisions_dangling > 0:
        error_console.print(
            f"[yellow]fincl:[/yellow] {result.decisions_dangling} journaled decision(s) "
            "no longer bind to a live anchor"
        )
        raise typer.Exit(3)


@app.command("report")
def report(
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """Show the latest extraction report."""
    try:
        _prepare_for_query(database, no_refresh)
        if json_output:
            typer.echo(json_report(latest_report_data(database)))
        elif plain:
            typer.echo(build_report(database))
        else:
            _emit_mapping(latest_report_data(database), False, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


@app.command("coverage")
def coverage(
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """Show statement coverage and selection status."""
    try:
        _prepare_for_query(database, no_refresh)
        data = coverage_report_data(database)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report(data))
    elif plain:
        typer.echo(build_coverage_report(database))
    else:
        table = Table(title="Statement coverage")
        for heading in ("Source", "Statement", "Period", "Status", "Selection", "Pages", "Rows"):
            table.add_column(heading)
        for statement in data["statements"]:
            table.add_row(
                str(statement["source_type"]),
                str(statement["statement_date"]),
                f"{statement['period_start']} - {statement['period_end']}",
                str(statement["status"]),
                str(statement["selection_state"]),
                str(statement["pages"]),
                str(statement["transaction_rows"]),
            )
        console.print(table)
        covered = ", ".join(
            f"{interval['start']}..{interval['end']}" for interval in data["covered_intervals"]
        )
        console.print(
            f"Coverage status: {data['coverage_status']} [{covered or 'none'}] "
            f"(required: {', '.join(data['required_sources']) or 'none'})"
        )
        console.print(f"Confirmed transfer links: {data['confirmed_transfer_links']}")
        console.print(f"Dangling journal decisions: {data['dangling_decisions']}")
        stale = data["needs_reextraction"]
        if stale:
            console.print(
                f"[yellow]Evidence needing re-extraction:[/yellow] {len(stale)} "
                "(only stale-version extraction runs; not in analytics)"
            )
            for blob in stale:
                console.print(f"  {blob['original_path']} ({blob['profiles']})")
        _print_category_warnings(data["categories"])


@app.command("economic-flow")
def economic_flow(
    database: Path = typer.Option(..., "--database"),
    start: str | None = typer.Option(None, "--start", help="Analysis start date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, "--end", help="Analysis end date (YYYY-MM-DD)."),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """Show exact per-currency external economic flow and source coverage."""
    try:
        _prepare_for_query(database, no_refresh)
        data = economic_flow_report_data(database, start=start, end=end)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report(data))
    elif plain:
        typer.echo(build_economic_flow_report(database, start=start, end=end))
    else:
        table = Table(title="Economic flow (per currency)")
        for heading in (
            "Currency", "Spending", "Income", "Refunds", "Reimb.", "Non-income in",
            "Pending review", "Other", "Fees", "Net", "Excl. links", "Covered",
        ):
            table.add_column(heading)
        for currency in data["currencies"]:
            table.add_row(
                str(currency["currency"]), str(currency["spending"]), str(currency["income"]),
                str(currency["refunds"]), str(currency["reimbursements"]),
                str(currency["non_income_inflow"]),
                str(currency["pending_review"]), str(currency["other_classified_inbound"]),
                str(currency["fees"]), str(currency["net_economic_flow"]),
                str(currency["excluded_confirmed_internal_entries"]),
                str(currency["coverage_complete"]),
            )
        console.print(table)
        if data["consolidated_sgd_total"] is None:
            console.print(
                f"Consolidated SGD total: unavailable ({data['consolidated_sgd_total_status']})."
            )
        else:
            console.print(f"Consolidated SGD total: {data['consolidated_sgd_total']} SGD.")
        console.print(f"Coverage complete: {data['coverage_complete']}")
        covered = ", ".join(
            f"{interval['start']}..{interval['end']}" for interval in data["covered_intervals"]
        )
        console.print(
            f"Coverage status: {data['coverage_status']} [{covered or 'none'}]"
        )
        for reason in data["coverage_status_reasons"]:
            console.print(f"  - {reason}")
        if data["excluded_entry_count"]:
            console.print(
                f"Entries excluded from analysis: {data['excluded_entry_count']} "
                f"(period-precision: {data['period_precision_excluded']})"
            )
        console.print(
            f"Analysis window: {data['analysis_window']} ({data['analysis_window_kind']})"
        )
        console.print(f"Dangling journal decisions: {data['dangling_decisions']}")


@app.command("contributors")
def contributors(
    database: Path = typer.Option(..., "--database"),
    bucket: str = typer.Option(
        "spending", "--bucket",
        help="Economic-flow bucket to inspect (spending, refunds, fees, income, …).",
    ),
    start: str | None = typer.Option(None, "--start", help="Analysis start date (YYYY-MM-DD)."),
    end: str | None = typer.Option(None, "--end", help="Analysis end date (YYYY-MM-DD)."),
    currency: str = typer.Option("SGD", "--currency"),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """List the exact economic components behind a bucket total.

    Reads the same shared projection the economic-flow report folds into its
    totals, so a component sum here reconciles exactly with that report.
    """
    from .analysis.components import build_components
    from .money import format_decimal
    from .report import _analysis_intervals, _coverage_contract

    try:
        _prepare_for_query(database, no_refresh)
        connection = connect_sqlite(database)
        try:
            covered = _coverage_contract(connection).covered_intervals
            analysis, _kind = _analysis_intervals(covered, start=start, end=end)
            projection = build_components(
                connection, covered_intervals=covered, analysis_intervals=analysis
            )
        finally:
            connection.close()
    except (FinCLError, OSError) as exc:
        _fail(exc)

    rows = [
        c for c in projection.components
        if c.currency == currency and (c.nets if bucket == "net" else c.bucket == bucket)
    ]
    rows.sort(key=lambda c: (-abs(c.coefficient), c.origin_anchor))
    if json_output:
        typer.echo(json_report({
            "bucket": bucket, "currency": currency, "count": len(rows),
            "components": [
                {
                    "origin_component_id": c.origin_component_id,
                    "entry_anchor": c.origin_anchor, "role": c.role, "bucket": c.bucket,
                    "value": format_decimal(c.coefficient, c.scale),
                    "currency": c.currency, "category": c.category_key,
                    "merchant": c.merchant_display, "date": c.represented_start,
                    "precision": c.precision, "nets": c.nets,
                }
                for c in rows
            ],
        }))
        return
    table = Table(title=f"Contributors — {bucket} ({currency})")
    for heading in ("Date", "Merchant", "Category", "Role", "Value", "Nets"):
        table.add_column(heading)
    for c in rows:
        table.add_row(
            c.represented_start, c.merchant_display, c.category_key, c.role,
            format_decimal(c.coefficient, c.scale), "yes" if c.nets else "no",
        )
    console.print(table)
    console.print(f"{len(rows)} component(s).")


def _record_duplicate(database: Path, kind: str, primary: str, candidate: str, reason: str) -> None:
    import hashlib
    import uuid as _uuid

    from .application import record_decision
    from .common import canonical_json
    from .relationships import validate_duplicate

    try:
        anchors = {"primary": primary, "candidate": candidate}
        payload: dict[str, object] = {"reason": reason.strip()}
        validate_duplicate(kind, anchors, payload)
        with _ledger(database) as connection:
            for role, anchor in anchors.items():
                if resolve_anchor(connection, anchor) is None:
                    raise FinCLError(f"the {role} transaction does not resolve")
        request_id = str(_uuid.uuid4())
        intent = hashlib.sha256(
            canonical_json({"kind": kind, "anchors": anchors, "payload": payload}).encode()
        ).hexdigest()
        receipt = record_decision(
            database, request_id=request_id, intent_hash=intent, decision_kind=kind,
            anchors=anchors, payload=payload, policy_version="dedupe-v1",
            snapshot=f"{kind}: {reason.strip()}",
        )
        _emit_mapping(
            {"seq": receipt.journal_seq, "record_hash": receipt.record_hash,
             "status": "recorded_refresh_pending" if receipt.refresh_pending else "applied"},
            True, False,
        )
    except (FinCLError, OSError) as exc:
        _fail(exc)


@duplicates_app.command("list")
def duplicates_list(
    database: Path = typer.Option(..., "--database"),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """Automatic duplicate proposals across distinct imports awaiting a decision."""
    from .queries import relationships_review

    try:
        _prepare_for_query(database, no_refresh)
        data = relationships_review(database, kind="duplicate", limit=200)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report(data))
        return
    if not data["proposals"]:
        console.print("No cross-import duplicate proposals.")
        return
    table = Table(title=f"Duplicate proposals ({data['total']})")
    for heading in ("Primary", "Candidate", "Account", "Date", "Amount", "Merchant"):
        table.add_column(heading)
    for proposal in data["proposals"]:
        table.add_row(
            proposal["primary"], proposal["candidate"], proposal["account"],
            proposal["date"], f"{proposal['amount']} {proposal['currency']}", proposal["merchant"],
        )
    console.print(table)


@duplicates_app.command("confirm")
def duplicates_confirm(
    primary: str = typer.Option(..., "--primary"),
    candidate: str = typer.Option(..., "--candidate"),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Record that ``candidate`` is the same transaction as ``primary`` (suppresses it)."""
    _record_duplicate(database, "DUPLICATE_CONFIRM", primary, candidate, reason)


@duplicates_app.command("reject")
def duplicates_reject(
    primary: str = typer.Option(..., "--primary"),
    candidate: str = typer.Option(..., "--candidate"),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Record that the pair are distinct real purchases."""
    _record_duplicate(database, "DUPLICATE_REJECT", primary, candidate, reason)


@duplicates_app.command("retire")
def duplicates_retire(
    primary: str = typer.Option(..., "--primary"),
    candidate: str = typer.Option(..., "--candidate"),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Remove a prior confirm/reject so the pair can be proposed again."""
    _record_duplicate(database, "DUPLICATE_RETIRE", primary, candidate, reason)


@refunds_app.command("list")
def refunds_list(
    database: Path = typer.Option(..., "--database"),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """Recorded refund allocations (refund anchor -> purchase anchor, exact amounts)."""
    try:
        _prepare_for_query(database, no_refresh)
        with _ledger(database) as connection:
            rows = [
                dict(row) for row in connection.execute(
                    "SELECT refund_anchor, purchase_anchor, currency, "
                    "printf('%s', refund_coefficient / power(10.0, refund_scale)) AS refund_amount, "
                    "printf('%s', purchase_coefficient / power(10.0, purchase_scale)) AS purchase_amount "
                    "FROM refund_allocations ORDER BY refund_anchor, purchase_anchor"
                )
            ]
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report({"allocations": rows}))
        return
    if not rows:
        console.print("No refund allocations recorded.")
        return
    table = Table(title=f"Refund allocations ({len(rows)})")
    for heading in ("Refund", "Purchase", "Refund amt", "Purchase amt", "Currency"):
        table.add_column(heading)
    for row in rows:
        table.add_row(row["refund_anchor"], row["purchase_anchor"],
                      str(row["refund_amount"]), str(row["purchase_amount"]), row["currency"])
    console.print(table)


@refunds_app.command("set")
def refunds_set(
    refund: str = typer.Option(..., "--refund"),
    allocation: list[str] = typer.Option(
        ..., "--allocation",
        help="purchase_anchor,refund_amount,purchase_amount (repeatable)",
    ),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Replace every allocation for one refund (REFUND_ALLOCATION_SET)."""
    import hashlib
    import uuid as _uuid

    from .application import record_refund_allocation
    from .common import canonical_json

    try:
        allocations: list[dict[str, object]] = []
        for spec in allocation:
            parts = spec.split(",")
            if len(parts) != 3:
                raise FinCLError(
                    "each --allocation is purchase_anchor,refund_amount,purchase_amount"
                )
            allocations.append({
                "purchase_anchor": parts[0], "refund_amount": parts[1],
                "purchase_amount": parts[2],
            })
        request_id = str(_uuid.uuid4())
        payload = {"allocations": allocations, "reason": reason.strip()}
        intent = hashlib.sha256(
            canonical_json({"kind": "REFUND_ALLOCATION_SET",
                            "anchors": {"refund": refund}, "payload": payload}).encode()
        ).hexdigest()
        receipt = record_refund_allocation(
            database, request_id=request_id, intent_hash=intent, refund_anchor=refund,
            allocations=allocations, reason=reason.strip(),
        )
        _emit_mapping(
            {"seq": receipt.journal_seq, "record_hash": receipt.record_hash,
             "status": "recorded_refresh_pending" if receipt.refresh_pending else "applied"},
            True, False,
        )
    except (FinCLError, OSError) as exc:
        _fail(exc)


@refunds_app.command("retire")
def refunds_retire(
    refund: str = typer.Option(..., "--refund"),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Remove every allocation for one refund (REFUND_ALLOCATION_RETIRE)."""
    _record_simple_relationship(
        database, "REFUND_ALLOCATION_RETIRE", {"refund": refund}, reason, "refund-v1",
    )


@refunds_app.command("reject")
def refunds_reject(
    refund: str = typer.Option(..., "--refund"),
    purchase: str = typer.Option(..., "--purchase"),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Suppress one automatic (refund, purchase) proposal (REFUND_LINK_REJECT)."""
    _record_simple_relationship(
        database, "REFUND_LINK_REJECT", {"refund": refund, "purchase": purchase},
        reason, "refund-v1",
    )


def _record_simple_relationship(
    database: Path, kind: str, anchors: dict[str, str], reason: str, policy: str,
) -> None:
    import hashlib
    import uuid as _uuid

    from .application import record_decision
    from .common import canonical_json

    try:
        payload: dict[str, object] = {"reason": reason.strip()}
        request_id = str(_uuid.uuid4())
        intent = hashlib.sha256(
            canonical_json({"kind": kind, "anchors": anchors, "payload": payload}).encode()
        ).hexdigest()
        receipt = record_decision(
            database, request_id=request_id, intent_hash=intent, decision_kind=kind,
            anchors=anchors, payload=payload, policy_version=policy,
            snapshot=f"{kind}: {reason.strip()}",
        )
        _emit_mapping(
            {"seq": receipt.journal_seq, "record_hash": receipt.record_hash,
             "status": "recorded_refresh_pending" if receipt.refresh_pending else "applied"},
            True, False,
        )
    except (FinCLError, OSError) as exc:
        _fail(exc)


@app.command("link-transfers")
def link_transfers(database: Path = typer.Option(..., "--database")) -> None:
    """Run only the narrow deterministic Phase 2 transfer linkers."""
    try:
        with _ledger(database) as connection:
            created = link_narrow_transfers(connection)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    console.print(f"Created {created} confirmed transfer link(s).")


@app.command("import-fx-rates")
def import_fx_rates(
    file: Path = typer.Option(..., "--file", help="JSON rate document, or a MAS weekly CSV with --mas-weekly-csv."),
    database: Path = typer.Option(..., "--database"),
    mas_weekly_csv: bool = typer.Option(
        False,
        "--mas-weekly-csv",
        help="Parse a MAS 'Exchange Rates (Weekly)' CSV, reducing each month to its last end-of-period value.",
    ),
    source_version: str = typer.Option(
        "",
        "--source-version",
        help="Immutable fx_rates version label; required with --mas-weekly-csv (e.g. mas-weekly-2026-09).",
    ),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Load reference FX rates used to value cross-currency flow into SGD."""
    try:
        with _ledger(database) as connection:
            if mas_weekly_csv:
                if not source_version.strip():
                    raise FinCLError("--source-version is required with --mas-weekly-csv")
                written = load_mas_weekly_csv_file(
                    connection, file, source_version=source_version.strip()
                )
            else:
                written = load_fx_rates_file(connection, file)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    _emit_mapping({"status": "LOADED", "rates_written": written}, plain, json_output)


@app.command("profile")
def profile(
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """Show repeatable observations about imported source profiles."""
    try:
        _prepare_for_query(database, no_refresh)
        data = profile_report_data(database)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report(data))
    elif plain:
        typer.echo(build_profile_report(database))
    else:
        table = Table(title="Extraction profiles")
        for heading in ("Profile", "Parser", "Policy", "Status", "Runs", "Raw", "Ledger"):
            table.add_column(heading)
        for item in data["profiles"]:
            table.add_row(
                str(item["source_profile_version"]),
                str(item["parser_version"]),
                str(item["extraction_policy_version"]),
                str(item["status"]),
                str(item["extractions"]),
                str(item["raw_records"]),
                str(item["ledger_entries"]),
            )
        console.print(table)
        _print_category_warnings(data["categories"])


def _print_category_warnings(categories: dict[str, Any]) -> None:
    if categories.get("status") == "NOT_RUN":
        console.print("[dim]Categories: not yet run (fincl categorize).[/dim]")
        return
    by_group = ", ".join(f"{g}: {n}" for g, n in sorted(categories["by_group"].items()))
    console.print(f"Categories — {by_group}")
    warnings = categories.get("warnings", {})
    if warnings:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(warnings.items()))
        console.print(f"[yellow]Category warnings:[/yellow] {detail}")


@app.command("rebuild")
def rebuild(
    database: Path = typer.Option(..., "--database", help="Existing ledger to rebuild from."),
    evidence_dir: Path = typer.Option(..., "--evidence-dir", help="Verified local evidence vault."),
    output: Path = typer.Option(..., "--output", help="New database path; must not exist."),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Recreate a new ledger database from verified evidence."""
    try:
        result = rebuild_database(database, evidence_dir, output)
        _emit_mapping(
            {
                "status": "REBUILT",
                "database": result.database,
                "sources_rebuilt": result.sources_rebuilt,
                "accepted_extractions": result.accepted_extractions,
                "quarantined_extractions": result.quarantined_extractions,
                "decisions_bound": result.decisions_bound,
                "decisions_dangling": result.decisions_dangling,
            },
            plain,
            json_output,
        )
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if result.decisions_dangling > 0:
        error_console.print(
            f"[yellow]fincl:[/yellow] {result.decisions_dangling} journaled decision(s) "
            "no longer bind to a live anchor"
        )
        raise typer.Exit(3)


@app.command("adopt-ledger")
def adopt_ledger_command(
    database: Path = typer.Option(..., "--database", help="Existing ledger to bind to its journal."),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Bind a legacy ledger to its decision journal (F24 journal association)."""
    try:
        with _ledger(database) as connection:
            association = adopt_ledger(connection, Path(database))
            replay_journal(connection)
        _emit_mapping(
            {
                "status": "ADOPTED",
                "ledger_id": association.ledger_id,
                "journal_id": association.journal_id,
                "journal_relative_path": association.journal_relative_path,
                "verified_seq": association.verified_seq,
                "verified_hash": association.verified_hash,
            },
            plain,
            json_output,
        )
    except (FinCLError, OSError) as exc:
        _fail(exc)


@app.command("backup")
def backup_command(
    database: Path = typer.Option(..., "--database"),
    evidence_dir: Path = typer.Option(..., "--evidence-dir"),
    output: Path = typer.Option(..., "--output", help="New bundle directory; must not exist."),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture a verified backup bundle (database, journal, evidence, manifest)."""
    try:
        result = backup_ledger(database, evidence_dir, output)
        _emit_mapping(
            {
                "status": "BACKED_UP",
                "location": result.location,
                "ledger_id": result.ledger_id,
                "evidence_objects": result.evidence_objects,
                "journal_seq": result.journal_seq,
            },
            plain,
            json_output,
        )
    except (FinCLError, OSError) as exc:
        _fail(exc)


@app.command("restore")
def restore_command(
    backup: Path = typer.Option(..., "--backup", help="Bundle produced by `fincl backup`."),
    output: Path = typer.Option(..., "--output", help="New ledger directory; must not exist."),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify a backup bundle and restore it into a new directory."""
    try:
        result = restore_ledger(backup, output)
        _emit_mapping(
            {
                "status": "RESTORED",
                "location": result.location,
                "ledger_id": result.ledger_id,
                "evidence_objects": result.evidence_objects,
            },
            plain,
            json_output,
        )
    except (FinCLError, OSError) as exc:
        _fail(exc)


@app.command("migrate")
def migrate_command(
    database: Path = typer.Option(..., "--database"),
    evidence_dir: Path = typer.Option(..., "--evidence-dir"),
    backup_output: Path = typer.Option(
        ..., "--backup-output", help="New verified backup bundle directory; must not exist."
    ),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Upgrade the ledger schema after capturing and verifying a fresh backup."""
    try:
        with ledger_writer(database) as writer:
            backup_ledger(
                database, evidence_dir, backup_output,
                writer=writer, allow_stale_schema=True,
            )
            result = apply_pending_migrations(
                database, backup_dir=backup_output, writer=writer
            )
    except (FinCLError, OSError) as exc:
        _fail(exc)
    _emit_mapping(
        {
            "status": "MIGRATED" if result.steps else "ALREADY_CURRENT",
            "from_version": result.from_version,
            "to_version": result.to_version,
            "steps": ",".join(str(step) for step in result.steps) or "none",
            "verified_backup": str(backup_output),
        },
        plain,
        json_output,
    )


@app.command("repair")
def repair_command(
    extraction_run: str,
    database: Path = typer.Option(..., "--database"),
    reason: str = typer.Option(..., "--reason"),
    allow_running: bool = typer.Option(False, "--allow-running"),
) -> None:
    """Repair a FAILED or explicitly acknowledged stale RUNNING generation."""
    try:
        result = recover_extraction(database, extraction_run, reason, allow_running=allow_running)
        _emit_recovery_result(result, "REPAIRED")
    except (FinCLError, OSError) as exc:
        _fail(exc)


@app.command("recover-extraction", hidden=True)
def recover_extraction_command(
    extraction_run: str,
    database: Path = typer.Option(..., "--database"),
    reason: str = typer.Option(..., "--reason"),
    allow_running: bool = typer.Option(False, "--allow-running"),
) -> None:
    """Compatibility alias for ``repair``."""
    try:
        result = recover_extraction(database, extraction_run, reason, allow_running=allow_running)
        _emit_recovery_result(result, "RECOVERED")
    except (FinCLError, OSError) as exc:
        _fail(exc)


@review_app.command("list")
def review_list(
    database: Path = typer.Option(..., "--database"),
    kind: str = typer.Option("link", "--kind"),
    json_output: bool = typer.Option(False, "--json"),
    plain: bool = typer.Option(False, "--plain"),
) -> None:
    """List proposed links and the current durable decision projection."""
    try:
        if kind == "uncategorized":
            _review_list_uncategorized(database, plain, json_output)
            return
        if kind not in {"link", "escape", "inbound"}:
            raise FinCLError("review kind must be link, escape, inbound, or uncategorized")
        table = {"link": "link_decisions", "escape": "invariant_escapes", "inbound": "inbound_classifications"}[kind]
        with _ledger(database) as connection:
            link_narrow_transfers(connection)
            decisions = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY journal_seq")]
            proposals = []
            if kind == "link":
                proposals = [dict(row) for row in connection.execute(
                    "SELECT * FROM transfer_links WHERE status='PROPOSED' ORDER BY created_at"
                )]
                for proposal in proposals:
                    proposal["left_anchor"] = entry_anchor(
                        connection, str(proposal["left_entry_id"])
                    )
                    proposal["right_anchor"] = entry_anchor(
                        connection, str(proposal["right_entry_id"])
                    )
        _emit_mapping({"kind": kind, "proposals": proposals, "decisions": decisions}, plain, json_output)
    except (FinCLError, OSError) as exc:
        _fail(exc)


def _review_list_uncategorized(database: Path, plain: bool, json_output: bool) -> None:
    with _ledger(database) as connection:
        run_categorize(connection)
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT e.natural_anchor AS anchor, ec.precedence_tier, ec.basis,
                       ec.ambiguous, ec.direction_conflict,
                       o.merchant_normalized, o.merchant_canonical, o.alias_ambiguous,
                       o.description_text, o.paynow_to
                FROM entry_categories ec
                JOIN ledger_entries e ON e.id = ec.entry_id
                LEFT JOIN merchant_observations o ON o.entry_id = ec.entry_id
                WHERE ec.precedence_tier = 6 OR ec.ambiguous = 1
                   OR ec.direction_conflict = 1 OR o.alias_ambiguous = 1
                ORDER BY e.natural_anchor
                """
            )
        ]
    _emit_mapping({"kind": "uncategorized", "entries": rows}, plain, json_output)


_LINK_KIND_HELP = (
    "SAME_CURRENCY_TRANSFER or CURRENCY_EXCHANGE. Required to confirm a pair "
    "automation never proposed; must match the proposal otherwise."
)


@review_app.command("confirm-link")
def review_confirm_link(
    left: str = typer.Option(..., "--left"), right: str = typer.Option(..., "--right"),
    reason: str = typer.Option(..., "--reason"), database: Path = typer.Option(..., "--database"),
    link_kind: str | None = typer.Option(None, "--link-kind", help=_LINK_KIND_HELP),
) -> None:
    _record_link_decision(database, "LINK_CONFIRM", left, right, reason, link_kind)


@review_app.command("reject-link")
def review_reject_link(
    left: str = typer.Option(..., "--left"), right: str = typer.Option(..., "--right"),
    reason: str = typer.Option(..., "--reason"), database: Path = typer.Option(..., "--database"),
    link_kind: str | None = typer.Option(None, "--link-kind", help=_LINK_KIND_HELP),
) -> None:
    _record_link_decision(database, "LINK_REJECT", left, right, reason, link_kind)


@review_app.command("escape-invariant")
def review_escape_invariant(
    code: str = typer.Option(..., "--code"), anchors: str = typer.Option(..., "--anchors"),
    reason: str = typer.Option(..., "--reason"), database: Path = typer.Option(..., "--database"),
) -> None:
    try:
        if not reason.strip():
            raise FinCLError("a non-empty reason is required")
        values = [value.strip() for value in anchors.split(",") if value.strip()]
        with _ledger(database) as connection:
            resolved = [resolve_anchor(connection, value) for value in values]
            if not values or any(entry_id is None for entry_id in resolved):
                raise FinCLError("all invariant escape anchors must currently resolve")
            if not invariant_violation_is_live(
                connection, code, [entry_id for entry_id in resolved if entry_id is not None]
            ):
                raise FinCLError(
                    f"no live BLOCKING violation of {code} is raised at those anchors; "
                    "an escape may only downgrade a currently-failing check"
                )
            record = append_decision(
                default_journal_path(database), decision_kind="INVARIANT_ESCAPE",
                anchors={f"anchor_{index}": value for index, value in enumerate(values)},
                payload={"code": code, "reason": reason.strip()}, policy_version="review-v1",
                snapshot=f"Invariant escape {code}: {reason.strip()}",
            )
            link_narrow_transfers(connection)
        _emit_mapping({"seq": record.seq, "record_hash": record.record_hash}, True, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


@review_app.command("classify-inbound")
def review_classify_inbound(
    anchor: str = typer.Option(..., "--anchor"), classification: str = typer.Option(..., "--as"),
    reason: str = typer.Option(..., "--reason"), database: Path = typer.Option(..., "--database"),
) -> None:
    allowed = {"income", "family_transfer", "gift", "reimbursement", "self_transfer", "other"}
    try:
        if classification not in allowed or not reason.strip():
            raise FinCLError("classification must be supported and reason must be non-empty")
        with _ledger(database) as connection:
            if resolve_anchor(connection, anchor) is None:
                raise FinCLError("the inbound anchor must currently resolve")
            record = append_decision(
                default_journal_path(database), decision_kind="INBOUND_CLASSIFICATION", anchors={"entry": anchor},
                payload={"classification": classification, "reason": reason.strip()}, policy_version="review-v1",
                snapshot=f"Inbound classification {classification}: {reason.strip()}",
            )
            link_narrow_transfers(connection)
        _emit_mapping({"seq": record.seq, "record_hash": record.record_hash}, True, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


_CATEGORY_POLICY = "category-engine-2026-v1"


@app.command("categorize")
def categorize_command(
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Recompute every entry's category and record a reproducible run manifest."""
    try:
        with _ledger(database) as connection:
            run_id = run_categorize(connection)
            summary = summarize_run(connection, run_id)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    _emit_mapping(summary, plain, json_output)


def _append_rule(database: Path, match: dict[str, Any], category: str, reason: str, dry_run: bool) -> None:
    canonical = canonical_match(match)
    resolve_category(category)  # refuse an unknown category before writing
    rule_id = rule_id_for(canonical)
    if dry_run:
        with _ledger(database) as connection:
            affected = _preview_rule(connection, canonical)
        _emit_mapping({"dry_run": True, "rule_id": rule_id, "would_match_entries": affected}, True, False)
        return
    with _ledger(database) as connection:
        record = append_decision(
            default_journal_path(database), decision_kind="CATEGORY_RULE_SET",
            anchors={"rule_id": rule_id},
            payload={"match": canonical, "category": category, "reason": reason.strip()},
            policy_version=_CATEGORY_POLICY,
            snapshot=f"Category rule {rule_id} -> {category}: {reason.strip()}",
        )
        run_categorize(connection)
    _emit_mapping({"seq": record.seq, "rule_id": rule_id, "record_hash": record.record_hash}, True, False)


def _preview_rule(connection: sqlite3.Connection, match: dict[str, Any]) -> int:
    """A conservative count of selected entries whose text the predicate matches."""
    key = next(k for k in ("merchant_canonical", "paynow_to", "description_prefix", "description_contains") if k in match)
    literal = match[key]
    if key == "merchant_canonical":
        sql = ("SELECT count(*) FROM merchant_observations "
               "WHERE merchant_normalized = ? OR merchant_canonical = ?")
        args: tuple[Any, ...] = (literal, literal)
    elif key == "paynow_to":
        sql, args = "SELECT count(*) FROM merchant_observations WHERE paynow_to = ?", (literal,)
    elif key == "description_prefix":
        sql = "SELECT count(*) FROM merchant_observations WHERE substr(description_text, 1, ?) = ?"
        args = (len(literal), literal)
    else:
        sql, args = "SELECT count(*) FROM merchant_observations WHERE instr(description_text, ?) > 0", (literal,)
    row = connection.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


@rules_app.command("add")
def rules_add(
    category: str = typer.Option(..., "--category"),
    merchant: str | None = typer.Option(None, "--merchant"),
    paynow_to: str | None = typer.Option(None, "--paynow-to"),
    description_prefix: str | None = typer.Option(None, "--description-prefix"),
    description_contains: str | None = typer.Option(None, "--description-contains"),
    account: str | None = typer.Option(None, "--account"),
    min_amount: str | None = typer.Option(None, "--min-amount"),
    max_amount: str | None = typer.Option(None, "--max-amount"),
    currency: str | None = typer.Option(None, "--currency"),
    reason: str = typer.Option("", "--reason"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Append a CATEGORY_RULE_SET and restate matching history."""
    try:
        predicates = {
            "merchant_canonical": merchant,
            "paynow_to": paynow_to,
            "description_prefix": description_prefix,
            "description_contains": description_contains,
        }
        chosen = {k: v for k, v in predicates.items() if v is not None}
        if len(chosen) != 1:
            raise FinCLError("pass exactly one of --merchant / --paynow-to / --description-prefix / --description-contains")
        match: dict[str, Any] = dict(chosen)
        if account is not None:
            match["account"] = account
        if min_amount is not None or max_amount is not None:
            if currency is None:
                raise FinCLError("--currency is required with --min-amount / --max-amount")
            band: dict[str, Any] = {"currency": currency.upper()}
            if min_amount is not None:
                parsed = ExactMoney.parse(min_amount)
                band["min"] = {"coefficient": parsed.coefficient, "scale": parsed.scale}
            if max_amount is not None:
                parsed = ExactMoney.parse(max_amount)
                band["max"] = {"coefficient": parsed.coefficient, "scale": parsed.scale}
            match["amount_band"] = canonical_amount_band(band)
        _append_rule(database, match, category, reason, dry_run)
    except (FinCLError, OSError, ValueError) as exc:
        _fail(exc)


@rules_app.command("alias")
def rules_alias(
    canonical: str = typer.Argument(...),
    pattern: list[str] = typer.Option(..., "--pattern", help="exact:LIT / prefix:LIT / regex:LIT, repeatable"),
    display: str | None = typer.Option(None, "--display"),
    reason: str = typer.Option("", "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Append a MERCHANT_ALIAS_SET carrying the full pattern list."""
    try:
        patterns: list[dict[str, str]] = []
        for spec in pattern:
            kind, _, literal = spec.partition(":")
            if kind not in {"exact", "prefix", "regex"} or not literal:
                raise FinCLError(f"pattern {spec!r} must be exact:LIT / prefix:LIT / regex:LIT")
            patterns.append({kind: literal})
        validate_alias_patterns(patterns)
        payload: dict[str, Any] = {"patterns": patterns, "reason": (reason or "alias").strip()}
        if display is not None:
            payload["display_name"] = display
        with _ledger(database) as connection:
            record = append_decision(
                default_journal_path(database), decision_kind="MERCHANT_ALIAS_SET",
                anchors={"canonical": canonical}, payload=payload, policy_version=_CATEGORY_POLICY,
                snapshot=f"Merchant alias {canonical}: {len(patterns)} pattern(s)",
            )
            run_categorize(connection)
        _emit_mapping({"seq": record.seq, "canonical": canonical, "record_hash": record.record_hash}, True, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


@rules_app.command("alias-retire")
def rules_alias_retire(
    canonical: str = typer.Argument(...),
    reason: str = typer.Option("retired", "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    _append_retire(database, "MERCHANT_ALIAS_RETIRE", {"canonical": canonical}, reason)


@rules_app.command("retire")
def rules_retire(
    rule_id: str = typer.Argument(...),
    reason: str = typer.Option("retired", "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    _append_retire(database, "CATEGORY_RULE_RETIRE", {"rule_id": rule_id}, reason)


def _append_retire(database: Path, kind: str, anchors: dict[str, str], reason: str) -> None:
    try:
        if not reason.strip():
            raise FinCLError("a non-empty reason is required")
        with _ledger(database) as connection:
            record = append_decision(
                default_journal_path(database), decision_kind=kind, anchors=anchors,
                payload={"reason": reason.strip()}, policy_version=_CATEGORY_POLICY,
                snapshot=f"{kind} {anchors}: {reason.strip()}",
            )
            run_categorize(connection)
        _emit_mapping({"seq": record.seq, "record_hash": record.record_hash}, True, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


@rules_app.command("list")
def rules_list(
    database: Path = typer.Option(..., "--database"),
    json_output: bool = typer.Option(False, "--json"),
    plain: bool = typer.Option(False, "--plain"),
) -> None:
    try:
        with _ledger(database) as connection:
            run_categorize(connection)
            rules = [
                {
                    "rule_id": row["rule_id"],
                    "match": row["match_json"],
                    "category": f"{row['group_name']} / {row['category']}",
                    "migrated_from": row["migrated_from"],
                    "match_count": connection.execute(
                        "SELECT count(*) FROM entry_categories WHERE basis = ? OR basis LIKE ?",
                        (f"rule:{row['rule_id']}", f"alias:%:{row['rule_id']}"),
                    ).fetchone()[0],
                }
                for row in connection.execute(
                    "SELECT * FROM category_rules ORDER BY group_name, category, rule_id"
                )
            ]
            aliases = [
                {"canonical": row["canonical"], "patterns": row["patterns_json"],
                 "display_name": row["display_name"]}
                for row in connection.execute("SELECT * FROM merchant_aliases ORDER BY canonical")
            ]
        _emit_mapping({"rules": rules, "aliases": aliases}, plain, json_output)
    except (FinCLError, OSError) as exc:
        _fail(exc)


@rules_app.command("rewrite-category")
def rules_rewrite_category(
    from_category: str = typer.Option(..., "--from"),
    to_category: str = typer.Option(..., "--to"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Re-issue every active rule / override that resolves through a migrated category."""
    try:
        resolve_category(to_category)
        with _ledger(database) as connection:
            run_categorize(connection)
            rules = connection.execute(
                "SELECT rule_id, match_json FROM category_rules WHERE migrated_from = ?",
                (from_category,),
            ).fetchall()
            overrides = connection.execute(
                "SELECT entry_anchor FROM category_overrides WHERE migrated_from = ?",
                (from_category,),
            ).fetchall()
            if dry_run:
                _emit_mapping(
                    {"dry_run": True, "rules": len(rules), "overrides": len(overrides)},
                    True, False,
                )
                return
            for row in rules:
                append_decision(
                    default_journal_path(database), decision_kind="CATEGORY_RULE_SET",
                    anchors={"rule_id": row["rule_id"]},
                    payload={"match": canonical_match(json.loads(row["match_json"])),
                             "category": to_category, "reason": f"rewrite from {from_category}"},
                    policy_version=_CATEGORY_POLICY,
                    snapshot=f"Rewrite rule {row['rule_id']} -> {to_category}",
                )
            for row in overrides:
                append_decision(
                    default_journal_path(database), decision_kind="CATEGORY_OVERRIDE",
                    anchors={"entry": row["entry_anchor"]},
                    payload={"category": to_category, "reason": f"rewrite from {from_category}"},
                    policy_version=_CATEGORY_POLICY,
                    snapshot=f"Rewrite override {row['entry_anchor']} -> {to_category}",
                )
            run_categorize(connection)
        _emit_mapping({"rewritten_rules": len(rules), "rewritten_overrides": len(overrides)}, True, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


@review_app.command("categorize")
def review_categorize(
    anchor: str = typer.Argument(...),
    category: str = typer.Option(..., "--as"),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
) -> None:
    """Append a CATEGORY_OVERRIDE — precedence tier 1, beats every rule."""
    try:
        if not reason.strip():
            raise FinCLError("a non-empty reason is required")
        resolve_category(category)
        with _ledger(database) as connection:
            if resolve_anchor(connection, anchor) is None:
                raise FinCLError("the entry anchor must currently resolve")
            record = append_decision(
                default_journal_path(database), decision_kind="CATEGORY_OVERRIDE",
                anchors={"entry": anchor},
                payload={"category": category, "reason": reason.strip()},
                policy_version=_CATEGORY_POLICY,
                snapshot=f"Category override {anchor} -> {category}: {reason.strip()}",
            )
            run_categorize(connection)
        _emit_mapping({"seq": record.seq, "record_hash": record.record_hash}, True, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


_LINK_KINDS = {"SAME_CURRENCY_TRANSFER", "CURRENCY_EXCHANGE"}


def _record_link_decision(
    database: Path, kind: str, left: str, right: str, reason: str,
    link_kind_option: str | None = None,
) -> None:
    try:
        if not reason.strip():
            raise FinCLError("a non-empty reason is required")
        if link_kind_option is not None and link_kind_option not in _LINK_KINDS:
            raise FinCLError("link kind must be SAME_CURRENCY_TRANSFER or CURRENCY_EXCHANGE")
        with _ledger(database) as connection:
            resolved_left = resolve_anchor(connection, left)
            resolved_right = resolve_anchor(connection, right)
            if resolved_left is None or resolved_right is None:
                raise FinCLError("both anchors must currently resolve")
            row = connection.execute(
                """SELECT method_version, link_kind FROM transfer_links
                   WHERE left_entry_id=? AND right_entry_id=?""",
                (resolved_left, resolved_right),
            ).fetchone()
            if row is not None:
                policy = row["method_version"]
                link_kind = row["link_kind"]
                if link_kind_option is not None and link_kind_option != link_kind:
                    raise FinCLError(
                        f"automation proposed this pair as {link_kind}; "
                        f"--link-kind {link_kind_option} conflicts"
                    )
            else:
                if kind == "LINK_CONFIRM" and link_kind_option is None:
                    raise FinCLError(
                        "no automatic proposal exists for this pair; pass "
                        "--link-kind SAME_CURRENCY_TRANSFER or --link-kind CURRENCY_EXCHANGE "
                        "to record a manual link"
                    )
                policy = "manual-v1"
                link_kind = link_kind_option or "SAME_CURRENCY_TRANSFER"
            record = append_decision(
                default_journal_path(database), decision_kind=kind, anchors={"left": left, "right": right},
                payload={"reason": reason.strip(), "link_kind": link_kind}, policy_version=policy,
                snapshot=f"{kind} for {left} and {right}: {reason.strip()}",
            )
            link_narrow_transfers(connection)
        _emit_mapping({"seq": record.seq, "record_hash": record.record_hash}, True, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


def _emit_recovery_result(result: Any, status: str) -> None:
    _emit_mapping(
        {
            "status": status,
            "recovery_event": result.event_id,
            "extraction_run": result.extraction_run_id,
            "evidence_sha256": result.evidence_sha256,
            "prior_status": result.prior_status,
        },
        plain=True,
        json_output=False,
    )


def _import_document(
    source: Path,
    database: Path,
    evidence_dir: Path,
    kind: str,
    plain: bool,
    json_output: bool,
) -> None:
    try:
        service = DocumentImportService.plaintext_phase2(database, evidence_dir)
        importer = {
            "DBS": service.import_dbs,
            "PAYLAH": service.import_paylah,
            "MARI_SAVINGS": service.import_maribank,
            "MARI_CARD": service.import_mari_card,
        }[kind]
        result = importer(source)
        data = {
            "status": result.status,
            "source_type": result.source_type,
            "statement_date": result.statement_date,
            "import_run": result.import_run_id,
            "extraction_run": result.extraction_run_id,
            "evidence_sha256": result.evidence_sha256,
            "evidence_reused": result.evidence_reused,
            "extraction_reused": result.extraction_reused,
            "transaction_rows": result.transaction_count,
            "document_records": result.document_record_count,
            "blocking_issues": result.blocking_issue_count,
            "warning_issues": result.warning_issue_count,
            "reconciliation": f"{result.reconciliation_passed}/{result.reconciliation_total}",
            "transfer_links_created": result.transfer_links_created,
        }
        _emit_mapping(data, plain, json_output)
        if result.status != "ACCEPTED":
            raise typer.Exit(2)
    except (FinCLError, OSError) as exc:
        _fail(exc)


def _emit_mapping(data: dict[str, Any], plain: bool, json_output: bool) -> None:
    if json_output:
        typer.echo(json_report(data))
        return
    if plain:
        typer.echo("\n".join(f"{key}: {value}" for key, value in data.items()))
        return
    table = Table(show_header=False, box=None)
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")
    for key, value in data.items():
        if isinstance(value, dict):
            value = ", ".join(f"{nested_key}={nested_value}" for nested_key, nested_value in sorted(value.items())) or "none"
        style = "green" if key == "status" and value == "ACCEPTED" else None
        table.add_row(key.replace("_", " "), str(value), style=style)
    console.print(table)


def _fail(exc: Exception) -> NoReturn:
    error_console.print(f"[red]fincl:[/red] {exc}")
    raise typer.Exit(1)


@app.command("serve")
def serve_command(
    database: Path = typer.Option(..., "--database"),
    port: int = typer.Option(8765, "--port", min=1024, max=65535),
    open_browser: bool = typer.Option(False, "--open"),
    evidence_dir: Path | None = typer.Option(None, "--evidence-dir"),
    demo: bool = typer.Option(False, "--demo", help="Label a synthetic demonstration ledger."),
) -> None:
    """Serve the bundled personal finance UI on authenticated loopback HTTP."""
    import secrets
    import webbrowser

    import uvicorn

    from .api import create_app
    from .db import connect_readonly

    try:
        # Refuse absent/old ledgers before any write or automatic migration.
        connection = connect_readonly(database)
        connection.close()
        assets = Path(__file__).parent / "static"
        if not (assets / "index.html").exists():
            raise FinCLError("UI assets are missing. Run npm ci and npm run build in frontend/.")
        refresh_ledger(database)
        secret = secrets.token_urlsafe(32)
        url = f"http://127.0.0.1:{port}/#bootstrap={secret}"
        console = Console()
        console.print("Open this private, one-use launch link:")
        console.print(url, markup=False, highlight=False, soft_wrap=True)
        if open_browser:
            webbrowser.open(url)
        uvicorn.run(create_app(database, port=port, bootstrap_secret=secret, evidence_dir=evidence_dir, demo=demo), host="127.0.0.1", port=port, access_log=False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


def _record_debt(
    database: Path, kind: str, anchors: dict[str, str], payload: dict[str, object],
    *, json_output: bool = False, plain: bool = False,
) -> None:
    import hashlib
    import uuid as _uuid

    from .application import record_debt_decision
    from .common import canonical_json

    try:
        request_id = str(_uuid.uuid4())
        intent = hashlib.sha256(
            canonical_json({"kind": kind, "anchors": anchors, "payload": payload}).encode()
        ).hexdigest()
        receipt = record_debt_decision(
            database, request_id=request_id, intent_hash=intent,
            decision_kind=kind, anchors=anchors, payload=payload,
        )
        _emit_mapping(
            {"seq": receipt.journal_seq, "record_hash": receipt.record_hash,
             "debt": anchors.get("debt", ""),
             "event": anchors.get("event", ""),
             "status": "recorded_refresh_pending" if receipt.refresh_pending else "applied"},
            plain, json_output,
        )
    except (FinCLError, OSError) as exc:
        _fail(exc)


@debt_app.command("open")
def debt_open(
    direction: str = typer.Option(..., "--direction", help="payable or receivable."),
    currency: str = typer.Option(..., "--currency"),
    counterparty: str = typer.Option(..., "--counterparty"),
    opened_on: str = typer.Option(..., "--opened-on", help="YYYY-MM-DD."),
    reason: str = typer.Option(..., "--reason"),
    due_on: str | None = typer.Option(None, "--due-on", help="YYYY-MM-DD (optional)."),
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Open a personal debt identity (PERSONAL_DEBT_OPEN); no principal is created."""
    import uuid as _uuid

    _record_debt(
        database, "PERSONAL_DEBT_OPEN", {"debt": str(_uuid.uuid4())},
        {
            "direction": direction.strip().upper(),
            "currency": currency.strip().upper(),
            "counterparty_label": counterparty.strip(),
            "opened_on": opened_on.strip(),
            "due_on": due_on.strip() if due_on else None,
            "reason": reason.strip(),
        },
        json_output=json_output, plain=plain,
    )


@debt_app.command("details")
def debt_details(
    debt: str = typer.Option(..., "--debt"),
    counterparty: str = typer.Option(..., "--counterparty"),
    reason: str = typer.Option(..., "--reason"),
    due_on: str | None = typer.Option(None, "--due-on"),
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Rename the counterparty or set the due date (PERSONAL_DEBT_DETAILS_SET)."""
    _record_debt(
        database, "PERSONAL_DEBT_DETAILS_SET", {"debt": debt},
        {
            "counterparty_label": counterparty.strip(),
            "due_on": due_on.strip() if due_on else None,
            "reason": reason.strip(),
        },
        json_output=json_output, plain=plain,
    )


def _parse_allocation(spec: str) -> dict[str, str]:
    """``anchor=principal[:interest[:fee[:separate_fee]]]`` -> allocation item.

    The anchor is separated with ``=`` because a natural anchor itself contains
    ``:``; the (up to four) amounts after it are ``:``-separated.
    """
    anchor, _, amounts = spec.partition("=")
    if not _ or not anchor.strip() or not amounts.strip():
        raise FinCLError(
            "an --allocation is anchor=principal[:interest[:fee[:separate_fee]]]"
        )
    values = [p.strip() for p in amounts.split(":")] + ["0.00", "0.00", "0.00"]
    return {
        "entry_anchor": anchor.strip(),
        "principal_amount": values[0],
        "interest_amount": values[1],
        "fee_amount": values[2],
        "separate_fee_amount": values[3],
    }


@debt_app.command("add-event")
def debt_add_event(
    debt: str = typer.Option(..., "--debt"),
    kind: str = typer.Option(..., "--kind", help="BORROWED / LENT / PRINCIPAL_REPAID / ..."),
    direction: str = typer.Option(..., "--direction", help="payable or receivable."),
    principal: str = typer.Option(..., "--principal", help="Exact amount in the debt currency."),
    effective_on: str = typer.Option(..., "--effective-on", help="YYYY-MM-DD."),
    reason: str = typer.Option(..., "--reason"),
    allocation: list[str] = typer.Option(
        [], "--allocation",
        help="Source split anchor=principal[:interest[:fee[:separate_fee]]] (repeatable).",
    ),
    expense_category: str | None = typer.Option(
        None, "--expense-category", help="Required for an EXPENSE_PAID_FOR_ME event."
    ),
    opening_balance: bool = typer.Option(
        False, "--opening-balance", help="Record a pre-existing obligation with no observed funding."
    ),
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Add a debt principal event (PERSONAL_DEBT_EVENT_SET).

    An opening balance and the off-ledger EXPENSE_PAID_FOR_ME carry no
    ``--allocation``; every other cash event needs one or more that sum to
    ``--principal``.
    """
    import uuid as _uuid

    try:
        allocations = [_parse_allocation(spec) for spec in allocation]
    except FinCLError as exc:
        _fail(exc)
        return

    _record_debt(
        database, "PERSONAL_DEBT_EVENT_SET",
        {"debt": debt, "event": str(_uuid.uuid4())},
        {
            "event_kind": kind.strip().upper(),
            "direction": direction.strip().upper(),
            "principal_amount": principal.strip(),
            "effective_on": effective_on.strip(),
            "entry_allocations": allocations,
            "opening_balance": bool(opening_balance),
            "expense_category": expense_category.strip() if expense_category else None,
            "reason": reason.strip(),
        },
        json_output=json_output, plain=plain,
    )


@debt_app.command("residual-set")
def debt_residual_set(
    entry: str = typer.Option(..., "--entry", help="The source transaction anchor."),
    expense: str = typer.Option(..., "--expense", help="The owner's own share, exact amount."),
    category: str = typer.Option(..., "--category", help="Expense category key for the owner share."),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Classify the source amount left after every debt allocation (ENTRY_RESIDUAL_SET)."""
    _record_debt(
        database, "ENTRY_RESIDUAL_SET", {"entry": entry.strip()},
        {"expense_amount": expense.strip(), "expense_category": category.strip(),
         "reason": reason.strip()},
        json_output=json_output, plain=plain,
    )


@debt_app.command("residual-retire")
def debt_residual_retire(
    entry: str = typer.Option(..., "--entry"),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Remove the owner-share residual for an entry (ENTRY_RESIDUAL_RETIRE)."""
    _record_debt(
        database, "ENTRY_RESIDUAL_RETIRE", {"entry": entry.strip()},
        {"reason": reason.strip()},
        json_output=json_output, plain=plain,
    )


@debt_app.command("retire-event")
def debt_retire_event(
    debt: str = typer.Option(..., "--debt"),
    event: str = typer.Option(..., "--event"),
    reason: str = typer.Option(..., "--reason"),
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Retire a debt event, keeping it in the timeline history (PERSONAL_DEBT_EVENT_RETIRE)."""
    _record_debt(
        database, "PERSONAL_DEBT_EVENT_RETIRE", {"debt": debt, "event": event},
        {"reason": reason.strip()},
        json_output=json_output, plain=plain,
    )


@debt_app.command("list")
def debt_list(
    database: Path = typer.Option(..., "--database"),
    as_of: str | None = typer.Option(None, "--as-of", help="YYYY-MM-DD; defaults to today."),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """List payables and receivables separately; they are never netted."""
    from .debts.queries import debts_page

    try:
        _prepare_for_query(database, no_refresh)
        data = debts_page(database, as_of=as_of, limit=200)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report(data))
        return
    for heading, rows in (("I owe", data["i_owe"]), ("Owed to me", data["owed_to_me"])):
        table = Table(title=f"{heading} — as of {data['as_of']}")
        for column in ("Debt", "Counterparty", "Currency", "Remaining", "Status", "Reconciliation"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                row["debt_id"], row["counterparty_label"], row["currency"],
                row["remaining_principal"]["value"], row["status"],
                row["reconciliation_status"],
            )
        console.print(table)


@debt_app.command("show")
def debt_show(
    debt: str = typer.Option(..., "--debt"),
    database: Path = typer.Option(..., "--database"),
    as_of: str | None = typer.Option(None, "--as-of"),
    json_output: bool = typer.Option(False, "--json"),
    no_refresh: bool = typer.Option(False, "--no-refresh", help=_NO_REFRESH_HELP),
) -> None:
    """Show one debt: identity, remaining principal and its dated event history."""
    from .debts.queries import debt_detail

    try:
        _prepare_for_query(database, no_refresh)
        data = debt_detail(database, debt, as_of=as_of)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report(data))
        return
    body = data["debt"]
    console.print(
        f"{body['counterparty_label']} — "
        f"{'I owe' if body['direction'] == 'PAYABLE' else 'owed to me'} "
        f"{body['remaining_principal']['value']} {body['currency']} "
        f"(as of {data['as_of']}, {body['status']})"
    )
    table = Table(title="Principal history")
    for column in ("Date", "Event", "Opening", "Principal", "Bound", "Reason"):
        table.add_column(column)
    for event in body["principal_history"]:
        table.add_row(
            event["effective_on"], event["event_kind"],
            "yes" if event["opening_balance"] else "",
            ("−" if event["signed"] < 0 else "+") + event["principal"]["value"],
            "yes" if event["bound"] else "no", event["reason"],
        )
    console.print(table)


def main() -> None:
    app()
