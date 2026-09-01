from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import gcd
from pathlib import Path

from .db import ConnectionFactory, connect_sqlite, initialize_schema
from .decisions import replay_journal, resolve_anchor
from .documents import (
    PDF_EXTRACTION_POLICY_VERSION,
    PDF_PARSER_VERSION,
    DocumentRecord,
    ParsedDocument,
    generic_extraction_identity,
    parse_dbs_consolidated,
    parse_paylah,
)
from .errors import EvidenceError, FinCLError
from .evidence import EvidenceCapture, EvidenceStore, PlaintextEvidenceStore
from .money import ExactMoney, format_decimal

DBS_PAYLAH_LINK_VERSION = "dbs-paylah-exact-date-amount-description-v1"
DBS_REVOLUT_LEGACY_LINK_VERSION = "dbs-revolut-exact-amount-currency-window-description-v1"
DBS_REVOLUT_CARD_TOPUP_LINK_VERSION = "dbs-revolut-card-topup-exact-amount-window-v1"
DBS_REVOLUT_PAYNOW_TOPUP_LINK_VERSION = "dbs-revolut-paynow-topup-exact-amount-window-v1"
REVOLUT_EXCHANGE_LINK_VERSION = "revolut-exchange-started-at-pockets-v1"
AMBIGUOUS_MUTUAL_MATCH = "AMBIGUOUS_MUTUAL_MATCH"
SELECTION_LOST = "SELECTION_LOST"


@dataclass(frozen=True, slots=True)
class EligibleLinkLeg:
    """The source account/pocket and canonical shape a linker may consume."""

    source_type: str
    currency_pocket: str | None
    economic_direction: str
    event_kinds: frozenset[str]

    def matches(self, row: sqlite3.Row) -> bool:
        return (
            row["source_type"] == self.source_type
            and (self.currency_pocket is None or row["currency_pocket"] == self.currency_pocket)
            and row["economic_direction"] == self.economic_direction
            and row["event_kind"] in self.event_kinds
        )


@dataclass(frozen=True, slots=True)
class SameCurrencyLinker:
    """A declared automatic linker, including its complete eligible leg pair."""

    method_version: str
    left: EligibleLinkLeg
    right: EligibleLinkLeg
    max_days: int


@dataclass(frozen=True, slots=True)
class ExchangeLinker:
    """A declared cross-currency linker with its complete eligible leg pair."""

    method_version: str
    left: EligibleLinkLeg
    right: EligibleLinkLeg


# These declarations are the eligible-pair table for automatic same-currency
# links. A leg is never matched unless it satisfies one of these narrow scopes.
DBS_PAYLAH_LINKER = SameCurrencyLinker(
    DBS_PAYLAH_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"TOPUP"})),
    EligibleLinkLeg("DBS_PAYLAH", "SGD", "IN", frozenset({"TOPUP"})),
    max_days=0,
)
DBS_REVOLUT_CARD_TOPUP_LINKER = SameCurrencyLinker(
    DBS_REVOLUT_CARD_TOPUP_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"CARD_PAYMENT"})),
    EligibleLinkLeg("REVOLUT_CSV", "SGD", "IN", frozenset({"TOPUP", "TRANSFER"})),
    max_days=2,
)
DBS_REVOLUT_PAYNOW_TOPUP_LINKER = SameCurrencyLinker(
    DBS_REVOLUT_PAYNOW_TOPUP_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"TRANSFER"})),
    EligibleLinkLeg("REVOLUT_CSV", "SGD", "IN", frozenset({"TOPUP", "TRANSFER"})),
    max_days=2,
)
REVOLUT_EXCHANGE_LINKER = ExchangeLinker(
    REVOLUT_EXCHANGE_LINK_VERSION,
    EligibleLinkLeg("REVOLUT_CSV", None, "OUT", frozenset({"EXCHANGE"})),
    EligibleLinkLeg("REVOLUT_CSV", None, "IN", frozenset({"EXCHANGE"})),
)
AUTOMATIC_SAME_CURRENCY_LINK_METHODS = (
    DBS_PAYLAH_LINKER.method_version,
    DBS_REVOLUT_LEGACY_LINK_VERSION,
    DBS_REVOLUT_CARD_TOPUP_LINKER.method_version,
    DBS_REVOLUT_PAYNOW_TOPUP_LINKER.method_version,
)


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

    def import_dbs(self, source_path: Path) -> DocumentImportResult:
        return self._import(source_path, "DBS_CONSOLIDATED")

    def import_paylah(self, source_path: Path) -> DocumentImportResult:
        return self._import(source_path, "DBS_PAYLAH")

    def _import(self, source_path: Path, source_type: str) -> DocumentImportResult:
        connection = self.connection_factory(self.database_path)
        try:
            return self._run_import(connection, source_path, source_type)
        finally:
            connection.close()

    def _run_import(
        self, connection: sqlite3.Connection, source_path: Path, source_type: str
    ) -> DocumentImportResult:
        initialize_schema(connection)
        replay_journal(connection)
        import_run_id = "imp_" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO import_runs(id, started_at, status, source_path) VALUES (?, ?, 'RUNNING', ?)",
            (import_run_id, _now(), str(Path(source_path).resolve())),
        )
        connection.commit()
        try:
            capture = self.evidence_store.capture(Path(source_path))
            self._record_source(connection, capture)
            with self.evidence_store.open_verified(capture.sha256, capture.evidence_key) as evidence:
                data = evidence.read()
            if hashlib.sha256(data).hexdigest() != capture.sha256:
                raise EvidenceError("bytes supplied to the PDF parser failed evidence verification")
            parsed = (
                parse_dbs_consolidated(data)
                if source_type == "DBS_CONSOLIDATED"
                else parse_paylah(data)
            )
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
            links_created = link_narrow_transfers(connection) if status == "ACCEPTED" else 0
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
    def _record_source(connection: sqlite3.Connection, capture: EvidenceCapture) -> None:
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
                capture.original_path,
                capture.original_path,
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
        if has_conflict:
            connection.execute(
                """
                UPDATE statements SET selection_state = 'CANDIDATE', conflict_state = 'CONFLICT'
                WHERE statement_identity = ?
                """,
                (parsed.statement_identity,),
            )
            for conflict in conflicts:
                _revoke_links_for_extraction(
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
        if status == "ACCEPTED":
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
        )


