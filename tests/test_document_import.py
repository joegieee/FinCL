from __future__ import annotations

import calendar
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.db import SCHEMA_VERSION, connect_sqlite, initialize_schema
from fincl.document_service import DocumentImportService, link_narrow_transfers
from fincl.documents import (
    DBS_BALANCE_EQUATION_VERSION,
    MARI_CARD_PROFILE_MINUS_I,
    MARI_CARD_PROFILE_PLUS_I,
    MARI_SAVINGS_3COL_PROFILE,
    MARI_SAVINGS_4COL_PROFILE,
    PAYLAH_BALANCE_EQUATION_VERSION,
    PAYLAH_NEW_PROFILE_VERSION,
    PAYLAH_OLD_PROFILE_VERSION,
    REVOLUT_PDF_PROFILE,
    TRUST_SAVINGS_PROFILE,
    _reconcile_dbs,
    parse_dbs_consolidated,
    parse_mari_card,
    parse_maribank_savings,
    parse_paylah,
    parse_revolut_pdf,
    parse_trust_savings,
)
from fincl.report import build_coverage_report, build_report

REPOSITORY = Path(__file__).resolve().parents[1]
RAW = REPOSITORY / "raw"
DBS_FILES = sorted(RAW.glob("Statement*.pdf"))
PAYLAH_FILES = sorted((RAW / "paylah").glob("PDF document*.pdf"))
MARI_SAVINGS_FILES = sorted(RAW.glob("*_MariBank_e-Statement.pdf"))
MARI_CARD_FILES = sorted(RAW.glob("*_Mari_Credit_Card_E-Statement.pdf"))
TRUST_FILES = sorted((RAW / "Trust").glob("PDF document*.pdf"))
REVOLUT_FILES = sorted((RAW / "Rev").glob("account-statement_*.pdf"))
FIXTURES = REPOSITORY / "tests" / "fixtures"

# Specific statements the persistence tests import. They are read from the
# gitignored corpus only when present; the expectations below are properties of
# the parse/persist pipeline, never counts or values copied from the statements.
DBS_PERSIST_SOURCE = RAW / "Statement 1.pdf"
PAYLAH_PERSIST_SOURCE = RAW / "paylah" / "PDF document.pdf"
# Earliest and latest statements of the sorted savings corpus (two distinct files).
MARI_SAVINGS_PERSIST_SOURCES = (
    (MARI_SAVINGS_FILES[0], MARI_SAVINGS_FILES[-1]) if len(MARI_SAVINGS_FILES) >= 2 else ()
)
TRUST_PERSIST_SOURCES = (RAW / "Trust" / "PDF document 1.pdf", RAW / "Trust" / "PDF document.pdf")


def _mari_card_persist_source() -> Path:
    """The latest corpus card statement that prints a foreign-currency purchase."""
    for path in reversed(MARI_CARD_FILES):
        if any(r.foreign_currency for r in parse_mari_card(path.read_bytes()).records):
            return path
    return MARI_CARD_FILES[-1]


def _is_currency_code(value: str | None) -> bool:
    return value is not None and len(value) == 3 and value.isalpha() and value.isupper()


def _is_month_end(value: str) -> bool:
    day = date.fromisoformat(value)
    return day.day == calendar.monthrange(day.year, day.month)[1]


@unittest.skipUnless(DBS_FILES and PAYLAH_FILES, "representative PDF corpus unavailable")
class DocumentParserCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = [*DBS_FILES, *PAYLAH_FILES]
        cls.before = {
            path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in cls.sources
        }
        cls.dbs = [parse_dbs_consolidated(path.read_bytes()) for path in DBS_FILES]
        cls.paylah = [parse_paylah(path.read_bytes()) for path in PAYLAH_FILES]

    @classmethod
    def tearDownClass(cls):
        after = {
            path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in cls.sources
        }
        if after != cls.before:
            raise AssertionError("a representative source statement changed during parsing")

    def test_dbs_complete_geometry_provenance_and_exact_reconciliation(self):
        self.assertTrue(all(document.accepted for document in self.dbs))
        for document in self.dbs:
            self.assertTrue(document.pages)
            self.assertTrue(any(record.record_kind == "TRANSACTION" for record in document.records))
            self.assertTrue(any(check.blocking for check in document.checks))
        blocking_checks = [
            check for document in self.dbs for check in document.checks if check.blocking
        ]
        self.assertTrue(blocking_checks)
        self.assertTrue(all(check.passed is True for check in blocking_checks))
        self.assertTrue(
            all(check.equation_version == DBS_BALANCE_EQUATION_VERSION for check in blocking_checks)
        )
        currencies = {section.currency for document in self.dbs for section in document.sections}
        self.assertIn("SGD", currencies)
        self.assertTrue(all(_is_currency_code(currency) for currency in currencies), currencies)
        self.assertGreater(
            sum(page.rotated_ignored_count for document in self.dbs for page in document.pages),
            0,
        )
        self._assert_complete_document_provenance(self.dbs)

    def test_paylah_both_layouts_row_coverage_and_exact_balance(self):
        self.assertTrue(all(document.accepted for document in self.paylah))
        self.assertEqual(
            {document.source_profile_version for document in self.paylah},
            {PAYLAH_OLD_PROFILE_VERSION, PAYLAH_NEW_PROFILE_VERSION},
        )
        for document in self.paylah:
            self.assertTrue(document.pages)
            self.assertTrue(any(record.record_kind == "TRANSACTION" for record in document.records))
            balance_checks = [check for check in document.checks if check.check_type == "STATEMENT_BALANCE"]
            self.assertEqual(len(balance_checks), 1)
            self.assertIs(balance_checks[0].passed, True)
            self.assertEqual(balance_checks[0].equation_version, PAYLAH_BALANCE_EQUATION_VERSION)
            diagnostics = [
                check for check in document.checks if check.check_type == "AMOUNT_PAIRING_DIAGNOSTIC"
            ]
            self.assertEqual(len(diagnostics), 1)
            self.assertFalse(diagnostics[0].blocking)
        blocking_checks = [
            check for document in self.paylah for check in document.checks if check.blocking
        ]
        self.assertTrue(blocking_checks)
        self.assertTrue(all(check.passed is True for check in blocking_checks))
        self._assert_complete_document_provenance(self.paylah)

    def test_statement_periods_follow_the_billing_cycle_without_gaps(self):
        # The period is the cycle the statement date implies, not the first/last
        # posted transaction, so a boundary day with no transaction cannot open a
        # false one-day gap in the F23 coverage contract.
        from fincl.documents import _calendar_month_period, _paylah_cycle_period

        for documents, label in ((self.dbs, "DBS"), (self.paylah, "PayLah")):
            spans = sorted(
                (document.period_start, document.period_end) for document in documents
            )
            self.assertTrue(all(start and end for start, end in spans), label)
            for (_, prev_end), (next_start, _) in zip(spans, spans[1:], strict=False):
                gap = date.fromisoformat(next_start) - date.fromisoformat(prev_end)
                self.assertLessEqual(gap, timedelta(days=1), f"{label}: gap after {prev_end}")

        for document in self.paylah:
            start, end = _paylah_cycle_period(date.fromisoformat(document.statement_date))
            self.assertEqual(document.period_start, start.isoformat())
            self.assertEqual(document.period_end, end.isoformat())
            self.assertEqual(document.period_end, document.statement_date)
        for document in self.dbs:
            start, end = _calendar_month_period(date.fromisoformat(document.statement_date))
            self.assertEqual((document.period_start, document.period_end), (start.isoformat(), end.isoformat()))
            self.assertTrue(document.period_start.endswith("-01"))

    def test_source_files_are_read_only_inputs(self):
        after = {
            path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.sources
        }
        self.assertEqual(after, self.before)

    def _assert_complete_document_provenance(self, documents):
        for document in documents:
            self.assertTrue(all(page.status == "ACCOUNTED" for page in document.pages))
            self.assertEqual(
                sum(page.transaction_rows for page in document.pages),
                sum(record.record_kind == "TRANSACTION" for record in document.records),
            )
            self.assertEqual([record.ordinal for record in document.records], list(range(1, len(document.records) + 1)))
            self.assertEqual(len({record.source_anchor for record in document.records}), len(document.records))
            for record in document.records:
                self.assertTrue(record.raw_text)
                self.assertTrue(record.source_anchor.startswith("pdf:"))
                x0, top, x1, bottom = record.bbox
                self.assertLessEqual(x0, x1)
                self.assertLessEqual(top, bottom)


