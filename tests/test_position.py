"""The as-of liquid position read (cash-flow framework Stage 1 / 2A groundwork).

A real ledger is built through the Revolut import path and ``refresh_ledger``;
the assertions cover the printed-running-balance basis, staleness disclosure,
the withheld total when a pocket has no current balance, and the dormant
manual-observation supersession once a row is projected into schema 19.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.position import position_page
from fincl.position.queries import _derived_at
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.statement_pdfs import (
    DbsPocket,
    Row,
    SavingsRow,
    TrustRow,
    dbs_pdf_bytes,
    mari_savings_pdf_bytes,
    trust_pdf_bytes,
)


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "360.00"],
]


class PositionReadTests(unittest.TestCase):
    def test_a_derived_balance_counts_movements_after_its_boundary_through_the_target_day(self) -> None:
        boundary = {
            "as_of": "2026-01-10", "sort_at": "2026-01-10T12:00:00", "balance": "100.00",
            "coefficient": 10000, "scale": 2, "basis": "statement-running-balance",
        }
        movements = [
            {"position_at": position_at, "effective_precision": "DAY",
             "amount_coefficient": cents, "amount_scale": 2, "fee_coefficient": None}
            for position_at, cents in (
                ("2026-01-10T12:00:00", 100),
                ("2026-01-10T12:00:01", 200),
                ("2026-01-20", 400),
                ("2026-01-20T23:59:59", 800),
                ("2026-01-21T00:00:00", 1600),
            )
        ]

        derived = _derived_at("2026-01-20", [boundary], movements, [("2026-01-10", "2026-01-20")])

        self.assertEqual(derived["balance"], "114.00")
        self.assertEqual(derived["movement_count"], 3)

    def test_different_boundary_dates_bridge_to_a_common_coverage_cutoff(self) -> None:
        """Raw balance dates need not coincide when both pockets are complete onward."""
        rows = [
            ["Topup", "Current", "2026-03-01 10:00:00", "2026-03-01 10:00:01",
             "SGD seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
            ["Topup", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
             "USD seed", "50.00", "0.00", "USD", "COMPLETED", "50.00"],
        ]
        database = _ledger(rows)
        # The Revolut PDF statement itself certifies its period now, so no
        # patching is needed: both pockets are proven through the statement's
        # printed close on the 5th even though their last rows differ by date.
        page = position_page(database, today=date(2026, 3, 5))

        self.assertTrue(page["current_position_supported"])
        self.assertEqual(page["supported_cutoff"], "2026-03-05")
        self.assertEqual(page["liquid_total"], {"SGD": "900.00", "USD": "50.00"})
        self.assertEqual(page["included_pockets"][0]["derived_as_of"], "2026-03-05")

    def test_uncertified_quiet_interval_does_not_bridge_a_boundary_forward(self) -> None:
        database = _ledger(_ROWS)

        page = position_page(database, today=date(2026, 3, 10))

        self.assertFalse(page["current_position_supported"])
        self.assertIn(
            "complete actual-movement coverage is not established through this date",
            {gap["reason"] for gap in page["missing_intervals"]},
        )

    def _dbs_ledger(self, pockets: list[DbsPocket]) -> Path:
        root = Path(tempfile.mkdtemp())
        database = root / "ledger.sqlite3"
        source = root / "dbs.pdf"
        source.write_bytes(dbs_pdf_bytes(date(2026, 1, 31), pockets))
        DocumentImportService.plaintext_phase2(database, root / "evidence").import_dbs(source)
        refresh_ledger(database)
        return database

    def test_two_ledgers_in_one_pocket_close_at_their_sum(self) -> None:
        database = self._dbs_ledger([
            DbsPocket("SINGAPORE DOLLAR", Decimal("500.00"), (
                Row(date(2026, 1, 5), "Salary", Decimal("100.00")),
                Row(date(2026, 1, 20), "Rent", Decimal("-80.00")),
            )),
            DbsPocket("SINGAPORE DOLLAR", Decimal("1000.00"), (
                Row(date(2026, 1, 7), "Bonus", Decimal("10.00")),
            )),
        ])

        page = position_page(database, today=date(2026, 1, 31))
        mid_month = position_page(database, today=date(2026, 1, 10))

        self.assertEqual(page["liquid_total"], {"SGD": "1530.00"})
        self.assertEqual(page["cash_bridge"]["pockets"][0]["opening"], "1500.00")
        self.assertTrue(page["cash_bridge"]["pockets"][0]["reconciled"])
        self.assertEqual(
            mid_month["partial_position"]["contributing_pockets"][0]["balance"], "1610.00"
        )

    def test_a_row_lost_from_the_ledger_fails_against_the_printed_opening(self) -> None:
        database = self._dbs_ledger([
            DbsPocket("SINGAPORE DOLLAR", Decimal("500.00"), (
                Row(date(2026, 1, 5), "Salary", Decimal("100.00")),
                Row(date(2026, 1, 20), "Rent", Decimal("-80.00")),
            )),
        ])
        connection = sqlite3.connect(database)
        with connection:
            connection.execute(
                "UPDATE ledger_entries SET analytics_eligible=0 WHERE amount_text='-80.00'"
            )
        connection.close()

        bridge = position_page(database, today=date(2026, 1, 31))["cash_bridge"]["pockets"][0]

        self.assertEqual(bridge["opening"], "500.00")
        self.assertFalse(bridge["reconciled"])
        self.assertEqual(
            bridge["unsupported_reason"],
            "intervening movements do not reconcile the balance boundaries",
        )

    def test_a_quiet_pocket_closes_on_its_printed_balance(self) -> None:
        root = Path(tempfile.mkdtemp())
        database = root / "ledger.sqlite3"
        source = root / "revolut.pdf"
        source.write_bytes(csv_bytes(_ROWS, quiet={"EUR": "100.00"}))
        DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
        refresh_ledger(database)

        page = position_page(database, today=date(2026, 3, 5))

        self.assertEqual(page["liquid_total"]["EUR"], "100.00")

    def test_latest_printed_running_balance_is_the_pocket_basis(self) -> None:
        database = _ledger(_ROWS)

        page = position_page(database, today=date(2026, 3, 10))

        self.assertEqual(len(page["pockets"]), 1)
        pocket = page["pockets"][0]
        self.assertEqual(pocket["currency_pocket"], "SGD")
        self.assertEqual(pocket["basis"], "statement-closing-balance")
        self.assertEqual(pocket["balance"], "360.00")
        self.assertEqual(pocket["as_of"], "2026-03-05")
        self.assertTrue(pocket["liquid"])
        self.assertEqual(page["liquid_total"], {"SGD": "360.00"})
        self.assertEqual(page["liquid_total_withheld"], {})

    def test_a_pocket_older_than_the_window_is_returned_but_flagged_stale(self) -> None:
        database = _ledger(_ROWS)

        fresh = position_page(database, today=date(2026, 3, 12))["pockets"][0]
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["staleness_days"], 7)

        stale = position_page(database, today=date(2026, 5, 1))["pockets"][0]
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["balance"], "360.00")
        self.assertEqual(page_currency_total(database, date(2026, 5, 1)), {"SGD": "360.00"})

    def test_a_newer_manual_observation_supersedes_the_printed_balance(self) -> None:
        database = _ledger(_ROWS)
        _project_observation(
            database,
            account_id=_only_account_id(database),
            currency_pocket="SGD",
            observed_on="2026-04-02",
            balance="355.10",
        )

        pocket = position_page(database, today=date(2026, 4, 5))["pockets"][0]

        self.assertEqual(pocket["basis"], "manual-observation")
        self.assertEqual(pocket["balance"], "355.10")
        self.assertEqual(pocket["as_of"], "2026-04-02")
        # The printed evidence is still carried alongside for the drill-down.
        self.assertEqual(pocket["statement_running_balance"]["balance"], "360.00")

    def test_a_retired_manual_observation_falls_back_to_the_printed_balance(self) -> None:
        database = _ledger(_ROWS)
        account_id = _only_account_id(database)
        _project_observation(
            database,
            account_id=account_id,
            currency_pocket="SGD",
            observed_on="2026-04-02",
            balance="355.10",
            retired=True,
        )

        pocket = position_page(database, today=date(2026, 4, 5))["pockets"][0]

        self.assertEqual(pocket["basis"], "statement-closing-balance")
        self.assertEqual(pocket["balance"], "360.00")

    def test_known_opening_reconciles_external_movements_to_closing(self) -> None:
        database = _ledger(_ROWS)

        bridge = position_page(database, today=date(2026, 3, 5))["cash_bridge"]["pockets"][0]

        # The statement period opens before the seed row, so the seed reads as
        # an external inflow and the derived opening is zero.
        self.assertEqual(bridge["opening"], "0.00")
        self.assertEqual(bridge["external_inflows"], "900.00")
        self.assertEqual(bridge["external_outflows"], "540.00")
        self.assertEqual(bridge["calculated_closing"], "360.00")
        self.assertEqual(bridge["closing"], "360.00")
        self.assertTrue(bridge["reconciled"])

    def test_a_chain_of_adjacent_statements_bridges_across_statement_boundaries(self) -> None:
        """Two separate statements with no gap between them still certify the
        full span, even though no single statement covers it end to end."""
        root = Path(tempfile.mkdtemp())
        database = root / "ledger.sqlite3"
        service = DocumentImportService.plaintext_phase2(database, root / "evidence")
        first = root / "first.csv"
        first.write_bytes(csv_bytes([
            ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
             "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
            ["Card Payment", "Current", "2026-01-31 10:00:00", "2026-01-31 10:00:01",
             "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
        ]))
        service.import_revolut_pdf(first)
        second = root / "second.csv"
        second.write_bytes(csv_bytes([
            ["Card Payment", "Current", "2026-02-01 10:00:00", "2026-02-01 10:00:01",
             "Rent", "-100.00", "0.00", "SGD", "COMPLETED", "760.00"],
            ["Card Payment", "Current", "2026-02-15 10:00:00", "2026-02-15 10:00:01",
             "Utilities", "-20.00", "0.00", "SGD", "COMPLETED", "740.00"],
        ]))
        service.import_revolut_pdf(second)
        refresh_ledger(database)

        bridge = position_page(database, today=date(2026, 2, 15))["cash_bridge"]["pockets"][0]

        self.assertEqual(bridge["opening_as_of"], "2025-12-31")
        self.assertEqual(bridge["closing_as_of"], "2026-02-15")
        self.assertEqual(bridge["calculated_closing"], "740.00")
        self.assertEqual(bridge["closing"], "740.00")
        self.assertTrue(bridge["reconciled"])
        self.assertIsNone(bridge["unsupported_reason"])

    def test_a_gap_between_statements_prevents_the_bridge_across_it(self) -> None:
        """When a day between two statements is not covered by any certified
        period, the span cannot be certified as complete even if the two
        statements individually reconcile."""
        root = Path(tempfile.mkdtemp())
        database = root / "ledger.sqlite3"
        service = DocumentImportService.plaintext_phase2(database, root / "evidence")
        first = root / "first.csv"
        first.write_bytes(csv_bytes([
            ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
             "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
            ["Card Payment", "Current", "2026-01-31 10:00:00", "2026-01-31 10:00:01",
             "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
        ]))
        service.import_revolut_pdf(first)
        second = root / "second.csv"
        second.write_bytes(csv_bytes([
            ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
             "Rent", "-100.00", "0.00", "SGD", "COMPLETED", "760.00"],
            ["Card Payment", "Current", "2026-02-15 10:00:00", "2026-02-15 10:00:01",
             "Utilities", "-20.00", "0.00", "SGD", "COMPLETED", "740.00"],
        ]))
        service.import_revolut_pdf(second)
        refresh_ledger(database)

        bridge = position_page(database, today=date(2026, 2, 15))["cash_bridge"]["pockets"][0]

        self.assertFalse(bridge["reconciled"])
        self.assertEqual(
            bridge["unsupported_reason"],
            "intervening movement completeness is not supported",
        )

    def test_internal_transfer_legs_remain_per_pocket_and_cancel_once_combined(self) -> None:
        rows = [
            ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
             "Seed", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
            ["Transfer", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01",
             "Move out", "-100.00", "0.00", "SGD", "COMPLETED", "900.00"],
            ["Transfer", "Current", "2026-01-03 10:00:00", "2026-01-03 10:00:01",
             "Move in", "100.00", "0.00", "SGD", "COMPLETED", "1000.00"],
        ]
        database = _ledger(rows)
        connection = sqlite3.connect(database)
        try:
            legs = connection.execute(
                "SELECT id FROM ledger_entries ORDER BY effective_at"
            ).fetchall()[1:]
            connection.execute(
                """INSERT INTO transfer_links(
                    id,left_entry_id,right_entry_id,link_kind,left_currency,
                    left_amount_text,left_amount_coefficient,left_amount_scale,
                    right_currency,right_amount_text,right_amount_coefficient,
                    right_amount_scale,method_version,status,evidence_json,created_at)
                    VALUES ('owned-pair',?,?,'SAME_CURRENCY_TRANSFER','SGD','100.00',10000,2,
                            'SGD','100.00',10000,2,'test-v1','CONFIRMED','{}','2026-01-03')""",
                (legs[0][0], legs[1][0]),
            )
            connection.commit()
        finally:
            connection.close()

        page = position_page(database, today=date(2026, 1, 3))
        bridge = page["cash_bridge"]["pockets"][0]

        self.assertEqual(bridge["owned_transfer_inflows"], "100.00")
        self.assertEqual(bridge["owned_transfer_outflows"], "100.00")
        self.assertEqual(bridge["external_inflows"], "1000.00")
        self.assertEqual(bridge["external_outflows"], "0")
        self.assertEqual(page["cash_bridge"]["combined"]["owned_transfer_pairs_cancelled"], 1)
        self.assertEqual(page["cash_bridge"]["combined"]["by_currency"]["SGD"], {
            "opening": "0.00", "external_inflows": "1000.00",
            "external_outflows": "0", "closing": "1000.00",
            "reconciled": True, "owned_transfer_pairs_cancelled": 1,
        })
        self.assertEqual(page["combined_liquid_position"], {"SGD": "1000.00"})

    def test_stale_position_names_the_missing_movement_interval(self) -> None:
        database = _ledger(_ROWS)

        page = position_page(database, today=date(2026, 5, 1))

        self.assertFalse(page["current_position_supported"])
        self.assertTrue(page["stale"])
        self.assertEqual(page["supported_cutoff"], "2026-03-05")
        self.assertEqual(page["missing_intervals"], [{
            "account_id": _only_account_id(database), "currency_pocket": "SGD",
            "start": "2026-03-06", "end": "2026-05-01",
            "reason": "complete actual-movement coverage is not established through this date",
        }])

    def test_missing_movement_support_prevents_an_exact_bridge_claim(self) -> None:
        database = _ledger(_ROWS)
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "DELETE FROM document_reconciliation_checks WHERE check_type = 'STATEMENT_BALANCE'"
            )
            connection.commit()
        finally:
            connection.close()

        page = position_page(database, today=date(2026, 3, 5))
        bridge = page["cash_bridge"]["pockets"][0]

        self.assertFalse(bridge["reconciled"])
        self.assertEqual(
            bridge["unsupported_reason"],
            "intervening movement completeness is not supported",
        )
        self.assertFalse(page["current_position_supported"])

    def test_card_balance_is_excluded_from_liquid_cash(self) -> None:
        database = _ledger(_ROWS)
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE accounts SET source_type='MARI_CARD'")
            connection.commit()
        finally:
            connection.close()

        page = position_page(database, today=date(2026, 3, 5))

        self.assertFalse(page["pockets"][0]["liquid"])
        self.assertIn("obligation", page["pockets"][0]["excluded_reason"])
        self.assertEqual(page["included_pockets"], [])
        self.assertEqual(page["combined_liquid_position"], {})
        self.assertIsNone(page["pockets"][0]["derived_balance"])
        self.assertEqual(page["pockets"][0]["coverage_status"], "unsupported")

    def test_unsupported_current_position_keeps_last_known_amount(self) -> None:
        database = _ledger(_ROWS)

        page = position_page(database, today=date(2026, 3, 20))

        self.assertFalse(page["current_position_supported"])
        self.assertEqual(page["included_pockets"][0]["balance"], "360.00")
        self.assertEqual(page["included_pockets"][0]["as_of"], "2026-03-05")
        self.assertTrue(page["current_position_reasons"])

    def test_balance_observation_set_and_retire_replay_through_the_journal(self) -> None:
        database = _ledger(_ROWS)
        observation = "11111111-1111-4111-8111-111111111111"
        journal = default_journal_path(database)
        append_decision(
            journal, decision_kind="BALANCE_OBSERVATION_SET",
            anchors={"observation": observation},
            payload={"account_id": _only_account_id(database), "currency_pocket": "SGD",
                     "observed_on": "2026-04-02", "balance": "355.10", "reason": "verified"},
            policy_version="position-v1", snapshot="manual balance",
        )
        connection = sqlite3.connect(database)
        try:
            replay_journal(connection)
        finally:
            connection.close()
        self.assertEqual(
            position_page(database, today=date(2026, 4, 2))["pockets"][0]["balance"],
            "355.10",
        )
        append_decision(
            journal, decision_kind="BALANCE_OBSERVATION_RETIRE",
            anchors={"observation": observation}, payload={"reason": "superseded"},
            policy_version="position-v1", snapshot="retire manual balance",
        )
        connection = sqlite3.connect(database)
        try:
            replay_journal(connection)
        finally:
            connection.close()
        self.assertEqual(
            position_page(database, today=date(2026, 4, 2))["pockets"][0]["balance"],
            "360.00",
        )


class StatementBoundaryTests(unittest.TestCase):
    def _imported(self, name: str, data: bytes) -> Path:
        root = Path(tempfile.mkdtemp())
        source = root / "statement.pdf"
        source.write_bytes(data)
        getattr(DocumentImportService.plaintext_phase2(root / "ledger.sqlite3", root / "evidence"), name)(source)
        refresh_ledger(root / "ledger.sqlite3")
        return root / "ledger.sqlite3"

    def test_a_fee_in_the_first_statement_still_bridges(self) -> None:
        database = _ledger([
            ["Topup", "Current", "2026-03-01 10:00:00", "2026-03-01 10:00:01",
             "Seed", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
            ["Card Payment", "Current", "2026-03-02 10:00:00", "2026-03-02 10:00:01",
             "Shop", "-50.00", "2.00", "SGD", "COMPLETED", "448.00"],
        ])

        bridge = position_page(database, today=date(2026, 3, 2))["cash_bridge"]["pockets"][0]

        self.assertTrue(bridge["reconciled"], bridge)
        self.assertEqual(bridge["opening"], "0.00")

    def test_same_day_rows_end_on_the_statements_last_balance(self) -> None:
        rows = [["Topup", "Current", "2026-03-01 10:00:00", "2026-03-01 10:00:01",
                 "Seed", "100.00", "0.00", "SGD", "COMPLETED", "100.00"]]
        for shop in range(1, 13):
            rows.append(["Card Payment", "Current", f"2026-03-05 10:{shop:02d}:00",
                         f"2026-03-05 10:{shop:02d}:01", f"Shop {shop}", "-1.00", "0.00",
                         "SGD", "COMPLETED", f"{100 - shop}.00"])
        rows.append(["Card Payment", "Current", "2026-03-20 10:00:00", "2026-03-20 10:00:01",
                     "Tail", "-5.00", "0.00", "SGD", "COMPLETED", "83.00"])

        page = position_page(_ledger(rows), today=date(2026, 3, 10))

        self.assertEqual(page["pockets"][0]["balance"], "88.00")

    def test_a_pocket_closes_at_the_sum_of_its_trust_sub_accounts(self) -> None:
        database = self._imported("import_trust", trust_pdf_bytes(
            date(2026, 8, 1), date(2026, 8, 31), date(2026, 9, 1), Decimal("10.00"),
            [TrustRow(date(2026, 8, 3), "SYNTHETIC HOLDER", Decimal("40.00"))],
            Decimal("200.00"), [TrustRow(date(2026, 8, 5), "SYNTHETIC HOLDER", Decimal("100.00"))],
        ))

        page = position_page(database, today=date(2026, 8, 31))

        self.assertEqual(page["pockets"][0]["balance"], "350.00")
        self.assertEqual(page["cash_bridge"]["pockets"][0]["opening"], "210.00")

    def test_a_row_dated_before_its_statement_stays_in_the_bridge(self) -> None:
        database = self._imported("import_dbs", dbs_pdf_bytes(date(2026, 1, 31), [DbsPocket(
            "SINGAPORE DOLLAR", Decimal("100.00"),
            (Row(date(2025, 12, 31), "SYNTHETIC SHOP", Decimal("-3.60")),
             Row(date(2026, 1, 10), "SYNTHETIC SHOP", Decimal("-5.00"))),
        )]))
        connection = sqlite3.connect(database)
        dated = connection.execute("SELECT min(substr(effective_at, 1, 10)) FROM ledger_entries").fetchone()[0]
        connection.close()
        self.assertEqual(dated, "2025-12-31")

        bridge = position_page(database, today=date(2026, 1, 31))["cash_bridge"]["pockets"][0]

        self.assertTrue(bridge["reconciled"], bridge)
        self.assertEqual(bridge["calculated_closing"], "91.40")

    def test_no_balance_is_derived_inside_a_month_with_an_undated_row(self) -> None:
        root = Path(tempfile.mkdtemp())
        service = DocumentImportService.plaintext_phase2(root / "ledger.sqlite3", root / "evidence")
        statements = (
            (date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1), Decimal("0.00"),
             [SavingsRow(date(2026, 1, 5), "SYNTHETIC HOLDER", Decimal("10.00"), ("PayNow Transfer",))],
             Decimal("0.00")),
            (date(2026, 2, 1), date(2026, 2, 28), date(2026, 3, 1), Decimal("10.00"),
             [SavingsRow(date(2026, 2, 5), "BOOK NOOK", Decimal("-4.00"), ("Card Payment",))],
             Decimal("0.01")),
        )
        for index, statement in enumerate(statements):
            source = root / f"savings-{index}.pdf"
            source.write_bytes(mari_savings_pdf_bytes(*statement))
            service.import_maribank(source)
        refresh_ledger(root / "ledger.sqlite3")

        pocket = position_page(root / "ledger.sqlite3", today=date(2026, 2, 20))["pockets"][0]

        self.assertEqual(pocket["best_supported_cutoff"], "2026-01-31")
        self.assertIn("precision", pocket["blocking_reason"])

        closed = position_page(root / "ledger.sqlite3", today=date(2026, 2, 28))["pockets"][0]
        self.assertEqual((closed["balance"], closed["basis"]), ("6.01", "statement-closing-balance"))


def page_currency_total(database: Path, today: date) -> dict[str, str]:
    return position_page(database, today=today)["liquid_total"]


def _only_account_id(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("SELECT id FROM accounts").fetchall()
    finally:
        connection.close()
    assert len(rows) == 1, rows
    return rows[0][0]


def _project_observation(
    database: Path,
    *,
    account_id: str,
    currency_pocket: str,
    observed_on: str,
    balance: str,
    retired: bool = False,
) -> None:
    """Insert straight into the schema-19 projection for isolated read tests."""
    whole, _, fraction = balance.partition(".")
    coefficient = int(whole + fraction)
    scale = len(fraction)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            INSERT INTO balance_observations
                (observation, account_id, currency_pocket, observed_on,
                 balance_coefficient, balance_scale, journal_seq, retired)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"obs-{observed_on}",
                account_id,
                currency_pocket,
                observed_on,
                coefficient,
                scale,
                1,
                1 if retired else 0,
            ),
        )
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