def link_narrow_transfers(
    connection: sqlite3.Connection, *, journal_path: Path | None = None
) -> int:
    """Refresh all derived same-currency links, then retain exchange links."""
    replay_journal(connection, journal_path)
    return refresh_automatic_transfer_links(connection)


def refresh_automatic_transfer_links(connection: sqlite3.Connection) -> int:
    """Recompute automatic transfer links from currently selected evidence.

    Automatic links are a projection, not decision history: every row for the
    automatic same-currency methods is discarded and rebuilt from the evidence
    currently selected.  The return value is the number of confirmed links that
    are new relative to the prior projection, so an unchanged refresh reports 0.
    """
    placeholders = ", ".join("?" for _ in AUTOMATIC_SAME_CURRENCY_LINK_METHODS)
    before = {
        row[0]
        for row in connection.execute(
            f"SELECT id FROM transfer_links WHERE status = 'CONFIRMED' "
            f"AND method_version IN ({placeholders})",
            AUTOMATIC_SAME_CURRENCY_LINK_METHODS,
        )
    }
    connection.execute(
        f"DELETE FROM transfer_links WHERE method_version IN ({placeholders})",
        AUTOMATIC_SAME_CURRENCY_LINK_METHODS,
    )
    created = link_revolut_exchanges(connection)
    dbs_rows = connection.execute(
        """
        SELECT e.id, e.effective_at AS posted_date, e.currency_pocket,
               e.amount_coefficient, e.amount_scale, e.amount_text, e.event_kind,
               e.economic_direction, s.source_type, r.description_raw
        FROM ledger_entries e
        JOIN document_records r ON r.id = e.document_record_id
        JOIN statements s ON s.id = e.statement_id
        WHERE e.source_family = 'DOCUMENT'
          AND s.source_type = 'DBS_CONSOLIDATED'
          AND s.selection_state = 'SELECTED'
          AND e.analytics_eligible = 1
          AND e.economic_direction = 'OUT'
        """
    ).fetchall()
    paylah_rows = connection.execute(
        """
        SELECT e.id, e.effective_at AS posted_date, e.currency_pocket,
               e.amount_coefficient, e.amount_scale, e.amount_text, e.event_kind,
               e.economic_direction, s.source_type, r.description_raw
        FROM ledger_entries e
        JOIN document_records r ON r.id = e.document_record_id
        JOIN statements s ON s.id = e.statement_id
        WHERE e.source_family = 'DOCUMENT'
          AND s.source_type = 'DBS_PAYLAH'
          AND s.selection_state = 'SELECTED'
          AND e.analytics_eligible = 1
          AND e.economic_direction = 'IN'
        """
    ).fetchall()
    revolut_rows = connection.execute(
        """
        SELECT e.id, substr(e.effective_at, 1, 10) AS posted_date,
               e.currency_pocket, e.amount_coefficient, e.amount_scale,
               e.amount_text, e.event_kind, e.economic_direction,
               'REVOLUT_CSV' AS source_type, r.description_raw
        FROM ledger_entries e
        JOIN raw_records r ON r.id = e.raw_record_id
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE e.source_family = 'CSV'
          AND x.status = 'ACCEPTED' AND e.analytics_eligible = 1
          AND e.economic_direction = 'IN'
          AND e.event_kind IN ('TOPUP', 'TRANSFER')
        """
    ).fetchall()
    dbs_topups = [
        row
        for row in dbs_rows
        if DBS_PAYLAH_LINKER.left.matches(row)
        and "TOP-UP TO PAYLAH" in (row["description_raw"] or "").upper()
    ]
    paylah_topups = [
        row
        for row in paylah_rows
        if DBS_PAYLAH_LINKER.right.matches(row)
        and "TOP UP WALLET FROM MY ACCOUNT" in (row["description_raw"] or "").upper()
    ]
    card_topups = [
        row
        for row in dbs_rows
        if DBS_REVOLUT_CARD_TOPUP_LINKER.left.matches(row)
        and _is_revolut_card_topup(row["description_raw"])
    ]
    paynow_topups = [
        row
        for row in dbs_rows
        if DBS_REVOLUT_PAYNOW_TOPUP_LINKER.left.matches(row)
        and _is_revolut_paynow_topup(row["description_raw"])
    ]
    card_revolut_rows = [
        row for row in revolut_rows if DBS_REVOLUT_CARD_TOPUP_LINKER.right.matches(row)
    ]
    paynow_revolut_rows = [
        row for row in revolut_rows if DBS_REVOLUT_PAYNOW_TOPUP_LINKER.right.matches(row)
    ]
    candidates = [
        *_candidate_pairs(
            dbs_topups,
            paylah_topups,
            DBS_PAYLAH_LINKER.method_version,
            max_days=DBS_PAYLAH_LINKER.max_days,
        ),
        *_candidate_pairs(
            card_topups,
            card_revolut_rows,
            DBS_REVOLUT_CARD_TOPUP_LINKER.method_version,
            max_days=DBS_REVOLUT_CARD_TOPUP_LINKER.max_days,
        ),
        *_candidate_pairs(
            paynow_topups,
            paynow_revolut_rows,
            DBS_REVOLUT_PAYNOW_TOPUP_LINKER.method_version,
            max_days=DBS_REVOLUT_PAYNOW_TOPUP_LINKER.max_days,
        ),
    ]
    _store_candidates(connection, candidates)
    _apply_link_decisions(connection)
    after = {
        row[0]
        for row in connection.execute(
            f"SELECT id FROM transfer_links WHERE status = 'CONFIRMED' "
            f"AND method_version IN ({placeholders})",
            AUTOMATIC_SAME_CURRENCY_LINK_METHODS,
        )
    }
    connection.commit()
    return created + len(after - before)