class SyntheticDocumentFixtureTests(unittest.TestCase):
    def test_synthetic_dbs_multicurrency_geometry_and_rotated_text(self):
        parsed = parse_dbs_consolidated((FIXTURES / "dbs_multicurrency.pdf").read_bytes())

        self.assertTrue(parsed.accepted)
        self.assertEqual([section.currency for section in parsed.sections], ["SGD", "USD"])
        self.assertEqual(sum(record.record_kind == "TRANSACTION" for record in parsed.records), 3)
        self.assertEqual(parsed.pages[0].transaction_rows, 3)
        self.assertGreater(parsed.pages[0].rotated_ignored_count, 0)
        self.assertTrue(all(check.passed is True for check in parsed.checks if check.blocking))
        self.assertTrue(any("REF TEST-SGD-0001" in (record.reference_raw or "") for record in parsed.records))

    def test_missing_dbs_opening_balance_reports_once_per_section_and_resumes(self):
        parsed = parse_dbs_consolidated((FIXTURES / "dbs_multicurrency.pdf").read_bytes())
        records = [
            record
            for record in parsed.records
            if record.raw_fields.get("supporting_kind") != "OPENING_BALANCE"
        ]
        problems = []

        checks = _reconcile_dbs(records, parsed.sections, problems)

        opening_issues = [problem for problem in problems if problem.code == "DBS_OPENING_BALANCE_MISSING"]
        self.assertEqual(len(opening_issues), 2)
        self.assertFalse(any(problem.code == "DBS_BALANCE_CHECK_UNAVAILABLE" for problem in problems))
        self.assertTrue(any(check.check_type == "RUNNING_BALANCE" and check.passed for check in checks))

    def test_synthetic_paylah_rows_references_and_balance(self):
        parsed = parse_paylah((FIXTURES / "paylah_legacy.pdf").read_bytes())

        self.assertTrue(parsed.accepted)
        transactions = [record for record in parsed.records if record.record_kind == "TRANSACTION"]
        self.assertEqual(len(transactions), 2)
        self.assertEqual([record.direction_marker for record in transactions], ["CR", "DB"])
        self.assertTrue(all(record.reference_raw for record in transactions))
        balance = next(check for check in parsed.checks if check.check_type == "STATEMENT_BALANCE")
        self.assertTrue(balance.passed)

    def test_unclassified_page_fails_closed_and_cli_exits_two(self):
        source = FIXTURES / "unclassified.pdf"
        parsed = parse_dbs_consolidated(source.read_bytes())
        self.assertFalse(parsed.accepted)
        self.assertIn("UNCLASSIFIED_PDF_PAGE", {problem.code for problem in parsed.problems})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = CliRunner().invoke(
                app,
                [
                    "import-dbs",
                    str(source),
                    "--database",
                    str(root / "fincl.sqlite3"),
                    "--evidence-dir",
                    str(root / "evidence"),
                    "--json",
                ],
            )
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertEqual(json.loads(result.output)["status"], "QUARANTINED")


