from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from .db import connect_sqlite, initialize_schema
from .decision_journal import append_decision, default_journal_path
from .decisions import entry_anchor, resolve_anchor
from .document_service import DocumentImportService, link_narrow_transfers
from .errors import FinCLError
from .rebuild import rebuild_database
from .recovery import recover_extraction
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
review_app = typer.Typer(help="Review and record durable transfer decisions.")
app.add_typer(review_app, name="review")
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


@app.command("economic-flow")
def economic_flow(
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show exact per-currency external economic flow and source coverage."""
    try:
        data = economic_flow_report_data(database)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report(data))
    elif plain:
        typer.echo(build_economic_flow_report(database))
    else:
        table = Table(title="Economic flow (per currency)")
        for heading in ("Currency", "Spending", "Income", "Unclassified inbound", "Fees", "Net", "Excluded links"):
            table.add_column(heading)
        for currency in data["currencies"]:
            table.add_row(
                str(currency["currency"]), str(currency["spending"]), str(currency["income"]),
                str(currency["unclassified_inbound"]), str(currency["fees"]),
                str(currency["net_economic_flow"]), str(currency["excluded_confirmed_internal_entries"]),
            )
        console.print(table)
        console.print("Consolidated SGD total: unavailable until frozen FX rates are imported.")


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


@app.command("profile")
def profile(
    database: Path = typer.Option(..., "--database"),
    plain: bool = typer.Option(False, "--plain"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show repeatable observations about imported source profiles."""
    try:
        data = profile_report_data(database)
    except (FinCLError, OSError) as exc:
        _fail(exc)
    if json_output:
        typer.echo(json_report(data))
    elif plain:
        typer.echo(build_profile_report(database))
    else:
        table = Table(title="Extraction profiles")
        for heading in ("Profile", "Parser", "Policy", "Status", "Runs", "Raw", "Canonical"):
            table.add_column(heading)
        for item in data["profiles"]:
            table.add_row(
                str(item["source_profile_version"]),
                str(item["parser_version"]),
                str(item["extraction_policy_version"]),
                str(item["status"]),
                str(item["extractions"]),
                str(item["raw_records"]),
                str(item["canonical_entries"]),
            )
        console.print(table)


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
        connection = connect_sqlite(database)
        initialize_schema(connection)
        link_narrow_transfers(connection)
        if kind not in {"link", "escape", "inbound"}:
            raise FinCLError("review kind must be link, escape, or inbound")
        table = {"link": "link_decisions", "escape": "invariant_escapes", "inbound": "inbound_classifications"}[kind]
        decisions = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY journal_seq")]
        proposals = []
        if kind == "link":
            proposals = [dict(row) for row in connection.execute(
                "SELECT * FROM transfer_links WHERE status='PROPOSED' ORDER BY created_at"
            )]
            for proposal in proposals:
                proposal["left_anchor"] = entry_anchor(
                    connection, str(proposal["left_entry_kind"]), str(proposal["left_entry_id"])
                )
                proposal["right_anchor"] = entry_anchor(
                    connection, str(proposal["right_entry_kind"]), str(proposal["right_entry_id"])
                )
        connection.close()
        _emit_mapping({"kind": kind, "proposals": proposals, "decisions": decisions}, plain, json_output)
    except (FinCLError, OSError) as exc:
        _fail(exc)


@review_app.command("confirm-link")
def review_confirm_link(
    left: str = typer.Option(..., "--left"), right: str = typer.Option(..., "--right"),
    reason: str = typer.Option(..., "--reason"), database: Path = typer.Option(..., "--database"),
) -> None:
    _record_link_decision(database, "LINK_CONFIRM", left, right, reason)


@review_app.command("reject-link")
def review_reject_link(
    left: str = typer.Option(..., "--left"), right: str = typer.Option(..., "--right"),
    reason: str = typer.Option(..., "--reason"), database: Path = typer.Option(..., "--database"),
) -> None:
    _record_link_decision(database, "LINK_REJECT", left, right, reason)


@review_app.command("escape-invariant")
def review_escape_invariant(
    code: str = typer.Option(..., "--code"), anchors: str = typer.Option(..., "--anchors"),
    reason: str = typer.Option(..., "--reason"), database: Path = typer.Option(..., "--database"),
) -> None:
    try:
        if not reason.strip():
            raise FinCLError("a non-empty reason is required")
        values = [value.strip() for value in anchors.split(",") if value.strip()]
        connection = connect_sqlite(database)
        initialize_schema(connection)
        if not values or any(resolve_anchor(connection, value) is None for value in values):
            raise FinCLError("all invariant escape anchors must currently resolve")
        record = append_decision(
            default_journal_path(database), decision_kind="INVARIANT_ESCAPE",
            anchors={f"anchor_{index}": value for index, value in enumerate(values)},
            payload={"code": code, "reason": reason.strip()}, policy_version="review-v1",
            snapshot=f"Invariant escape {code}: {reason.strip()}",
        )
        link_narrow_transfers(connection)
        connection.close()
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
        connection = connect_sqlite(database)
        initialize_schema(connection)
        if resolve_anchor(connection, anchor) is None:
            raise FinCLError("the inbound anchor must currently resolve")
        record = append_decision(
            default_journal_path(database), decision_kind="INBOUND_CLASSIFICATION", anchors={"entry": anchor},
            payload={"classification": classification, "reason": reason.strip()}, policy_version="review-v1",
            snapshot=f"Inbound classification {classification}: {reason.strip()}",
        )
        link_narrow_transfers(connection)
        connection.close()
        _emit_mapping({"seq": record.seq, "record_hash": record.record_hash}, True, False)
    except (FinCLError, OSError) as exc:
        _fail(exc)


def _record_link_decision(database: Path, kind: str, left: str, right: str, reason: str) -> None:
    try:
        if not reason.strip():
            raise FinCLError("a non-empty reason is required")
        connection = connect_sqlite(database)
        initialize_schema(connection)
        resolved_left, resolved_right = resolve_anchor(connection, left), resolve_anchor(connection, right)
        if resolved_left is None or resolved_right is None:
            raise FinCLError("both anchors must currently resolve")
        row = connection.execute(
            """SELECT method_version, link_kind FROM transfer_links
               WHERE left_entry_kind=? AND left_entry_id=? AND right_entry_kind=? AND right_entry_id=?""",
            (resolved_left[0], resolved_left[1], resolved_right[0], resolved_right[1]),
        ).fetchone()
        policy = row["method_version"] if row is not None else "manual-v1"
        link_kind = row["link_kind"] if row is not None else "SAME_CURRENCY_TRANSFER"
        record = append_decision(
            default_journal_path(database), decision_kind=kind, anchors={"left": left, "right": right},
            payload={"reason": reason.strip(), "link_kind": link_kind}, policy_version=policy,
            snapshot=f"{kind} for {left} and {right}: {reason.strip()}",
        )
        link_narrow_transfers(connection)
        connection.close()
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
