from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .common import LEDGER_EVENT_KINDS
from .common import canonical_json as _json
from .common import stable_id as _stable_id
from .common import utc_now as _now
from .db import ConnectionFactory, connect_sqlite, initialize_schema
from .decisions import replay_journal
from .documents import (
    PDF_EXTRACTION_POLICY_VERSION,
    PDF_PARSER_VERSION,
    DocumentRecord,
    ParsedDocument,
    generic_extraction_identity,
    parse_dbs_consolidated,
    parse_mari_card,
    parse_maribank_savings,
    parse_paylah,
    parse_revolut_pdf,
    parse_trust_savings,
)
from .errors import EvidenceError, FinCLError
from .evidence import EvidenceCapture, EvidenceStore, PlaintextEvidenceStore
from .locking import WriterContext, ledger_writer
from .money import ExactMoney, format_decimal
from .reconciliation import (
    SELECTION_LOST,
    link_narrow_transfers,
    revoke_links_for_extraction,
)
from .selection import recompute_selection
from .supersession import containing_statement, supersede, supersede_contained, supersession_of

# Re-exported: importers historically reach these through document_service.
__all__ = [
    "DocumentImportResult",
    "DocumentImportService",
    "SELECTION_LOST",
    "link_narrow_transfers",
    "revoke_links_for_extraction",
]

_DOCUMENT_PARSERS = {
    "DBS_CONSOLIDATED": parse_dbs_consolidated,
    "DBS_PAYLAH": parse_paylah,
    "MARI_SAVINGS": parse_maribank_savings,
    "MARI_CARD": parse_mari_card,
    "TRUST_SAVINGS": parse_trust_savings,
    "REVOLUT_PDF": parse_revolut_pdf,
}


@dataclass(frozen=True, slots=True)
class DocumentImportResult:
    import_run_id: str
    extraction_run_id: str
    evidence_sha256: str
    evidence_reused: bool
    extraction_reused: bool
    status: str
    source_type: str
    statement_date: str
    transaction_count: int
    document_record_count: int
    blocking_issue_count: int
    warning_issue_count: int
    reconciliation_passed: int
    reconciliation_total: int
    transfer_links_created: int
    # Statement periods, as "start..end". `superseded_by` names the statement
    # that already carries every row of this one, so nothing new was counted;
    # `supersedes` names the statements this one replaced.
    superseded_by: str | None = None
    supersedes: tuple[str, ...] = ()
    account_id: str = ""
    account_display_name: str = ""
    period_start: str | None = None
    period_end: str | None = None