@unittest.skipUnless(MARI_SAVINGS_FILES, "MariBank savings corpus unavailable")
class MariBankSavingsCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in MARI_SAVINGS_FILES
        }
        cls.documents = [parse_maribank_savings(path.read_bytes()) for path in MARI_SAVINGS_FILES]

    @classmethod
    def tearDownClass(cls):
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in MARI_SAVINGS_FILES}
        if after != cls.before:
            raise AssertionError("a MariBank savings source statement changed during parsing")

    def test_every_statement_is_accepted_with_exact_reconciliation(self):
        self.assertTrue(all(document.accepted for document in self.documents))
        blocking = [check for document in self.documents for check in document.checks if check.blocking]
        self.assertTrue(blocking)
        self.assertTrue(all(check.passed is True for check in blocking))
        self.assertFalse(
            any(problem.severity == "BLOCKING" for document in self.documents for problem in document.problems)
        )
        self.assertEqual(
            {section.section_kind for document in self.documents for section in document.sections},
            {"SAVINGS_LEDGER", "INTEREST_SCHEDULE", "INVESTMENT"},
        )

    def test_both_interest_profiles_are_present_and_frozen(self):
        self.assertEqual(
            {document.source_profile_version for document in self.documents},
            {MARI_SAVINGS_3COL_PROFILE, MARI_SAVINGS_4COL_PROFILE},
        )

    def test_daily_interest_rows_are_supporting_not_ledger_candidates(self):
        for document in self.documents:
            daily = [
                record
                for record in document.records
                if record.raw_fields.get("supporting_kind") == "DAILY_INTEREST"
            ]
            self.assertTrue(all(record.record_kind == "SUPPORTING_BALANCE" for record in daily))
            credited = [
                record
                for record in document.records
                if record.record_kind == "TRANSACTION" and record.date_precision == "MONTH"
            ]
            total = next(
                (
                    record
                    for record in document.records
                    if record.raw_fields.get("supporting_kind") == "TOTAL_INTEREST_CREDITED"
                ),
                None,
            )
            if total is not None and total.credit is not None and total.credit.coefficient != 0:
                self.assertEqual(len(credited), 1)
                self.assertEqual(credited[0].credit.text, total.credit.text)
            else:
                self.assertEqual(credited, [])

    def test_investment_sections_are_recognized_regions_not_savings_activity(self):
        regions = [
            record
            for document in self.documents
            for record in document.records
            if record.raw_fields.get("region", "").startswith("INVESTMENTS_")
        ]
        self.assertEqual(len(regions), 2 * len(self.documents))
        self.assertTrue(all(record.record_kind == "RECOGNIZED_REGION" for record in regions))
        self.assertTrue(all(record.section_ordinal == 3 for record in regions))

    def test_provenance_is_complete_and_ordinals_are_dense(self):
        for document in self.documents:
            self.assertEqual(
                [record.ordinal for record in document.records],
                list(range(1, len(document.records) + 1)),
            )
            self.assertEqual(
                len({record.source_anchor for record in document.records}), len(document.records)
            )
            self.assertTrue(all(page.status == "ACCOUNTED" for page in document.pages))
            for record in document.records:
                self.assertTrue(record.raw_text)
                self.assertTrue(record.source_anchor.startswith("pdf:mari-savings:"))


@unittest.skipUnless(TRUST_FILES, "Trust savings corpus unavailable")
class TrustSavingsCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in TRUST_FILES}
        cls.documents = [parse_trust_savings(path.read_bytes()) for path in TRUST_FILES]

    @classmethod
    def tearDownClass(cls):
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in TRUST_FILES}
        if after != cls.before:
            raise AssertionError("a Trust savings source statement changed during parsing")

    def test_every_statement_is_accepted_with_exact_reconciliation(self):
        self.assertTrue(all(document.accepted for document in self.documents))
        blocking = [
            check for document in self.documents for check in document.checks if check.blocking
        ]
        self.assertTrue(blocking)
        self.assertTrue(all(check.passed is True for check in blocking))
        self.assertFalse(
            any(
                problem.severity == "BLOCKING"
                for document in self.documents
                for problem in document.problems
            )
        )
        self.assertEqual(
            {section.section_kind for document in self.documents for section in document.sections},
            {"MAIN_LEDGER", "SAVINGS_LEDGER"},
        )
        self.assertEqual(
            {document.source_profile_version for document in self.documents},
            {TRUST_SAVINGS_PROFILE},
        )
        self.assertEqual(
            {document.account_id for document in self.documents},
            {"trust-savings"},
        )

    def test_periods_come_straight_from_the_printed_statement_range(self):
        # Each statement covers one whole calendar month, and the corpus runs
        # month to month without gaps or overlaps.
        periods = sorted((document.period_start, document.period_end) for document in self.documents)
        self.assertEqual(len(set(periods)), len(periods))
        for start, end in periods:
            first = date.fromisoformat(start)
            self.assertEqual(first.day, 1, start)
            self.assertEqual(
                date.fromisoformat(end),
                first.replace(day=calendar.monthrange(first.year, first.month)[1]),
                (start, end),
            )
        for (_, prev_end), (next_start, _) in zip(periods, periods[1:], strict=False):
            self.assertEqual(date.fromisoformat(next_start), date.fromisoformat(prev_end) + timedelta(days=1))

    def test_boundary_rows_are_supporting_balances_not_ledger_candidates(self):
        for document in self.documents:
            boundaries = [
                record
                for record in document.records
                if record.raw_fields.get("supporting_kind")
                in {"OPENING_BALANCE", "CLOSING_BALANCE"}
            ]
            # one opening and one closing per sub-account ledger
            self.assertEqual(len(boundaries), 4)
            self.assertTrue(
                all(record.record_kind == "SUPPORTING_BALANCE" for record in boundaries)
            )
            self.assertTrue(all(record.balance is not None for record in boundaries))

    def test_foreign_currency_purchases_keep_the_fcy_amount_and_rate(self):
        foreign = [
            record
            for document in self.documents
            for record in document.records
            if record.record_kind == "TRANSACTION" and record.foreign_currency is not None
        ]
        self.assertTrue(foreign)
        for record in foreign:
            self.assertTrue(_is_currency_code(record.foreign_currency), record.source_anchor)
            self.assertNotEqual(record.foreign_currency, "SGD", record.source_anchor)
            self.assertIsNotNone(record.foreign_amount, record.source_anchor)
            self.assertIsNotNone(record.debit, record.source_anchor)
            self.assertIsNone(record.credit, record.source_anchor)
            fx = record.raw_fields.get("fx")
            self.assertIsInstance(fx, dict, record.source_anchor)
            self.assertEqual(fx["currency"], record.foreign_currency)
            self.assertTrue(fx["rate_to_sgd"])

    def test_incoming_rows_are_credits_and_bare_amounts_are_debits(self):
        for document in self.documents:
            transactions = [
                record for record in document.records if record.record_kind == "TRANSACTION"
            ]
            for record in transactions:
                # Every posting is signed one way: a "+" amount is a credit, a
                # bare amount is a debit, never both and never neither.
                self.assertEqual(
                    (record.credit is None) + (record.debit is None), 1, record.source_anchor
                )
                self.assertTrue(record.date_value, record.source_anchor)
                self.assertTrue(
                    document.period_start <= record.date_value <= document.period_end,
                    record.source_anchor,
                )
            for record in transactions:
                if record.description_raw == "Interest":
                    self.assertIsNotNone(record.credit, record.source_anchor)
                    self.assertIsNone(record.debit, record.source_anchor)
        self.assertTrue(
            any(
                record.record_kind == "TRANSACTION" and record.description_raw == "Interest"
                for document in self.documents
                for record in document.records
            )
        )
        self.assertTrue(
            any(
                record.record_kind == "TRANSACTION" and record.credit is not None
                and record.description_raw != "Interest"
                for document in self.documents
                for record in document.records
            )
        )

    def test_provenance_is_complete_and_ordinals_are_dense(self):
        for document in self.documents:
            self.assertEqual(
                [record.ordinal for record in document.records],
                list(range(1, len(document.records) + 1)),
            )
            self.assertEqual(
                len({record.source_anchor for record in document.records}),
                len(document.records),
            )
            self.assertTrue(all(page.status == "ACCOUNTED" for page in document.pages))
            for record in document.records:
                self.assertTrue(record.raw_text)
                self.assertTrue(record.source_anchor.startswith("pdf:trust:"))


