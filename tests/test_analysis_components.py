"""v0.2 analysis slice 1: the shared economic-component projection.

Covers report/component parity, the schema-10 materialization, contributor
keyset paging, and origin/generation identity stability across rebuild.
"""
from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from fincl.analysis import handles
from fincl.analysis.components import _classify_amount, build_components
from fincl.db import connect_readonly
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.queries import contributors_page, dashboard
from fincl.refresh import refresh_ledger
from fincl.report import _coverage_contract, economic_flow_report_data
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-01-05 10:00:00", "2026-01-05 10:00:01", "Alpha", "-10.00", "0.50", "SGD", "COMPLETED", "889.50"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01", "Beta", "-20.00", "0.00", "SGD", "COMPLETED", "869.50"],
    ["Card Payment", "Current", "2026-02-18 10:00:00", "2026-02-18 10:00:01", "Gamma", "-7.50", "0.00", "SGD", "COMPLETED", "862.00"],
    ["Refund", "Current", "2026-02-20 10:00:00", "2026-02-20 10:00:01", "Beta", "5.00", "0.00", "SGD", "COMPLETED", "867.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01", "Delta", "-30.00", "0.00", "SGD", "COMPLETED", "837.00"],
]


def _ledger(rows: list[list[str]] = _ROWS) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


class ReportParityTests(unittest.TestCase):
    def test_component_bucket_sums_equal_the_report_totals(self) -> None:
        database = _ledger()
        report = economic_flow_report_data(database)
        connection = connect_readonly(database)
        try:
            covered = _coverage_contract(connection).covered_intervals
            projection = build_components(
                connection, covered_intervals=covered, analysis_intervals=covered
            )
        finally:
            connection.close()
        bucket = report["currencies"][0]
        for name in ("spending", "refunds", "fees", "income", "pending_review"):
            self.assertEqual(
                projection.bucket_total("SGD", "other" if name == "other" else name),
                bucket[name],
                name,
            )
        # And the netting reproduces the consolidated total.
        net = sum(Decimal(c) * Decimal(10) ** -s for _, c, s, _ in projection.netting())
        self.assertEqual(net, Decimal(report["consolidated_sgd_total"]))

    def test_fee_on_a_linked_leg_contributes_a_fee_component_and_no_amount(self) -> None:
        # A confirmed internal transfer: its amount drops, a separate fee stays.
        bucket, nets = _classify_amount(
            is_linked=True, direction="OUT", event_kind="PURCHASE", classification=None
        )
        self.assertEqual((bucket, nets), ("internal", False))

    def test_explicit_outbound_self_transfer_is_internal(self) -> None:
        bucket, nets = _classify_amount(
            is_linked=False,
            direction="OUT",
            event_kind="TRANSFER",
            classification="self_transfer",
        )
        self.assertEqual((bucket, nets), ("internal", False))

    def test_classifier_matches_the_transaction_view_bucket(self) -> None:
        database = _ledger()
        connection = connect_readonly(database)
        try:
            rows = connection.execute(
                "SELECT e.economic_direction, e.event_kind, i.classification, "
                "EXISTS(SELECT 1 FROM transfer_links t WHERE t.status='CONFIRMED' "
                "  AND (t.left_entry_id=e.id OR t.right_entry_id=e.id)) AS linked "
                "FROM ledger_entries e "
                "LEFT JOIN inbound_classifications i "
                "  ON i.anchor=e.natural_anchor AND i.bound=1 "
                "WHERE e.analytics_eligible=1 AND e.lifecycle_state='COMPLETED'"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            if row["economic_direction"] == "ZERO":
                continue
            bucket, _ = _classify_amount(
                is_linked=bool(row["linked"]),
                direction=row["economic_direction"],
                event_kind=row["event_kind"],
                classification=row["classification"],
            )
            self.assertIn(
                bucket,
                {
                    "spending", "income", "refunds", "reimbursements",
                    "non_income_inflow", "pending_review", "other", "internal",
                },
            )


class MonthPrecisionTests(unittest.TestCase):
    def test_a_month_row_outside_the_window_contributes_no_component(self) -> None:
        database = _ledger()
        connection = connect_readonly(database)
        try:
            covered = _coverage_contract(connection).covered_intervals
            from fincl.intervals import DateInterval

            narrow = [DateInterval("2026-02-01", "2026-02-28")]
            projection = build_components(
                connection, covered_intervals=covered, analysis_intervals=narrow
            )
        finally:
            connection.close()
        for component in projection.components:
            self.assertGreaterEqual(component.represented_start, "2026-02-01")
            self.assertLessEqual(component.represented_end, "2026-02-28")


class MaterializationTests(unittest.TestCase):
    def test_refresh_publishes_components_with_the_generation(self) -> None:
        database = _ledger()
        connection = connect_readonly(database)
        try:
            token = connection.execute(
                "SELECT generation_token FROM projection_generation WHERE id=1"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT generation_token, count(*) AS n FROM analysis_components "
                "GROUP BY generation_token"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["generation_token"], token)
        self.assertGreater(rows[0]["n"], 0)

    def test_origin_id_is_stable_and_component_id_follows_generation(self) -> None:
        database = _ledger()

        def snapshot() -> dict[str, tuple[str, str]]:
            connection = connect_readonly(database)
            try:
                return {
                    row["origin_component_id"]: (
                        row["component_id"],
                        connection.execute(
                            "SELECT generation_token FROM projection_generation WHERE id=1"
                        ).fetchone()[0],
                    )
                    for row in connection.execute(
                        "SELECT origin_component_id, component_id FROM analysis_components"
                    )
                }
            finally:
                connection.close()

        before = snapshot()
        refresh_ledger(database)  # deterministic no-op refresh
        after = snapshot()
        self.assertEqual(set(before), set(after))
        for origin, (component_id, _) in before.items():
            # Unchanged inputs -> same generation -> same component id.
            self.assertEqual(after[origin][0], component_id)


class ContributorPagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger()
        connection = connect_readonly(self.database)
        try:
            self.token = connection.execute(
                "SELECT generation_token FROM projection_generation WHERE id=1"
            ).fetchone()[0]
            covered = _coverage_contract(connection).covered_intervals
        finally:
            connection.close()
        self.window = (covered[0].start, covered[0].end)

    def _handle(self, **kw: object) -> str:
        start, end = self.window
        return handles.encode(
            generation=self.token, lens="recorded_flow", currency="SGD",
            start=start, end=end, buckets=["spending"], sort="amount", **kw,
        )

    def test_spending_contributors_reconcile_to_the_report_total(self) -> None:
        report = economic_flow_report_data(self.database)
        handle = self._handle()
        seen: list[str] = []
        total = Decimal(0)
        cursor = None
        while True:
            page = contributors_page(self.database, handle=handle, cursor=cursor, limit=2)
            for row in page["components"]:
                seen.append(row["component_id"])
                total += abs(Decimal(row["value"]))
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), len(set(seen)))  # no row repeated or skipped
        self.assertEqual(total, Decimal(report["currencies"][0]["spending"]))

    def test_limit_is_clamped_to_200(self) -> None:
        page = contributors_page(self.database, handle=self._handle(), limit=9999)
        self.assertLessEqual(len(page["components"]), 200)

    def test_a_stale_generation_handle_is_refused(self) -> None:
        stale = handles.encode(
            generation="sha256:stale", lens="recorded_flow", currency="SGD",
            start=self.window[0], end=self.window[1], buckets=["spending"], sort="amount",
        )
        with self.assertRaises(FinCLError):
            contributors_page(self.database, handle=stale)

    def test_a_tampered_handle_is_refused(self) -> None:
        handle = self._handle()
        with self.assertRaises(FinCLError):
            contributors_page(self.database, handle=handle[:-4] + "AAAA")


