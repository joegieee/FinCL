"""Reporting-currency plan M1 (docs/PLAN-reporting-currency-and-money-flow.md §8).

Covers: the currency registry, the journalled ``REPORTING_CURRENCY_SET``
decision and its projection, the ``direct-or-inverse-v1`` rate path resolver,
the schema-23 migration, the two new read routes, and the §5.3 explicit-as-of
invariant for the money-flow call graph.
"""

from __future__ import annotations

import re
import tempfile
import unittest
import uuid
from pathlib import Path

from fincl.db import SCHEMA_VERSION, connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.refresh import refresh_ledger
from fincl.reporting_currency import (
    RATE_PATH_NOT_CONFIGURED,
    REPORTING_CURRENCY_SET,
    currency_coverage_preview,
    is_registered_currency,
    reporting_policy,
    resolve_rate_path,
    structural_rate_path,
    validate_reporting_currency_payload,
)
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_api import ApiTestBase

_ANCHORS = {"ledger": "default"}


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


_USD_ROWS = [
    ["Topup", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01",
     "Seed", "100.00", "0.00", "USD", "COMPLETED", "100.00"],
    ["Card Payment", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:01",
     "Buy", "-40.00", "0.00", "USD", "COMPLETED", "60.00"],
]


class ValidationTests(unittest.TestCase):
    def test_currency_must_be_in_the_registry(self) -> None:
        with self.assertRaises(FinCLError):
            validate_reporting_currency_payload(
                REPORTING_CURRENCY_SET, _ANCHORS,
                {"currency": "ZZZ", "reason": "test"},
            )

    def test_a_valid_payload_passes(self) -> None:
        validate_reporting_currency_payload(
            REPORTING_CURRENCY_SET, _ANCHORS,
            {"currency": "USD", "reason": "operator moved abroad"},
        )

    def test_registry_membership(self) -> None:
        self.assertTrue(is_registered_currency("SGD"))
        self.assertTrue(is_registered_currency("JPY"))
        self.assertFalse(is_registered_currency("XXX"))


class DefaultPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = connect_sqlite(Path(self.temporary.name) / "fincl.sqlite3")
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def test_absent_decision_resolves_to_sgd(self) -> None:
        policy = reporting_policy(self.connection)
        self.assertEqual(policy.currency, "SGD")
        self.assertEqual(policy.minor_unit_scale, 2)
        self.assertTrue(policy.is_default)
        self.assertIsNone(policy.journal_seq)


class RatePathResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = connect_sqlite(Path(self.temporary.name) / "fincl.sqlite3")
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def test_identity_when_native_equals_reporting(self) -> None:
        result = resolve_rate_path(
            self.connection, native_currency="SGD", reporting_currency="SGD", period="2026-01"
        )
        self.assertEqual(result.basis, "identity")
        self.assertTrue(result.available)
        self.assertEqual(result.numerator, 1)
        self.assertEqual(result.denominator, 1)

    def test_direct_rate_is_used_as_is(self) -> None:
        load_fx_rates(self.connection, {
            "source": "MAS monthly average", "source_version": "mas-2026-v1",
            "quote_currency": "SGD",
            "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.25"}],
        })
        result = resolve_rate_path(
            self.connection, native_currency="USD", reporting_currency="SGD", period="2026-01"
        )
        self.assertEqual(result.basis, "direct")
        self.assertFalse(result.inverted)
        self.assertEqual(result.numerator, 5)
        self.assertEqual(result.denominator, 4)

    def test_inverse_is_the_exact_reciprocal(self) -> None:
        load_fx_rates(self.connection, {
            "source": "MAS monthly average", "source_version": "mas-2026-v1",
            "quote_currency": "SGD",
            "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.25"}],
        })
        # Reporting in USD, native SGD: no direct SGD->USD row, so the SGD
        # (native) -> ... no: here native=SGD, reporting=USD. A direct row
        # would be base=SGD, quote=USD (none loaded). The loaded row is
        # base=USD, quote=SGD, which is exactly the reporting->native
        # direction when reporting=USD, native=SGD — the inverse case.
        result = resolve_rate_path(
            self.connection, native_currency="SGD", reporting_currency="USD", period="2026-01"
        )
        self.assertEqual(result.basis, "inverse")
        self.assertTrue(result.inverted)
        self.assertEqual(result.numerator, 4)
        self.assertEqual(result.denominator, 5)

    def test_no_triangulation_leaves_a_third_currency_unavailable(self) -> None:
        load_fx_rates(self.connection, {
            "source": "MAS monthly average", "source_version": "mas-2026-v1",
            "quote_currency": "SGD",
            "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.25"}],
        })
        # EUR has no configured pair with SGD at all.
        result = resolve_rate_path(
            self.connection, native_currency="EUR", reporting_currency="SGD", period="2026-01"
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason, RATE_PATH_NOT_CONFIGURED)

    def test_structural_path_is_independent_of_a_dated_gap(self) -> None:
        load_fx_rates(self.connection, {
            "source": "MAS monthly average", "source_version": "mas-2026-v1",
            "quote_currency": "SGD",
            "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.25"}],
        })
        # A month with no imported rate is still structurally configured.
        structural = structural_rate_path(self.connection, "USD", "SGD")
        self.assertTrue(structural.available)
        dated = resolve_rate_path(
            self.connection, native_currency="USD", reporting_currency="SGD", period="2099-12"
        )
        self.assertFalse(dated.available)