class TrustSavingsFixtureTests(unittest.TestCase):
    def test_synthetic_statement_parses_reconciles_and_threads_fx(self):
        parsed = parse_trust_savings((FIXTURES / "trust_savings.pdf").read_bytes())

        self.assertTrue(parsed.accepted)
        self.assertEqual(parsed.source_profile_version, TRUST_SAVINGS_PROFILE)
        self.assertEqual(parsed.account_id, "trust-savings")
        self.assertEqual((parsed.period_start, parsed.period_end), ("2026-08-01", "2026-08-31"))
        self.assertEqual(parsed.statement_date, "2026-09-01")
        self.assertEqual(
            [section.section_kind for section in parsed.sections],
            ["MAIN_LEDGER", "SAVINGS_LEDGER"],
        )

        blocking = [check for check in parsed.checks if check.blocking]
        self.assertTrue(blocking)
        self.assertTrue(all(check.passed is True for check in blocking))
        self.assertFalse(any(problem.severity == "BLOCKING" for problem in parsed.problems))

        transactions = [r for r in parsed.records if r.record_kind == "TRANSACTION"]
        self.assertEqual(
            [(t.description_raw, t.date_value) for t in transactions],
            [
                ("TAN SYNTHETIC NAME", "2026-08-01"),
                ("SYNTHETIC MERCHANT EU", "2026-08-09"),
                ("Interest", "2026-08-31"),
            ],
        )
        self.assertEqual(transactions[0].credit.text, "50.00")
        self.assertIsNone(transactions[0].debit)

        fx = transactions[1]
        self.assertEqual(fx.debit.text, "8.20")
        self.assertEqual(fx.foreign_currency, "EUR")
        self.assertEqual(fx.foreign_amount.text, "5.00")
        self.assertEqual(fx.raw_fields["fx"], {
            "currency": "EUR",
            "rate_to_sgd": "1.6400",
            "fx_charge_percent": "0",
        })

        boundaries = [
            r for r in parsed.records
            if r.raw_fields.get("supporting_kind") in {"OPENING_BALANCE", "CLOSING_BALANCE"}
        ]
        self.assertEqual(len(boundaries), 4)
        self.assertTrue(all(r.record_kind == "SUPPORTING_BALANCE" for r in boundaries))

    def test_broken_balance_walk_is_a_blocking_refusal_and_cli_exits_two(self):
        source = FIXTURES / "trust_savings_unbalanced.pdf"
        parsed = parse_trust_savings(source.read_bytes())

        self.assertFalse(parsed.accepted)
        self.assertIn(
            "TRUST_BALANCE_MISMATCH", {problem.code for problem in parsed.problems}
        )
        failed = [c for c in parsed.checks if c.blocking and c.passed is not True]
        self.assertEqual(
            [c.context.get("section") for c in failed], ["Main Account"]
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = CliRunner().invoke(
                app,
                [
                    "import-trust",
                    str(source),
                    "--database",
                    str(root / "fincl.sqlite3"),
                    "--evidence-dir",
                    str(root / "evidence"),
                    "--json",
                ],
            )
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertEqual(json.loads(result.output)["status"], "QUARANTINED")


class RevolutPdfFixtureTests(unittest.TestCase):
    def test_synthetic_statement_parses_reconciles_and_splits_lifecycle(self):
        parsed = parse_revolut_pdf((FIXTURES / "revolut_statement.pdf").read_bytes())

        self.assertTrue(parsed.accepted)
        self.assertEqual(parsed.source_profile_version, REVOLUT_PDF_PROFILE)
        self.assertEqual(parsed.account_id, "acct_revolut_current")
        self.assertEqual((parsed.period_start, parsed.period_end), ("2026-01-02", "2026-01-15"))
        self.assertEqual(parsed.statement_date, "2026-01-15")

        blocking = [check for check in parsed.checks if check.blocking]
        self.assertTrue(blocking)
        self.assertTrue(all(check.passed is True for check in blocking))
        self.assertFalse(any(problem.severity == "BLOCKING" for problem in parsed.problems))

        transactions = [r for r in parsed.records if r.record_kind == "TRANSACTION"]
        pending = [r for r in parsed.records if r.raw_fields.get("lifecycle") == "PENDING"]
        reverted = [r for r in parsed.records if r.raw_fields.get("lifecycle") == "REVERTED"]
        self.assertEqual((len(transactions), len(pending), len(reverted)), (7, 1, 1))

        by_desc = {
            (t.description_raw, t.currency_raw): t for t in transactions
        }
        topup = by_desc[("ACME CLIENT LTD", "SGD")]
        self.assertEqual(topup.credit.text, "500.00")
        self.assertEqual(topup.raw_fields["event_kind"], "TOPUP")

        grab = by_desc[("Grab", "SGD")]
        self.assertEqual(grab.debit.text, "9.60")
        self.assertEqual(grab.fee.text, "0.10")
        self.assertEqual(grab.raw_fields["event_kind"], "CARD_PAYMENT")

        refund = next(t for t in transactions if t.raw_fields["event_kind"] == "REFUND")
        self.assertEqual(refund.credit.text, "12.00")

        exchanges = [t for t in transactions if t.raw_fields["event_kind"] == "EXCHANGE"]
        self.assertEqual({t.currency_raw for t in exchanges}, {"SGD", "USD"})

        for record in parsed.records:
            self.assertTrue(record.source_anchor.startswith("pdf:revolut:"))

    def test_broken_balance_walk_is_a_blocking_refusal_and_cli_exits_two(self):
        source = FIXTURES / "revolut_statement_unbalanced.pdf"
        parsed = parse_revolut_pdf(source.read_bytes())

        self.assertFalse(parsed.accepted)
        self.assertIn(
            "REVOLUT_PDF_BALANCE_MISMATCH", {problem.code for problem in parsed.problems}
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = CliRunner().invoke(
                app,
                [
                    "import-revolut-pdf",
                    str(source),
                    "--database",
                    str(root / "fincl.sqlite3"),
                    "--evidence-dir",
                    str(root / "evidence"),
                    "--json",
                ],
            )
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertEqual(json.loads(result.output)["status"], "QUARANTINED")


@unittest.skipUnless(REVOLUT_FILES, "Revolut PDF corpus unavailable")
class RevolutPdfCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in REVOLUT_FILES
        }
        cls.documents = [parse_revolut_pdf(path.read_bytes()) for path in REVOLUT_FILES]

    @classmethod
    def tearDownClass(cls):
        after = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in REVOLUT_FILES
        }
        if after != cls.before:
            raise AssertionError("a Revolut source statement changed during parsing")

    def test_every_statement_reconciles_with_the_expected_record_split(self):
        self.assertTrue(all(document.accepted for document in self.documents))
        transactions = sum(
            r.record_kind == "TRANSACTION"
            for document in self.documents
            for r in document.records
        )
        reverted = sum(
            r.raw_fields.get("lifecycle") == "REVERTED"
            for document in self.documents
            for r in document.records
        )
        pending = sum(
            r.raw_fields.get("lifecycle") == "PENDING"
            for document in self.documents
            for r in document.records
        )
        self.assertGreater(transactions, 0)
        self.assertGreaterEqual(reverted, 0)
        self.assertGreaterEqual(pending, 0)
        for document in self.documents:
            self.assertTrue(any(r.record_kind == "TRANSACTION" for r in document.records))
            for record in document.records:
                # Pending and reverted rows are kept as evidence, never as postings.
                if record.raw_fields.get("lifecycle") in {"PENDING", "REVERTED"}:
                    self.assertNotEqual(record.record_kind, "TRANSACTION", record.source_anchor)

        self.assertFalse(
            any(
                problem.severity == "BLOCKING"
                for document in self.documents
                for problem in document.problems
            )
        )
        self.assertTrue(
            all(
                check.passed is True
                for document in self.documents
                for check in document.checks
                if check.blocking
            )
        )
        for document in self.documents:
            self.assertTrue(all(page.status == "ACCOUNTED" for page in document.pages))
            for record in document.records:
                self.assertTrue(record.source_anchor.startswith("pdf:revolut:"))


