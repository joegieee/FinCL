"""Reference FX rates: loader validation, exact ratios, consolidation, rebuild."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.db import connect_sqlite, initialize_schema
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.fx import (
    load_fx_rates,
    load_mas_weekly_csv_file,
    parse_mas_weekly_csv,
    rate_fraction,
)
from fincl.rebuild import rebuild_database
from fincl.report import economic_flow_report_data
from tests.revolut_pdf import revolut_pdf_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01", "Seed", "100.00", "0.00", "USD", "COMPLETED", "100.00"],
    ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01", "Buy", "-40.00", "0.00", "USD", "COMPLETED", "60.00"],
    ["Card Payment", "Current", "2026-02-06 10:00:00", "2026-02-06 10:00:01", "Buy2", "-10.00", "0.00", "USD", "COMPLETED", "50.00"],
]


def _csv_bytes() -> bytes:
    return revolut_pdf_bytes(_ROWS)


def _doc(rates: list[dict[str, str]], source_version: str = "mas-2026-v1") -> dict[str, object]:
    return {
        "source": "MAS monthly average",
        "source_version": source_version,
        "quote_currency": "SGD",
        "rates": rates,
    }


class FxLoaderValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = connect_sqlite(Path(self.temporary.name) / "fincl.sqlite3")
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def test_rejects_malformed_documents(self) -> None:
        cases = [
            {"source": "s", "quote_currency": "SGD", "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.3"}]},
            _doc([{"base_currency": "US", "period": "2026-01", "rate": "1.3"}]),
            _doc([{"base_currency": "USD", "period": "2026-1", "rate": "1.3"}]),
            _doc([{"base_currency": "USD", "period": "2026-01", "rate": "0"}]),
            _doc([{"base_currency": "USD", "period": "2026-01", "rate": "-1.3"}]),
            _doc([{"base_currency": "SGD", "period": "2026-01", "rate": "1.0"}]),
            _doc([]),
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(FinCLError):
                load_fx_rates(self.connection, payload)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM fx_rates").fetchone()[0], 0
        )

    def test_conflicting_reimport_refuses_and_preserves_original(self) -> None:
        original = _doc([{"base_currency": "USD", "period": "2026-01", "rate": "1.3400"}])
        load_fx_rates(self.connection, original)
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM fx_rates")]
        with self.assertRaisesRegex(FinCLError, "immutable FX"):
            load_fx_rates(self.connection, _doc([
                {"base_currency": "USD", "period": "2026-01", "rate": "1.3500"}
            ]))
        self.assertEqual([tuple(row) for row in self.connection.execute("SELECT * FROM fx_rates")], before)

    def test_identical_old_reimport_cannot_reactivate_old_rate(self) -> None:
        old = _doc([{"base_currency": "USD", "period": "2026-01", "rate": "1.34"}])
        load_fx_rates(self.connection, old)
        load_fx_rates(self.connection, _doc([
            {"base_currency": "USD", "period": "2026-01", "rate": "1.35"}
        ], "v2"))
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM fx_rates")]
        self.assertEqual(load_fx_rates(self.connection, old), 0)
        self.assertEqual([tuple(row) for row in self.connection.execute("SELECT * FROM fx_rates")], before)
        self.assertEqual(rate_fraction(self.connection, "USD", "SGD", "2026-01"), Fraction(135, 100))

    def test_version_membership_and_provenance_are_immutable(self) -> None:
        row = {"base_currency": "USD", "period": "2026-01", "rate": "1.34"}
        second = {"base_currency": "USD", "period": "2026-02", "rate": "1.35"}
        load_fx_rates(self.connection, _doc([row, second]))
        changed_source = _doc([row, second])
        changed_source["source"] = "different source"
        for doc in (_doc([row]), _doc([row, second, dict(row, period="2026-03")]), changed_source):
            with self.subTest(doc=doc), self.assertRaises(FinCLError):
                load_fx_rates(self.connection, doc)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM fx_rates").fetchone()[0], 2)

    def test_conflicting_rows_in_new_document_roll_back_entire_import(self) -> None:
        with self.assertRaises(FinCLError):
            load_fx_rates(self.connection, _doc([
                {"base_currency": "USD", "period": "2026-01", "rate": "1.34"},
                {"base_currency": "USD", "period": "2026-01", "rate": "1.35"},
            ]))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM fx_rates").fetchone()[0], 0)

    def test_rate_fraction_is_exact(self) -> None:
        load_fx_rates(self.connection, _doc([{"base_currency": "USD", "period": "2026-01", "rate": "1.3402"}]))
        self.assertEqual(
            rate_fraction(self.connection, "USD", "SGD", "2026-01"),
            Fraction(13402, 10000),
        )
        self.assertIsNone(rate_fraction(self.connection, "USD", "SGD", "2026-09"))


class FxConsolidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "revolut.csv"
        self.source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(self.source)

    def _load(self, rates: list[dict[str, str]]) -> None:
        connection = connect_sqlite(self.database)
        try:
            initialize_schema(connection)
            load_fx_rates(connection, _doc(rates))
        finally:
            connection.close()

    def _add_late_starting_source(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            account_id = connection.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
            # The Revolut PDF import already owns one statement per extraction
            # run (and one SELECTED run per blob), so the synthetic source needs
            # its own evidence and run.
            late_sha = "f" * 64
            connection.execute(
                """
                INSERT INTO source_content(
                    sha256, byte_size, media_type, evidence_key, storage_kind,
                    original_path, last_observed_path, first_seen_at, last_seen_at,
                    integrity_status
                ) VALUES (?, 1, 'application/pdf', 'k-late', 'PLAINTEXT', 'late.pdf',
                          'late.pdf', '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z',
                          'VERIFIED')
                """,
                (late_sha,),
            )
            connection.execute(
                """
                INSERT INTO extraction_runs(
                    id, evidence_sha256, source_profile_version, parser_version,
                    extraction_policy_version, started_at, completed_at, status,
                    selection_state
                ) VALUES (
                    'synthetic-late-run', ?, 'dbs-consolidated-2025-v2', 'p1', 'e1',
                    '2026-03-01T00:00:00Z', '2026-03-01T00:00:01Z', 'ACCEPTED', 'SELECTED'
                )
                """,
                (late_sha,),
            )
            connection.execute(
                """
                INSERT INTO statements(
                    id, extraction_run_id, evidence_sha256, account_id, institution,
                    source_type, statement_date, period_start, period_end,
                    statement_identity, selection_state, conflict_state
                ) VALUES (
                    'synthetic-late-source', 'synthetic-late-run', ?, ?, 'Synthetic',
                    'DBS_CONSOLIDATED', '2026-02-28', '2026-01-07', '2026-02-28',
                    'synthetic:late-source', 'SELECTED', 'NONE'
                )
                """,
                (late_sha, account_id),
            )
            connection.commit()
        finally:
            connection.close()

    def test_total_unavailable_until_rates_are_loaded(self) -> None:
        data = economic_flow_report_data(self.database)
        self.assertIsNone(data["consolidated_sgd_total"])
        self.assertEqual(
            data["consolidated_sgd_total_status"], "UNAVAILABLE_FX_RATES_NOT_IMPORTED"
        )

    def test_total_unavailable_when_a_month_is_missing(self) -> None:
        self._load([{"base_currency": "USD", "period": "2026-01", "rate": "1.35"}])
        data = economic_flow_report_data(self.database)
        self.assertIsNone(data["consolidated_sgd_total"])
        self.assertEqual(
            data["consolidated_sgd_total_status"], "UNAVAILABLE_FX_RATES_NOT_IMPORTED"
        )

    def test_total_available_and_exact_when_every_month_has_a_rate(self) -> None:
        self._load(
            [
                {"base_currency": "USD", "period": "2026-01", "rate": "1.35"},
                {"base_currency": "USD", "period": "2026-02", "rate": "1.30"},
            ]
        )
        data = economic_flow_report_data(self.database)
        # netting: -40.00 USD in Jan, -10.00 USD in Feb (the +100 topup is
        # pending_review and does not net).  -40*1.35 + -10*1.30 = -67.00
        self.assertEqual(data["consolidated_sgd_total"], "-67.00")
        self.assertEqual(data["fx_selection_policy"], "latest-first-import-v1")
        self.assertEqual(
            [(row["period"], row["rate_text"], row["source_version"]) for row in data["fx_inputs"]],
            [("2026-01", "1.35", "mas-2026-v1"), ("2026-02", "1.30", "mas-2026-v1")],
        )
        output = self.root / "manifest-rebuilt.sqlite3"
        rebuild_database(self.database, self.evidence, output)
        rebuilt = economic_flow_report_data(output)
        self.assertEqual(rebuilt["fx_inputs"], data["fx_inputs"])
        self.assertEqual(rebuilt["consolidated_sgd_total"], data["consolidated_sgd_total"])
        self.assertEqual(data["consolidated_sgd_total_status"], "AVAILABLE")

    def test_explicit_covered_window_releases_a_deliberately_scoped_total(self) -> None:
        self._load(
            [
                {"base_currency": "USD", "period": "2026-01", "rate": "1.35"},
                {"base_currency": "USD", "period": "2026-02", "rate": "1.30"},
            ]
        )
        self._add_late_starting_source()

        default = economic_flow_report_data(self.database)
        self.assertEqual(default["consolidated_sgd_total_status"], "UNAVAILABLE_COVERAGE_INCOMPLETE")
        self.assertEqual(
            default["analysis_window"], {"start": "2026-01-07", "end": "2026-02-06"}
        )

        scoped = economic_flow_report_data(
            self.database, start="2026-01-07", end="2026-02-06"
        )
        self.assertEqual(scoped["analysis_window_kind"], "EXPLICIT")
        self.assertTrue(scoped["coverage_complete"])
        self.assertEqual(scoped["currencies"][0]["entries_outside_covered_window"], 2)
        self.assertEqual(scoped["currencies"][0]["entries_outside_analysis_window"], 2)
        self.assertEqual(scoped["consolidated_sgd_total"], "-13.00")
        self.assertEqual(scoped["consolidated_sgd_total_status"], "AVAILABLE")

    def test_cli_accepts_an_explicit_analysis_window(self) -> None:
        self._load(
            [
                {"base_currency": "USD", "period": "2026-01", "rate": "1.35"},
                {"base_currency": "USD", "period": "2026-02", "rate": "1.30"},
            ]
        )
        self._add_late_starting_source()

        result = CliRunner().invoke(
            app,
            [
                "economic-flow", "--database", str(self.database), "--start", "2026-01-07",
                "--end", "2026-02-06", "--json",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["consolidated_sgd_total"], "-13.00")

    def test_rebuild_carries_rates_forward(self) -> None:
        self._load([{"base_currency": "USD", "period": "2026-01", "rate": "1.35"}])
        output = self.root / "rebuilt.sqlite3"
        rebuild_database(self.database, self.evidence, output)

        connection = sqlite3.connect(output)
        try:
            rows = connection.execute(
                "SELECT base_currency, period, rate_text FROM fx_rates"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [("USD", "2026-01", "1.35")])


_MAS_CSV = """MAS: Financial Database - Exchange Rates

