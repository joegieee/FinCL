"""v0.2 analysis slice 4: refund allocation decisions and receipt-period cost."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fincl.application import record_decision, record_refund_allocation
from fincl.db import connect_readonly
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.queries import analysis_page
from fincl.refresh import refresh_ledger
from fincl.report import economic_flow_report_data
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-02-01 09:00:00", "2026-02-01 09:00:01", "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01", "Gadget Store", "-80.00", "0.00", "SGD", "COMPLETED", "820.00"],
    ["Card Payment", "Current", "2026-02-08 10:00:00", "2026-02-08 10:00:01", "Grocer", "-30.00", "0.00", "SGD", "COMPLETED", "790.00"],
    ["Card Refund", "Current", "2026-02-20 10:00:00", "2026-02-20 10:00:01", "Gadget Store", "80.00", "0.00", "SGD", "COMPLETED", "870.00"],
]


def _ledger() -> tuple[Path, dict[str, str]]:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(_ROWS))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    connection = sqlite3.connect(database)
    try:
        anchors = {
            row[1]: row[0]
            for row in connection.execute(
                "SELECT e.natural_anchor, COALESCE(m.merchant_normalized, m.merchant_canonical, 'x') "
                "FROM ledger_entries e LEFT JOIN merchant_observations m ON m.entry_id = e.id"
            )
        }
    finally:
        connection.close()
    return database, anchors


def _refund_and_purchase(database: Path) -> tuple[str, str]:
    connection = sqlite3.connect(database)
    try:
        refund = connection.execute(
            "SELECT natural_anchor FROM ledger_entries WHERE event_kind = 'REFUND'"
        ).fetchone()[0]
        purchase = connection.execute(
            "SELECT natural_anchor FROM ledger_entries "
            "WHERE economic_direction = 'OUT' AND abs(amount_coefficient) = 8000"
        ).fetchone()[0]
        return refund, purchase
    finally:
        connection.close()


class RefundAllocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database, _ = _ledger()
        self.refund, self.purchase = _refund_and_purchase(self.database)

    def _set(self, allocations: list[dict[str, str]], rid: str = "r1") -> None:
        record_refund_allocation(
            self.database, request_id=rid, intent_hash=rid + "h",
            refund_anchor=self.refund, allocations=allocations, reason="matched receipt",
        )

    def test_allocation_projects_and_does_not_move_recorded_flow(self) -> None:
        before = economic_flow_report_data(self.database)["currencies"][0]
        self._set([{"purchase_anchor": self.purchase, "refund_amount": "80.00",
                    "purchase_amount": "80.00"}])
        after = economic_flow_report_data(self.database)["currencies"][0]
        # Recorded flow is unchanged: the refund still nets as a refund inflow.
        self.assertEqual(before["refunds"], after["refunds"])
        self.assertEqual(before["spending"], after["spending"])

        connection = connect_readonly(self.database)
        try:
            row = connection.execute(
                "SELECT refund_coefficient, purchase_coefficient, currency "
                "FROM refund_allocations WHERE refund_anchor = ?", (self.refund,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((row["refund_coefficient"], row["currency"]), (8000, "SGD"))

    def test_receipt_period_cost_nets_the_refund_into_the_purchase_category(self) -> None:
        self._set([{"purchase_anchor": self.purchase, "refund_amount": "80.00",
                    "purchase_amount": "80.00"}])
        page = analysis_page(self.database, start="2026-02-01", end="2026-02-20")
        by_cat = {r["category"]: r for r in page["receipt_period_cost"]}
        # Gadget Store purchase and its refund are both Unclassified here.
        cost = by_cat["Unclassified / Unclassified"]
        self.assertEqual(cost["gross"]["value"], "110.00")          # 80 + 30
        self.assertEqual(cost["refunds_received"]["value"], "80.00")
        self.assertEqual(cost["net_cost"]["value"], "30.00")

    def test_over_allocation_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._set([{"purchase_anchor": self.purchase, "refund_amount": "80.00",
                        "purchase_amount": "100.00"}])  # exceeds the 80.00 purchase

    def test_mismatched_same_currency_amounts_are_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._set([{"purchase_anchor": self.purchase, "refund_amount": "80.00",
                        "purchase_amount": "70.00"}])

    def test_allocated_total_over_the_refund_is_refused(self) -> None:
        grocer = _refund_and_purchase  # noqa: F841 - readability
        connection = sqlite3.connect(self.database)
        try:
            other = connection.execute(
                "SELECT natural_anchor FROM ledger_entries "
                "WHERE abs(amount_coefficient) = 3000 AND economic_direction = 'OUT'"
            ).fetchone()[0]
        finally:
            connection.close()
        with self.assertRaises(FinCLError):
            self._set([
                {"purchase_anchor": self.purchase, "refund_amount": "80.00", "purchase_amount": "80.00"},
                {"purchase_anchor": other, "refund_amount": "30.00", "purchase_amount": "30.00"},
            ])  # 110 allocated against an 80.00 refund

    def test_retire_removes_the_allocation(self) -> None:
        self._set([{"purchase_anchor": self.purchase, "refund_amount": "80.00",
                    "purchase_amount": "80.00"}])
        record_decision(
            self.database, request_id="r2", intent_hash="r2h",
            decision_kind="REFUND_ALLOCATION_RETIRE", anchors={"refund": self.refund},
            payload={"reason": "unlink"}, policy_version="refund-v1", snapshot="retire",
        )
        connection = connect_readonly(self.database)
        try:
            count = connection.execute(
                "SELECT count(*) FROM refund_allocations"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_link_reject_suppresses_one_pair(self) -> None:
        self._set([{"purchase_anchor": self.purchase, "refund_amount": "80.00",
                    "purchase_amount": "80.00"}])
        record_decision(
            self.database, request_id="r3", intent_hash="r3h",
            decision_kind="REFUND_LINK_REJECT",
            anchors={"refund": self.refund, "purchase": self.purchase},
            payload={"reason": "not this purchase"}, policy_version="refund-v1", snapshot="rej",
        )
        connection = connect_readonly(self.database)
        try:
            count = connection.execute(
                "SELECT count(*) FROM refund_allocations"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_projection_is_stable_across_a_no_op_refresh(self) -> None:
        self._set([{"purchase_anchor": self.purchase, "refund_amount": "80.00",
                    "purchase_amount": "80.00"}])
        first = refresh_ledger(self.database).generation_token
        second = refresh_ledger(self.database).generation_token
        self.assertEqual(first, second)


class CliRefundsTests(unittest.TestCase):
    def test_cli_set_then_retire(self) -> None:
        from typer.testing import CliRunner

        from fincl.cli import app

        database, _ = _ledger()
        refund, purchase = _refund_and_purchase(database)
        runner = CliRunner()
        out = runner.invoke(app, [
            "refunds", "set", "--database", str(database), "--refund", refund,
            "--allocation", f"{purchase},80.00,80.00", "--reason", "receipt matched",
        ])
        self.assertEqual(out.exit_code, 0, out.output)
        listing = runner.invoke(app, ["refunds", "list", "--database", str(database),
                                      "--json", "--no-refresh"])
        self.assertIn(purchase, listing.output)

        retire = runner.invoke(app, ["refunds", "retire", "--database", str(database),
                                     "--refund", refund, "--reason", "unlink"])
        self.assertEqual(retire.exit_code, 0, retire.output)
        connection = connect_readonly(database)
        try:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM refund_allocations").fetchone()[0], 0
            )
        finally:
            connection.close()

    def test_cli_set_refuses_over_allocation(self) -> None:
        from typer.testing import CliRunner

        from fincl.cli import app

        database, _ = _ledger()
        refund, purchase = _refund_and_purchase(database)
        out = CliRunner().invoke(app, [
            "refunds", "set", "--database", str(database), "--refund", refund,
            "--allocation", f"{purchase},80.00,100.00", "--reason", "too much",
        ])
        self.assertNotEqual(out.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
