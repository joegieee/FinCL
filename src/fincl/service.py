from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .db import ConnectionFactory, connect_sqlite, initialize_schema
from .decisions import replay_journal
from .errors import EvidenceError, FinCLError
from .evidence import EvidenceCapture, EvidenceStore, PlaintextEvidenceStore
from .money import MAX_ABS_COEFFICIENT, ExactMoney, exact_sum, format_decimal
from .revolut import (
    BALANCE_EQUATION_VERSION,
    EXTRACTION_POLICY_VERSION,
    PARSER_VERSION,
    SOURCE_PROFILE_VERSION,
    ProfileMismatch,
    RevolutExport,
    RevolutRow,
    cells_json,
    extraction_identity,
    parse_revolut_csv,
)

REVOLUT_ACCOUNT_ID = "acct_revolut_current"


@dataclass(frozen=True, slots=True)
class ImportResult:
    import_run_id: str
    extraction_run_id: str
    evidence_sha256: str
    evidence_reused: bool
    extraction_reused: bool
    status: str
    raw_record_count: int
    canonical_entry_count: int
    blocking_issue_count: int
    warning_issue_count: int


class FinCLService:
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
    def plaintext_phase1(
        cls, database_path: Path, evidence_directory: Path
    ) -> FinCLService:
        return cls(database_path, PlaintextEvidenceStore(evidence_directory))

    def import_revolut(self, source_path: Path) -> ImportResult:
        connection = self.connection_factory(self.database_path)
        try:
            initialize_schema(connection)
            replay_journal(connection)
            import_run_id = "imp_" + uuid.uuid4().hex
            started_at = _now()
            connection.execute(
                "INSERT INTO import_runs(id, started_at, status, source_path) VALUES (?, ?, 'RUNNING', ?)",
                (import_run_id, started_at, str(Path(source_path).resolve())),
            )
            connection.commit()
            try:
                capture = self.evidence_store.capture(Path(source_path))
                self._record_source(connection, capture, started_at)
                extraction_run_id = extraction_identity(capture.sha256)
                existing = connection.execute(
                    "SELECT * FROM extraction_runs WHERE id = ?", (extraction_run_id,)
                ).fetchone()
                if existing is not None:
                    if existing["status"] == "RUNNING":
                        raise FinCLError(
                            "the same extraction identity is already marked RUNNING; "
                            "inspect the prior run before retrying"
                        )
                    return self._result_from_existing(
                        connection, import_run_id, capture, existing
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
                        SOURCE_PROFILE_VERSION,
                        PARSER_VERSION,
                        EXTRACTION_POLICY_VERSION,
                        _now(),
                    ),
                )
                connection.commit()
                return self._extract_new(
                    connection, import_run_id, extraction_run_id, capture
                )
            except Exception as exc:
                connection.rollback()
                connection.execute(
                    """
                    UPDATE import_runs
                    SET completed_at = ?, status = 'FAILED', error_message = ?
                    WHERE id = ?
                    """,
                    (_now(), str(exc), import_run_id),
                )
                connection.commit()
                raise
        finally:
            connection.close()

    @staticmethod
    def _record_source(
        connection: sqlite3.Connection, capture: EvidenceCapture, observed_at: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO source_content(
                sha256, byte_size, media_type, evidence_key, storage_kind,
                original_path, last_observed_path, first_seen_at, last_seen_at,
                integrity_status
            ) VALUES (?, ?, 'text/csv', ?, ?, ?, ?, ?, ?, 'VERIFIED')
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
                observed_at,
                observed_at,
            ),
        )
        connection.commit()

    def _extract_new(
        self,
        connection: sqlite3.Connection,
        import_run_id: str,
        extraction_run_id: str,
        capture: EvidenceCapture,
    ) -> ImportResult:
        try:
            with self.evidence_store.open_verified(
                capture.sha256, capture.evidence_key
            ) as evidence:
                data = evidence.read()
            if hashlib.sha256(data).hexdigest() != capture.sha256:
                raise EvidenceError(
                    "bytes read for extraction do not match the verified evidence hash"
                )
            export = parse_revolut_csv(data)
        except ProfileMismatch as exc:
            self._quarantine_profile_mismatch(
                connection, extraction_run_id, import_run_id, capture, exc
            )
            return self._load_result(
                connection,
                import_run_id,
                extraction_run_id,
                capture,
                extraction_reused=False,
            )
        except Exception:
            connection.execute(
                "UPDATE extraction_runs SET completed_at = ?, status = 'FAILED' WHERE id = ?",
                (_now(), extraction_run_id),
            )
            connection.commit()
            raise

        try:
            self._persist_export(connection, extraction_run_id, capture.sha256, export)
            self._validate_exchange_pairs(connection, extraction_run_id)
            self._reconcile(connection, extraction_run_id, export)
            blocking = connection.execute(
                """
                SELECT count(*) FROM extraction_issues
                WHERE extraction_run_id = ? AND severity = 'BLOCKING'
                """,
                (extraction_run_id,),
            ).fetchone()[0]
            status = "QUARANTINED" if blocking else "ACCEPTED"
            if status == "ACCEPTED":
                connection.execute(
                    """
                    UPDATE ledger_entries
                    SET analytics_eligible = 1
                    WHERE extraction_run_id = ? AND source_family = 'CSV'
                      AND lifecycle_state = 'COMPLETED'
                    """,
                    (extraction_run_id,),
                )
            raw_count = connection.execute(
                "SELECT count(*) FROM raw_records WHERE extraction_run_id = ?",
                (extraction_run_id,),
            ).fetchone()[0]
            canonical_count = connection.execute(
                "SELECT count(*) FROM ledger_entries "
                "WHERE extraction_run_id = ? AND source_family = 'CSV'",
                (extraction_run_id,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE extraction_runs
                SET completed_at = ?, status = ?, raw_record_count = ?,
                    canonical_entry_count = ?
                WHERE id = ?
                """,
                (_now(), status, raw_count, canonical_count, extraction_run_id),
            )
            self._complete_import(
                connection,
                import_run_id,
                capture.sha256,
                extraction_run_id,
                reused=False,
            )
            connection.commit()
            if status == "ACCEPTED":
                # Kept in the document service for now because the same command
                # also links DBS and PayLah transfers.
                from .document_service import link_narrow_transfers

                link_narrow_transfers(connection)
        except Exception:
            connection.rollback()
            connection.execute(
                "UPDATE extraction_runs SET completed_at = ?, status = 'FAILED' WHERE id = ?",
                (_now(), extraction_run_id),
            )
            connection.commit()
            raise
        return self._load_result(
            connection,
            import_run_id,
            extraction_run_id,
            capture,
            extraction_reused=False,
        )

    @staticmethod
    def _validate_exchange_pairs(connection: sqlite3.Connection, extraction_run_id: str) -> None:
        """Fail closed unless each completed Revolut exchange has one opposing leg."""
        rows = connection.execute(
            """
            SELECT e.raw_record_id,
                   COALESCE(e.alt_date_value, e.effective_at) AS started_at,
                   e.currency_pocket, e.economic_direction
            FROM ledger_entries e
            WHERE e.extraction_run_id = ? AND e.source_family = 'CSV'
              AND e.event_kind = 'EXCHANGE'
              AND e.lifecycle_state = 'COMPLETED'
            ORDER BY started_at, e.raw_record_id
            """,
            (extraction_run_id,),
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault(row["started_at"], []).append(row)
        for started_at, group in groups.items():
            outgoing = [row for row in group if row["economic_direction"] == "OUT"]
            incoming = [row for row in group if row["economic_direction"] == "IN"]
            valid = (
                len(group) == 2
                and len(outgoing) == 1
                and len(incoming) == 1
                and outgoing[0]["currency_pocket"] != incoming[0]["currency_pocket"]
            )
            if valid:
                continue
            for row in group:
                _insert_issue(
                    connection,
                    extraction_run_id,
                    row["raw_record_id"],
                    "BLOCKING",
                    "EXCHANGE_PAIRING_INCOMPLETE",
                    "completed Revolut exchange does not have exactly one opposing currency leg",
                    {"started_at": started_at, "legs_at_timestamp": len(group)},
                )

    @staticmethod
    def _persist_export(
        connection: sqlite3.Connection,
        extraction_run_id: str,
        evidence_sha256: str,
        export: RevolutExport,
    ) -> None:
        connection.execute(
            """
            INSERT INTO accounts(id, institution, source_type, display_name, masked_identifier)
            VALUES (?, 'Revolut', 'REVOLUT_CSV', 'Revolut Current', 'Current')
            ON CONFLICT(id) DO NOTHING
            """,
            (REVOLUT_ACCOUNT_ID,),
        )
        connection.execute(
            """
            UPDATE extraction_runs
            SET header_text = ?, header_cells_json = ?, header_sha256 = ?,
                physical_data_row_count = ?
            WHERE id = ?
            """,
            (
                export.header_text,
                cells_json(export.header_cells),
                export.header_sha256,
                export.physical_data_row_count,
                extraction_run_id,
            ),
        )
        if not export.rows:
            _insert_issue(
                connection,
                extraction_run_id,
                None,
                "BLOCKING",
                "NO_DATA_ROWS",
                "the recognized Revolut header has no physical data rows",
                {},
            )
        for row in export.rows:
            raw_record_id = _stable_id("raw", extraction_run_id, row.source_anchor)
            connection.execute(
                """
                INSERT INTO raw_records(
                    id, extraction_run_id, evidence_sha256, source_profile_version,
                    parser_version, header_sha256, source_anchor, extraction_ordinal,
                    physical_line_start, physical_line_end, record_kind,
                    raw_record_text, raw_cells_json, type_raw, product_raw,
                    started_at_raw, completed_at_raw, description_raw,
                    amount_text, amount_coefficient, amount_scale,
                    fee_text, fee_coefficient, fee_scale, currency_raw, state_raw,
                    balance_text, balance_coefficient, balance_scale,
                    confidence, required_fields_complete
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TRANSACTION', ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    raw_record_id,
                    extraction_run_id,
                    evidence_sha256,
                    SOURCE_PROFILE_VERSION,
                    PARSER_VERSION,
                    export.header_sha256,
                    row.source_anchor,
                    row.ordinal,
                    row.physical_line_start,
                    row.physical_line_end,
                    row.raw_record_text,
                    cells_json(row.cells),
                    _null_if_blank(row.type_raw),
                    _null_if_blank(row.product_raw),
                    _null_if_blank(row.started_at_raw),
                    _null_if_blank(row.completed_at_raw),
                    _null_if_blank(row.description_raw),
                    _null_if_blank(row.amount_text),
                    _coefficient(row.amount),
                    _scale(row.amount),
                    _null_if_blank(row.fee_text),
                    _coefficient(row.fee),
                    _scale(row.fee),
                    _null_if_blank(row.currency_raw),
                    _null_if_blank(row.state_raw),
                    _null_if_blank(row.balance_text),
                    _coefficient(row.balance),
                    _scale(row.balance),
                    "HIGH" if row.required_fields_complete else "UNRESOLVED",
                    int(row.required_fields_complete),
                ),
            )
            for problem in row.problems:
                _insert_issue(
                    connection,
                    extraction_run_id,
                    raw_record_id,
                    problem.severity,
                    problem.code,
                    problem.message,
                    problem.context,
                )
            if row.required_fields_complete:
                _insert_canonical(
                    connection, extraction_run_id, evidence_sha256, raw_record_id, row
                )

    @staticmethod
    def _reconcile(
        connection: sqlite3.Connection,
        extraction_run_id: str,
        export: RevolutExport,
    ) -> int:
        pockets: dict[str, list[tuple[RevolutRow, str]]] = {}
        for row in export.rows:
            if (
                row.required_fields_complete
                and row.currency_raw is not None
                and row.balance is not None
                and row.amount is not None
                and row.fee is not None
            ):
                raw_id = _stable_id("raw", extraction_run_id, row.source_anchor)
                pockets.setdefault(row.currency_raw, []).append((row, raw_id))

        mismatches = 0
        for currency, records in pockets.items():
            if len(records) < 2:
                _insert_issue(
                    connection,
                    extraction_run_id,
                    records[0][1] if records else None,
                    "INFO",
                    "BALANCE_CONTINUITY_UNAVAILABLE",
                    "fewer than two balance-bearing rows are available for this pocket",
                    {"currency_pocket": currency, "balance_rows": len(records)},
                )
            unexpected_order = next(
                (
                    (prior, prior_id, current, current_id)
                    for (prior, prior_id), (current, current_id) in zip(
                        records, records[1:], strict=False
                    )
                    if _effective_source_timestamp(current) < _effective_source_timestamp(prior)
                ),
                None,
            )
            if unexpected_order is not None:
                prior, prior_id, current, current_id = unexpected_order
                mismatches += 1
                _insert_issue(
                    connection,
                    extraction_run_id,
                    current_id,
                    "BLOCKING",
                    "SOURCE_ORDER_UNEXPECTED",
                    "balance-bearing rows are not in non-decreasing source timestamp order",
                    {
                        "currency_pocket": currency,
                        "prior_raw_record_id": prior_id,
                        "prior_timestamp": _effective_source_timestamp(prior),
                        "current_timestamp": _effective_source_timestamp(current),
                    },
                )
                continue
            for (prior, prior_id), (current, current_id) in zip(
                records, records[1:], strict=False
            ):
                # `pockets` only holds rows whose amount, fee, and balance parsed
                # (see the membership test above), so these are all present here.
                assert prior.balance is not None
                assert current.amount is not None
                assert current.fee is not None
                assert current.balance is not None
                negative_fee = ExactMoney(
                    -current.fee.coefficient,
                    current.fee.scale,
                    "-" + current.fee.text,
                )
                try:
                    expected_scale = max(
                        prior.balance.scale,
                        current.amount.scale,
                        current.fee.scale,
                        current.balance.scale,
                    )
                    expected_coefficient, _ = exact_sum(
                        ExactMoney(
                            prior.balance.rescaled_coefficient(expected_scale),
                            expected_scale,
                            prior.balance.text,
                        ),
                        ExactMoney(
                            current.amount.rescaled_coefficient(expected_scale),
                            expected_scale,
                            current.amount.text,
                        ),
                        ExactMoney(
                            negative_fee.rescaled_coefficient(expected_scale),
                            expected_scale,
                            negative_fee.text,
                        ),
                    )
                    actual_coefficient = current.balance.rescaled_coefficient(expected_scale)
                    difference_coefficient = actual_coefficient - expected_coefficient
                    if abs(difference_coefficient) > MAX_ABS_COEFFICIENT:
                        raise OverflowError("reconciliation difference exceeds the configured limit")
                except OverflowError as exc:
                    mismatches += 1
                    _insert_issue(
                        connection,
                        extraction_run_id,
                        current_id,
                        "BLOCKING",
                        "RECONCILIATION_PRECISION_OVERFLOW",
                        "balance equation cannot be represented within configured exact-money limits",
                        {
                            "currency_pocket": currency,
                            "equation_version": BALANCE_EQUATION_VERSION,
                            "reason": str(exc),
                        },
                    )
                    continue
                passed = difference_coefficient == 0
                difference_text = format_decimal(difference_coefficient, expected_scale)
                expected_text = format_decimal(expected_coefficient, expected_scale)
                connection.execute(
                    """
                    INSERT INTO reconciliation_checks(
                        extraction_run_id, currency_pocket, prior_raw_record_id,
                        current_raw_record_id, equation_version, expected_text,
                        expected_coefficient, expected_scale, actual_text,
                        actual_coefficient, actual_scale, difference_text,
                        difference_coefficient, difference_scale, passed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        extraction_run_id,
                        currency,
                        prior_id,
                        current_id,
                        BALANCE_EQUATION_VERSION,
                        expected_text,
                        expected_coefficient,
                        expected_scale,
                        current.balance.text,
                        actual_coefficient,
                        expected_scale,
                        difference_text,
                        difference_coefficient,
                        expected_scale,
                        int(passed),
                    ),
                )
                if not passed:
                    mismatches += 1
                    _insert_issue(
                        connection,
                        extraction_run_id,
                        current_id,
                        "BLOCKING",
                        "BALANCE_CONTINUITY_MISMATCH",
                        "running balance does not satisfy the versioned pocket equation",
                        {
                            "currency_pocket": currency,
                            "equation_version": BALANCE_EQUATION_VERSION,
                            "prior_balance": prior.balance.text,
                            "amount": current.amount.text,
                            "fee": current.fee.text,
                            "expected": expected_text,
                            "actual": current.balance.text,
                            "difference": difference_text,
                        },
                    )
        return mismatches

    @staticmethod
    def _quarantine_profile_mismatch(
        connection: sqlite3.Connection,
        extraction_run_id: str,
        import_run_id: str,
        capture: EvidenceCapture,
        error: ProfileMismatch,
    ) -> None:
        header_sha256 = (
            hashlib.sha256(error.header_text.encode("utf-8")).hexdigest()
            if error.header_text is not None
            else None
        )
        connection.execute(
            """
            UPDATE extraction_runs
            SET completed_at = ?, status = 'QUARANTINED', header_text = ?,
                header_cells_json = ?, header_sha256 = ?, physical_data_row_count = ?,
                raw_record_count = 0, canonical_entry_count = 0
            WHERE id = ?
            """,
            (
                _now(),
                error.header_text,
                cells_json(error.header_cells) if error.header_cells is not None else None,
                header_sha256,
                error.physical_data_row_count,
                extraction_run_id,
            ),
        )
        _insert_issue(
            connection,
            extraction_run_id,
            None,
            "BLOCKING",
            "SOURCE_PROFILE_MISMATCH",
            str(error),
            {
                "source_profile_version": SOURCE_PROFILE_VERSION,
                "physical_data_row_count": error.physical_data_row_count,
            },
        )
        FinCLService._complete_import(
            connection,
            import_run_id,
            capture.sha256,
            extraction_run_id,
            reused=False,
        )
        connection.commit()

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
            UPDATE import_runs
            SET completed_at = ?, status = 'SUCCEEDED', evidence_sha256 = ?,
                extraction_run_id = ?, extraction_reused = ?
            WHERE id = ?
            """,
            (_now(), evidence_sha256, extraction_run_id, int(reused), import_run_id),
        )

    def _result_from_existing(
        self,
        connection: sqlite3.Connection,
        import_run_id: str,
        capture: EvidenceCapture,
        extraction: sqlite3.Row,
    ) -> ImportResult:
        if extraction["status"] == "FAILED":
            connection.execute(
                """
                UPDATE import_runs
                SET completed_at = ?, status = 'FAILED', evidence_sha256 = ?,
                    extraction_run_id = ?, extraction_reused = 1,
                    error_message = 'matching extraction identity previously failed'
                WHERE id = ?
                """,
                (_now(), capture.sha256, extraction["id"], import_run_id),
            )
            connection.commit()
            raise FinCLError("matching extraction identity previously failed")
        self._complete_import(
            connection,
            import_run_id,
            capture.sha256,
            extraction["id"],
            reused=True,
        )
        connection.commit()
        return self._load_result(
            connection,
            import_run_id,
            extraction["id"],
            capture,
            extraction_reused=True,
        )

    @staticmethod
    def _load_result(
        connection: sqlite3.Connection,
        import_run_id: str,
        extraction_run_id: str,
        capture: EvidenceCapture,
        *,
        extraction_reused: bool,
    ) -> ImportResult:
        extraction = connection.execute(
            "SELECT * FROM extraction_runs WHERE id = ?", (extraction_run_id,)
        ).fetchone()
        counts = dict(
            connection.execute(
                """
                SELECT severity, count(*) AS count
                FROM extraction_issues WHERE extraction_run_id = ?
                GROUP BY severity
                """,
                (extraction_run_id,),
            ).fetchall()
        )
        return ImportResult(
            import_run_id=import_run_id,
            extraction_run_id=extraction_run_id,
            evidence_sha256=capture.sha256,
            evidence_reused=capture.reused,
            extraction_reused=extraction_reused,
            status=extraction["status"],
            raw_record_count=extraction["raw_record_count"] or 0,
            canonical_entry_count=extraction["canonical_entry_count"] or 0,
            blocking_issue_count=counts.get("BLOCKING", 0),
            warning_issue_count=counts.get("WARNING", 0),
        )


def _insert_canonical(
    connection: sqlite3.Connection,
    extraction_run_id: str,
    evidence_sha256: str,
    raw_record_id: str,
    row: RevolutRow,
) -> None:
    assert row.started_at_raw is not None
    assert row.amount is not None
    assert row.fee is not None
    assert row.currency_raw is not None
    assert row.state_raw is not None
    canonical_id = _stable_id("entry", extraction_run_id, row.source_anchor)
    natural_anchor = f"{evidence_sha256}:{row.source_anchor}:{row.ordinal}"
    event_kind = re.sub(r"[^A-Z0-9]+", "_", (row.type_raw or "UNKNOWN").upper()).strip("_")
    direction = "IN" if row.amount.coefficient > 0 else "OUT" if row.amount.coefficient < 0 else "ZERO"
    has_alt = bool(row.completed_at_raw) and row.completed_at_raw != row.started_at_raw
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
            ?, 'CSV', ?, ?, ?, NULL, NULL, ?, ?, ?, ?, 'DAY', ?, ?, ?, ?, ?, ?, 0,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL
        )
        """,
        (
            canonical_id,
            extraction_run_id,
            evidence_sha256,
            raw_record_id,
            natural_anchor,
            REVOLUT_ACCOUNT_ID,
            row.currency_raw,
            row.completed_at_raw or row.started_at_raw,
            row.amount.text,
            row.amount.coefficient,
            row.amount.scale,
            event_kind or "UNKNOWN",
            direction,
            row.state_raw,
            "STARTED" if has_alt else None,
            row.started_at_raw if has_alt else None,
            row.started_at_raw if has_alt else None,
            "DAY" if has_alt else None,
            row.fee.text,
            row.fee.coefficient,
            row.fee.scale,
            row.balance.text if row.balance else None,
            _coefficient(row.balance),
            _scale(row.balance),
        ),
    )


def _insert_issue(
    connection: sqlite3.Connection,
    extraction_run_id: str,
    raw_record_id: str | None,
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            extraction_run_id,
            raw_record_id,
            severity,
            code,
            message,
            json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            _now(),
        ),
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _coefficient(value: ExactMoney | None) -> int | None:
    return value.coefficient if value is not None else None


def _scale(value: ExactMoney | None) -> int | None:
    return value.scale if value is not None else None


def _null_if_blank(value: str | None) -> str | None:
    return value if value not in (None, "") else None


def _effective_source_timestamp(row: RevolutRow) -> str:
    assert row.started_at_raw is not None
    return row.completed_at_raw or row.started_at_raw


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")
