"""Stage 3A — forecast readiness and the input manifest.

A real ledger is built through the Revolut import path and ``refresh_ledger``.
The four cases the manifest has to get right:

1. missing opening cash blocks the cash-balance outputs only — the observed
   cost table, income evidence and spending evidence stay available;
2. no evidenced classified income is a named gap, and a pending inflow is kept
   separate rather than read as zero or turned into an invented salary;
3. an unclassified observed outflow survives as non-cuttable unresolved
   spending and does not block recurrence or baseline inference; and
4. an input that reports a different generation makes the manifest stale
   instead of being silently combined.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import fincl.forecast.manifest as manifest_module
from fincl.api import create_app
from fincl.forecast import (
    FORECAST_READINESS_LEVELS,
    READINESS_UNAVAILABLE,
    forecast_input_manifest,
)
from fincl.refresh import refresh_ledger
from fincl.service import FinCLService
from tests.asgi_client import ASGITestClient
from tests.test_revolut_import import csv_bytes

_TODAY = date(2026, 4, 10)


def _rows() -> list[list[str]]:
    """One seed top-up (a pending inflow, not classified income) and three
    months of unclassified card spending."""
    rows = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "5000.00", "0.00", "SGD", "COMPLETED", "5000.00"],
    ]
    balance = 5000.0
    for month in ("01", "02", "03"):
        for day, amount, merchant in (
            ("05", "-30.00", "Corner Shop"),
            ("15", "-45.00", "Mystery Merchant XYZ"),
            ("20", "-1200.00", "Landlord Rentco"),
        ):
            balance += float(amount)
            rows.append([
                "Card Payment", "Current", f"2026-{month}-{day} 10:00:00",
                f"2026-{month}-{day} 10:00:01", merchant, amount, "0.00",
                "SGD", "COMPLETED", f"{balance:.2f}",
            ])
    return rows


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    FinCLService.plaintext_phase1(database, root / "evidence").import_revolut(source)
    refresh_ledger(database)
    return database


class MissingOpeningCashTests(unittest.TestCase):
    def test_missing_opening_cash_blocks_balances_only(self) -> None:
        database = _ledger(_rows())
        # Drop every running balance: the pocket stays liquid but has no
        # supported opening-cash boundary.
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE ledger_entries SET balance_coefficient=NULL, balance_scale=NULL"
            )
            connection.commit()
        finally:
            connection.close()

        manifest = forecast_input_manifest(database, today=_TODAY)

        self.assertFalse(manifest["stale"])
        cash = manifest["outputs"]["cash_balances"]
        self.assertEqual(cash["readiness"], READINESS_UNAVAILABLE)
        self.assertFalse(cash["available"])
        self.assertTrue(cash["reason"])
        self.assertEqual(
            manifest["cash"]["by_currency"]["SGD"]["readiness"], READINESS_UNAVAILABLE
        )

        # The observed cost table and the two evidence views are untouched.
        for output in ("observed_cost_table", "income_evidence", "spending_evidence"):
            level = manifest["outputs"][output]["readiness"]
            self.assertIn(level, FORECAST_READINESS_LEVELS)
            self.assertNotEqual(level, READINESS_UNAVAILABLE, output)
        self.assertTrue(manifest["observed_cost"]["available"])
        self.assertEqual(len(manifest["observed_cost"]["months"]), 3)


class NoEvidencedIncomeTests(unittest.TestCase):
    def test_no_classified_income_is_a_named_gap_without_invented_income(self) -> None:
        manifest = forecast_input_manifest(_ledger(_rows()), today=_TODAY)

        income = manifest["income"]
        self.assertFalse(income["classified_income_evidenced"])
        self.assertEqual(income["streams"], [])
        # No invented salary anywhere in the income section.
        self.assertNotIn("expected_income", income)

        gap = income["gap"]
        self.assertIsNotNone(gap)
        self.assertEqual(gap["kind"], "no_classified_income_stream")
        self.assertIn("stress case", gap["treatment"])
        self.assertIn("no salary is invented", gap["treatment"])

        # The seed top-up is a pending inflow, retained separately and never
        # read as zero expected income.
        self.assertEqual(income["pending_inflow_by_currency"].get("SGD"), "5000.00")
        self.assertEqual(gap["pending_inflow_by_currency"].get("SGD"), "5000.00")

        self.assertTrue(
            any("stress case" in item for item in manifest["limitations"])
        )


class UnclassifiedOutflowTests(unittest.TestCase):
    def test_unclassified_outflow_survives_as_non_cuttable_unresolved_spending(self) -> None:
        manifest = forecast_input_manifest(_ledger(_rows()), today=_TODAY)

        unresolved = manifest["unresolved_items"]["unclassified_spending"]
        self.assertTrue(unresolved)
        for item in unresolved:
            self.assertFalse(item["cuttable"])
            self.assertEqual(item["role"], "unresolved_spending")
            self.assertEqual(item["kind"], "resolve-categorization")
            # the readiness task's own note is carried through verbatim
            self.assertIn("not discretionary and not zero", item["note"])

        # imperfect categorisation does not block recurrence or baseline work
        for output in ("observed_cost_table", "recurrence_inference", "routine_baseline"):
            self.assertNotEqual(
                manifest["outputs"][output]["readiness"], READINESS_UNAVAILABLE, output
            )
        self.assertTrue(
            any("unresolved spending" in item for item in manifest["limitations"])
        )


class MixedGenerationTests(unittest.TestCase):
    def test_a_mixed_generation_input_is_reported_stale_not_combined(self) -> None:
        database = _ledger(_rows())
        real = manifest_module.obligations_page(database, as_of=None, today=_TODAY)
        forged = {**real, "generation": "sha256:" + "0" * 64}

        with patch.object(manifest_module, "obligations_page", return_value=forged):
            manifest = forecast_input_manifest(database, today=_TODAY)

        self.assertTrue(manifest["stale"])
        self.assertEqual(manifest["mixed_generation_inputs"], ["obligations_page"])
        self.assertEqual(
            manifest["reported_generations"]["obligations_page"], "sha256:" + "0" * 64
        )
        self.assertTrue(
            any("obligations_page" in reason for reason in manifest["stale_reasons"])
        )
        # No mixed-generation numbers are handed back.
        self.assertFalse(manifest["observed_cost"]["available"])
        for output in manifest["outputs"].values():
            self.assertEqual(output["readiness"], READINESS_UNAVAILABLE)


class ManifestEndpointTests(unittest.TestCase):
    def test_endpoint_returns_the_manifest(self) -> None:
        database = _ledger(_rows())
        root = database.parent
        app = create_app(
            database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=root / "evidence",
        )
        client = ASGITestClient(app, base_url="http://127.0.0.1:8765")
        self.addCleanup(client.close)
        client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": "http://127.0.0.1:8765", "content-type": "application/json"},
        )

        response = client.get("/api/v1/forecast/manifest")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("outputs", body)
        self.assertIn("sources", body)
        self.assertEqual(
            {source["input"] for source in body["sources"]},
            {
                "baseline_readiness", "cost_income_table", "baseline_roles",
                "baseline_estimate", "obligations_page", "position_page",
            },
        )
        pinned = {s["input"]: s["generation_pinned"] for s in body["sources"]}
        self.assertFalse(pinned["position_page"])
        self.assertTrue(pinned["obligations_page"])


if __name__ == "__main__":
    unittest.main()
