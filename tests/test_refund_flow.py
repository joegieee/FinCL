"""Receipt-period refund flow, independent of future purchase attribution."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from typer.testing import CliRunner

from fincl.cli import app
from fincl.db import connect_sqlite
from fincl.report import build_economic_flow_report, economic_flow_report_data
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes


class RefundFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "ledger.sqlite3"
        source = root / "refund.csv"
        source.write_bytes(csv_bytes([
            ["Card Payment", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Purchase", "-10.00", "0.00", "SGD", "COMPLETED", "90.00"],
            ["Card Refund", "Current", "2026-02-01 10:00:00", "2026-02-01 10:00:01", "Refund", "5.00", "0.00", "SGD", "COMPLETED", "95.00"],
        ]))
        result = FinCLService.plaintext_phase1(self.database, root / "evidence").import_revolut(source)
        self.assertEqual(result.status, "ACCEPTED")

    def test_refund_nets_in_receipt_period_without_becoming_income(self) -> None:
        data = economic_flow_report_data(self.database)
        bucket = data["currencies"][0]
        self.assertEqual(bucket["refunds"], "5.00")
        self.assertEqual(bucket["income"], "0")
        self.assertEqual(bucket["pending_review"], "0")
        self.assertEqual(data["consolidated_sgd_total"], "-5.00")
        receipt = economic_flow_report_data(self.database, start="2026-02-01", end="2026-02-01")
        self.assertEqual(receipt["consolidated_sgd_total"], "5.00")
        purchase = economic_flow_report_data(self.database, start="2026-01-01", end="2026-01-01")
        self.assertEqual(purchase["consolidated_sgd_total"], "-10.00")
        self.assertIn("refunds=5.00", build_economic_flow_report(self.database))

    def test_explicit_self_transfer_decision_overrides_refund_default(self) -> None:
        connection = connect_sqlite(self.database)
        try:
            anchor = connection.execute(
                "SELECT natural_anchor FROM ledger_entries WHERE event_kind = 'REFUND'"
            ).fetchone()[0]
        finally:
            connection.close()
        result = CliRunner().invoke(app, [
            "review", "classify-inbound", "--database", str(self.database),
            "--anchor", anchor, "--as", "self_transfer", "--reason", "source label corrected",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        data = economic_flow_report_data(self.database)
        self.assertEqual(data["currencies"][0]["refunds"], "0")
        self.assertEqual(data["currencies"][0]["excluded_self_transfer_entries"], 1)
        self.assertEqual(data["consolidated_sgd_total"], "-10.00")
