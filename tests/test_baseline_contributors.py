"""Baseline drill-down: the components behind each month of the decomposition,
and the observed range published beside the estimate."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from fincl.api import create_app
from fincl.application import record_decision
from fincl.baseline import baseline_contributors, baseline_estimate
from fincl.db import connect_readonly
from fincl.decisions import entry_anchor
from fincl.document_service import DocumentImportService
from fincl.refresh import refresh_ledger
from tests.asgi_client import ASGITestClient
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_TODAY = date(2026, 4, 15)
_ORIGIN = "http://127.0.0.1:8765"


def _rows() -> list[list[str]]:
    rows: list[list[str]] = [
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "5000.00", "0.00", "SGD", "COMPLETED", "5000.00"],
    ]
    balance = Decimal("5000.00")

    def add(kind: str, day: str, merchant: str, amount: str) -> None:
        nonlocal balance
        balance += Decimal(amount)
        rows.append([kind, "Current", f"{day} 10:00:00", f"{day} 10:00:01", merchant, amount,
                     "0.00", "SGD", "COMPLETED", f"{balance}"])

    for month in ("01", "02", "03"):
        add("Card Payment", f"2026-{month}-02", "Landlord", "-500.00")
        add("Card Payment", f"2026-{month}-09", "Corner Shop", "-20.00")
        add("Card Payment", f"2026-{month}-16", "Corner Shop", "-30.00")
    add("Card Refund", "2026-02-14", "Corner Shop", "10.00")
    add("Card Payment", "2026-02-20", "Corner Shop", "-2100.00")
    add("Card Payment", "2026-03-31", "Corner Shop", "-3.00")
    return rows


class BaselineContributorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "fincl.sqlite3"
        self.evidence = root / "evidence"
        source = root / "revolut.csv"
        source.write_bytes(csv_bytes(_rows()))
        DocumentImportService.plaintext_phase2(self.database, self.evidence).import_revolut_pdf(source)
        refresh_ledger(self.database)
        self._bind_rent()

    def _bind_rent(self) -> None:
        connection = connect_readonly(self.database)
        try:
            rent = connection.execute(
                "SELECT id, effective_at FROM ledger_entries WHERE amount_coefficient = -50000 ORDER BY effective_at"
            ).fetchall()
            anchors = [entry_anchor(connection, row["id"]) for row in rent]
            scheduled = [row["effective_at"][:10] for row in rent]
        finally:
            connection.close()
        record_decision(
            self.database, request_id="rent-set", intent_hash="rent-set",
            decision_kind="RECURRING_COMMITMENT_SET", anchors={"commitment": "rent"},
            payload={
                "name": "Rent", "currency": "SGD", "merchant_key": "landlord",
                "account_ids": ["acct-1"], "seed_entry_anchors": anchors,
                "schedule": {"kind": "monthly", "day": 2, "anchor_on": "2026-01-02"},
                "expected_amount": "500.00", "amount_tolerance": "0.00",
                "status": "active", "ended_on": None, "reason": "confirmed rent",
            },
            policy_version="planning-v1", snapshot="rent", refresh=False,
        )
        for index, (anchor, scheduled_on) in enumerate(zip(anchors, scheduled, strict=True)):
            record_decision(
                self.database, request_id=f"rent-{index}", intent_hash=f"rent-{index}",
                decision_kind="RECURRING_OCCURRENCE_SET",
                anchors={"commitment": "rent", "scheduled_on": scheduled_on},
                payload={"entry": anchor, "reason": "bound"},
                policy_version="planning-v1", snapshot="occ", refresh=index == len(anchors) - 1,
            )

    def _parts(self, month: str) -> dict[str, dict]:
        read = baseline_contributors(self.database, today=_TODAY, currency="SGD", month=month)
        self.assertTrue(read["reconciles"], month)
        return {part["part"]: part for part in read["parts"]}

    def test_each_month_partitions_into_the_published_decomposition(self) -> None:
        estimate = baseline_estimate(self.database, today=_TODAY)
        self.assertEqual(estimate["sample"]["months"], ["2026-01", "2026-02", "2026-03"])
        for row in estimate["by_currency"]["SGD"]["months"]:
            parts = self._parts(row["month"])
            for name, part in parts.items():
                self.assertEqual(part["total"], row[name], (row["month"], name))
                self.assertTrue(part["reconciles"])

    def test_each_component_lands_in_the_term_the_estimator_assigned_it(self) -> None:
        february = self._parts("2026-02")
        self.assertEqual([c["amount"] for c in february["identified_recurring"]["contributors"]], ["500.00"])
        self.assertEqual([c["amount"] for c in february["unresolved_irregular"]["contributors"]], ["2100.00"])
        self.assertEqual([c["amount"] for c in february["adjustments"]["contributors"]], ["-10.00"])
        self.assertEqual(sorted(c["amount"] for c in february["routine_variable_residual"]["contributors"]),
                         ["20.00", "30.00"])
        irregular = february["unresolved_irregular"]["contributors"][0]
        flagged = baseline_estimate(self.database, today=_TODAY)["by_currency"]["SGD"]["irregular_risk_entries"]
        self.assertEqual([entry["entry_anchor"] for entry in flagged], [irregular["entry_anchor"]])
        self.assertEqual(irregular["date"], "2026-02-20")

    def test_a_month_outside_the_window_is_refused(self) -> None:
        from fincl.errors import FinCLError
        with self.assertRaisesRegex(FinCLError, "outside the baseline window"):
            baseline_contributors(self.database, today=_TODAY, currency="SGD", month="2025-12")

    def test_the_observed_range_names_the_lowest_and_highest_month_on_the_headline_basis(self) -> None:
        sgd = baseline_estimate(self.database, today=_TODAY)["by_currency"]["SGD"]
        spread = sgd["observed_range"]
        self.assertTrue(spread["not_a_probability"])
        # flat fixed rent + that month's routine residual
        self.assertEqual(spread["routine_lifestyle_baseline"],
                         {"min_amount": "550.00", "min_month": "2026-01", "max_amount": "553.00", "max_month": "2026-03"})
        self.assertEqual(spread["irregular_allowance_monthly"]["months_with_entries"], 1)
        self.assertEqual(spread["total_observed_monthly"]["max_amount"], "2650.00")
        self.assertEqual(spread["total_observed_monthly"]["max_month"], "2026-02")
        self.assertLessEqual(Decimal(spread["routine_lifestyle_baseline"]["min_amount"]),
                             Decimal(sgd["routine_lifestyle_baseline"]))

    def test_the_reported_read_values_each_contributor_and_keeps_the_estimate_totals_native(self) -> None:
        app = create_app(self.database, port=8765, bootstrap_secret="secret", require_access_key=True,
                         evidence_dir=self.evidence)
        client = ASGITestClient(app, base_url=_ORIGIN)
        self.addCleanup(client.close)
        client.post("/session", json={"secret": "secret"}, headers={"origin": _ORIGIN, "content-type": "application/json"})
        with mock.patch("fincl.clock.today", return_value=_TODAY):
            drill = client.get("/api/v1/reporting/workspaces/baseline/contributors?currency=SGD&month=2026-02")
            estimate = client.get("/api/v1/reporting/workspaces/baseline/estimate")
            refused = client.get("/api/v1/baseline/contributors?currency=SGD&month=2025-12")
        self.assertEqual(drill.status_code, 200, drill.text)
        valued = {tuple(item["path"]): item["value"] for item in drill.json()["amounts"]}
        adjustments = drill.json()["native"]["parts"].index(
            next(p for p in drill.json()["native"]["parts"] if p["part"] == "adjustments"))
        refund = valued[("parts", adjustments, "contributors", 0, "amount")]
        self.assertEqual((refund["purpose"], refund["valuation_as_of"]), ("flow", "2026-02-14"))
        self.assertEqual(valued[("parts", adjustments, "total")]["reported"]["value"], "-10.00")
        # A same-currency report shows the estimator's own figures, refunds and
        # matched recurring cost included, not a mean of recorded owner cost.
        native = estimate.json()["native"]["by_currency"]["SGD"]
        reported = {tuple(item["path"]): item["value"] for item in estimate.json()["amounts"]}
        for field in ("routine_lifestyle_baseline", "total_observed_monthly", "historical_surplus_monthly"):
            self.assertEqual(reported[("by_currency", "SGD", field)]["reported"]["value"], native[field], field)
        self.assertEqual(refused.status_code, 422)


if __name__ == "__main__":
    unittest.main()
