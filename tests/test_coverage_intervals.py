"""F23 gate R1: interval-set coverage contract.

Covers the ``docs/DESIGN-recovery-and-application.md`` §2 interval table, date
validation, the monthly-precision boundary, a stale/quarantined required family,
and the refusal to produce a numeric total across a coverage gap.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.errors import FinCLError
from fincl.intervals import (
    DateInterval,
    contains_range,
    intersect_all,
    month_span,
    normalize,
    uncovered_dates,
)
from fincl.report import (
    _analysis_intervals,
    coverage_report_data,
    economic_flow_report_data,
)
from fincl.revolut import EXPECTED_HEADER
from fincl.service import FinCLService

_HEADER = list(EXPECTED_HEADER)


def _csv_bytes(rows: list[list[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(_HEADER)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _row(kind: str, date: str, amount: str, balance: str, description: str) -> list[str]:
    stamp = f"{date} 10:00:00"
    return [kind, "Current", stamp, stamp, description, amount, "0.00", "USD", "COMPLETED", balance]


class IntervalTableTests(unittest.TestCase):
    """The five synthetic rows of DESIGN §2's fixture table."""

    def test_row1_two_disjoint_intervals_survive_and_february_is_refused(self) -> None:
        source_a = normalize(
            [DateInterval("2026-01-01", "2026-01-31"), DateInterval("2026-03-01", "2026-03-31")]
        )
        source_b = normalize([DateInterval("2026-01-01", "2026-03-31")])
        covered = intersect_all([source_a, source_b])
        self.assertEqual(
            covered,
            [
                DateInterval("2026-01-01", "2026-01-31"),
                DateInterval("2026-03-01", "2026-03-31"),
            ],
        )
        with self.assertRaisesRegex(FinCLError, "2026-02-01..2026-02-28"):
            _analysis_intervals(covered, start="2026-02-01", end="2026-02-28")

    def test_row2_adjacent_days_merge_within_a_family(self) -> None:
        merged = normalize(
            [DateInterval("2026-01-01", "2026-01-15"), DateInterval("2026-01-16", "2026-01-31")]
        )
        self.assertEqual(merged, [DateInterval("2026-01-01", "2026-01-31")])
        self.assertEqual(
            intersect_all([merged, [DateInterval("2026-01-10", "2026-01-20")]]),
            [DateInterval("2026-01-10", "2026-01-20")],
        )

    def test_row3_non_overlapping_families_yield_no_coverage(self) -> None:
        self.assertEqual(
            intersect_all(
                [[DateInterval("2026-01-01", "2026-01-31")], [DateInterval("2026-03-01", "2026-03-31")]]
            ),
            [],
        )

    def test_row4_a_required_family_with_no_interval_collapses_coverage(self) -> None:
        self.assertEqual(
            intersect_all([[DateInterval("2026-01-01", "2026-01-31")], []]),
            [],
        )

    def test_row5_identical_spans_keep_their_exact_first_and_last_dates(self) -> None:
        self.assertEqual(
            intersect_all(
                [[DateInterval("2026-01-01", "2026-01-31")], [DateInterval("2026-01-01", "2026-01-31")]]
            ),
            [DateInterval("2026-01-01", "2026-01-31")],
        )

    def test_uncovered_dates_names_every_gap_span(self) -> None:
        covered = [
            DateInterval("2026-01-01", "2026-01-31"),
            DateInterval("2026-03-01", "2026-03-31"),
        ]
        self.assertFalse(contains_range(covered, "2026-01-15", "2026-03-10"))
        self.assertEqual(
            uncovered_dates(covered, "2026-01-15", "2026-03-10"),
            [DateInterval("2026-02-01", "2026-02-28")],
        )


class IntervalValidationTests(unittest.TestCase):
    def test_unreal_calendar_date_is_refused(self) -> None:
        with self.assertRaisesRegex(FinCLError, "real YYYY-MM-DD"):
            DateInterval("2026-02-30", "2026-03-01")

    def test_start_after_end_is_refused(self) -> None:
        with self.assertRaisesRegex(FinCLError, "start is after its end"):
            DateInterval("2026-03-05", "2026-03-01")

    def test_explicit_window_needs_two_ordered_iso_dates(self) -> None:
        covered = [DateInterval("2026-01-01", "2026-01-31")]
        with self.assertRaisesRegex(FinCLError, "YYYY-MM-DD"):
            _analysis_intervals(covered, start="2026-13-01", end="2026-01-05")
        with self.assertRaisesRegex(FinCLError, "--start and --end together"):
            _analysis_intervals(covered, start="2026-01-05", end=None)


