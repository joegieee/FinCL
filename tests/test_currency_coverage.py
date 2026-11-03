"""Coverage of one currency, with every account answering for itself."""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fincl import coverage
from fincl.baseline import readiness
from fincl.intervals import DateInterval, contains_range


class PocketCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            """
            CREATE TABLE ledger_entries (account_id TEXT, currency_pocket TEXT);
            CREATE TABLE movement_coverage_certifications
                (account_id TEXT, currency_pocket TEXT, period_start TEXT, period_end TEXT, retired INTEGER);
            INSERT INTO ledger_entries VALUES ('a', 'SGD'), ('b', 'SGD'), ('c', 'USD');
            """
        )
        statements = {
            "a": [DateInterval("2026-01-01", "2026-03-31")],
            "b": [DateInterval("2026-02-01", "2026-02-28")],
            "c": [DateInterval("2026-01-01", "2026-01-31")],
        }
        contract = SimpleNamespace(covered_intervals=[DateInterval("2026-01-01", "2026-03-31")])
        self.ends: tuple[dict[tuple[str, str], dict[str, str]], dict[str, str]] = ({}, {})
        for owner, name, value in (
            (coverage, "account_statement_coverage", statements),
            (coverage, "coverage_contract", contract),
            (readiness, "_account_scopes_from", self.ends),
        ):
            patcher = patch.object(owner, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _covered(self, start: str, end: str) -> bool:
        return contains_range(coverage.pocket_covered_intervals(self.connection, "SGD"), start, end)

    def test_an_account_does_not_answer_for_another(self) -> None:
        self.assertFalse(self._covered("2026-03-01", "2026-03-31"))

    def test_an_account_owes_nothing_before_its_first_statement(self) -> None:
        self.assertTrue(self._covered("2026-01-01", "2026-02-28"))

    def test_an_other_currency_account_is_not_required(self) -> None:
        self.assertTrue(self._covered("2026-02-01", "2026-02-28"))

    def test_certifying_the_remaining_span_closes_the_gap(self) -> None:
        self.connection.execute(
            "INSERT INTO movement_coverage_certifications VALUES ('b', 'SGD', '2026-03-01', '2026-03-31', 0)"
        )

        self.assertTrue(self._covered("2026-03-01", "2026-03-31"))

    def test_an_archived_account_owes_nothing_after_it_closed(self) -> None:
        self.ends[1]["b"] = "2026-02-28"

        self.assertTrue(self._covered("2026-03-01", "2026-03-31"))

    def test_a_stated_end_for_the_pocket_does_the_same(self) -> None:
        self.ends[0][("b", "SGD")] = {"expected_start": "2026-02-01", "expected_end": "2026-02-28"}

        self.assertTrue(self._covered("2026-03-01", "2026-03-31"))