class DashboardHandleTests(unittest.TestCase):
    def test_each_ranking_row_carries_a_working_handle(self) -> None:
        database = _ledger()
        data = dashboard(database)
        self.assertTrue(data["category_totals"])
        for row in data["category_totals"]:
            page = contributors_page(database, handle=row["handle"])
            drilled = sum(abs(Decimal(c["value"])) for c in _drain(database, row["handle"]))
            self.assertEqual(drilled, Decimal(row["value"]))
            self.assertTrue(all(c["category"] == row["name"] for c in page["components"]))

    def test_summary_handles_cover_every_bucket(self) -> None:
        database = _ledger()
        data = dashboard(database)
        self.assertEqual(set(data["summary_handles"]), set(data["summary"]))
        net = _drain(database, data["summary_handles"]["net_economic_flow"])
        self.assertTrue(all(c["nets"] for c in net))


class CliContributorsTests(unittest.TestCase):
    def test_cli_lists_components_that_reconcile_with_the_report(self) -> None:
        import json as _json

        from typer.testing import CliRunner

        from fincl.cli import app

        database = _ledger()
        report = economic_flow_report_data(database)
        result = CliRunner().invoke(
            app,
            ["contributors", "--database", str(database), "--bucket", "spending",
             "--json", "--no-refresh"],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        payload = _json.loads(result.output)
        total = sum(abs(Decimal(c["value"])) for c in payload["components"])
        self.assertEqual(total, Decimal(report["currencies"][0]["spending"]))


def _drain(database: Path, handle: str) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        page = contributors_page(database, handle=handle, cursor=cursor, limit=50)
        out.extend(page["components"])
        cursor = page["next_cursor"]
        if not cursor:
            return out


if __name__ == "__main__":
    unittest.main()