class CoveragePreviewTests(unittest.TestCase):
    def test_a_currency_with_no_configured_pair_is_named(self) -> None:
        database = _ledger(_USD_ROWS)
        connection = connect_sqlite(database)
        try:
            preview = currency_coverage_preview(connection, "SGD")
            self.assertIn("USD", preview["structurally_unconfigured"])
            self.assertEqual(preview["by_currency"]["USD"]["reason"], RATE_PATH_NOT_CONFIGURED)
        finally:
            connection.close()

    def test_a_ledger_native_in_the_reporting_currency_is_identity(self) -> None:
        database = _ledger(_USD_ROWS)
        connection = connect_sqlite(database)
        try:
            preview = currency_coverage_preview(connection, "USD")
            self.assertEqual(preview["structurally_unconfigured"], [])
            self.assertEqual(preview["by_currency"]["USD"]["basis"], "identity")
        finally:
            connection.close()


class JournalReplayTests(unittest.TestCase):
    def test_replay_reproduces_the_decision_and_a_later_one_supersedes(self) -> None:
        database = _ledger(_USD_ROWS)
        # USD has no configured path to SGD, but reporting currency SGD is
        # still structurally recordable for a currency the operator has not
        # yet loaded rates for at all -- here we exercise the pure journal/
        # projection path directly rather than through the API's structural
        # rejection, so record a decision for the ledger's own native
        # currency (USD), which always has an identity path.
        journal_path = default_journal_path(database)
        append_decision(
            journal_path, decision_kind=REPORTING_CURRENCY_SET, anchors=_ANCHORS,
            payload={"currency": "USD", "reason": "first decision"},
            policy_version="reporting-currency-v1", snapshot="USD",
        )
        connection = connect_sqlite(database)
        try:
            replay_journal(connection)
            policy = reporting_policy(connection)
            self.assertEqual(policy.currency, "USD")
            self.assertFalse(policy.is_default)
            first_seq = policy.journal_seq
        finally:
            connection.close()

        append_decision(
            journal_path, decision_kind=REPORTING_CURRENCY_SET, anchors=_ANCHORS,
            payload={"currency": "SGD", "reason": "second decision supersedes"},
            policy_version="reporting-currency-v1", snapshot="SGD",
        )
        connection = connect_sqlite(database)
        try:
            replay_journal(connection)
            policy = reporting_policy(connection)
            self.assertEqual(policy.currency, "SGD")
            self.assertGreater(policy.journal_seq, first_seq)
        finally:
            connection.close()

        # Replaying again from scratch reproduces the same final state.
        connection = connect_sqlite(database)
        try:
            replay_journal(connection)
            policy_again = reporting_policy(connection)
            self.assertEqual(policy_again.currency, "SGD")
            self.assertEqual(policy_again.journal_seq, policy.journal_seq)
        finally:
            connection.close()