def _apply_link_decisions(connection: sqlite3.Connection) -> None:
    """Overlay bound journal decisions on this freshly-derived link projection."""
    decisions = connection.execute(
        "SELECT * FROM link_decisions WHERE bound = 1 ORDER BY journal_seq"
    ).fetchall()
    for decision in decisions:
        left = resolve_anchor(connection, decision["left_anchor"])
        right = resolve_anchor(connection, decision["right_anchor"])
        if left is None or right is None:
            continue
        row = connection.execute(
            """SELECT id, method_version, evidence_json FROM transfer_links
               WHERE left_entry_id=? AND right_entry_id=?""",
            (left, right),
        ).fetchone()
        if row is None:
            continue  # Confirmation without a candidate is deliberately not guessed.
        evidence = json.loads(row["evidence_json"])
        evidence["decision"] = {
            "seq": decision["journal_seq"],
            "policy_version": decision["policy_version"],
        }
        if decision["decision"] == "REJECT":
            if decision["policy_version"] == row["method_version"]:
                connection.execute("DELETE FROM transfer_links WHERE id = ?", (row["id"],))
            else:
                connection.execute(
                    "UPDATE transfer_links SET status='REJECTED', evidence_json=? WHERE id=?",
                    (_json(evidence), row["id"]),
                )
            continue
        occupied = connection.execute(
            """SELECT 1 FROM transfer_links WHERE status='CONFIRMED' AND id <> ?
               AND (left_entry_id IN (?, ?) OR right_entry_id IN (?, ?))""",
            (row["id"], left, right, left, right),
        ).fetchone()
        if occupied is None:
            connection.execute(
                "UPDATE transfer_links SET status='CONFIRMED', evidence_json=? WHERE id=?",
                (_json(evidence), row["id"]),
            )


