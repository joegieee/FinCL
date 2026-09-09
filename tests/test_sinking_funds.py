"""Stage 4A — minimal sinking funds.

The arithmetic tests exercise :mod:`fincl.funds.models` directly rather than
through a ledger, so a regression names the invariant it broke instead of the
fixture that happened to expose it. The read and journal tests then build a
real ledger through the Revolut import path.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.forecast.engine import (
    COLUMN_IRREGULAR,
    DIRECTION_OUT,
    TimelineEvent,
    run_timeline,
)
from fincl.funds import (
    CashSeparation,
    contribution_plan,
    fund_forecast_events,
    sinking_funds_page,
    validate_fund_payload,
)
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "1500.00", "0.00", "SGD", "COMPLETED", "1500.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "1460.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Insurance", "-460.00", "0.00", "SGD", "COMPLETED", "1000.00"],
]


def _ledger(rows: list[list[str]] | None = None) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows if rows is not None else _ROWS))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _project_fund(
    database: Path,
    *,
    fund: str,
    name: str = "Annual insurance",
    currency: str = "SGD",
    amount_due: str | None = "200.00",
    due_on: str | None = "2026-06-01",
    amount_basis: str = "evidenced_future_invoice",
    date_basis: str = "evidenced_invoice_date",
    source_reference: str = "invoice:2026",
    contribution_dates: list[str] | None = None,
    linked_event_id: str | None = None,
    allocations: list[tuple[str, str, str]] = (),
    release: tuple[str, str] | None = None,
) -> None:
    """Insert straight into the schema-21 projections for isolated read tests."""

    def parts(value: str) -> tuple[int, int]:
        whole, _, fraction = value.partition(".")
        return int(whole + fraction), len(fraction)

    connection = sqlite3.connect(database)
    try:
        due_parts = parts(amount_due) if amount_due is not None else (None, None)
        connection.execute(
            "INSERT INTO sinking_funds(fund, name, currency, amount_due_coefficient, "
            "amount_due_scale, due_on, amount_basis, date_basis, source_reference, "
            "contribution_dates_json, linked_event_id, journal_seq) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (fund, name, currency, due_parts[0], due_parts[1], due_on, amount_basis,
             date_basis, source_reference, json.dumps(contribution_dates or []),
             linked_event_id),
        )
        for allocation, allocated_on, amount in allocations:
            coefficient, scale = parts(amount)
            connection.execute(
                "INSERT INTO sinking_fund_allocations(fund, allocation, allocated_on, "
                "amount_coefficient, amount_scale, journal_seq, retired) "
                "VALUES (?,?,?,?,?,1,0)",
                (fund, allocation, allocated_on, coefficient, scale),
            )
        if release is not None:
            coefficient, scale = parts(release[1])
            connection.execute(
                "INSERT INTO sinking_fund_releases(fund, released_on, "
                "amount_coefficient, amount_scale, journal_seq) VALUES (?,?,?,?,1)",
                (fund, release[0], coefficient, scale),
            )
        connection.commit()
    finally:
        connection.close()


class CentralInvariantTests(unittest.TestCase):
    """Total cash, earmarks and unallocated cash stay three separate quantities."""

    def test_an_earmark_reduces_unallocated_cash_but_not_total_cash(self) -> None:
        opening = CashSeparation(
            currency="SGD", total_cash_minor=100_000, earmarked_minor=0
        )

        earmarked = opening.with_contribution(20_000)

        self.assertEqual(earmarked.total_cash_minor, 100_000)
        self.assertEqual(earmarked.earmarked_minor, 20_000)
        self.assertEqual(earmarked.unallocated_minor, 80_000)
        cell = earmarked.as_dict(2)
        self.assertEqual(cell["total_cash"], "1000.00")
        self.assertEqual(cell["earmarked_funds"], "200.00")
        self.assertEqual(cell["unallocated_cash"], "800.00")

    def test_paying_the_linked_expense_reduces_cash_and_releases_the_earmark(self) -> None:
        earmarked = CashSeparation(
            currency="SGD", total_cash_minor=100_000, earmarked_minor=20_000
        )

        settled = earmarked.with_settlement(amount_minor=20_000, release_minor=20_000)

        self.assertEqual(settled.total_cash_minor, 80_000)
        self.assertEqual(settled.earmarked_minor, 0)
        self.assertEqual(settled.unallocated_minor, 80_000)
        cell = settled.as_dict(2)
        self.assertEqual(cell["total_cash"], "800.00")
        self.assertEqual(cell["earmarked_funds"], "0.00")

    def test_the_earmark_is_released_exactly_once(self) -> None:
        settled = CashSeparation(
            currency="SGD", total_cash_minor=100_000, earmarked_minor=20_000
        ).with_settlement(amount_minor=20_000, release_minor=20_000)

        with self.assertRaises(ValueError):
            settled.with_settlement(amount_minor=0, release_minor=1)

    def test_reserve_adjusted_unallocated_cash_deducts_the_minimum_reserve(self) -> None:
        state = CashSeparation(
            currency="SGD",
            total_cash_minor=100_000,
            earmarked_minor=20_000,
            reserve_minor=30_000,
        )

        self.assertEqual(state.unallocated_minor, 80_000)
        self.assertEqual(state.reserve_adjusted_unallocated_minor, 50_000)
        cell = state.as_dict(2)
        self.assertEqual(cell["minimum_reserve"], "300.00")
        self.assertEqual(cell["reserve_adjusted_unallocated_cash"], "500.00")
        self.assertIsNone(cell["reserve_shortfall"])

    def test_an_earmark_beyond_liquid_cash_warns_rather_than_being_trimmed(self) -> None:
        state = CashSeparation(
            currency="SGD", total_cash_minor=10_000, earmarked_minor=25_000
        )

        cell = state.as_dict(2)
        self.assertTrue(cell["unfunded"])
        self.assertEqual(cell["earmarked_funds"], "250.00")
        self.assertEqual(cell["unallocated_cash"], "-150.00")
        self.assertIn("not backed by liquid cash", cell["unfunded_warning"])


class ContributionArithmeticTests(unittest.TestCase):
    def test_the_final_contribution_absorbs_the_rounding_remainder(self) -> None:
        plan = contribution_plan(
            amount_due_minor=100_00,
            allocated_now_minor=0,
            remaining_dates=["2026-04-01", "2026-05-01", "2026-06-01"],
        )

        amounts = [item["amount_minor"] for item in plan["schedule"]]
        self.assertEqual(amounts, [3333, 3333, 3334])
        self.assertEqual(sum(amounts), 10_000)
        self.assertTrue(plan["sums_exactly"])
        self.assertEqual(plan["required_contribution_minor"], 3333)
        self.assertEqual(plan["final_contribution_minor"], 3334)

    def test_the_recorded_allocation_reduces_what_remains_to_contribute(self) -> None:
        plan = contribution_plan(
            amount_due_minor=20_000,
            allocated_now_minor=5_000,
            remaining_dates=["2026-04-01", "2026-05-01", "2026-06-01"],
        )

        self.assertEqual(plan["shortfall_minor"], 15_000)
        self.assertEqual(
            [item["amount_minor"] for item in plan["schedule"]], [5_000, 5_000, 5_000]
        )

    def test_an_over_allocated_fund_requires_nothing_further(self) -> None:
        plan = contribution_plan(
            amount_due_minor=20_000,
            allocated_now_minor=25_000,
            remaining_dates=["2026-06-01"],
        )

        self.assertEqual(plan["shortfall_minor"], 0)
        self.assertEqual(plan["schedule"], [{"on": "2026-06-01", "amount_minor": 0}])

    def test_an_unknown_amount_yields_no_schedule_and_says_why(self) -> None:
        plan = contribution_plan(
            amount_due_minor=None,
            allocated_now_minor=0,
            remaining_dates=["2026-06-01"],
        )

        self.assertFalse(plan["available"])
        self.assertIsNone(plan["required_contribution_minor"])
        self.assertIn("no sourced amount", plan["reason"])


class SolvencyTests(unittest.TestCase):
    """Scheduling contributions cannot rescue an insolvent cash path."""

    def _walk(self, extra: list[TimelineEvent]) -> dict:
        return run_timeline(
            currency="SGD",
            scale=2,
            opening_minor=10_000,
            start="2026-03-31",
            end="2026-06-30",
            events=[
                TimelineEvent(
                    event_id="rent:2026-04-01",
                    source_id="rent:2026-04-01",
                    column=COLUMN_IRREGULAR,
                    direction=DIRECTION_OUT,
                    on="2026-04-01",
                    amount_minor=30_000,
                    label="Rent",
                ),
                *extra,
            ],
        )

    def test_a_contribution_schedule_emits_no_cash_event_and_changes_no_day(self) -> None:
        # Every contribution date falls inside the modelled horizon; the fund's
        # own due date sits beyond it. So the only thing this fund could put on
        # the timeline is its savings plan — and a savings plan is not cash.
        page = {
            "funds": [
                {
                    "fund": "insurance",
                    "name": "Annual insurance",
                    "currency": "SGD",
                    "status": "scheduled",
                    "linked_event_id": None,
                    "target": {
                        "amount_due": "200.00",
                        "due_on": "2026-12-01",
                        "amount_basis": "evidenced_future_invoice",
                        "date_basis": "evidenced_invoice_date",
                        "source_reference": "invoice:2026",
                    },
                    "planned_contributions": {
                        "remaining_dates": ["2026-04-15", "2026-05-15", "2026-06-15"],
                        "required_contribution": "66.66",
                        "final_contribution": "66.68",
                    },
                    "recorded_allocation": {"allocated_now": "66.66"},
                }
            ]
        }

        events, _ = fund_forecast_events(page, start="2026-03-31", end="2026-06-30")
        self.assertEqual(events, [])

        baseline = self._walk([])
        with_fund = self._walk(list(events))

        # The rent still empties the account on 1 April and nothing the fund
        # schedules puts a cent back on any day.
        self.assertEqual(
            [day["closing"] for day in with_fund["days"]],
            [day["closing"] for day in baseline["days"]],
        )
        self.assertEqual(with_fund["lowest_balance"]["amount"], "-200.00")
        self.assertTrue(
            [day for day in with_fund["days"] if day["closing"].startswith("-")]
        )


class ForecastLinkTests(unittest.TestCase):
    def test_a_fund_without_a_link_creates_its_own_dated_event(self) -> None:
        page = {
            "funds": [
                {
                    "fund": "insurance",
                    "name": "Annual insurance",
                    "currency": "SGD",
                    "status": "scheduled",
                    "linked_event_id": None,
                    "target": {
                        "amount_due": "200.00",
                        "due_on": "2026-06-01",
                        "amount_basis": "evidenced_future_invoice",
                        "date_basis": "evidenced_invoice_date",
                        "source_reference": "invoice:2026",
                    },
                }
            ]
        }

        events, notes = fund_forecast_events(page)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.source_id, "fund:insurance:due")
        self.assertEqual(event.on, "2026-06-01")
        self.assertEqual(event.column, COLUMN_IRREGULAR)
        self.assertTrue(event.removes_from_residual)
        self.assertEqual(event.residual_category, "manifest:irregular:SGD")
        self.assertEqual(event.residual_amount, "200.00")
        self.assertEqual(notes, [])

    def test_a_fund_linked_to_an_existing_event_creates_no_duplicate(self) -> None:
        page = {
            "funds": [
                {
                    "fund": "insurance",
                    "name": "Annual insurance",
                    "currency": "SGD",
                    "status": "scheduled",
                    "linked_event_id": "debt:abc",
                    "target": {
                        "amount_due": "200.00",
                        "due_on": "2026-06-01",
                        "amount_basis": "recorded_obligation",
                        "date_basis": "stated_due_date",
                        "source_reference": "debt:abc",
                    },
                }
            ]
        }

        events, notes = fund_forecast_events(page, existing_source_ids=["debt:abc"])

        self.assertEqual(events, [])
        self.assertIn("no duplicate event is created", notes[0])

    def test_a_fund_with_an_unknown_date_is_not_given_a_guessed_one(self) -> None:
        page = {
            "funds": [
                {
                    "fund": "roof",
                    "name": "Roof repair",
                    "currency": "SGD",
                    "status": "incomplete",
                    "linked_event_id": None,
                    "target": {
                        "amount_due": "800.00",
                        "due_on": None,
                        "amount_basis": "observed_irregular_expense",
                        "date_basis": "unknown",
                        "source_reference": "fund-proposal:irregular:SGD:home:2026-02",
                    },
                }
            ]
        }

        events, notes = fund_forecast_events(page)

        self.assertEqual(events, [])
        self.assertIn("irregular allowance is retained", notes[0])


class SinkingFundsPageTests(unittest.TestCase):
    def test_cash_earmarks_and_unallocated_cash_are_reported_apart(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="insurance",
            contribution_dates=["2026-04-01", "2026-05-01", "2026-06-01"],
            allocations=[("a1", "2026-03-01", "200.00")],
        )

        page = sinking_funds_page(database, as_of="2026-03-05")

        cell = page["cash_separation"]["by_currency"]["SGD"]
        self.assertTrue(cell["available"])
        self.assertEqual(cell["total_cash"], "1000.00")
        self.assertEqual(cell["earmarked_funds"], "200.00")
        self.assertEqual(cell["unallocated_cash"], "800.00")
        self.assertFalse(cell["unfunded"])

    def test_target_planned_contributions_and_recorded_allocation_stay_distinct(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="insurance",
            amount_due="300.00",
            contribution_dates=["2026-04-01", "2026-05-01", "2026-06-01"],
            allocations=[("a1", "2026-03-01", "60.00")],
        )

        fund = sinking_funds_page(database, as_of="2026-03-05")["funds"][0]

        self.assertEqual(fund["target"]["amount_due"], "300.00")
        self.assertEqual(fund["recorded_allocation"]["allocated_now"], "60.00")
        self.assertEqual(fund["earmark"]["outstanding"], "60.00")
        planned = fund["planned_contributions"]
        self.assertEqual(planned["remaining_dates"],
                         ["2026-04-01", "2026-05-01", "2026-06-01"])
        self.assertEqual(planned["required_contribution"], "80.00")
        self.assertEqual([item["amount"] for item in planned["schedule"]],
                         ["80.00", "80.00", "80.00"])
        self.assertTrue(planned["sums_exactly"])
        self.assertEqual(fund["shortfall"], "240.00")

    def test_time_passing_does_not_promote_a_plan_into_a_recorded_allocation(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="insurance",
            amount_due="300.00",
            contribution_dates=["2026-04-01", "2026-05-01", "2026-06-01"],
        )

        later = sinking_funds_page(database, as_of="2026-05-15")["funds"][0]

        # Two contribution dates have gone by with nothing journalled against
        # them, so nothing was saved and the whole target still has to be found.
        self.assertEqual(later["recorded_allocation"]["allocated_now"], "0.00")
        self.assertEqual(later["earmark"]["outstanding"], "0.00")
        self.assertEqual(later["planned_contributions"]["remaining_dates"], ["2026-06-01"])
        self.assertEqual(later["shortfall"], "300.00")

    def test_an_insufficient_allocation_stays_visible(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="insurance",
            amount_due="500.00",
            contribution_dates=["2026-04-01"],
            allocations=[("a1", "2026-03-01", "120.00")],
        )

        fund = sinking_funds_page(database, as_of="2026-03-05")["funds"][0]

        self.assertEqual(fund["shortfall"], "380.00")
        self.assertFalse(fund["fully_allocated"])
        self.assertTrue(fund["insufficient_allocation"]["visible"])
        self.assertEqual(fund["insufficient_allocation"]["amount"], "380.00")

    def test_an_overdue_expense_shows_an_immediate_requirement_not_a_refusal(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="insurance",
            amount_due="400.00",
            due_on="2026-02-20",
            contribution_dates=["2026-02-01"],
            allocations=[("a1", "2026-02-01", "100.00")],
        )

        fund = sinking_funds_page(database, as_of="2026-03-05")["funds"][0]

        self.assertEqual(fund["status"], "overdue")
        immediate = fund["immediate_funding_requirement"]
        self.assertEqual(immediate["amount"], "300.00")
        self.assertTrue(immediate["remains_an_obligation"])
        self.assertEqual(fund["planned_contributions"]["remaining_dates"], [])

    def test_a_released_fund_holds_no_earmark(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="insurance",
            allocations=[("a1", "2026-02-01", "200.00")],
            release=("2026-03-05", "200.00"),
        )

        page = sinking_funds_page(database, as_of="2026-03-05")

        fund = page["funds"][0]
        self.assertEqual(fund["status"], "released")
        self.assertEqual(fund["earmark"]["outstanding"], "0.00")
        self.assertEqual(fund["earmark"]["released"], "200.00")
        cell = page["cash_separation"]["by_currency"]["SGD"]
        self.assertEqual(cell["earmarked_funds"], "0.00")
        self.assertEqual(cell["unallocated_cash"], cell["total_cash"])

    def test_an_earmark_larger_than_cash_warns_on_the_fund_and_the_currency(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="insurance",
            amount_due="2000.00",
            allocations=[("a1", "2026-02-01", "1200.00")],
        )

        page = sinking_funds_page(database, as_of="2026-03-05")

        cell = page["cash_separation"]["by_currency"]["SGD"]
        self.assertTrue(cell["unfunded"])
        self.assertEqual(cell["unallocated_cash"], "-200.00")
        self.assertIsNotNone(page["funds"][0]["unfunded_warning"])

    def test_a_fund_with_an_unknown_amount_stays_incomplete(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="roof",
            name="Roof repair",
            amount_due=None,
            amount_basis="unknown",
            due_on=None,
            date_basis="unknown",
            source_reference="fund-proposal:irregular:SGD:home:2026-02",
        )

        fund = sinking_funds_page(database, as_of="2026-03-05")["funds"][0]

        self.assertEqual(fund["status"], "incomplete")
        self.assertFalse(fund["target"]["available"])
        self.assertIn("guessed", fund["target"]["reason"])
        self.assertIsNone(fund["shortfall"])

    def test_a_goal_backed_fund_discloses_its_manual_basis(self) -> None:
        database = _ledger()
        _project_fund(
            database,
            fund="laptop",
            name="Laptop",
            amount_due="900.00",
            amount_basis="manual_goal_target",
            due_on="2026-09-01",
            date_basis="manual_goal_target_date",
            source_reference="fund-proposal:goal:laptop",
        )

        fund = sinking_funds_page(database, as_of="2026-03-05")["funds"][0]

        self.assertTrue(fund["target"]["manual_basis"])
        self.assertIn("evidence that cash exists",
                      fund["target"]["manual_basis_disclosure"])
        # A manual target buys no earmark: only journalled contributions do.
        self.assertEqual(fund["recorded_allocation"]["allocated_now"], "0.00")


class ProposalTests(unittest.TestCase):
    def test_an_isolated_irregular_expense_is_proposed_without_proven_recurrence(self) -> None:
        database = _ledger()

        page = sinking_funds_page(database, as_of="2026-03-31")

        irregular = [
            item
            for item in page["proposals"]
            if item["source"]["kind"] == "identified_irregular_expense"
        ]
        self.assertTrue(irregular)
        proposal = irregular[0]
        self.assertFalse(proposal["recurrence"]["known"])
        self.assertIn("does not prove a yearly bill", proposal["recurrence"]["note"])
        self.assertEqual(proposal["observed_date"]["precision"], "month")
        self.assertFalse(proposal["complete"])
        self.assertTrue(proposal["irregular_allowance_retained"])
        self.assertTrue(proposal["incomplete_reasons"])

    def test_a_source_already_carried_by_a_confirmed_fund_is_not_proposed_again(self) -> None:
        database = _ledger()
        page = sinking_funds_page(database, as_of="2026-03-31")
        irregular = [
            item
            for item in page["proposals"]
            if item["source"]["kind"] == "identified_irregular_expense"
        ][0]

        _project_fund(
            database,
            fund="insurance",
            source_reference=irregular["proposal_id"],
        )
        again = sinking_funds_page(database, as_of="2026-03-31")

        self.assertNotIn(
            irregular["proposal_id"],
            [item["proposal_id"] for item in again["proposals"]],
        )


class FundJournalTests(unittest.TestCase):
    def _append(self, database: Path, kind: str, anchors: dict, payload: dict) -> None:
        append_decision(
            default_journal_path(database),
            decision_kind=kind,
            anchors=anchors,
            payload=payload,
            policy_version="stage-4a",
            snapshot="test",
        )

    def test_a_journalled_fund_and_contribution_replay_into_an_earmark(self) -> None:
        database = _ledger()
        self._append(database, "SINKING_FUND_SET", {"fund": "insurance"}, {
            "name": "Annual insurance",
            "currency": "SGD",
            "amount_due": "300.00",
            "due_on": "2026-06-01",
            "amount_basis": "evidenced_future_invoice",
            "date_basis": "evidenced_invoice_date",
            "source_reference": "invoice:2026",
            "contribution_dates": ["2026-04-01", "2026-05-01"],
            "linked_event_id": None,
            "reason": "the renewal notice states the amount and the date",
        })
        self._append(
            database,
            "SINKING_FUND_ALLOCATION_SET",
            {"fund": "insurance", "allocation": "a1"},
            {"allocated_on": "2026-03-01", "amount": "100.00",
             "reason": "moved into the fund"},
        )
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            replay_journal(connection)
        finally:
            connection.close()

        fund = sinking_funds_page(database, as_of="2026-03-05")["funds"][0]

        self.assertEqual(fund["fund"], "insurance")
        self.assertEqual(fund["recorded_allocation"]["allocated_now"], "100.00")
        self.assertEqual(fund["earmark"]["outstanding"], "100.00")
        self.assertEqual(fund["planned_contributions"]["required_contribution"], "100.00")

    def test_an_unsourced_amount_is_refused_at_the_authoring_boundary(self) -> None:
        with self.assertRaises(FinCLError):
            validate_fund_payload("SINKING_FUND_SET", {"fund": "insurance"}, {
                "name": "Annual insurance",
                "currency": "SGD",
                "amount_due": None,
                "due_on": None,
                # An absent amount may not borrow a basis it does not have.
                "amount_basis": "evidenced_future_invoice",
                "date_basis": "unknown",
                "source_reference": "invoice:2026",
                "contribution_dates": [],
                "linked_event_id": None,
                "reason": "no amount is stated anywhere",
            })

    def test_a_negative_contribution_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            validate_fund_payload(
                "SINKING_FUND_ALLOCATION_SET",
                {"fund": "insurance", "allocation": "a1"},
                {"allocated_on": "2026-03-01", "amount": "-10.00", "reason": "no"},
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