class SgdNoOpTests(unittest.TestCase):
    def test_sgd_is_the_default_and_changes_no_current_output(self) -> None:
        from fincl.report import economic_flow_report_data

        database = _ledger([
            ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
             "Coffee", "-4.00", "0.00", "SGD", "COMPLETED", "96.00"],
        ])
        before = economic_flow_report_data(database)
        connection = connect_sqlite(database)
        try:
            policy = reporting_policy(connection)
        finally:
            connection.close()
        self.assertEqual(policy.currency, "SGD")
        self.assertTrue(policy.is_default)
        after = economic_flow_report_data(database)
        self.assertEqual(before, after)


class SchemaTests(unittest.TestCase):
    def test_reporting_policy_table_exists_at_the_current_schema(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        connection = connect_sqlite(Path(temporary.name) / "fincl.sqlite3")
        self.addCleanup(connection.close)
        initialize_schema(connection)
        self.assertGreaterEqual(SCHEMA_VERSION, 23)
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reporting_policy'"
        ).fetchone()
        self.assertIsNotNone(row)


class ApiTests(ApiTestBase):
    def command(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> dict:
        csrf = self.bootstrap()
        body = {
            "request_id": str(uuid.uuid4()),
            "expected_generation": self.overview()["generation"],
            "kind": kind, "anchors": anchors, "payload": payload,
        }
        return self.post("/api/v1/commands", body, csrf)

    def test_reporting_currency_read_defaults_to_sgd(self) -> None:
        self.bootstrap()
        response = self.client.get("/api/v1/reporting-currency")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["reporting_policy"]["currency"], "SGD")
        self.assertTrue(data["reporting_policy"]["is_default"])
        self.assertIn("SGD", [c["code"] for c in data["currency_registry"]])

    def test_setting_the_ledgers_own_native_currency_succeeds(self) -> None:
        # The fixture ledger (tests.test_api._ROWS) is entirely SGD, so SGD is
        # always an identity path regardless of loaded rates.
        response = self.command(
            REPORTING_CURRENCY_SET, dict(_ANCHORS),
            {"currency": "SGD", "reason": "confirm the default explicitly"},
        )
        self.assertIn(response.status_code, {200, 202}, response.text)
        data = self.client.get("/api/v1/reporting-currency").json()
        self.assertEqual(data["reporting_policy"]["currency"], "SGD")
        self.assertFalse(data["reporting_policy"]["is_default"])

    def test_a_currency_with_no_configured_path_is_rejected(self) -> None:
        response = self.command(
            REPORTING_CURRENCY_SET, dict(_ANCHORS),
            {"currency": "USD", "reason": "no USD rates loaded for this SGD-only ledger"},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_fx_coverage_route(self) -> None:
        self.bootstrap()
        response = self.client.get("/api/v1/fx/coverage")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["reporting_currency"], "SGD")
        self.assertIn("SGD", data["by_currency"])
        self.assertEqual(data["by_currency"]["SGD"]["basis"], "identity")


class NoWallClockInMoneyFlowGraphTests(unittest.TestCase):
    """§5.3 / invariant 6: no reader in the money-flow call graph consults the
    wall clock; ``today``/``as_of`` are threaded in from the caller instead."""

    _FILES = (
        "src/fincl/position/queries.py",
        "src/fincl/obligations/queries.py",
        "src/fincl/baseline/readiness.py",
        "src/fincl/baseline/queries.py",
        "src/fincl/baseline/estimators.py",
        "src/fincl/baseline/roles.py",
        "src/fincl/cards/cash_conversion.py",
        "src/fincl/forecast/manifest.py",
        "src/fincl/forecast/engine.py",
        "src/fincl/controls/queries.py",
        "src/fincl/networth/queries.py",
    )
    _PATTERN = re.compile(r"date\.today\(\)|datetime\.now\(")

    def test_no_wall_clock_call_in_the_named_money_flow_readers(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        for relative in self._FILES:
            path = repo_root / relative
            text = path.read_text()
            self.assertNotRegex(
                text, self._PATTERN,
                f"{relative} calls the wall clock directly; today/as_of must be "
                "threaded in from the caller",
            )


if __name__ == "__main__":
    unittest.main()
