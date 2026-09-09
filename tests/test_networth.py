"""Stage 5B — tracked net worth.

The pure tests pin the composition rules that keep the subtotal honest: assets
minus liabilities reconciles, a card installment already inside card liability
is not added again, a manual debt linked to a tracked card cannot double-count,
and a missing valuation yields a known subtotal rather than a complete total.
The ledger test exercises the real read path end to end.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from fincl.application import record_debt_decision
from fincl.document_service import DocumentImportService
from fincl.networth import assemble_components, net_worth_page, summarise
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

# --------------------------------------------------------------------------- #
# Pure composition tests — no ledger required.
# --------------------------------------------------------------------------- #

def _position(pockets: list[dict]) -> dict:
    return {"pockets": pockets}


def _pocket(**over: object) -> dict:
    base = {
        "account_id": "acc-1",
        "display_name": "Everyday",
        "currency_pocket": "SGD",
        "source_type": "REVOLUT_CSV",
        "liquid": True,
        "balance": "1000.00",
        "as_of": "2026-06-30",
        "basis": "statement-running-balance",
        "excluded_reason": None,
        "unavailable_reason": None,
    }
    base.update(over)
    return base


def _obligations(rows: list[dict]) -> dict:
    return {"generation": "gen-x", "obligations": rows}


def _debt_row(**over: object) -> dict:
    base = {
        "origin": "personal_debt",
        "source": {"kind": "personal_debt", "id": "debt-1"},
        "label": "Alex",
        "currency": "SGD",
        "direction": "outflow",
        "date_basis": {"kind": "stated_due_date", "value": "2026-08-01"},
        "remaining_amount": {"value": "500.00", "currency": "SGD"},
        "remaining_unknown_reason": None,
        "confidence": "confirmed",
        "payment_status": "unknown",
        "status": "open",
        "stays_visible": True,
        "basis": "due date has not passed",
    }
    base.update(over)
    return base


def _cards(cards: list[dict]) -> dict:
    return {"cards": cards}


def _card(*, partitions: list[dict], as_of: str = "2026-06-30", **over: object) -> dict:
    base = {
        "account_id": "card-1",
        "label": "Mari Card",
        "currency": "SGD",
        "partitions": {
            "as_of": as_of,
            "partitions": partitions,
            "existing_liability_partitions": ["unpaid_billed", "posted_unbilled"],
        },
    }
    base.update(over)
    return base


def _known(name: str, amount: str) -> dict:
    return {"partition": name, "known": True, "amount": amount, "unresolved": []}


def _unknown(name: str, detail: str) -> dict:
    return {
        "partition": name,
        "known": False,
        "amount": None,
        "unresolved": [{"detail": detail}],
    }


class ReconciliationTests(unittest.TestCase):
    def test_assets_minus_liabilities_reconciles_per_currency(self) -> None:
        built = assemble_components(
            position=_position([
                _pocket(account_id="a", balance="1200.00"),
                _pocket(account_id="b", balance="300.00"),
            ]),
            obligations=_obligations([_debt_row(remaining_amount={"value": "450.00", "currency": "SGD"})]),
            cards=_cards([_card(partitions=[_known("unpaid_billed", "150.00"),
                                            _known("posted_unbilled", "0.00")])]),
            debt_flags={"debt-1": {"manual": False, "card_linked": False}},
        )
        sgd = summarise(built["components"], as_of="2026-06-30")["SGD"]
        self.assertEqual(sgd["tracked_assets"], "1500.00")
        self.assertEqual(sgd["tracked_liabilities"], "600.00")
        self.assertEqual(sgd["tracked_net_worth"], "900.00")
        self.assertEqual(
            Decimal(sgd["tracked_assets"]) - Decimal(sgd["tracked_liabilities"]),
            Decimal(sgd["tracked_net_worth"]),
        )
        self.assertTrue(sgd["complete"])


class CardInstallmentNotAddedTwiceTests(unittest.TestCase):
    def test_installment_and_card_statement_rows_from_obligations_are_ignored(self) -> None:
        built = assemble_components(
            position=_position([_pocket(balance="0.00")]),
            obligations=_obligations([
                _debt_row(),
                {
                    "origin": "installment_plan",
                    "source": {"kind": "installment_plan", "id": "plan-1"},
                    "label": "Laptop", "currency": "SGD", "direction": "outflow",
                    "date_basis": {"kind": "scheduled_due_date", "value": "2026-07-10"},
                    "remaining_amount": {"value": "300.00", "currency": "SGD"},
                    "remaining_unknown_reason": None, "confidence": "confirmed",
                    "payment_status": "unknown", "status": "open", "stays_visible": True,
                    "basis": "x",
                },
                {
                    "origin": "card_statement",
                    "source": {"kind": "card_statement", "id": "stmt-1"},
                    "label": "Mari Card", "currency": "SGD", "direction": "outflow",
                    "date_basis": {"kind": "statement_due_date", "value": "2026-07-13"},
                    "remaining_amount": {"value": "900.00", "currency": "SGD"},
                    "remaining_unknown_reason": None, "confidence": "confirmed",
                    "payment_status": "unknown", "status": "open", "stays_visible": True,
                    "basis": "x",
                },
            ]),
            cards=_cards([_card(partitions=[_known("unpaid_billed", "900.00"),
                                            _known("posted_unbilled", "0.00")])]),
            debt_flags={"debt-1": {"manual": False, "card_linked": False}},
        )
        kinds = [c["kind"] for c in built["components"]]
        self.assertNotIn("installment", "".join(kinds))
        # Only the card-conversion figure and the personal debt are liabilities.
        liabilities = [c for c in built["components"] if c["side"] == "liability"]
        self.assertEqual(
            sorted(c["origin"] for c in liabilities),
            ["card cash-conversion · posted_unbilled", "card cash-conversion · unpaid_billed",
             "obligations · personal_debt"],
        )
        sgd = summarise(built["components"], as_of="2026-06-30")["SGD"]
        # 900 card + 500 debt — the 300 installment and the 900 statement due are gone.
        self.assertEqual(sgd["tracked_liabilities"], "1400.00")


class LinkedManualDebtTests(unittest.TestCase):
    def test_a_manual_debt_linked_to_a_tracked_card_is_counted_once(self) -> None:
        built = assemble_components(
            position=_position([_pocket(balance="0.00")]),
            obligations=_obligations([_debt_row(label="DBS card, owed to Sam")]),
            cards=_cards([_card(partitions=[_known("unpaid_billed", "500.00"),
                                            _known("posted_unbilled", "0.00")])]),
            debt_flags={"debt-1": {"manual": True, "card_linked": True}},
        )
        debt = next(c for c in built["components"] if c["kind"] == "debt")
        self.assertFalse(debt["included"])
        self.assertIn("card", debt["note"])
        sgd = summarise(built["components"], as_of="2026-06-30")["SGD"]
        # The 500 appears once, from the card side; the manual debt line stays 0.
        self.assertEqual(sgd["tracked_liabilities"], "500.00")
        self.assertEqual(sgd["manual_liabilities"], "0.00")
        self.assertEqual(
            [c["component_id"] for c in sgd["excluded_components"]], ["debt:debt-1"]
        )

    def test_a_manual_opening_balance_debt_stays_on_the_manual_line(self) -> None:
        built = assemble_components(
            position=_position([_pocket(balance="360.00")]),
            obligations=_obligations([_debt_row(remaining_amount={"value": "400.00", "currency": "SGD"})]),
            cards=_cards([]),
            debt_flags={"debt-1": {"manual": True, "card_linked": False}},
        )
        sgd = summarise(built["components"], as_of="2026-06-30")["SGD"]
        self.assertEqual(sgd["tracked_liabilities"], "0.00")
        self.assertEqual(sgd["tracked_net_worth"], "360.00")
        self.assertEqual(sgd["manual_liabilities"], "400.00")
        self.assertEqual(sgd["net_worth_including_manual"], "-40.00")


class MissingValuationTests(unittest.TestCase):
    def test_an_unknown_card_partition_yields_a_known_but_incomplete_subtotal(self) -> None:
        built = assemble_components(
            position=_position([_pocket(balance="1000.00")]),
            obligations=_obligations([]),
            cards=_cards([_card(partitions=[
                _known("unpaid_billed", "200.00"),
                _unknown("posted_unbilled", "the unbilled window has no accepted evidence"),
            ])]),
            debt_flags={},
        )
        sgd = summarise(built["components"], as_of="2026-06-30")["SGD"]
        self.assertEqual(sgd["tracked_liabilities"], "200.00")
        self.assertEqual(sgd["tracked_net_worth"], "800.00")
        self.assertFalse(sgd["complete"])
        self.assertEqual(
            [m["component_id"] for m in sgd["missing_components"]],
            ["card:card-1:SGD:posted_unbilled"],
        )
        self.assertIn("no accepted evidence", sgd["missing_components"][0]["reason"])

    def test_an_unvalued_pocket_is_a_missing_position_not_a_zero(self) -> None:
        built = assemble_components(
            position=_position([
                _pocket(account_id="a", balance="500.00"),
                _pocket(account_id="b", balance=None, unavailable_reason="no supported balance boundary"),
            ]),
            obligations=_obligations([]),
            cards=_cards([]),
            debt_flags={},
        )
        sgd = summarise(built["components"], as_of="2026-06-30")["SGD"]
        self.assertEqual(sgd["tracked_assets"], "500.00")
        self.assertFalse(sgd["complete"])
        self.assertEqual(sgd["missing_components"][0]["component_id"], "cash:b:SGD")


# --------------------------------------------------------------------------- #
# Ledger test — the real read path.
# --------------------------------------------------------------------------- #

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "360.00"],
]


def _ledger() -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(_ROWS))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _account_id(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        return connection.execute("SELECT id FROM accounts").fetchone()[0]
    finally:
        connection.close()


class NetWorthLedgerTests(unittest.TestCase):
    def test_the_tracked_subtotal_is_never_labelled_total_wealth(self) -> None:
        page = net_worth_page(_ledger(), as_of="2026-03-06")
        self.assertEqual(page["label"], "tracked net worth")
        self.assertFalse(page["is_total_personal_wealth"])
        self.assertIn("not proof of total personal wealth", page["disclaimer"])
        sgd = page["per_currency"]["SGD"]
        self.assertEqual(sgd["tracked_assets"], "360.00")
        self.assertEqual(sgd["tracked_liabilities"], "0.00")
        self.assertEqual(sgd["tracked_net_worth"], "360.00")
        self.assertEqual(sgd["subtotal_note"][:16], "tracked subtotal")

    def test_a_manual_debt_is_reported_apart_from_the_evidence_only_total(self) -> None:
        database = _ledger()
        debt_id = str(uuid.uuid4())
        record_debt_decision(
            database, request_id=str(uuid.uuid4()), intent_hash="a" * 64,
            decision_kind="PERSONAL_DEBT_OPEN", anchors={"debt": debt_id},
            payload={"direction": "PAYABLE", "currency": "SGD",
                     "counterparty_label": "Alex", "opened_on": "2026-01-05",
                     "due_on": None, "reason": "opening balance owed before tracking"},
        )
        record_debt_decision(
            database, request_id=str(uuid.uuid4()), intent_hash="b" * 64,
            decision_kind="PERSONAL_DEBT_EVENT_SET",
            anchors={"debt": debt_id, "event": str(uuid.uuid4())},
            payload={"event_kind": "BORROWED", "direction": "PAYABLE",
                     "principal_amount": "400.00", "effective_on": "2026-01-05",
                     "entry_allocations": [], "opening_balance": True,
                     "expense_category": None, "reason": "pre-existing, funded before tracking"},
        )
        refresh_ledger(database)

        page = net_worth_page(database, as_of="2026-03-06")
        sgd = page["per_currency"]["SGD"]
        # Evidence-only tracked figures are untouched by the manual debt.
        self.assertEqual(sgd["tracked_liabilities"], "0.00")
        self.assertEqual(sgd["tracked_net_worth"], "360.00")
        # It shows on the manual line, keeping its basis.
        self.assertEqual(sgd["manual_liabilities"], "400.00")
        self.assertEqual(sgd["net_worth_including_manual"], "-40.00")
        debt = next(c for c in page["components"] if c["kind"] == "debt")
        self.assertEqual(debt["basis"], "manual")
        self.assertTrue(debt["included"])

    def test_two_dated_observations_produce_a_trend_that_labels_scope(self) -> None:
        database = _ledger()

        page = net_worth_page(database, as_of="2026-03-06", prior_as_of="2026-02-06")
        trend = page["trend"]
        self.assertEqual(trend["prior_as_of"], "2026-02-06")
        self.assertEqual(trend["current_as_of"], "2026-03-06")
        change = trend["change_by_currency"]["SGD"]
        self.assertEqual(change["prior_tracked_net_worth"], "860.00")
        self.assertEqual(change["current_tracked_net_worth"], "360.00")
        self.assertEqual(change["delta"], "-500.00")
        self.assertTrue(change["both_complete"])
        # Same pocket both times: the tracked scope is unchanged and it says so.
        self.assertFalse(trend["scope_changed"])
        self.assertIn("same tracked components", trend["comparability"])

    def test_a_manual_balance_observation_lands_on_the_manual_line(self) -> None:
        database = _ledger()
        _project_observation(database, _account_id(database), "2026-04-02", "500.00")

        sgd = net_worth_page(database, as_of="2026-04-05")["per_currency"]["SGD"]
        # A person-asserted balance is kept out of the evidence-only subtotal...
        self.assertEqual(sgd["tracked_assets"], "0.00")
        # ...but is shown, with its basis, and offered as a combined figure.
        self.assertEqual(sgd["manual_assets"], "500.00")
        self.assertEqual(sgd["net_worth_including_manual"], "500.00")


def _project_observation(database: Path, account_id: str, observed_on: str, balance: str) -> None:
    whole, _, fraction = balance.partition(".")
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO balance_observations (observation, account_id, currency_pocket, "
            "observed_on, balance_coefficient, balance_scale, journal_seq, retired) "
            "VALUES (?, ?, 'SGD', ?, ?, ?, 1, 0)",
            (f"obs-{observed_on}", account_id, observed_on,
             int(whole + fraction), len(fraction)),
        )
        connection.commit()
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
