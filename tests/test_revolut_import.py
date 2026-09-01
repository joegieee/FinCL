from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from fincl.cli import app
from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import default_journal_path, read_records
from fincl.errors import FinCLError
from fincl.report import build_report
from fincl.revolut import (
    BALANCE_EQUATION_VERSION,
    EXPECTED_HEADER,
    SOURCE_PROFILE_VERSION,
)
from fincl.service import FinCLService

GOOD_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed USD", "100.00", "0.00", "USD", "COMPLETED", "100.00"],
    ["Topup", "Current", "2026-01-01 10:01:00", "2026-01-01 10:01:01", "Seed SGD", "20.00", "0.00", "SGD", "COMPLETED", "20.00"],
    ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01", "Shop, branch", "-10.00", "1.00", "USD", "COMPLETED", "89.00"],
    ["Card Payment", "Current", "2026-01-02 12:00:00", "2026-01-02 12:00:01", "Two-line\nmerchant", "-5.00", "0.50", "SGD", "COMPLETED", "14.50"],
    ["Card Payment", "Current", "2026-01-03 13:00:00", "", "Reverted", "-3.00", "0.25", "USD", "REVERTED", ""],
    ["Card Payment", "Current", "2026-01-04 14:00:00", "", "Pending", "-2.00", "0.00", "USD", "PENDING", ""],
]


def csv_bytes(rows=GOOD_ROWS, header=EXPECTED_HEADER) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


class RevolutImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "revolut.csv"
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        self.service = FinCLService.plaintext_phase1(self.database, self.evidence)

    def tearDown(self):
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def test_complete_provenance_lifecycle_pockets_and_timestamps(self):
        source_bytes = csv_bytes()
        self.source.write_bytes(source_bytes)
        source_before = self.source.stat()

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "ACCEPTED")
        self.assertEqual(result.raw_record_count, 6)
        self.assertEqual(result.ledger_entry_count, 6)
        self.assertEqual(result.warning_issue_count, 1)
        self.assertEqual(result.blocking_issue_count, 0)
        self.assertEqual(self.source.read_bytes(), source_bytes)
        source_after = self.source.stat()
        self.assertEqual(source_before.st_mtime_ns, source_after.st_mtime_ns)

        with self.connect() as connection:
            extraction = connection.execute("SELECT * FROM extraction_runs").fetchone()
            self.assertEqual(extraction["source_profile_version"], SOURCE_PROFILE_VERSION)
            self.assertEqual(extraction["physical_data_row_count"], 6)
            self.assertEqual(extraction["raw_record_count"], 6)
            self.assertIsNotNone(extraction["started_at"])
            self.assertIsNotNone(extraction["completed_at"])
            self.assertEqual(extraction["header_cells_json"], '["Type","Product","Started Date","Completed Date","Description","Amount","Fee","Currency","State","Balance"]')
            raw = connection.execute(
                "SELECT * FROM raw_records WHERE extraction_ordinal = 4"
            ).fetchone()
            self.assertEqual(raw["description_raw"], "Two-line\nmerchant")
            self.assertEqual(raw["physical_line_end"], raw["physical_line_start"] + 1)
            self.assertIn('"Two-line\nmerchant"', raw["raw_record_text"])
            self.assertEqual(raw["amount_text"], "-5.00")
            self.assertEqual((raw["amount_coefficient"], raw["amount_scale"]), (-500, 2))
            self.assertEqual((raw["fee_coefficient"], raw["fee_scale"]), (50, 2))
            self.assertEqual((raw["balance_coefficient"], raw["balance_scale"]), (1450, 2))
            self.assertTrue(raw["source_anchor"].startswith("csv:"))
            self.assertEqual(raw["evidence_sha256"], hashlib.sha256(source_bytes).hexdigest())

            states = dict(
                connection.execute(
                    "SELECT lifecycle_state, count(*) FROM ledger_entries "
                    "WHERE source_family = 'CSV' GROUP BY lifecycle_state"
                ).fetchall()
            )
            self.assertEqual(states, {"COMPLETED": 4, "PENDING": 1, "REVERTED": 1})
            eligibility = dict(
                connection.execute(
                    "SELECT lifecycle_state, sum(analytics_eligible) FROM ledger_entries "
                    "WHERE source_family = 'CSV' GROUP BY lifecycle_state"
                ).fetchall()
            )
            self.assertEqual(eligibility, {"COMPLETED": 4, "PENDING": 0, "REVERTED": 0})
            pending = connection.execute(
                "SELECT * FROM ledger_entries WHERE lifecycle_state = 'PENDING'"
            ).fetchone()
            self.assertEqual(pending["effective_at"], "2026-01-04 14:00:00")
            self.assertIsNone(pending["alt_date_role"])
            pending_raw = connection.execute(
                "SELECT * FROM raw_records WHERE state_raw = 'PENDING'"
            ).fetchone()
            self.assertIsNone(pending_raw["completed_at_raw"])
            self.assertIsNone(pending_raw["balance_text"])
            self.assertTrue(pending_raw["raw_record_text"].endswith(",USD,PENDING,\r\n"))
            checks = connection.execute(
                "SELECT * FROM reconciliation_checks ORDER BY currency_pocket"
            ).fetchall()
            self.assertEqual(len(checks), 2)
            self.assertTrue(all(row["passed"] for row in checks))
            self.assertTrue(all(row["equation_version"] == BALANCE_EQUATION_VERSION for row in checks))
            self.assertEqual({row["currency_pocket"] for row in checks}, {"SGD", "USD"})
            account = connection.execute(
                "SELECT institution, source_type, display_name, masked_identifier FROM accounts"
            ).fetchone()
            self.assertEqual(
                tuple(account),
                ("Revolut", "REVOLUT_CSV", "Revolut Current", "Current"),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries "
                    "WHERE account_id NOT IN (SELECT id FROM accounts)"
                ).fetchone()[0],
                0,
            )

    def test_exact_reimport_reuses_evidence_and_extraction_without_domain_duplicates(self):
        self.source.write_bytes(csv_bytes())
        first = self.service.import_revolut(self.source)
        object_path = self.evidence / f"sha256/{first.evidence_sha256[:2]}/{first.evidence_sha256}"
        evidence_mtime = object_path.stat().st_mtime_ns

        second = self.service.import_revolut(self.source)

        self.assertTrue(second.evidence_reused)
        self.assertTrue(second.extraction_reused)
        self.assertEqual(first.extraction_run_id, second.extraction_run_id)
        self.assertEqual(object_path.stat().st_mtime_ns, evidence_mtime)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM source_content").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM extraction_runs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM raw_records").fetchone()[0], 6)
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries WHERE source_family = 'CSV'"
                ).fetchone()[0],
                6,
            )
            self.assertEqual(connection.execute("SELECT count(*) FROM import_runs").fetchone()[0], 2)
            reuse_flags = [
                row[0]
                for row in connection.execute(
                    "SELECT extraction_reused FROM import_runs ORDER BY started_at"
                )
            ]
            self.assertEqual(reuse_flags, [0, 1])

    def test_balance_mismatch_quarantines_all_canonical_analytics(self):
        rows = [list(row) for row in GOOD_ROWS]
        rows[2][-1] = "88.99"
        self.source.write_bytes(csv_bytes(rows))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "QUARANTINED")
        self.assertGreaterEqual(result.blocking_issue_count, 1)
        with self.connect() as connection:
            failed = connection.execute(
                "SELECT * FROM reconciliation_checks WHERE passed = 0"
            ).fetchone()
            self.assertEqual(failed["currency_pocket"], "USD")
            self.assertEqual(failed["expected_text"], "89.00")
            self.assertEqual(failed["actual_text"], "88.99")
            self.assertEqual(failed["difference_text"], "-0.01")
            self.assertEqual(
                connection.execute("SELECT sum(analytics_eligible) FROM ledger_entries").fetchone()[0],
                0,
            )

    def test_unexpected_source_order_is_distinct_from_balance_corruption(self):
        rows = [
            ["Topup", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01", "Later", "10.00", "0.00", "USD", "COMPLETED", "110.00"],
            ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Earlier", "100.00", "0.00", "USD", "COMPLETED", "100.00"],
        ]
        self.source.write_bytes(csv_bytes(rows))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "QUARANTINED")
        with self.connect() as connection:
            codes = [row[0] for row in connection.execute("SELECT code FROM extraction_issues")]
            self.assertIn("SOURCE_ORDER_UNEXPECTED", codes)
            self.assertNotIn("BALANCE_CONTINUITY_MISMATCH", codes)
            self.assertEqual(connection.execute("SELECT count(*) FROM reconciliation_checks").fetchone()[0], 0)

    def test_failed_extraction_can_be_recovered_with_durable_history(self):
        self.source.write_bytes(csv_bytes())
        with (
            patch("fincl.service.parse_revolut_csv", side_effect=RuntimeError("synthetic failure")),
            self.assertRaises(RuntimeError),
        ):
            self.service.import_revolut(self.source)
        with self.connect() as connection:
            failed = connection.execute(
                "SELECT id, evidence_sha256 FROM extraction_runs WHERE status = 'FAILED'"
            ).fetchone()
            failed_id = failed["id"]
            connection.execute(
                """
                INSERT INTO raw_records(
                    id, extraction_run_id, evidence_sha256, source_profile_version,
                    parser_version, header_sha256, source_anchor, extraction_ordinal,
                    physical_line_start, physical_line_end, record_kind,
                    raw_record_text, raw_cells_json, confidence, required_fields_complete
                ) VALUES ('raw_partial', ?, ?, 'test-profile', 'test-parser', ?,
                          'partial-anchor', 1, 1, 1, 'TRANSACTION', 'partial', '[]',
                          'UNRESOLVED', 0)
                """,
                (failed_id, failed["evidence_sha256"], "0" * 64),
            )
            connection.execute(
                """
                INSERT INTO extraction_issues(
                    extraction_run_id, raw_record_id, severity, code, message,
                    context_json, created_at
                ) VALUES (?, 'raw_partial', 'BLOCKING', 'PARTIAL_TEST',
                          'synthetic partial row', '{}', '2026-09-01T00:00:00+00:00')
                """,
                (failed_id,),
            )
        with self.assertRaises(FinCLError):
            self.service.import_revolut(self.source)

        recovery = CliRunner().invoke(
            app,
            [
                "recover-extraction",
                failed_id,
                "--database",
                str(self.database),
                "--reason",
                "retry synthetic failure",
            ],
        )
        self.assertEqual(recovery.exit_code, 0, recovery.output)
        self.assertIn("status: RECOVERED", recovery.output)
        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "ACCEPTED")
        self.assertEqual(result.extraction_run_id, failed_id)
        with self.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        event = read_records(default_journal_path(self.database))[-1]
        self.assertEqual(event.anchors["extraction_identity"], failed_id)
        self.assertEqual(event.payload["reason"], "retry synthetic failure")

    def test_invalid_money_is_retained_as_unresolved_raw_and_quarantined(self):
        rows = [list(row) for row in GOOD_ROWS]
        rows[2][5] = "1e2"
        self.source.write_bytes(csv_bytes(rows))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "QUARANTINED")
        self.assertEqual(result.raw_record_count, 6)
        self.assertEqual(result.ledger_entry_count, 5)
        with self.connect() as connection:
            raw = connection.execute(
                "SELECT * FROM raw_records WHERE extraction_ordinal = 3"
            ).fetchone()
            self.assertEqual(raw["amount_text"], "1e2")
            self.assertIsNone(raw["amount_coefficient"])
            self.assertEqual(raw["confidence"], "UNRESOLVED")
            self.assertEqual(raw["required_fields_complete"], 0)

    def test_reconciliation_rescale_overflow_is_a_blocking_issue(self):
        rows = [
            [
                "Topup", "Current", "2026-01-01 10:00:00",
                "2026-01-01 10:00:01", "Limit", "9000000000000000000",
                "0", "USD", "COMPLETED", "9000000000000000000",
            ],
            [
                "Topup", "Current", "2026-01-01 10:01:00",
                "2026-01-01 10:01:01", "Fraction", "0.1", "0.0", "USD",
                "COMPLETED", "9000000000000000000",
            ],
        ]
        self.source.write_bytes(csv_bytes(rows))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "QUARANTINED")
        with self.connect() as connection:
            issue = connection.execute(
                "SELECT code FROM extraction_issues WHERE severity = 'BLOCKING'"
            ).fetchone()[0]
            self.assertEqual(issue, "RECONCILIATION_PRECISION_OVERFLOW")

    def test_profile_header_mismatch_is_visible_and_quarantined(self):
        header = list(EXPECTED_HEADER)
        header[0] = "Transaction Type"
        self.source.write_bytes(csv_bytes(header=header))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "QUARANTINED")
        self.assertEqual(result.raw_record_count, 0)
        self.assertEqual(result.blocking_issue_count, 1)
        with self.connect() as connection:
            issue = connection.execute("SELECT * FROM extraction_issues").fetchone()
            self.assertEqual(issue["code"], "SOURCE_PROFILE_MISMATCH")

    def test_product_outside_frozen_profile_is_preserved_and_quarantined(self):
        rows = [list(row) for row in GOOD_ROWS]
        rows[0][1] = "Savings"
        self.source.write_bytes(csv_bytes(rows))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "QUARANTINED")
        with self.connect() as connection:
            raw = connection.execute(
                "SELECT product_raw, required_fields_complete FROM raw_records WHERE extraction_ordinal = 1"
            ).fetchone()
            self.assertEqual(tuple(raw), ("Savings", 0))
            issue = connection.execute(
                "SELECT code FROM extraction_issues WHERE raw_record_id IS NOT NULL AND code = 'UNSUPPORTED_PRODUCT'"
            ).fetchone()
            self.assertIsNotNone(issue)

    def test_overlapping_exports_retain_pending_to_completed_lifecycle(self):
        pending = [[
            "Card Payment", "Current", "2026-01-04 14:00:00", "", "Transit",
            "-2.00", "0.00", "USD", "PENDING", "",
        ]]
        completed = [[
            "Card Payment", "Current", "2026-01-04 14:00:00",
            "2026-01-05 14:00:00", "Transit", "-2.00", "0.00", "USD",
            "COMPLETED", "98.00",
        ]]
        self.source.write_bytes(csv_bytes(pending))
        first = self.service.import_revolut(self.source)
        self.source.write_bytes(csv_bytes(completed))
        second = self.service.import_revolut(self.source)

        self.assertNotEqual(first.extraction_run_id, second.extraction_run_id)
        with self.connect() as connection:
            states = [
                row[0]
                for row in connection.execute(
                    "SELECT lifecycle_state FROM ledger_entries ORDER BY lifecycle_state"
                )
            ]
            self.assertEqual(states, ["COMPLETED", "PENDING"])
            self.assertEqual(connection.execute("SELECT count(*) FROM source_content").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM extraction_runs").fetchone()[0], 2)

    def test_identical_legitimate_rows_receive_distinct_stable_anchors(self):
        duplicate_pending = [
            [
                "Card Payment", "Current", "2026-01-04 14:00:00", "", "Transit",
                "-2.00", "0.00", "USD", "PENDING", "",
            ],
            [
                "Card Payment", "Current", "2026-01-04 14:00:00", "", "Transit",
                "-2.00", "0.00", "USD", "PENDING", "",
            ],
        ]
        self.source.write_bytes(csv_bytes(duplicate_pending))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "ACCEPTED")
        with self.connect() as connection:
            anchors = [row[0] for row in connection.execute("SELECT source_anchor FROM raw_records")]
            self.assertEqual(len(anchors), 2)
            self.assertEqual(len(set(anchors)), 2)

    def test_empty_recognized_export_is_quarantined(self):
        self.source.write_bytes(csv_bytes([]))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "QUARANTINED")
        self.assertEqual(result.raw_record_count, 0)
        with self.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT code FROM extraction_issues").fetchone()[0],
                "NO_DATA_ROWS",
            )

    def test_report_is_inspectable_and_keeps_lifecycle_counts_visible(self):
        self.source.write_bytes(csv_bytes())
        self.service.import_revolut(self.source)

        report = build_report(self.database)

        self.assertIn("status: ACCEPTED", report)
        self.assertIn("physical_rows=6 raw_records=6 ledger_entries=6", report)
        self.assertIn("balance_checks: passed=2 total=2", report)
        self.assertIn("pocket USD: COMPLETED=2 PENDING=1 REVERTED=1", report)
        self.assertIn("pocket SGD: COMPLETED=2 PENDING=0 REVERTED=0", report)

    def test_event_kind_is_normalized_to_the_canonical_vocabulary(self):
        rows = [
            ["Card Payment", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Buy", "-10.00", "0.00", "SGD", "COMPLETED", "90.00"],
            ["Card Refund", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01", "Back", "5.00", "0.00", "SGD", "COMPLETED", "95.00"],
            ["Interest", "Current", "2026-01-03 10:00:00", "2026-01-03 10:00:01", "Odd", "1.00", "0.00", "SGD", "COMPLETED", "96.00"],
        ]
        self.source.write_bytes(csv_bytes(rows))

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "ACCEPTED")
        with self.connect() as connection:
            kinds = dict(
                connection.execute(
                    "SELECT amount_text, event_kind FROM ledger_entries ORDER BY effective_at"
                ).fetchall()
            )
        self.assertEqual(kinds["-10.00"], "CARD_PAYMENT")
        self.assertEqual(kinds["5.00"], "REFUND")
        self.assertEqual(kinds["1.00"], "OTHER")  # unmapped Revolut type

    def test_schema_has_no_real_money_storage(self):
        connection = connect_sqlite(self.database)
        initialize_schema(connection)
        for table in (
            "raw_records",
            "ledger_entries",
            "reconciliation_checks",
        ):
            declared_types = {row[2].upper() for row in connection.execute(f"PRAGMA table_info({table})")}
            self.assertNotIn("REAL", declared_types)
        security = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'storage_security'"
        ).fetchone()[0]
        self.assertEqual(security, "plaintext-phase1")
        connection.close()


if __name__ == "__main__":
    unittest.main()
