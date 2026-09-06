from __future__ import annotations

import csv
import io
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from fincl.db import (
    BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    connect_sqlite,
    initialize_schema,
)
from fincl.decision_journal import default_journal_path, read_records
from fincl.errors import FinCLError, SchemaVersionError
from fincl.intervals import DateInterval, intersect_all, normalize
from fincl.rebuild import rebuild_database
from fincl.recovery import recover_extraction
from fincl.report import (
    _analysis_intervals,
    build_profile_report,
    economic_flow_report_data,
    profile_report_data,
)
from fincl.revolut import EXPECTED_HEADER
from fincl.service import FinCLService


@contextmanager
def open_database(
    path: Path, *, row_factory: object = None
) -> Iterator[sqlite3.Connection]:
    """Commit or roll back like ``sqlite3``'s own context manager, then close.

    Using a raw connection as a context manager ends the transaction but leaves
    the connection open, which is where this suite's unclosed-database
    ResourceWarnings came from.
    """
    connection = sqlite3.connect(path)
    if row_factory is not None:
        connection.row_factory = row_factory  # type: ignore[assignment]
    try:
        with connection:
            yield connection
    finally:
        connection.close()


GOOD_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "100.00", "0.00", "USD", "COMPLETED", "100.00"],
    ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01", "Shop", "-10.00", "1.00", "USD", "COMPLETED", "89.00"],
]


def _csv_bytes(rows: list[list[str]] | None = None) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(EXPECTED_HEADER)
    writer.writerows(rows or GOOD_ROWS)
    return output.getvalue().encode("utf-8")


class SchemaVersionGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "fincl.sqlite3"

    def test_connect_sets_busy_timeout(self):
        connection = connect_sqlite(self.database)
        self.addCleanup(connection.close)
        timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, BUSY_TIMEOUT_MS)

    def test_initialize_is_idempotent_and_records_current_version(self):
        connection = connect_sqlite(self.database)
        self.addCleanup(connection.close)
        initialize_schema(connection)
        initialize_schema(connection)
        stored = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0]
        self.assertEqual(int(stored), SCHEMA_VERSION)

    def test_newer_schema_version_is_refused(self):
        connection = connect_sqlite(self.database)
        self.addCleanup(connection.close)
        initialize_schema(connection)
        connection.execute(
            "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
        connection.commit()
        with self.assertRaises(SchemaVersionError):
            initialize_schema(connection)

    def test_older_schema_version_without_migration_is_refused(self):
        connection = connect_sqlite(self.database)
        self.addCleanup(connection.close)
        initialize_schema(connection)
        connection.execute(
            "UPDATE schema_metadata SET value = '1' WHERE key = 'schema_version'"
        )
        connection.commit()
        with self.assertRaisesRegex(SchemaVersionError, "preserve the database") as caught:
            initialize_schema(connection)

        self.assertNotIn("re-import into a fresh database", str(caught.exception))

    def test_non_integer_schema_version_is_refused(self):
        connection = connect_sqlite(self.database)
        self.addCleanup(connection.close)
        initialize_schema(connection)
        connection.execute(
            "UPDATE schema_metadata SET value = 'garbage' WHERE key = 'schema_version'"
        )
        connection.commit()
        with self.assertRaises(SchemaVersionError):
            initialize_schema(connection)

    def test_event_kind_check_rejects_a_value_outside_the_vocabulary(self):
        connection = connect_sqlite(self.database)
        self.addCleanup(connection.close)
        initialize_schema(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO ledger_entries(
                    id, source_family, extraction_run_id, evidence_sha256,
                    raw_record_id, natural_anchor, account_id, currency_pocket,
                    effective_at, amount_text, amount_coefficient, amount_scale,
                    event_kind, economic_direction, lifecycle_state, analytics_eligible
                ) VALUES ('e1', 'CSV', 'x', 'y', 'r', 'a', 'acct', 'SGD',
                          '2026-01-01', '1.00', 100, 2, 'WHATEVER', 'IN',
                          'COMPLETED', 0)
                """
            )

    def test_superseded_schema_versions_are_refused_not_migrated(self):
        """No in-place migration path exists; a stale database is rebuilt instead."""
        connection = connect_sqlite(self.database)
        self.addCleanup(connection.close)
        initialize_schema(connection)
        for stale in ("3", "4", "5", "6"):
            connection.execute(
                "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
                (stale,),
            )
            connection.commit()
            with self.assertRaises(SchemaVersionError):
                initialize_schema(connection)


class RunningExtractionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = root / "fincl.sqlite3"
        self.service = FinCLService.plaintext_phase1(self.database, root / "evidence")

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return open_database(self.database, row_factory=sqlite3.Row)

    def _stall_extraction(self) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT id FROM extraction_runs").fetchone()
            connection.execute(
                "UPDATE extraction_runs SET status = 'RUNNING', completed_at = NULL WHERE id = ?",
                (row["id"],),
            )
        return row["id"]

    def test_second_import_refuses_while_extraction_is_running(self):
        first = self.service.import_revolut(self.source)
        self.assertEqual(first.status, "ACCEPTED")
        self._stall_extraction()
        with self.assertRaises(FinCLError):
            self.service.import_revolut(self.source)
        with self._connect() as connection:
            failed = connection.execute(
                "SELECT count(*) FROM import_runs WHERE status = 'FAILED'"
            ).fetchone()[0]
        self.assertEqual(failed, 1)

    def test_recover_running_requires_explicit_acknowledgement(self):
        self.service.import_revolut(self.source)
        extraction_id = self._stall_extraction()

        with self.assertRaises(FinCLError):
            recover_extraction(self.database, extraction_id, "no importer active")

        result = recover_extraction(
            self.database,
            extraction_id,
            "confirmed no importer active",
            allow_running=True,
        )
        self.assertEqual(result.prior_status, "RUNNING")
        with self._connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM extraction_runs WHERE id = ?", (extraction_id,)
                ).fetchone()
            )
        event = read_records(default_journal_path(self.database))[-1]
        self.assertEqual(event.payload["prior_status"], "RUNNING")
        self.assertEqual(event.payload["reason"], "confirmed no importer active")

        replayed = self.service.import_revolut(self.source)
        self.assertEqual(replayed.status, "ACCEPTED")

    def test_repair_retains_a_local_recovery_event(self):
        self.service.import_revolut(self.source)
        extraction_id = self._stall_extraction()

        result = recover_extraction(
            self.database,
            extraction_id,
            "confirmed no importer active",
            allow_running=True,
        )

        event = read_records(default_journal_path(self.database))[-1]
        self.assertEqual(event.payload["event_id"], result.event_id)
        self.assertEqual(event.anchors["extraction_identity"], extraction_id)
        self.assertEqual(event.payload["reason"], "confirmed no importer active")


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = root / "fincl.sqlite3"
        self.evidence = root / "evidence"
        self.service = FinCLService.plaintext_phase1(self.database, self.evidence)

    def test_rebuild_recreates_accepted_entries_from_evidence(self):
        imported = self.service.import_revolut(self.source)
        output = Path(self.temporary.name) / "rebuilt.sqlite3"

        result = rebuild_database(self.database, self.evidence, output)

        self.assertEqual(result.sources_rebuilt, 1)
        self.assertEqual(result.accepted_extractions, 1)
        with open_database(output) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries WHERE source_family = 'CSV'"
                ).fetchone()[0],
                imported.ledger_entry_count,
            )
            self.assertEqual(
                connection.execute("SELECT original_path FROM source_content").fetchone()[0],
                str(self.source.resolve()),
            )

    def test_rebuild_refuses_to_overwrite_existing_database(self):
        self.service.import_revolut(self.source)
        with self.assertRaises(FinCLError):
            rebuild_database(self.database, self.evidence, self.database)

    def test_profile_reports_observations_without_corpus_assertions(self):
        self.service.import_revolut(self.source)

        data = profile_report_data(self.database)

        self.assertEqual(len(data["profiles"]), 1)
        self.assertEqual(data["profiles"][0]["source_profile_version"], "revolut-csv-en-sg-2026-v1")
        self.assertIn("revolut-csv-en-sg-2026-v1", build_profile_report(self.database))

    def test_revolut_exchange_is_linked_with_reduced_implied_rate(self):
        self.source.write_bytes(
            _csv_bytes(
                [
                    ["Exchange", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "SGD to USD", "-100.00", "1.00", "SGD", "COMPLETED", "899.00"],
                    ["Exchange", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "SGD to USD", "75.00", "0.00", "USD", "COMPLETED", "75.00"],
                ]
            )
        )
        self.service.import_revolut(self.source)

        with open_database(self.database) as connection:
            link = connection.execute(
                """
                SELECT link_kind, left_currency, left_amount_text, right_currency,
                       right_amount_text, implied_rate_numerator, implied_rate_denominator
                FROM transfer_links
                """
            ).fetchone()
        self.assertEqual(link, ("CURRENCY_EXCHANGE", "SGD", "100.00", "USD", "75.00", 3, 4))

    def test_incomplete_revolut_exchange_is_quarantined(self):
        self.source.write_bytes(
            _csv_bytes(
                [["Exchange", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "SGD to USD", "-100.00", "0.00", "SGD", "COMPLETED", "900.00"]]
            )
        )

        result = self.service.import_revolut(self.source)

        self.assertEqual(result.status, "QUARANTINED")
        with open_database(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM extraction_issues WHERE code = 'EXCHANGE_PAIRING_INCOMPLETE'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("SELECT count(*) FROM transfer_links").fetchone()[0], 0)


class EconomicFlowReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = root / "fincl.sqlite3"
        FinCLService.plaintext_phase1(self.database, root / "evidence").import_revolut(self.source)

    def test_confirmed_internal_legs_are_excluded_but_fees_and_pending_review_remain_visible(self):
        before = economic_flow_report_data(self.database)
        before_link = before["currencies"]
        self.assertEqual(before_link[0]["spending"], "10.00")
        self.assertEqual(before_link[0]["income"], "0")
        self.assertEqual(before_link[0]["pending_review"], "100.00")
        self.assertTrue(before["coverage_complete"])
        self.assertTrue(before_link[0]["coverage_complete"])
        self.assertEqual(
            before_link[0]["covered_window"], {"start": "2026-01-01", "end": "2026-01-02"}
        )
        with open_database(self.database) as connection:
            entries = connection.execute(
                "SELECT id, amount_text, amount_coefficient, amount_scale, currency_pocket "
                "FROM ledger_entries ORDER BY effective_at"
            ).fetchall()
            connection.execute(
                """
                INSERT INTO transfer_links(
                    id, left_entry_id, right_entry_id,
                    link_kind, left_currency, left_amount_text, left_amount_coefficient, left_amount_scale,
                    right_currency, right_amount_text, right_amount_coefficient, right_amount_scale,
                    method_version, status, evidence_json, created_at
                ) VALUES (?, ?, ?, 'SAME_CURRENCY_TRANSFER', 'USD', '100.00', 10000, 2,
                          'USD', '10.00', 1000, 2, 'test-v1', 'CONFIRMED', '{}', '2026-01-02T00:00:00Z')
                """,
                ("test-link", entries[0][0], entries[1][0]),
            )

        data = economic_flow_report_data(self.database)

        self.assertEqual(data["consolidated_sgd_total"], None)
        self.assertEqual(data["consolidated_sgd_total_status"], "UNAVAILABLE_FX_RATES_NOT_IMPORTED")
        self.assertEqual(data["currencies"], [{
            "currency": "USD", "entries": 2, "spending": "0", "income": "0", "refunds": "0",
            "reimbursements": "0", "non_income_inflow": "0", "pending_review": "0",
            "other_classified_inbound": "0", "fees": "1.00", "net_economic_flow": "-1.00",
            "excluded_confirmed_internal_entries": 2,
            "excluded_self_transfer_entries": 0,
            "entries_outside_covered_window": 0,
            "entries_outside_analysis_window": 0,
            "coverage_complete": True,
            "covered_window": {"start": "2026-01-01", "end": "2026-01-02"},
            "analysis_window": {"start": "2026-01-01", "end": "2026-01-02"},
        }])
        self.assertEqual(data["coverage"][0]["source_type"], "REVOLUT_CSV")


_INBOUND_BUCKET_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "First in", "40.00", "0.00", "USD", "COMPLETED", "40.00"],
    ["Topup", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01", "Second in", "25.00", "0.00", "USD", "COMPLETED", "65.00"],
    ["Card Payment", "Current", "2026-01-03 10:00:00", "2026-01-03 10:00:01", "Spend", "-10.00", "1.00", "USD", "COMPLETED", "54.00"],
]


class EconomicFlowInboundBucketTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "revolut.csv"
        self.source.write_bytes(_csv_bytes(_INBOUND_BUCKET_ROWS))
        self.database = root / "fincl.sqlite3"
        FinCLService.plaintext_phase1(self.database, root / "evidence").import_revolut(self.source)

    def _inbound_anchors(self) -> list[str]:
        with open_database(self.database) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT natural_anchor FROM ledger_entries "
                    "WHERE economic_direction = 'IN' ORDER BY effective_at"
                )
            ]

    def _classify(self, anchor: str, classification: str) -> None:
        from typer.testing import CliRunner

        from fincl.cli import app

        result = CliRunner().invoke(
            app,
            [
                "review", "classify-inbound", "--anchor", anchor, "--as", classification,
                "--reason", "test", "--database", str(self.database),
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)

    def test_pending_review_before_any_classification(self):
        usd = economic_flow_report_data(self.database)["currencies"][0]
        self.assertEqual(usd["pending_review"], "65.00")
        self.assertEqual(usd["income"], "0")
        self.assertEqual(usd["net_economic_flow"], "-11.00")

    def test_income_and_non_income_inflow_route_separately(self):
        first, second = self._inbound_anchors()
        self._classify(first, "income")
        self._classify(second, "gift")

        usd = economic_flow_report_data(self.database)["currencies"][0]

        self.assertEqual(usd["income"], "40.00")
        self.assertEqual(usd["non_income_inflow"], "25.00")
        self.assertEqual(usd["reimbursements"], "0")
        self.assertEqual(usd["pending_review"], "0")
        self.assertEqual(usd["net_economic_flow"], "54.00")

    def test_self_transfer_and_other_are_excluded_from_net(self):
        first, second = self._inbound_anchors()
        self._classify(first, "self_transfer")
        self._classify(second, "other")

        usd = economic_flow_report_data(self.database)["currencies"][0]

        self.assertEqual(usd["income"], "0")
        self.assertEqual(usd["non_income_inflow"], "0")
        self.assertEqual(usd["other_classified_inbound"], "25.00")
        self.assertEqual(usd["excluded_self_transfer_entries"], 1)
        self.assertEqual(usd["net_economic_flow"], "-11.00")


class CoveredWindowTests(unittest.TestCase):
    def _spans(self, *pairs: tuple[str, str]) -> list[DateInterval]:
        return [DateInterval(start, end) for start, end in pairs]

    def test_single_source_window_is_its_own_range(self):
        self.assertEqual(
            intersect_all([self._spans(("2026-01-01", "2026-01-31"))]),
            [DateInterval("2026-01-01", "2026-01-31")],
        )

    def test_same_source_periods_union_when_overlapping_or_adjacent(self):
        self.assertEqual(
            normalize(
                self._spans(
                    ("2026-01-01", "2026-01-15"),
                    ("2026-01-16", "2026-02-05"),
                )
            ),
            [DateInterval("2026-01-01", "2026-02-05")],
        )

    def test_distinct_sources_intersect(self):
        self.assertEqual(
            intersect_all(
                [
                    self._spans(("2026-01-01", "2026-01-31")),
                    self._spans(("2026-01-10", "2026-02-28")),
                ]
            ),
            [DateInterval("2026-01-10", "2026-01-31")],
        )

    def test_a_required_family_with_no_interval_collapses_the_intersection(self):
        self.assertEqual(
            intersect_all([self._spans(("2026-01-01", "2026-01-31")), []]),
            [],
        )

    def test_disjoint_intersection_keeps_every_interval(self):
        self.assertEqual(
            intersect_all(
                [
                    self._spans(("2026-01-01", "2026-01-31"), ("2026-03-01", "2026-03-31")),
                    self._spans(("2026-01-01", "2026-03-31")),
                ]
            ),
            [
                DateInterval("2026-01-01", "2026-01-31"),
                DateInterval("2026-03-01", "2026-03-31"),
            ],
        )

    def test_explicit_analysis_window_must_lie_inside_one_covered_interval(self):
        covered = [
            DateInterval("2026-01-01", "2026-01-31"),
            DateInterval("2026-03-01", "2026-03-31"),
        ]
        self.assertEqual(
            _analysis_intervals(covered, start="2026-01-15", end="2026-01-20"),
            ([DateInterval("2026-01-15", "2026-01-20")], "EXPLICIT"),
        )
        with self.assertRaisesRegex(FinCLError, "2026-02-01..2026-02-28"):
            _analysis_intervals(covered, start="2026-01-15", end="2026-03-10")

    def test_explicit_analysis_window_requires_two_ordered_iso_dates(self):
        covered = [DateInterval("2026-01-01", "2026-01-31")]
        with self.assertRaisesRegex(FinCLError, "--start and --end together"):
            _analysis_intervals(covered, start="2026-01-10", end=None)
        with self.assertRaisesRegex(FinCLError, "YYYY-MM-DD"):
            _analysis_intervals(covered, start="Jan 10", end="2026-01-20")
        with self.assertRaisesRegex(FinCLError, "must not be after"):
            _analysis_intervals(covered, start="2026-01-20", end="2026-01-10")


if __name__ == "__main__":
    unittest.main()