class MariBankSavingsFixtureTests(unittest.TestCase):
    def test_synthetic_profiles_precision_and_reconciliation(self):
        parsed = parse_maribank_savings((FIXTURES / "maribank_savings.pdf").read_bytes())

        self.assertTrue(parsed.accepted)
        self.assertEqual(parsed.source_profile_version, MARI_SAVINGS_3COL_PROFILE)
        transactions = [record for record in parsed.records if record.record_kind == "TRANSACTION"]
        self.assertEqual(len(transactions), 3)
        credited = [record for record in transactions if record.date_precision == "MONTH"]
        self.assertEqual(len(credited), 1)
        self.assertEqual(credited[0].date_raw, "JUL")
        self.assertEqual(credited[0].date_value, "2026-07-31")
        self.assertEqual(credited[0].credit.text, "0.55")
        self.assertTrue(all(check.passed is True for check in parsed.checks if check.blocking))
        self.assertIn(
            "CREDITED_INTEREST_MATCHES_TOTAL", {check.check_type for check in parsed.checks}
        )
        daily = [
            record
            for record in parsed.records
            if record.raw_fields.get("supporting_kind") == "DAILY_INTEREST"
        ]
        self.assertEqual(len(daily), 3)
        self.assertTrue(all(record.record_kind == "SUPPORTING_BALANCE" for record in daily))
        regions = [
            record.raw_fields["region"]
            for record in parsed.records
            if record.record_kind == "RECOGNIZED_REGION"
        ]
        self.assertEqual(sorted(regions), ["INVESTMENTS_HOLDINGS", "INVESTMENTS_TRANSACTIONS"])

    def test_unknown_interest_profile_is_a_refusal_not_a_best_effort_shift(self):
        source = FIXTURES / "maribank_savings_unknown_profile.pdf"
        parsed = parse_maribank_savings(source.read_bytes())
        self.assertFalse(parsed.accepted)
        self.assertIn(
            "MARI_SAVINGS_UNKNOWN_INTEREST_PROFILE",
            {problem.code for problem in parsed.problems},
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = CliRunner().invoke(
                app,
                [
                    "import-maribank",
                    str(source),
                    "--database",
                    str(root / "fincl.sqlite3"),
                    "--evidence-dir",
                    str(root / "evidence"),
                    "--json",
                ],
            )
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertEqual(json.loads(result.output)["status"], "QUARANTINED")


@unittest.skipUnless(MARI_CARD_FILES, "Mari credit-card corpus unavailable")
class MariCardCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = {
            path: hashlib.sha256(path.read_bytes()).hexdigest() for path in MARI_CARD_FILES
        }
        cls.documents = [parse_mari_card(path.read_bytes()) for path in MARI_CARD_FILES]

    @classmethod
    def tearDownClass(cls):
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in MARI_CARD_FILES}
        if after != cls.before:
            raise AssertionError("a Mari credit-card source statement changed during parsing")

    def test_every_statement_reconciles_both_layers_exactly(self):
        self.assertTrue(all(document.accepted for document in self.documents))
        blocking = [check for document in self.documents for check in document.checks if check.blocking]
        self.assertTrue(all(check.passed is True for check in blocking))
        self.assertTrue(
            any(check.check_type == "SUMMARY_EQUATION" for check in blocking)
        )
        self.assertTrue(
            any(check.check_type == "SECTION_BUCKET_TOTAL" for check in blocking)
        )
        self.assertFalse(
            any(
                problem.severity == "BLOCKING"
                for document in self.documents
                for problem in document.problems
            )
        )

    def test_both_printed_equations_are_recognized_and_frozen(self):
        self.assertEqual(
            {document.source_profile_version for document in self.documents},
            {MARI_CARD_PROFILE_MINUS_I, MARI_CARD_PROFILE_PLUS_I},
        )

    def test_posted_and_transaction_dates_are_kept_separate(self):
        rows = [
            record
            for document in self.documents
            for record in document.records
            if record.record_kind == "TRANSACTION"
        ]
        self.assertTrue(rows)
        self.assertTrue(all(record.date_value for record in rows))
        with_alt = [record for record in rows if record.alt_date_role == "TRANSACTION"]
        self.assertTrue(with_alt)
        self.assertTrue(all(record.alt_date_raw for record in with_alt))

    def test_old_installment_dates_resolve_against_the_posted_date(self):
        with_alt = [
            record
            for document in self.documents
            for record in document.records
            if record.alt_date_raw
        ]
        self.assertTrue(with_alt)
        # The printed transaction date on an instalment posting predates the
        # billing cycle, but its year is still unambiguous: resolve it against
        # the posted date instead of leaving it partial.
        self.assertTrue(all(record.alt_date_value for record in with_alt))
        self.assertTrue(
            all(record.alt_date_value <= record.date_value for record in with_alt)
        )
        warnings = {
            problem.code
            for document in self.documents
            for problem in document.problems
            if problem.severity == "WARNING"
        }
        self.assertNotIn("MARI_CARD_PARTIAL_TRANSACTION_DATE", warnings)

    def test_installments_and_foreign_detail_are_preserved_as_facts(self):
        installments = [
            record.raw_fields["installment"]
            for document in self.documents
            for record in document.records
            if record.raw_fields.get("installment")
        ]
        self.assertTrue(installments)
        self.assertTrue(all({"family", "ordinal", "term"} <= set(item) for item in installments))
        foreign = [
            record
            for document in self.documents
            for record in document.records
            if record.foreign_amount is not None
        ]
        self.assertTrue(foreign)
        self.assertTrue(all(len(record.foreign_currency) == 3 for record in foreign))

    def test_provenance_is_complete(self):
        for document in self.documents:
            self.assertEqual(
                [record.ordinal for record in document.records],
                list(range(1, len(document.records) + 1)),
            )
            self.assertEqual(
                len({record.source_anchor for record in document.records}), len(document.records)
            )
            self.assertTrue(all(page.status == "ACCOUNTED" for page in document.pages))
            self.assertTrue(
                all(record.source_anchor.startswith("pdf:mari-card:") for record in document.records)
            )