def _is_revolut_card_topup(description: str | None) -> bool:
    text = (description or "").upper()
    return text.startswith("DEBIT CARD TRANSACTION") and "REVOLUT**" in text and "SINGAPORE SGP" in text


def _is_revolut_paynow_topup(description: str | None) -> bool:
    text = (description or "").upper()
    return "PAYNOW TRANSFER" in text and "TO: REVOLUT TECHNOLOGIES SINGAPORE" in text


def link_revolut_exchanges(connection: sqlite3.Connection) -> int:
    """Link complete Revolut exchange pairs and retain their exact implied rate."""
    rows = connection.execute(
        """
        SELECT e.id, e.extraction_run_id,
               COALESCE(e.alt_date_value, e.effective_at) AS started_at,
               e.currency_pocket, e.amount_text, e.amount_coefficient, e.amount_scale,
               e.economic_direction, e.event_kind, a.source_type
        FROM ledger_entries e
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        JOIN accounts a ON a.id = e.account_id
        WHERE e.source_family = 'CSV'
          AND x.status = 'ACCEPTED' AND e.analytics_eligible = 1
          AND e.lifecycle_state = 'COMPLETED'
        ORDER BY e.extraction_run_id, started_at, e.id
        """
    ).fetchall()
    rows = [
        row
        for row in rows
        if REVOLUT_EXCHANGE_LINKER.left.matches(row)
        or REVOLUT_EXCHANGE_LINKER.right.matches(row)
    ]
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["extraction_run_id"], row["started_at"]), []).append(row)

    created = 0
    for group in groups.values():
        if len(group) != 2:
            continue
        outgoing = [row for row in group if row["economic_direction"] == "OUT"]
        incoming = [row for row in group if row["economic_direction"] == "IN"]
        if len(outgoing) != 1 or len(incoming) != 1:
            continue
        left, right = outgoing[0], incoming[0]
        if left["currency_pocket"] == right["currency_pocket"]:
            continue
        numerator = abs(right["amount_coefficient"]) * 10**left["amount_scale"]
        denominator = abs(left["amount_coefficient"]) * 10**right["amount_scale"]
        divisor = gcd(numerator, denominator)
        link_id = _stable_id(
            "link", left["id"], right["id"], REVOLUT_EXCHANGE_LINKER.method_version
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO transfer_links(
                id, left_entry_id, right_entry_id,
                link_kind, left_currency, left_amount_text, left_amount_coefficient,
                left_amount_scale, right_currency, right_amount_text, right_amount_coefficient,
                right_amount_scale, implied_rate_numerator, implied_rate_denominator,
                method_version, status, evidence_json, created_at
            ) VALUES (?, ?, ?, 'CURRENCY_EXCHANGE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'CONFIRMED', ?, ?)
            """,
            (
                link_id,
                left["id"],
                right["id"],
                left["currency_pocket"],
                left["amount_text"].lstrip("-"),
                abs(left["amount_coefficient"]),
                left["amount_scale"],
                right["currency_pocket"],
                right["amount_text"].lstrip("-"),
                abs(right["amount_coefficient"]),
                right["amount_scale"],
                numerator // divisor,
                denominator // divisor,
                REVOLUT_EXCHANGE_LINKER.method_version,
                _json({"started_at": left["started_at"], "unique_pair": True}),
                _now(),
            ),
        )
        created += cursor.rowcount
    return created


def _candidate_pairs(
    left_rows: list[sqlite3.Row],
    right_rows: list[sqlite3.Row],
    method_version: str,
    *,
    max_days: int,
) -> list[tuple[sqlite3.Row, sqlite3.Row, str]]:
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, str]] = []
    for left in left_rows:
        for right in right_rows:
            if left["currency_pocket"] != right["currency_pocket"]:
                continue
            if not _opposite_exact_amount(left, right):
                continue
            day_difference = abs(
                (date.fromisoformat(left["posted_date"]) - date.fromisoformat(right["posted_date"])).days
            )
            if day_difference > max_days:
                continue
            candidates.append((left, right, method_version))
    return candidates


def _store_candidates(
    connection: sqlite3.Connection,
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, str]],
) -> int:
    left_counts: dict[str, int] = {}
    right_counts: dict[str, int] = {}
    for left, right, _ in candidates:
        left_counts[left["id"]] = left_counts.get(left["id"], 0) + 1
        right_counts[right["id"]] = right_counts.get(right["id"], 0) + 1
    created = 0
    for left, right, method_version in candidates:
        left_count = left_counts[left["id"]]
        right_count = right_counts[right["id"]]
        confirmed = left_count == 1 and right_count == 1
        link_id = _stable_id("link", left["id"], right["id"], method_version)
        evidence: dict[str, object] = {
            "date_left": left["posted_date"],
            "date_right": right["posted_date"],
            "description_evidence": True,
            "unique_candidate": confirmed,
        }
        if not confirmed:
            evidence["rejection"] = {
                "code": AMBIGUOUS_MUTUAL_MATCH,
                "left_candidates": left_count,
                "right_candidates": right_count,
            }
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO transfer_links(
                id, left_entry_id, right_entry_id, link_kind,
                left_currency, left_amount_text, left_amount_coefficient, left_amount_scale,
                right_currency, right_amount_text, right_amount_coefficient, right_amount_scale,
                method_version, status, evidence_json, created_at
            ) VALUES (?, ?, ?, 'SAME_CURRENCY_TRANSFER', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link_id,
                left["id"],
                right["id"],
                left["currency_pocket"],
                left["amount_text"].lstrip("-"),
                abs(left["amount_coefficient"]),
                left["amount_scale"],
                right["currency_pocket"],
                right["amount_text"].lstrip("-"),
                abs(right["amount_coefficient"]),
                right["amount_scale"],
                method_version,
                "CONFIRMED" if confirmed else "PROPOSED",
                _json(evidence),
                _now(),
            ),
        )
        created += cursor.rowcount if confirmed else 0
    return created


def _opposite_exact_amount(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    scale = max(left["amount_scale"], right["amount_scale"])
    left_value = left["amount_coefficient"] * 10 ** (scale - left["amount_scale"])
    right_value = right["amount_coefficient"] * 10 ** (scale - right["amount_scale"])
    return left_value == -right_value


def _revoke_links_for_extraction(
    connection: sqlite3.Connection, extraction_run_id: str, code: str
) -> int:
    """Reject links whose legs lost eligibility; ``code`` is a centralized reason."""
    entry_ids = {
        row[0]
        for row in connection.execute(
            "SELECT id FROM ledger_entries "
            "WHERE extraction_run_id = ? AND source_family = 'DOCUMENT'",
            (extraction_run_id,),
        )
    }
    if not entry_ids:
        return 0
    revoked = 0
    rows = connection.execute(
        "SELECT id, left_entry_id, right_entry_id, evidence_json FROM transfer_links WHERE status = 'CONFIRMED'"
    ).fetchall()
    for row in rows:
        if row["left_entry_id"] not in entry_ids and row["right_entry_id"] not in entry_ids:
            continue
        evidence = json.loads(row["evidence_json"])
        evidence["revocation"] = {"code": code, "recorded_at": _now()}
        connection.execute(
            "UPDATE transfer_links SET status = 'REJECTED', evidence_json = ? WHERE id = ?",
            (_json(evidence), row["id"]),
        )
        revoked += 1
    return revoked


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
    if parsed.source_type == "DBS_CONSOLIDATED":
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
    event_kind = _event_kind(parsed.source_type, record.description_raw or "")
    natural_anchor = f"{evidence_sha256}:{record.source_anchor}:{record.ordinal}"
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
            ?, 'DOCUMENT', ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'DAY', ?, ?, ?, ?, ?,
            'COMPLETED', 0,
            NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
            NULL, NULL, NULL, NULL
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
            amount_text,
            coefficient,
            source_amount.scale,
            event_kind,
            direction,
        ),
    )


def _event_kind(source_type: str, description: str) -> str:
    upper = description.upper()
    if "TOP-UP TO PAYLAH" in upper or "TOP UP WALLET" in upper:
        return "TOPUP"
    if "REFUND" in upper or "CASHBACK" in upper:
        return "REFUND"
    if "DEBIT CARD TRANSACTION" in upper:
        return "CARD_PAYMENT"
    if "PAYNOW" in upper or "FUNDS TRANSFER" in upper or "TRANSFER" in upper or "SEND MONEY" in upper:
        return "TRANSFER"
    return "PAYMENT" if source_type == "DBS_PAYLAH" else "OTHER"


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


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_" + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
