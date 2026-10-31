"""Remaining card-installment rows are forecast as card charges on projected cycles."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from fincl.cards.cash_conversion import card_cash_conversion_from
from fincl.db import connect_sqlite
from fincl.forecast.engine import _card_events, _installment_card_purchases, cash_timeline
from fincl.obligations.queries import obligations_page_from
from tests.test_card_cash_conversion import AS_OF, _build_ledger
from tests.test_forecast_sources import rent_ledger

TODAY = date(2026, 7, 6)


def _add_plan(database: Path, *, status: str = "CONFIRMED", card: str | None = "card-1") -> None:
    connection = connect_sqlite(database)
    try:
        connection.execute(
            "INSERT INTO installment_plans(plan_id, card_account_id, currency, merchant_label, term_months, "
            "principal_coefficient, principal_scale, opened_on, status, journal_seq, bound, reason) "
            "VALUES ('phone', ?, 'SGD', 'Phone', 4, 120000, 2, '2026-06-01', ?, 1, 1, 'plan')",
            (card, status),
        )
        for index, due_on in enumerate(("2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01"), start=1):
            connection.execute(
                "INSERT INTO installment_schedule(plan_id, schedule_index, due_on, principal_coefficient, "
                "principal_scale) VALUES ('phone', ?, ?, 30000, 2)",
                (index, due_on),
            )
        connection.commit()
    finally:
        connection.close()


class InstallmentCardChargeTests(unittest.TestCase):
    def _purchases(self, **plan: str | None) -> tuple[list[dict], Path]:
        database = _build_ledger(Path(tempfile.mkdtemp()))
        _add_plan(database, **plan)
        connection = connect_sqlite(database)
        try:
            obligations = obligations_page_from(connection, as_of=AS_OF, today=TODAY)
            return _installment_card_purchases(connection, obligations, cutoff=AS_OF, end="2027-01-05"), database
        finally:
            connection.close()

    def test_rows_after_the_cutoff_are_card_charges_that_replace_no_routine_spending(self) -> None:
        purchases, _ = self._purchases()

        self.assertEqual(
            [(p["id"], p["on"], p["amount"]) for p in purchases],
            [("installment:phone:2", "2026-08-01", "300.00"), ("installment:phone:3", "2026-09-01", "300.00"),
             ("installment:phone:4", "2026-10-01", "300.00")],
        )
        self.assertEqual({(p["account_id"], p["origin"], p["removes_from_residual"]) for p in purchases},
                         {("card-1", "installment_plan", False)})

    def test_they_settle_on_the_cards_projected_cycles_once_each(self) -> None:
        purchases, database = self._purchases()
        connection = connect_sqlite(database)
        try:
            cards = card_cash_conversion_from(connection, as_of=AS_OF, prospective_purchases=purchases)
        finally:
            connection.close()

        events, limits, missing = _card_events(cards, prospective=purchases)

        self.assertEqual((limits, missing), ([], []))
        installments = [e for e in events if e.provenance["origin"] == "installment_plan"]
        self.assertEqual(
            [(e.source_id, e.on, e.amount) for e in installments],
            [("installment:phone:2", "2026-09-12", "300.00"), ("installment:phone:3", "2026-10-13", "300.00"),
             ("installment:phone:4", "2026-11-12", "300.00")],
        )
        self.assertEqual({(e.label, e.removes_from_residual) for e in installments}, {("Phone (installment)", False)})

    def test_an_unconfirmed_plan_or_one_without_a_card_is_not_charged(self) -> None:
        self.assertEqual(self._purchases(status="PROPOSED")[0], [])
        self.assertEqual(self._purchases(card=None)[0], [])

    def test_a_plan_on_a_card_the_forecast_does_not_track_is_named_not_dropped(self) -> None:
        database = rent_ledger()
        _add_plan(database, card="card-unknown")

        timeline = cash_timeline(database, today=date(2026, 7, 1))

        self.assertTrue(any("Phone (installment) (SGD): the card it is charged to isn't tracked" in gap
                            for gap in timeline["answer_gaps"]))
        self.assertEqual(timeline["readiness"], "partial")


if __name__ == "__main__":
    unittest.main()