class MariCardFixtureTests(unittest.TestCase):
    def test_synthetic_card_reconciles_and_captures_every_row_kind(self):
        parsed = parse_mari_card((FIXTURES / "mari_card.pdf").read_bytes())

        self.assertTrue(parsed.accepted)
        self.assertEqual(parsed.source_profile_version, MARI_CARD_PROFILE_MINUS_I)
        self.assertTrue(all(check.passed is True for check in parsed.checks if check.blocking))
        self.assertEqual(
            {check.check_type for check in parsed.checks},
            {"SUMMARY_EQUATION", "SECTION_BUCKET_TOTAL", "STATEMENT_DUE_EQUALS_OUTSTANDING"},
        )
        rows = [record for record in parsed.records if record.record_kind == "TRANSACTION"]
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            {problem.code for problem in parsed.problems if problem.severity == "WARNING"},
            {"MARI_CARD_BLANK_TRANSACTION_DATE"},
        )
        self.assertTrue(any(record.foreign_currency == "USD" for record in rows))
        self.assertTrue(any(record.raw_fields.get("installment") for record in rows))
        blank = next(record for record in rows if record.alt_date_role is None)
        self.assertEqual(blank.date_value, "2026-07-06")
        # The instalment row prints a pre-cycle purchase date ("10 MAY"); its year
        # resolves against the "21 JUL 2026" posting rather than staying partial.
        instalment = next(record for record in rows if record.raw_fields.get("installment"))
        self.assertEqual(instalment.alt_date_raw, "10 MAY")
        self.assertEqual(instalment.alt_date_value, "2026-05-10")

    def test_unknown_summary_equation_blocks_generation(self):
        source = FIXTURES / "mari_card_unknown_equation.pdf"
        parsed = parse_mari_card(source.read_bytes())
        self.assertFalse(parsed.accepted)
        self.assertIn(
            "MARI_CARD_UNKNOWN_SUMMARY_EQUATION",
            {problem.code for problem in parsed.problems},
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = CliRunner().invoke(
                app,
                [
                    "import-mari-card",
                    str(source),
                    "--database",
                    str(root / "fincl.sqlite3"),
                    "--evidence-dir",
                    str(root / "evidence"),
                    "--json",
                ],
            )
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertEqual(json.loads(result.output)["status"], "QUARANTINED")


@unittest.skipUnless(DBS_FILES and PAYLAH_FILES, "representative PDF corpus unavailable")
class DocumentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        self.service = DocumentImportService.plaintext_phase2(self.database, self.evidence)

    def tearDown(self):
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @unittest.skipUnless(
        DBS_PERSIST_SOURCE.is_file() and PAYLAH_PERSIST_SOURCE.is_file(),
        "DBS/PayLah persistence statements unavailable",
    )
    def test_persistence_reimport_reports_cli_and_narrow_links(self):
        dbs_source = DBS_PERSIST_SOURCE
        paylah_source = PAYLAH_PERSIST_SOURCE
        source_before = {
            path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in (dbs_source, paylah_source)
        }

        dbs_first = self.service.import_dbs(dbs_source)
        paylah_first = self.service.import_paylah(paylah_source)
        dbs_second = self.service.import_dbs(dbs_source)
        paylah_second = self.service.import_paylah(paylah_source)

        self.assertEqual((dbs_first.status, paylah_first.status), ("ACCEPTED", "ACCEPTED"))
        self.assertGreater(dbs_first.transaction_count, 0)
        self.assertGreater(paylah_first.transaction_count, 0)
        self.assertEqual(
            (dbs_second.transaction_count, paylah_second.transaction_count),
            (dbs_first.transaction_count, paylah_first.transaction_count),
        )
        self.assertTrue(dbs_second.evidence_reused and dbs_second.extraction_reused)
        self.assertTrue(paylah_second.evidence_reused and paylah_second.extraction_reused)
        self.assertEqual(dbs_first.extraction_run_id, dbs_second.extraction_run_id)
        self.assertEqual(paylah_first.extraction_run_id, paylah_second.extraction_run_id)
        self.assertEqual(
            source_before,
            {
                path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in (dbs_source, paylah_source)
            },
        )

        with closing(self.connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM source_content").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM extraction_runs").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM statements").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM import_runs").fetchone()[0], 4)
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries WHERE source_family = 'DOCUMENT'"
                ).fetchone()[0],
                dbs_first.transaction_count + paylah_first.transaction_count,
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute("SELECT count(*) FROM page_coverage WHERE status <> 'ACCOUNTED'").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM document_reconciliation_checks WHERE blocking = 1 AND passed <> 1"
                ).fetchone()[0],
                0,
            )
            raw = connection.execute(
                "SELECT * FROM document_records WHERE record_kind = 'TRANSACTION' ORDER BY extraction_ordinal LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(raw["evidence_sha256"])
            self.assertIsNotNone(raw["source_anchor"])
            self.assertIsNotNone(raw["page_number"])
            self.assertIsNotNone(raw["x0_mpt"])
            self.assertIsNotNone(raw["raw_text"])
            self.assertIsNotNone(raw["raw_fields_json"])
            self.assertIsNotNone(raw["date_raw"])
            self.assertIsNotNone(raw["date_value"])
            self.assertIsNotNone(raw["currency_raw"])
            extraction_times = connection.execute(
                "SELECT started_at, completed_at FROM extraction_runs"
            ).fetchall()
            self.assertTrue(all(row["started_at"] and row["completed_at"] for row in extraction_times))
            import_times = connection.execute(
                "SELECT started_at, completed_at FROM import_runs"
            ).fetchall()
            self.assertTrue(all(row["started_at"] and row["completed_at"] for row in import_times))
            initial_links = connection.execute(
                "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
            ).fetchone()[0]
            self.assertGreater(initial_links, 0)
            self.assertEqual(link_narrow_transfers(connection), 0)
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM (SELECT left_entry_id FROM transfer_links GROUP BY left_entry_id HAVING count(*) > 1)"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM (SELECT right_entry_id FROM transfer_links GROUP BY right_entry_id HAVING count(*) > 1)"
                ).fetchone()[0],
                0,
            )

        self.assertIn("status: ACCEPTED", build_report(self.database))
        self.assertIn(f"confirmed_transfer_links={initial_links}", build_coverage_report(self.database))
        runner = CliRunner()
        report_result = runner.invoke(app, ["report", "--database", str(self.database), "--json"])
        self.assertEqual(report_result.exit_code, 0, report_result.output)
        self.assertEqual(json.loads(report_result.output)["status"], "ACCEPTED")
        coverage_result = runner.invoke(app, ["coverage", "--database", str(self.database), "--plain"])
        self.assertEqual(coverage_result.exit_code, 0, coverage_result.output)
        self.assertIn("DBS_CONSOLIDATED", coverage_result.output)
        self.assertIn("DBS_PAYLAH", coverage_result.output)
        link_result = runner.invoke(app, ["link-transfers", "--database", str(self.database)])
        self.assertEqual(link_result.exit_code, 0, link_result.output)
        self.assertIn("Created 0 confirmed transfer link(s).", link_result.output)

    @unittest.skipUnless(
        bool(MARI_SAVINGS_PERSIST_SOURCES) and all(path.is_file() for path in MARI_SAVINGS_PERSIST_SOURCES),
        "MariBank savings persistence statements unavailable",
    )
    def test_maribank_savings_persists_month_precision_and_is_idempotent(self):
        first_source, second_source = MARI_SAVINGS_PERSIST_SOURCES
        expected_month_rows = sum(
            record.record_kind == "TRANSACTION" and record.date_precision == "MONTH"
            for path in MARI_SAVINGS_PERSIST_SOURCES
            for record in parse_maribank_savings(path.read_bytes()).records
        )
        first = self.service.import_maribank(first_source)
        second = self.service.import_maribank(second_source)
        reimport = self.service.import_maribank(first_source)

        self.assertEqual((first.status, second.status), ("ACCEPTED", "ACCEPTED"))
        self.assertTrue(reimport.evidence_reused and reimport.extraction_reused)
        self.assertEqual(first.extraction_run_id, reimport.extraction_run_id)

        with closing(self.connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries WHERE source_family = 'DOCUMENT'"
                ).fetchone()[0],
                first.transaction_count + second.transaction_count,
            )
            # Credited interest prints a month, not a day: it persists with MONTH
            # precision, as an incoming positive amount dated on the month end.
            month_rows = connection.execute(
                "SELECT amount_text, economic_direction, effective_at FROM ledger_entries "
                "WHERE effective_precision = 'MONTH'"
            ).fetchall()
            self.assertGreater(expected_month_rows, 0)
            self.assertEqual(len(month_rows), expected_month_rows)
            for row in month_rows:
                self.assertEqual(row["economic_direction"], "IN")
                self.assertGreater(Decimal(row["amount_text"]), 0)
                self.assertTrue(_is_month_end(row["effective_at"][:10]))
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM document_reconciliation_checks WHERE blocking = 1 AND passed <> 1"
                ).fetchone()[0],
                0,
            )
        coverage = CliRunner().invoke(
            app, ["coverage", "--database", str(self.database), "--plain"]
        )
        self.assertEqual(coverage.exit_code, 0, coverage.output)
        self.assertIn("MARI_SAVINGS", coverage.output)

    @unittest.skipUnless(
        all(path.is_file() for path in TRUST_PERSIST_SOURCES),
        "Trust savings persistence statements unavailable",
    )
    def test_trust_savings_persists_foreign_detail_and_is_idempotent(self):
        foreign_source, other_source = TRUST_PERSIST_SOURCES
        parsed = [parse_trust_savings(path.read_bytes()) for path in TRUST_PERSIST_SOURCES]
        expected_credits = sum(
            record.record_kind == "TRANSACTION" and record.credit is not None
            for document in parsed
            for record in document.records
        )
        expected_foreign = sum(
            record.record_kind == "TRANSACTION" and record.foreign_currency is not None
            for document in parsed
            for record in document.records
        )
        first = self.service.import_trust(foreign_source)
        second = self.service.import_trust(other_source)
        reimport = self.service.import_trust(foreign_source)

        self.assertEqual((first.status, second.status), ("ACCEPTED", "ACCEPTED"))
        self.assertTrue(reimport.evidence_reused and reimport.extraction_reused)
        self.assertEqual(first.extraction_run_id, reimport.extraction_run_id)

        with closing(self.connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries WHERE source_family = 'DOCUMENT'"
                ).fetchone()[0],
                first.transaction_count + second.transaction_count,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries WHERE account_id = 'trust-savings' "
                    "AND economic_direction = 'IN'"
                ).fetchone()[0],
                expected_credits,
            )
            foreign = connection.execute(
                "SELECT source_currency, source_amount_text, amount_text FROM ledger_entries "
                "WHERE source_currency IS NOT NULL"
            ).fetchall()
            self.assertGreater(expected_foreign, 0)
            self.assertEqual(len(foreign), expected_foreign)
            for row in foreign:
                self.assertTrue(_is_currency_code(row["source_currency"]))
                self.assertNotEqual(row["source_currency"], "SGD")
                self.assertTrue(row["source_amount_text"])
                self.assertTrue(row["amount_text"])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM document_reconciliation_checks "
                    "WHERE blocking = 1 AND passed <> 1"
                ).fetchone()[0],
                0,
            )
            # The profile must be one selection.py still recognises as current,
            # or the run is demoted out of analytics and the coverage/profile
            # report flags every Trust statement as needing re-extraction.
            self.assertEqual(
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT selection_state FROM extraction_runs "
                        "WHERE source_profile_version = ?",
                        (TRUST_SAVINGS_PROFILE,),
                    ).fetchall()
                ],
                ["SELECTED"],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries "
                    "WHERE account_id = 'trust-savings' AND analytics_eligible = 0"
                ).fetchone()[0],
                0,
            )
        coverage = CliRunner().invoke(
            app, ["coverage", "--database", str(self.database), "--plain"]
        )
        self.assertEqual(coverage.exit_code, 0, coverage.output)
        self.assertIn("TRUST_SAVINGS", coverage.output)
        self.assertIn("needs_reextraction=0", coverage.output)

    @unittest.skipUnless(MARI_CARD_FILES, "Mari credit-card persistence statement unavailable")
    def test_mari_card_persists_alt_dates_and_foreign_detail_and_is_idempotent(self):
        source = _mari_card_persist_source()
        first = self.service.import_mari_card(source)
        reimport = self.service.import_mari_card(source)

        self.assertEqual(first.status, "ACCEPTED")
        self.assertTrue(reimport.evidence_reused and reimport.extraction_reused)

        with closing(self.connect()) as connection:
            entries = connection.execute(
                "SELECT count(*) FROM ledger_entries WHERE source_family = 'DOCUMENT'"
            ).fetchone()[0]
            self.assertGreater(entries, 0)
            self.assertEqual(entries, first.transaction_count)
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries "
                    "WHERE alt_date_role = 'TRANSACTION' AND alt_date_raw IS NOT NULL"
                ).fetchone()[0],
                entries,
            )
            # Every printed transaction date resolves, including the pre-cycle
            # instalment purchase dates, which anchor to their posted date.
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries "
                    "WHERE alt_date_raw IS NOT NULL AND alt_date_value IS NULL"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries "
                    "WHERE alt_date_value IS NOT NULL AND alt_date_value > effective_at"
                ).fetchone()[0],
                0,
            )
            self.assertGreater(
                connection.execute(
                    "SELECT count(*) FROM ledger_entries WHERE source_currency IS NOT NULL"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM document_reconciliation_checks WHERE blocking = 1 AND passed <> 1"
                ).fetchone()[0],
                0,
            )

    @unittest.skipUnless(
        DBS_PERSIST_SOURCE.is_file() and PAYLAH_PERSIST_SOURCE.is_file(),
        "DBS/PayLah persistence statements unavailable",
    )
    def test_different_hash_for_same_statement_is_visible_and_quarantined(self):
        source = DBS_PERSIST_SOURCE
        self.service.import_paylah(PAYLAH_PERSIST_SOURCE)
        first = self.service.import_dbs(source)
        with closing(self.connect()) as connection:
            confirmed_before_conflict = connection.execute(
                "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
            ).fetchone()[0]
        self.assertGreater(confirmed_before_conflict, 0)
        corrected = self.root / "corrected.pdf"
        shutil.copyfile(source, corrected)
        with corrected.open("ab") as stream:
            stream.write(b"\n% distinct candidate bytes\n")

        second = self.service.import_dbs(corrected)

        self.assertEqual(first.status, "ACCEPTED")
        self.assertEqual(second.status, "QUARANTINED")
        self.assertNotEqual(first.evidence_sha256, second.evidence_sha256)
        with closing(self.connect()) as connection:
            states = connection.execute(
                "SELECT selection_state, conflict_state FROM statements WHERE source_type = 'DBS_CONSOLIDATED' ORDER BY evidence_sha256"
            ).fetchall()
            self.assertEqual({tuple(row) for row in states}, {("CANDIDATE", "CONFLICT")})
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM extraction_issues WHERE code = 'STATEMENT_CONTENT_CONFLICT' AND severity = 'BLOCKING'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT sum(e.analytics_eligible) FROM ledger_entries e JOIN statements s ON s.id = e.statement_id WHERE e.source_family = 'DOCUMENT' AND s.source_type = 'DBS_CONSOLIDATED'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
                ).fetchone()[0],
                0,
            )
            rejected = connection.execute(
                "SELECT evidence_json FROM transfer_links WHERE status = 'REJECTED'"
            ).fetchall()
            # Every link the conflicting statement had confirmed is revoked.
            self.assertEqual(len(rejected), confirmed_before_conflict)
            self.assertTrue(all("revocation" in json.loads(row[0]) for row in rejected))

    def test_phase2_money_columns_never_use_sqlite_real(self):
        connection = connect_sqlite(self.database)
        initialize_schema(connection)
        for table in (
            "document_records",
            "ledger_entries",
            "document_reconciliation_checks",
            "transfer_links",
        ):
            declared_types = {
                row[2].upper() for row in connection.execute(f"PRAGMA table_info({table})")
            }
            self.assertNotIn("REAL", declared_types)
        self.assertEqual(
            connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()[0],
            str(SCHEMA_VERSION),
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
