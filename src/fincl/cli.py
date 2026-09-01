from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from .db import connect_sqlite, initialize_schema
from .document_service import DocumentImportService, link_narrow_transfers
from .errors import FinCLError
from .recovery import recover_extraction
from .report import (
    build_coverage_report,
    build_report,
    coverage_report_data,
    json_report,
    latest_report_data,
)
from .service import FinCLService

app = typer.Typer(
    name="fincl",
    help="Local evidence-backed financial statement intake.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
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
            "canonical_entries": result.canonical_entry_count,
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


@app.command("report")
def report(
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the latest extraction report."""
    try:
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
) -> None:
    """Show statement coverage and selection status."""
    try:
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
        console.print(f"Confirmed transfer links: {data['confirmed_transfer_links']}")


@app.command("link-transfers")
def link_transfers(database: Path = typer.Option(..., "--database")) -> None:
    """Run only the narrow deterministic Phase 2 transfer linkers."""
    try:
        connection = connect_sqlite(database)
        initialize_schema(connection)
        created = link_narrow_transfers(connection)
        connection.close()
    except (FinCLError, OSError) as exc:
        _fail(exc)
    console.print(f"Created {created} confirmed transfer link(s).")


@app.command("repair")
def repair_command(
    extraction_run: str,
    database: Path = typer.Option(..., "--database"),
    reason: str = typer.Option(..., "--reason"),
    allow_running: bool = typer.Option(False, "--allow-running"),
    journal: Path | None = typer.Option(
        None,
        "--journal",
        help="Decision journal path; defaults beside the database.",
    ),
) -> None:
    """Repair a FAILED or explicitly acknowledged stale RUNNING generation."""
    try:
        result = recover_extraction(
            database, extraction_run, reason, allow_running=allow_running, journal_path=journal
        )
        _emit_recovery_result(result, "REPAIRED")
    except (FinCLError, OSError) as exc:
        _fail(exc)


@app.command("recover-extraction", hidden=True)
def recover_extraction_command(
    extraction_run: str,
    database: Path = typer.Option(..., "--database"),
    reason: str = typer.Option(..., "--reason"),
    allow_running: bool = typer.Option(False, "--allow-running"),
    journal: Path | None = typer.Option(None, "--journal"),
) -> None:
    """Compatibility alias for ``repair``."""
    try:
        result = recover_extraction(
            database, extraction_run, reason, allow_running=allow_running, journal_path=journal
        )
        _emit_recovery_result(result, "RECOVERED")
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
            "journal_seq": result.journal_seq,
            "journal_path": result.journal_path,
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
        result = service.import_dbs(source) if kind == "DBS" else service.import_paylah(source)
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


def main() -> None:
    app()