class MonthlyPrecisionBoundaryTests(unittest.TestCase):
    def test_month_span_covers_the_whole_calendar_month(self) -> None:
        self.assertEqual(month_span("2026-02"), DateInterval("2026-02-01", "2026-02-28"))
        self.assertEqual(month_span("2026-12"), DateInterval("2026-12-01", "2026-12-31"))

    def test_a_month_entry_needs_its_entire_span_inside_the_analysis_window(self) -> None:
        february = month_span("2026-02")
        self.assertTrue(DateInterval("2026-02-01", "2026-02-28").contains_interval(february))
        self.assertFalse(DateInterval("2026-02-05", "2026-02-28").contains_interval(february))
        self.assertFalse(DateInterval("2026-02-01", "2026-02-27").contains_interval(february))


class CoverageContractDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "fincl.sqlite3"
        source = self.root / "revolut.csv"
        source.write_bytes(
            _csv_bytes(
                [
                    _row("Topup", "2026-01-05", "200.00", "200.00", "Seed"),
                    _row("Card Payment", "2026-02-10", "-11.00", "189.00", "February"),
                    _row("Card Payment", "2026-03-20", "-12.00", "177.00", "March"),
                ]
            )
        )
        FinCLService.plaintext_phase1(self.database, self.root / "evidence").import_revolut(source)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        return connection

    def _add_pdf_statement(
        self, *, period_start: str, period_end: str, run_status: str, selection_state: str
    ) -> None:
        connection = self._connect()
        account_id = connection.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        sha = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
        connection.execute(
            """
            INSERT INTO source_content(
                sha256, byte_size, media_type, evidence_key, storage_kind,
                original_path, last_observed_path, first_seen_at, last_seen_at,
                integrity_status
            ) VALUES (?, 1, 'application/pdf', ?, 'PLAINTEXT', 'synthetic.pdf',
                      'synthetic.pdf', '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z',
                      'VERIFIED')
            """,
            (sha, "evidence/" + sha),
        )
        run_id = "run_" + uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO extraction_runs(
                id, evidence_sha256, source_profile_version, parser_version,
                extraction_policy_version, started_at, status, selection_state
            ) VALUES (?, ?, 'dbs-consolidated-1', 'parser-1', 'policy-1',
                      '2026-04-01T00:00:00Z', ?, ?)
            """,
            (
                run_id,
                sha,
                run_status,
                "SELECTED" if selection_state == "SELECTED" else "QUARANTINED",
            ),
        )
        connection.execute(
            """
            INSERT INTO statements(
                id, extraction_run_id, evidence_sha256, account_id, institution,
                source_type, statement_date, period_start, period_end,
                statement_identity, selection_state, conflict_state
            ) VALUES (?, ?, ?, ?, 'Synthetic', 'DBS_CONSOLIDATED', ?, ?, ?, ?, ?, 'NONE')
            """,
            (
                "stmt_" + uuid.uuid4().hex,
                run_id,
                sha,
                account_id,
                period_end,
                period_start,
                period_end,
                "synthetic:" + run_id,
                selection_state,
            ),
        )
        connection.commit()

    def test_single_revolut_source_is_covered_with_observed_entry_span_basis(self) -> None:
        data = economic_flow_report_data(self.database)
        self.assertEqual(data["coverage_policy_version"], "coverage-intervals-v1")
        self.assertEqual(data["coverage_status"], "COVERED")
        self.assertEqual(data["required_sources"], ["REVOLUT_CSV"])
        self.assertEqual(
            data["covered_intervals"], [{"start": "2026-01-05", "end": "2026-03-20"}]
        )
        revolut = data["source_intervals"]["REVOLUT_CSV"]
        self.assertEqual(revolut["basis"], "observed_entry_span")
        self.assertFalse(revolut["completeness_established"])
        self.assertTrue(revolut["warnings"])

    def test_quarantined_pdf_family_stays_required_and_makes_coverage_unavailable(self) -> None:
        self._add_pdf_statement(
            period_start="2026-01-01",
            period_end="2026-01-31",
            run_status="QUARANTINED",
            selection_state="QUARANTINED",
        )
        data = economic_flow_report_data(self.database)
        self.assertEqual(data["required_sources"], ["DBS_CONSOLIDATED", "REVOLUT_CSV"])
        self.assertEqual(data["coverage_status"], "UNAVAILABLE")
        self.assertEqual(data["covered_intervals"], [])
        self.assertIn(
            "DBS_CONSOLIDATED", " ".join(data["coverage_status_reasons"])
        )
        self.assertIsNone(data["consolidated_sgd_total"])
        self.assertEqual(
            data["consolidated_sgd_total_status"], "UNAVAILABLE_COVERAGE_INCOMPLETE"
        )

    def test_empty_export_does_not_invent_bounds(self) -> None:
        connection = self._connect()
        connection.execute("DELETE FROM ledger_entries")
        connection.commit()
        data = economic_flow_report_data(self.database)
        self.assertEqual(data["required_sources"], ["REVOLUT_CSV"])
        self.assertEqual(data["covered_intervals"], [])
        self.assertEqual(data["coverage_status"], "UNAVAILABLE")
        self.assertIsNone(data["consolidated_sgd_total"])

    def test_disjoint_selected_pdf_periods_produce_a_gapped_status(self) -> None:
        self._add_pdf_statement(
            period_start="2026-01-01",
            period_end="2026-01-31",
            run_status="ACCEPTED",
            selection_state="SELECTED",
        )
        self._add_pdf_statement(
            period_start="2026-03-01",
            period_end="2026-03-31",
            run_status="ACCEPTED",
            selection_state="SELECTED",
        )
        data = economic_flow_report_data(self.database)
        self.assertEqual(data["coverage_status"], "GAPPED")
        self.assertEqual(
            data["covered_intervals"],
            [
                {"start": "2026-01-05", "end": "2026-01-31"},
                {"start": "2026-03-01", "end": "2026-03-20"},
            ],
        )
        # No numeric total across a gap without an explicit covered range.
        self.assertIsNone(data["consolidated_sgd_total"])
        self.assertEqual(
            data["consolidated_sgd_total_status"], "UNAVAILABLE_COVERAGE_INCOMPLETE"
        )

    def test_explicit_window_inside_one_gap_free_interval_is_accepted(self) -> None:
        self._add_pdf_statement(
            period_start="2026-01-01",
            period_end="2026-01-31",
            run_status="ACCEPTED",
            selection_state="SELECTED",
        )
        self._add_pdf_statement(
            period_start="2026-03-01",
            period_end="2026-03-31",
            run_status="ACCEPTED",
            selection_state="SELECTED",
        )
        scoped = economic_flow_report_data(
            self.database, start="2026-01-10", end="2026-01-20"
        )
        self.assertEqual(scoped["analysis_window_kind"], "EXPLICIT")
        self.assertEqual(
            scoped["analysis_intervals"], [{"start": "2026-01-10", "end": "2026-01-20"}]
        )
        self.assertTrue(scoped["coverage_complete"])
        self.assertEqual(scoped["requested_window"], {"start": "2026-01-10", "end": "2026-01-20"})

    def test_explicit_window_crossing_a_gap_is_refused_with_the_uncovered_dates(self) -> None:
        self._add_pdf_statement(
            period_start="2026-01-01",
            period_end="2026-01-31",
            run_status="ACCEPTED",
            selection_state="SELECTED",
        )
        self._add_pdf_statement(
            period_start="2026-03-01",
            period_end="2026-03-31",
            run_status="ACCEPTED",
            selection_state="SELECTED",
        )
        with self.assertRaisesRegex(FinCLError, "2026-02-01..2026-02-28"):
            economic_flow_report_data(self.database, start="2026-01-15", end="2026-03-10")

    def test_coverage_command_exposes_the_contract(self) -> None:
        data = coverage_report_data(self.database)
        self.assertEqual(data["coverage_policy_version"], "coverage-intervals-v1")
        self.assertEqual(data["coverage_status"], "COVERED")
        self.assertEqual(data["required_sources"], ["REVOLUT_CSV"])

    def test_cli_economic_flow_json_carries_the_contract_fields(self) -> None:
        result = CliRunner().invoke(
            app, ["economic-flow", "--database", str(self.database), "--json"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["coverage_status"], "COVERED")
        self.assertEqual(payload["coverage_policy_version"], "coverage-intervals-v1")
        self.assertEqual(payload["covered_intervals"], [{"start": "2026-01-05", "end": "2026-03-20"}])


if __name__ == "__main__":
    unittest.main()