Exchange Rates (Weekly)
Jan 2026 to Feb 2026


End of Period,,,S$ Per Unit of US Dollar
2026,Jan,02,1.2838
,,09,1.2853
,,30,1.2669
,Feb,06,1.2742
,,27,1.2636

"* Yearly, monthly and weekly figures are end period values."
"* Daily figures are values as of noon."
"""

REPOSITORY = Path(__file__).resolve().parents[1]
REAL_MAS_CSV = REPOSITORY / "raw" / "MAS_fx" / "Exchange Rates.csv"


class MasWeeklyCsvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.connection = connect_sqlite(self.root / "fincl.sqlite3")
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def test_reduces_each_month_to_its_last_end_of_period_value(self) -> None:
        doc = parse_mas_weekly_csv(_MAS_CSV, source_version="mas-weekly-test")
        self.assertEqual(doc["quote_currency"], "SGD")
        self.assertEqual(
            doc["rates"],
            [
                {"base_currency": "USD", "period": "2026-01", "rate": "1.2669"},
                {"base_currency": "USD", "period": "2026-02", "rate": "1.2636"},
            ],
        )

    def test_loads_through_the_exact_ratio_store(self) -> None:
        path = self.root / "rates.csv"
        path.write_text(_MAS_CSV)
        written = load_mas_weekly_csv_file(
            self.connection, path, source_version="mas-weekly-test"
        )
        self.assertEqual(written, 2)
        self.assertEqual(
            rate_fraction(self.connection, "USD", "SGD", "2026-01"),
            Fraction(12669, 10000),
        )
        row = self.connection.execute(
            "SELECT source, source_version FROM fx_rates LIMIT 1"
        ).fetchone()
        self.assertIn("Monetary Authority of Singapore", row[0])
        self.assertEqual(row[1], "mas-weekly-test")

    def test_rejects_per_n_and_unmapped_currency_columns(self) -> None:
        per_hundred = _MAS_CSV.replace(
            "S$ Per Unit of US Dollar", "S$ Per 100 Japanese Yen"
        )
        unmapped = _MAS_CSV.replace(
            "S$ Per Unit of US Dollar", "S$ Per Unit of Martian Credit"
        )
        for text in (per_hundred, unmapped):
            with self.subTest(text=text.splitlines()[6]), self.assertRaises(FinCLError):
                parse_mas_weekly_csv(text, source_version="v")

    def test_rejects_a_csv_with_no_rate_rows(self) -> None:
        header_only = "\n".join(_MAS_CSV.splitlines()[:7]) + "\n"
        with self.assertRaises(FinCLError):
            parse_mas_weekly_csv(header_only, source_version="v")

    @unittest.skipUnless(REAL_MAS_CSV.exists(), "real MAS export not present")
    def test_real_mas_export_parses_to_month_end_usd(self) -> None:
        text = REAL_MAS_CSV.read_text(encoding="utf-8-sig")
        doc = parse_mas_weekly_csv(text, source_version="mas-weekly-test")
        entries = doc["rates"]
        assert isinstance(entries, list)
        self.assertGreater(len(entries), 0)
        self.assertEqual(doc["source_version"], "mas-weekly-test")
        self.assertTrue(all(entry["base_currency"] == "USD" for entry in entries))

        periods = [entry["period"] for entry in entries]
        for period in periods:
            self.assertRegex(period, r"^\d{4}-(0[1-9]|1[0-2])$")
        self.assertEqual(periods, sorted(set(periods)), "unique and strictly increasing")

        for entry in entries:
            rate = Decimal(entry["rate"])
            self.assertTrue(Decimal("0.5") < rate < Decimal("3"), entry["period"])

        # Loading is idempotent: a second load of the same document adds nothing.
        self.assertEqual(
            load_mas_weekly_csv_file(
                self.connection, REAL_MAS_CSV, source_version="mas-weekly-test"
            ),
            len(entries),
        )
        self.assertEqual(
            load_mas_weekly_csv_file(
                self.connection, REAL_MAS_CSV, source_version="mas-weekly-test"
            ),
            0,
        )
        versions = {
            row[0]
            for row in self.connection.execute("SELECT source_version FROM fx_rates")
        }
        self.assertEqual(versions, {"mas-weekly-test"})


class FxCliTests(unittest.TestCase):
    def test_import_fx_rates_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "fincl.sqlite3"
            connect_sqlite(database).close()
            rates_file = root / "rates.json"
            rates_file.write_text(
                json.dumps(
                    _doc(
                        [
                            {"base_currency": "USD", "period": "2026-01", "rate": "1.35"},
                            {"base_currency": "USD", "period": "2026-02", "rate": "1.30"},
                        ]
                    )
                )
            )
            result = CliRunner().invoke(
                app,
                [
                    "import-fx-rates", "--file", str(rates_file),
                    "--database", str(database), "--json",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(json.loads(result.output)["rates_written"], 2)

    def test_import_fx_rates_mas_weekly_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "fincl.sqlite3"
            connect_sqlite(database).close()
            csv_file = root / "mas.csv"
            csv_file.write_text(_MAS_CSV)

            missing_version = CliRunner().invoke(
                app,
                [
                    "import-fx-rates", "--file", str(csv_file),
                    "--database", str(database), "--mas-weekly-csv", "--json",
                ],
            )
            self.assertEqual(missing_version.exit_code, 1, missing_version.output)

            ok = CliRunner().invoke(
                app,
                [
                    "import-fx-rates", "--file", str(csv_file),
                    "--database", str(database), "--mas-weekly-csv",
                    "--source-version", "mas-weekly-2026-09", "--json",
                ],
            )
            self.assertEqual(ok.exit_code, 0, ok.output)
            self.assertEqual(json.loads(ok.output)["rates_written"], 2)


if __name__ == "__main__":
    unittest.main()