class DocumentImportService:
    def __init__(
        self,
        database_path: Path,
        evidence_store: EvidenceStore,
        connection_factory: ConnectionFactory = connect_sqlite,
    ):
        self.database_path = Path(database_path)
        self.evidence_store = evidence_store
        self.connection_factory = connection_factory

    @classmethod
    def plaintext_phase2(
        cls, database_path: Path, evidence_directory: Path
    ) -> DocumentImportService:
        return cls(database_path, PlaintextEvidenceStore(evidence_directory))

    def import_dbs(
        self, source_path: Path, *, writer: WriterContext | None = None,
        source_name: str | None = None,
    ) -> DocumentImportResult:
        return self._import(source_path, "DBS_CONSOLIDATED", writer=writer, source_name=source_name)

    def import_paylah(
        self, source_path: Path, *, writer: WriterContext | None = None,
        source_name: str | None = None,
    ) -> DocumentImportResult:
        return self._import(source_path, "DBS_PAYLAH", writer=writer, source_name=source_name)

    def import_maribank(
        self, source_path: Path, *, writer: WriterContext | None = None,
        source_name: str | None = None,
    ) -> DocumentImportResult:
        return self._import(source_path, "MARI_SAVINGS", writer=writer, source_name=source_name)

    def import_mari_card(
        self, source_path: Path, *, writer: WriterContext | None = None,
        source_name: str | None = None,
    ) -> DocumentImportResult:
        return self._import(source_path, "MARI_CARD", writer=writer, source_name=source_name)

    def import_trust(
        self, source_path: Path, *, writer: WriterContext | None = None,
        source_name: str | None = None,
    ) -> DocumentImportResult:
        return self._import(source_path, "TRUST_SAVINGS", writer=writer, source_name=source_name)

    def import_revolut_pdf(
        self, source_path: Path, *, writer: WriterContext | None = None,
        source_name: str | None = None,
    ) -> DocumentImportResult:
        return self._import(source_path, "REVOLUT_PDF", writer=writer, source_name=source_name)

    def _import(
        self, source_path: Path, source_type: str, *, writer: WriterContext | None = None,
        source_name: str | None = None,
    ) -> DocumentImportResult:
        """``source_name`` replaces the recorded path when ``source_path`` is only
        a spooled copy, so the import is recorded under the name it was given."""
        with ledger_writer(self.database_path, existing=writer):
            connection = self.connection_factory(self.database_path)
            try:
                return self._run_import(connection, source_path, source_type, source_name)
            finally:
                connection.close()

    def _run_import(
        self, connection: sqlite3.Connection, source_path: Path, source_type: str,
        source_name: str | None = None,
    ) -> DocumentImportResult:
        initialize_schema(connection)
        replay_journal(connection)
        import_run_id = "imp_" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO import_runs(id, started_at, status, source_path) VALUES (?, ?, 'RUNNING', ?)",
            (import_run_id, _now(), source_name or str(Path(source_path).resolve())),
        )
        connection.commit()
        try:
            capture = self.evidence_store.capture(Path(source_path))
            self._record_source(connection, capture, source_name or capture.original_path)
            with self.evidence_store.open_verified(capture.sha256, capture.evidence_key) as evidence:
                data = evidence.read()
            if hashlib.sha256(data).hexdigest() != capture.sha256:
                raise EvidenceError("bytes supplied to the PDF parser failed evidence verification")
            parsed = _DOCUMENT_PARSERS[source_type](data)
            extraction_run_id = generic_extraction_identity(
                capture.sha256, parsed.source_profile_version
            )
            existing = connection.execute(
                "SELECT * FROM extraction_runs WHERE id = ?", (extraction_run_id,)
            ).fetchone()
            if existing is not None:
                if existing["status"] == "RUNNING":
                    raise FinCLError("matching PDF extraction is already marked RUNNING")
                self._complete_import(
                    connection,
                    import_run_id,
                    capture.sha256,
                    extraction_run_id,
                    reused=True,
                )
                connection.commit()
                return self._load_result(
                    connection,
                    import_run_id,
                    extraction_run_id,
                    capture,
                    parsed,
                    extraction_reused=True,
                    links_created=0,
                )

            connection.execute(
                """
                INSERT INTO extraction_runs(
                    id, evidence_sha256, source_profile_version, parser_version,
                    extraction_policy_version, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'RUNNING')
                """,
                (
                    extraction_run_id,
                    capture.sha256,
                    parsed.source_profile_version,
                    PDF_PARSER_VERSION,
                    PDF_EXTRACTION_POLICY_VERSION,
                    _now(),
                ),
            )
            connection.commit()
            status = self._persist_document(
                connection, extraction_run_id, capture.sha256, parsed
            )
            if status == "ACCEPTED":
                recompute_selection(connection)
                links_created = link_narrow_transfers(connection)
            else:
                links_created = 0
            self._complete_import(
                connection,
                import_run_id,
                capture.sha256,
                extraction_run_id,
                reused=False,
            )
            connection.commit()
            return self._load_result(
                connection,
                import_run_id,
                extraction_run_id,
                capture,
                parsed,
                extraction_reused=False,
                links_created=links_created,
            )
        except Exception as exc:
            connection.rollback()
            connection.execute(
                """
                UPDATE import_runs SET completed_at = ?, status = 'FAILED', error_message = ?
                WHERE id = ?
                """,
                (_now(), str(exc), import_run_id),
            )
            connection.commit()
            raise

    @staticmethod
    def _record_source(
        connection: sqlite3.Connection, capture: EvidenceCapture, recorded_path: str
    ) -> None:
        now = _now()
        connection.execute(
            """
            INSERT INTO source_content(
                sha256, byte_size, media_type, evidence_key, storage_kind,
                original_path, last_observed_path, first_seen_at, last_seen_at,
                integrity_status
            ) VALUES (?, ?, 'application/pdf', ?, ?, ?, ?, ?, ?, 'VERIFIED')
            ON CONFLICT(sha256) DO UPDATE SET
                last_observed_path = excluded.last_observed_path,
                last_seen_at = excluded.last_seen_at
            """,
            (
                capture.sha256,
                capture.byte_size,
                capture.evidence_key,
                capture.storage_kind,
                recorded_path,
                recorded_path,
                now,
                now,
            ),
        )
        connection.commit()

    @staticmethod
    def _persist_document(
        connection: sqlite3.Connection,
        extraction_run_id: str,
        evidence_sha256: str,
        parsed: ParsedDocument,
    ) -> str:
        connection.execute(
            """
            INSERT INTO accounts(id, institution, source_type, display_name, masked_identifier)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                parsed.account_id,
                parsed.institution,
                parsed.source_type,
                parsed.account_display_name,
                parsed.masked_identifier,
            ),
        )
        statement_id = _stable_id("stmt", parsed.statement_identity, evidence_sha256)
        conflicts = connection.execute(
            "SELECT id, extraction_run_id FROM statements WHERE statement_identity = ? AND evidence_sha256 <> ?",
            (parsed.statement_identity, evidence_sha256),
        ).fetchall()
        has_conflict = bool(conflicts)
        selection_state = "QUARANTINED" if not parsed.accepted else "CANDIDATE" if has_conflict else "SELECTED"
        connection.execute(
            """
            INSERT INTO statements(
                id, extraction_run_id, evidence_sha256, account_id, institution,
                source_type, statement_date, period_start, period_end,
                statement_identity, selection_state, conflict_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                statement_id,
                extraction_run_id,
                evidence_sha256,
                parsed.account_id,
                parsed.institution,
                parsed.source_type,
                parsed.statement_date,
                parsed.period_start,
                parsed.period_end,
                parsed.statement_identity,
                selection_state,
                "CONFLICT" if has_conflict else "NONE",
            ),
        )
        section_ids: dict[int, str] = {}
        for section in parsed.sections:
            section_id = _stable_id("sec", statement_id, str(section.ordinal))
            section_ids[section.ordinal] = section_id
            connection.execute(
                """
                INSERT INTO statement_sections(
                    id, statement_id, section_ordinal, section_kind, currency,
                    heading_raw, start_page, end_page
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    section_id,
                    statement_id,
                    section.ordinal,
                    section.section_kind,
                    section.currency,
                    section.heading_raw,
                    section.start_page,
                    section.end_page,
                ),
            )

        for page in parsed.pages:
            connection.execute(
                """
                INSERT INTO page_coverage(
                    extraction_run_id, statement_id, page_number, classification,
                    word_count, upright_word_count, rotated_ignored_count,
                    transaction_rows, supporting_records, status, context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extraction_run_id,
                    statement_id,
                    page.page_number,
                    page.classification,
                    page.word_count,
                    page.upright_word_count,
                    page.rotated_ignored_count,
                    page.transaction_rows,
                    page.supporting_records,
                    page.status,
                    _json(page.context),
                ),
            )

        for record in parsed.records:
            record_id = _stable_id("doc", extraction_run_id, record.source_anchor)
            _insert_document_record(
                connection,
                record_id,
                extraction_run_id,
                evidence_sha256,
                statement_id,
                section_ids.get(record.section_ordinal or -1),
                parsed,
                record,
            )
            if record.record_kind == "TRANSACTION" and record.required_fields_complete:
                _insert_document_entry(
                    connection,
                    extraction_run_id,
                    evidence_sha256,
                    statement_id,
                    parsed,
                    record_id,
                    record,
                )

        for problem in parsed.problems:
            _insert_document_issue(
                connection,
                extraction_run_id,
                problem.severity,
                problem.code,
                problem.message,
                {**problem.context, "source_anchor": problem.source_anchor},
            )
        for check in parsed.checks:
            connection.execute(
                """
                INSERT INTO document_reconciliation_checks(
                    extraction_run_id, statement_id, currency_pocket, check_type,
                    equation_version, expected_text, expected_coefficient,
                    expected_scale, actual_text, actual_coefficient, actual_scale,
                    difference_text, difference_coefficient, difference_scale,
                    passed, blocking, context_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extraction_run_id,
                    statement_id,
                    check.currency_pocket,
                    check.check_type,
                    check.equation_version,
                    _money_text(check.expected),
                    _money_coefficient(check.expected),
                    _money_scale(check.expected),
                    _money_text(check.actual),
                    _money_coefficient(check.actual),
                    _money_scale(check.actual),
                    _money_text(check.difference),
                    _money_coefficient(check.difference),
                    _money_scale(check.difference),
                    None if check.passed is None else int(check.passed),
                    int(check.blocking),
                    _json(check.context),
                ),
            )

        if parsed.card_summary is not None:
            _persist_card_statement_facts(
                connection, extraction_run_id, statement_id, parsed
            )

        # A statement whose rows another selected statement of the same account
        # already carries is the same evidence again: a re-download with
        # different bytes, or a mid-month export inside the month's statement.
        # It is kept as evidence and never counted twice, and is not a conflict.
        superseded_by = containing_statement(connection, statement_id) if parsed.accepted else None
        if superseded_by is not None:
            has_conflict = False
            supersede(connection, statement_id, superseded_by)
        elif has_conflict:
            connection.execute(
                """
                UPDATE statements SET selection_state = 'CANDIDATE', conflict_state = 'CONFLICT'
                WHERE statement_identity = ?
                """,
                (parsed.statement_identity,),
            )
            for conflict in conflicts:
                revoke_links_for_extraction(
                    connection,
                    conflict["extraction_run_id"],
                    SELECTION_LOST,
                )
                connection.execute(
                    "UPDATE ledger_entries SET analytics_eligible = 0 "
                    "WHERE extraction_run_id = ? AND source_family = 'DOCUMENT'",
                    (conflict["extraction_run_id"],),
                )
            _insert_document_issue(
                connection,
                extraction_run_id,
                "BLOCKING",
                "STATEMENT_CONTENT_CONFLICT",
                "different evidence objects claim the same authoritative statement identity",
                {"statement_identity": parsed.statement_identity},
            )

        elif parsed.accepted:
            supersede_contained(connection, statement_id)

        blocking_issues = connection.execute(
            "SELECT count(*) FROM extraction_issues WHERE extraction_run_id = ? AND severity = 'BLOCKING'",
            (extraction_run_id,),
        ).fetchone()[0]
        status = "ACCEPTED" if parsed.accepted and not has_conflict and blocking_issues == 0 else "QUARANTINED"
        transaction_count = sum(record.record_kind == "TRANSACTION" for record in parsed.records)
        connection.execute(
            """
            UPDATE extraction_runs
            SET completed_at = ?, status = ?, physical_data_row_count = ?,
                raw_record_count = ?, canonical_entry_count = ?
            WHERE id = ?
            """,
            (
                _now(),
                status,
                transaction_count,
                len(parsed.records),
                transaction_count,
                extraction_run_id,
            ),
        )
        if status == "ACCEPTED" and superseded_by is None:
            connection.execute(
                "UPDATE ledger_entries SET analytics_eligible = 1 "
                "WHERE extraction_run_id = ? AND source_family = 'DOCUMENT'",
                (extraction_run_id,),
            )
        connection.commit()
        return status

    @staticmethod
    def _complete_import(
        connection: sqlite3.Connection,
        import_run_id: str,
        evidence_sha256: str,
        extraction_run_id: str,
        *,
        reused: bool,
    ) -> None:
        connection.execute(
            """
            UPDATE import_runs SET completed_at = ?, status = 'SUCCEEDED',
                evidence_sha256 = ?, extraction_run_id = ?, extraction_reused = ?
            WHERE id = ?
            """,
            (_now(), evidence_sha256, extraction_run_id, int(reused), import_run_id),
        )

    @staticmethod
    def _load_result(
        connection: sqlite3.Connection,
        import_run_id: str,
        extraction_run_id: str,
        capture: EvidenceCapture,
        parsed: ParsedDocument,
        *,
        extraction_reused: bool,
        links_created: int,
    ) -> DocumentImportResult:
        extraction = connection.execute(
            "SELECT * FROM extraction_runs WHERE id = ?", (extraction_run_id,)
        ).fetchone()
        issue_counts = {
            row["severity"]: row["count"]
            for row in connection.execute(
                "SELECT severity, count(*) AS count FROM extraction_issues WHERE extraction_run_id = ? GROUP BY severity",
                (extraction_run_id,),
            )
        }
        checks = connection.execute(
            """
            SELECT count(*) AS total,
                   sum(CASE WHEN passed = 1 THEN 1 ELSE 0 END) AS passed
            FROM document_reconciliation_checks
            WHERE extraction_run_id = ? AND blocking = 1
            """,
            (extraction_run_id,),
        ).fetchone()
        return DocumentImportResult(
            import_run_id,
            extraction_run_id,
            capture.sha256,
            capture.reused,
            extraction_reused,
            extraction["status"],
            parsed.source_type,
            parsed.statement_date,
            extraction["physical_data_row_count"] or 0,
            extraction["raw_record_count"] or 0,
            issue_counts.get("BLOCKING", 0),
            issue_counts.get("WARNING", 0),
            checks["passed"] or 0,
            checks["total"] or 0,
            links_created,
            *supersession_of(connection, extraction_run_id),
            parsed.account_id,
            parsed.account_display_name,
            parsed.period_start,
            parsed.period_end,
        )


# The versioned semantic role vocabulary for `card_statement_facts`. A change to
# what a role means, or a new role backed by a different field of the printed
# evidence, bumps this and re-extracts; it never mutates a stored row.
CARD_STATEMENT_ROLE_VERSION = "mari-card-summary-roles-v1"


def _persist_card_statement_facts(
    connection: sqlite3.Connection,
    extraction_run_id: str,
    statement_id: str,
    parsed: ParsedDocument,
) -> None:
    """Project the parsed card-statement summary into `card_statement_facts`.

    One row per (statement, currency pocket, field role) with the exact printed
    money or date, a coarse source locator, and the parser version. The printed
    summary equation and its pass/fail are NOT copied here — the cards
    projection folds those from `document_reconciliation_checks`.
    """
    summary = parsed.card_summary
    assert summary is not None
    pocket = "SGD"  # Mari card summary buckets and outstanding are SGD.
    rows: list[tuple[str, str, ExactMoney | None, str | None, str]] = []

    def money(role: str, value: ExactMoney | None, locator: str) -> None:
        if value is not None:
            rows.append((role, "money", value, None, locator))

    def calendar(role: str, value: str | None, locator: str) -> None:
        if value is not None:
            rows.append((role, "date", None, value, locator))

    calendar("statement_date", summary.statement_date, "mari-card:account-summary")
    calendar("payment_due_date", summary.payment_due_date, "mari-card:account-summary")
    calendar("statement_period_start", parsed.period_start, "mari-card:statement-period")
    calendar("statement_period_end", parsed.period_end, "mari-card:statement-period")
    money("credit_limit", summary.credit_limit, "mari-card:account-summary")
    money("payment_due", summary.statement_due, "mari-card:account-summary")
    money("minimum_payment", summary.minimum_payment, "mari-card:account-summary")
    money(
        "closing_liability",
        summary.current_outstanding,
        "mari-card:statement-summary:current-outstanding",
    )
    money(
        "opening_liability",
        summary.buckets.get("A"),
        "mari-card:statement-summary:bucket-A",
    )
    for letter, value in sorted(summary.buckets.items()):
        money(
            f"summary_bucket_{letter}",
            value,
            f"mari-card:statement-summary:bucket-{letter}",
        )

    for role, kind, money_value, text, locator in rows:
        connection.execute(
            """
            INSERT INTO card_statement_facts(
                statement_id, extraction_run_id, currency_pocket, field_role,
                value_kind, value_text, value_coefficient, value_scale,
                source_locator, parser_version, role_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(statement_id, currency_pocket, field_role) DO UPDATE SET
                extraction_run_id = excluded.extraction_run_id,
                value_kind = excluded.value_kind,
                value_text = excluded.value_text,
                value_coefficient = excluded.value_coefficient,
                value_scale = excluded.value_scale,
                source_locator = excluded.source_locator,
                parser_version = excluded.parser_version,
                role_version = excluded.role_version
            """,
            (
                statement_id,
                extraction_run_id,
                pocket,
                role,
                kind,
                money_value.text if money_value is not None else text,
                money_value.coefficient if money_value is not None else None,
                money_value.scale if money_value is not None else None,
                locator,
                PDF_PARSER_VERSION,
                CARD_STATEMENT_ROLE_VERSION,
            ),
        )


def _insert_document_record(
    connection: sqlite3.Connection,
    record_id: str,
    extraction_run_id: str,
    evidence_sha256: str,
    statement_id: str,
    section_id: str | None,
    parsed: ParsedDocument,
    record: DocumentRecord,
) -> None:
    x0, top, x1, bottom = (round(value * 1000) for value in record.bbox)
    connection.execute(
        """
        INSERT INTO document_records(
            id, extraction_run_id, evidence_sha256, statement_id, section_id,
            source_profile_version, parser_version, source_anchor,
            extraction_ordinal, page_number, x0_mpt, top_mpt, x1_mpt,
            bottom_mpt, record_kind, raw_text, raw_fields_json, date_raw,
            date_value, description_raw, reference_raw, debit_text,
            debit_coefficient, debit_scale, credit_text, credit_coefficient,
            credit_scale, amount_text, amount_coefficient, amount_scale,
            direction_marker, currency_raw, balance_text, balance_coefficient,
            balance_scale, confidence, required_fields_complete
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            extraction_run_id,
            evidence_sha256,
            statement_id,
            section_id,
            parsed.source_profile_version,
            PDF_PARSER_VERSION,
            record.source_anchor,
            record.ordinal,
            record.page_number,
            x0,
            top,
            x1,
            bottom,
            record.record_kind,
            record.raw_text,
            _json(record.raw_fields),
            record.date_raw,
            record.date_value,
            record.description_raw,
            record.reference_raw,
            _money_text(record.debit),
            _money_coefficient(record.debit),
            _money_scale(record.debit),
            _money_text(record.credit),
            _money_coefficient(record.credit),
            _money_scale(record.credit),
            _money_text(record.amount),
            _money_coefficient(record.amount),
            _money_scale(record.amount),
            record.direction_marker,
            record.currency_raw,
            _money_text(record.balance),
            _money_coefficient(record.balance),
            _money_scale(record.balance),
            record.confidence,
            int(record.required_fields_complete),
        ),
    )


def _insert_document_entry(
    connection: sqlite3.Connection,
    extraction_run_id: str,
    evidence_sha256: str,
    statement_id: str,
    parsed: ParsedDocument,
    record_id: str,
    record: DocumentRecord,
) -> None:
    if parsed.source_type in (
        "DBS_CONSOLIDATED",
        "MARI_SAVINGS",
        "TRUST_SAVINGS",
        "REVOLUT_PDF",
    ):
        source_amount = record.credit if record.credit is not None else record.debit
        assert source_amount is not None
        coefficient = source_amount.coefficient if record.credit is not None else -source_amount.coefficient
    else:
        source_amount = record.amount
        assert source_amount is not None
        coefficient = source_amount.coefficient if record.direction_marker == "CR" else -source_amount.coefficient
    assert record.date_value and record.currency_raw
    amount_text = format_decimal(coefficient, source_amount.scale)
    direction = "IN" if coefficient > 0 else "OUT" if coefficient < 0 else "ZERO"
    event_kind = _event_kind(
        parsed.source_type, record.description_raw or "", record.raw_fields
    )
    natural_anchor = f"{evidence_sha256}:{record.source_anchor}:{record.ordinal}"
    alt_role = record.alt_date_role
    alt_precision = "DAY" if alt_role is not None else None
    foreign = record.foreign_amount
    fee = record.fee
    # The Revolut PDF carries a reconciled per-row running balance; keep it on
    # the ledger entry so the liquid-position read has a statement-running
    # balance basis, exactly as the retired CSV path provided. Other document
    # sources leave it NULL and fall back to the printed closing balance.
    balance = record.balance if parsed.source_type == "REVOLUT_PDF" else None
    connection.execute(
        """
        INSERT INTO ledger_entries(
            id, source_family, extraction_run_id, evidence_sha256, raw_record_id,
            document_record_id, statement_id, natural_anchor, account_id,
            currency_pocket, effective_at, effective_precision, amount_text,
            amount_coefficient, amount_scale, event_kind, economic_direction,
            lifecycle_state, analytics_eligible,
            alt_date_role, alt_date_value, alt_date_raw, alt_date_precision,
            fee_text, fee_coefficient, fee_scale,
            balance_text, balance_coefficient, balance_scale,
            source_currency, source_amount_text, source_amount_coefficient,
            source_amount_scale
        ) VALUES (
            ?, 'DOCUMENT', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            'COMPLETED', 0,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?
        )
        """,
        (
            _stable_id("entry", extraction_run_id, record.source_anchor),
            extraction_run_id,
            evidence_sha256,
            record_id,
            statement_id,
            natural_anchor,
            parsed.account_id,
            record.currency_raw,
            record.date_value,
            record.date_precision,
            amount_text,
            coefficient,
            source_amount.scale,
            event_kind,
            direction,
            alt_role,
            record.alt_date_value if alt_role is not None else None,
            record.alt_date_raw if alt_role is not None else None,
            alt_precision,
            format_decimal(fee.coefficient, fee.scale) if fee is not None else None,
            fee.coefficient if fee is not None else None,
            fee.scale if fee is not None else None,
            format_decimal(balance.coefficient, balance.scale) if balance is not None else None,
            balance.coefficient if balance is not None else None,
            balance.scale if balance is not None else None,
            record.foreign_currency,
            format_decimal(foreign.coefficient, foreign.scale) if foreign is not None else None,
            foreign.coefficient if foreign is not None else None,
            foreign.scale if foreign is not None else None,
        ),
    )


def _event_kind(
    source_type: str, description: str, raw_fields: dict[str, object] | None = None
) -> str:
    """Classify a document line into the canonical ledger vocabulary."""
    if source_type == "REVOLUT_PDF" and raw_fields is not None:
        # The Revolut PDF parser classifies each row from its printed card
        # marker and direction (revolut_pdf_event_kind); the generic keyword
        # ladder below would misclassify most of these.
        parser_kind = raw_fields.get("event_kind")
        if isinstance(parser_kind, str) and parser_kind in LEDGER_EVENT_KINDS:
            return parser_kind
    upper = description.upper()
    if "TOP-UP TO PAYLAH" in upper or "TOP UP WALLET" in upper:
        kind = "TOPUP"
    elif "REFUND" in upper or "CASHBACK" in upper:
        kind = "REFUND"
    elif "DEBIT CARD TRANSACTION" in upper or "CARD PAYMENT" in upper:
        kind = "CARD_PAYMENT"
    elif (
        "PAYNOW" in upper
        or "FUNDS TRANSFER" in upper
        or "TRANSFER" in upper
        or "SEND MONEY" in upper
        or "REPAYMENT" in upper
        or "MONEYSEND" in upper
    ):
        kind = "TRANSFER"
    else:
        kind = "PAYMENT" if source_type == "DBS_PAYLAH" else "OTHER"
    assert kind in LEDGER_EVENT_KINDS
    return kind


def _insert_document_issue(
    connection: sqlite3.Connection,
    extraction_run_id: str,
    severity: str,
    code: str,
    message: str,
    context: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO extraction_issues(
            extraction_run_id, raw_record_id, severity, code, message,
            context_json, created_at
        ) VALUES (?, NULL, ?, ?, ?, ?, ?)
        """,
        (extraction_run_id, severity, code, message, _json(context), _now()),
    )


def _money_text(value: ExactMoney | None) -> str | None:
    return value.text if value is not None else None


def _money_coefficient(value: ExactMoney | None) -> int | None:
    return value.coefficient if value is not None else None


def _money_scale(value: ExactMoney | None) -> int | None:
    return value.scale if value is not None else None
