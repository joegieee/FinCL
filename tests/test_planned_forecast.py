"""The planned-flow forecast adapter, checked against the engine's own event type."""

from __future__ import annotations

import unittest
import uuid
from typing import Any
from unittest import mock

from fincl.decisions import replay_journal
from fincl.forecast.engine import (
    COLUMN_INCOME,
    COLUMN_IRREGULAR,
    TIMING_INFERRED,
    TIMING_KNOWN,
    ForecastEvent,
    _card_events,
)
from fincl.forecast.sources import SourceContext
from fincl.planned.forecast import planned_source
from tests.test_planned_flows import _flow, _occurrence, _PlannedLedger


class PlannedSourceTests(_PlannedLedger):
    context = SourceContext(
        cutoff="2026-01-20", start="2026-01-21", end="2026-07-20", today="2026-02-10",
        residual_category_ids=frozenset(), existing_source_ids=frozenset(),
    )

    def _events(self, context: SourceContext | None = None) -> dict[str, ForecastEvent]:
        replay_journal(self.connection)
        result = planned_source(self.connection, context or self.context)
        self.assertIsNotNone(result.generation)
        return {event.source_id.rsplit(":", 1)[1]: event for event in result.events}

    def test_open_occurrences_become_forecast_events(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(
            certainty="maybe", baseline_treatment="irregular_allowance",
            occurrences=[_occurrence("day", "2026-02-15", "80.00"), _occurrence("window", "2026-03-01", "300.00", "2026-03-31"),
                         _occurrence("late", "2026-09-01", "10.00")],
        ))
        events = self._events()
        self.assertEqual(set(events), {"day", "window"})
        day, window = events["day"], events["window"]
        self.assertEqual(day.source_id, f"planned:{self.flow}:day")
        self.assertEqual((day.on, day.timing_basis, day.amount, day.column), ("2026-02-15", TIMING_KNOWN, "80.00", COLUMN_IRREGULAR))
        self.assertEqual(day.confidence, "possible")
        self.assertEqual((window.on, window.timing_basis, window.window_earliest, window.window_latest),
                         (None, TIMING_INFERRED, "2026-03-01", "2026-03-31"))
        self.assertTrue(day.removes_from_residual)
        self.assertEqual((day.residual_category, day.residual_amount), ("manifest:irregular:SGD", "80.00"))
        self.assertEqual(day.provenance["origin"], "planned")

    def test_income_is_never_removed_from_spending(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(
            name="Bonus", direction="inflow", certainty="expected",
            occurrences=[_occurrence("mar", "2026-03-01", "2000.00", "2026-03-31")],
        ))
        event = self._events()["mar"]
        self.assertEqual((event.column, event.direction, event.confidence), (COLUMN_INCOME, "inflow", "likely"))
        self.assertFalse(event.removes_from_residual)
        self.assertEqual(event.provenance["origin"], "planned_income")

    def test_overdue_items_keep_their_dates_and_ask_what_happened(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(occurrences=[_occurrence("a", "2026-01-10", "40.00")]))
        income = str(uuid.uuid4())
        self._append("PLANNED_FLOW_SET", {"flow": income}, _flow(
            name="Refund", direction="inflow", occurrences=[_occurrence("a", "2026-01-15", "25.00")],
        ))
        replay_journal(self.connection)
        result = planned_source(self.connection, self.context)
        by_flow = {event.provenance["flow"]: event for event in result.events}
        self.assertEqual((by_flow[self.flow].on, by_flow[self.flow].confidence), ("2026-01-10", "confirmed"))
        self.assertTrue(by_flow[self.flow].provenance["overdue"])
        self.assertEqual(by_flow[income].confidence, "possible")
        self.assertEqual(sorted(item["flow"] for item in result.action_items), sorted([self.flow, income]))
        self.assertEqual({item["todo"]["page"] for item in result.action_items}, {"Planned"})

    def test_matched_items_and_existing_events_are_not_emitted_again(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(occurrences=[
            _occurrence("a", "2026-01-02", "120.00"), _occurrence("b", "2026-02-02", "10.00"),
        ]))
        self._append(*self._match())
        context = SourceContext(**{**self.context.__dict__, "existing_source_ids": frozenset({f"planned:{self.flow}:b"})})
        self.assertEqual(self._events(context), {})


    def test_a_payment_matched_after_the_cutoff_is_its_own_event(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(occurrences=[_occurrence("a", "2026-01-15", "51.25")]))
        self._append(*self._match(amount="51.25", entry=self._anchor("-5125")))
        context = SourceContext(**{**self.context.__dict__, "cutoff": "2026-01-10", "start": "2026-01-11"})
        events = self._events(context)
        self.assertEqual(list(events), ["2026-01-20"])
        paid = events["2026-01-20"]
        self.assertEqual((paid.source_id, paid.on, paid.amount, paid.confidence),
                         (f"planned:{self.flow}:a:paid:2026-01-20", "2026-01-20", "51.25", "confirmed"))

    def test_uncounted_income_and_maybe_spending_stay_possible(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(certainty="maybe", occurrences=[_occurrence("old", "2026-01-12", "40.00")]))
        income = str(uuid.uuid4())
        self._append("PLANNED_FLOW_SET", {"flow": income}, _flow(
            name="Prize", direction="inflow", certainty="maybe", occurrences=[_occurrence("mar", "2026-03-05", "500.00")],
        ))
        replay_journal(self.connection)
        by_flow = {event.provenance["flow"]: event for event in planned_source(self.connection, self.context).events}
        self.assertEqual((by_flow[self.flow].confidence, by_flow[self.flow].provenance["overdue"]), ("possible", True))
        self.assertEqual((by_flow[income].on, by_flow[income].confidence), ("2026-03-05", "possible"))

    def _trip(self, spend: list[dict[str, str]]) -> tuple[str, Any]:
        trip = str(uuid.uuid4())
        self._append("TRIP_SET", {"trip": trip}, {"name": "Spring trip", "start_on": "2026-03-01", "end_on": "2026-03-07",
                                                  "countries": [], "reason": "booked"})
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(
            name="Spring trip", link={"kind": "trip", "id": trip}, occurrences=[_occurrence("t", "2026-03-01", "500.00", "2026-03-07")],
        ))
        return trip, lambda connection, trip_id, currency, as_of: {"parts": spend}

    def test_a_trip_budget_replaces_the_trip_allowance_when_there_is_one(self) -> None:
        _, spend = self._trip([{"on": "2026-01-25", "amount": "120.00"}])
        replay_journal(self.connection)
        with_term = SourceContext(**{**self.context.__dict__, "residual_category_ids": frozenset({"manifest:trip:SGD"})})
        events = {e.source_id.split(":")[-1]: e for e in planned_source(self.connection, with_term, trip_spend=spend).events}
        self.assertEqual((events["t"].amount, events["t"].residual_category), ("380.00", "manifest:trip:SGD"))
        self.assertEqual((events["2026-01-25"].amount, events["2026-01-25"].residual_category), ("120.00", "manifest:trip:SGD"))
        plain = planned_source(self.connection, self.context, trip_spend=spend).events
        self.assertTrue(all(not event.removes_from_residual for event in plain))
        self.assertIn("no trip allowance", plain[0].provenance["notes"][0])

    def test_card_paid_spending_settles_through_the_card(self) -> None:
        account = str(self.connection.execute("SELECT id FROM accounts").fetchone()[0])
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(
            account_id=account, baseline_treatment="irregular_allowance", occurrences=[_occurrence("a", "2026-03-02", "900.00", "2026-03-20")],
        ))
        replay_journal(self.connection)
        with mock.patch("fincl.planned.forecast._card_accounts", return_value=frozenset({account})):
            with mock.patch("fincl.planned.forecast.card_plans", return_value=frozenset({(account, "SGD")})):
                result = planned_source(self.connection, self.context)
            self.assertEqual(result.events, [])
            [purchase] = result.prospective_card_purchases
            self.assertEqual({key: purchase[key] for key in ("id", "account_id", "on", "amount", "residual_category")},
                             {"id": f"planned:{self.flow}:a", "account_id": account, "on": "2026-03-02", "amount": "900.00",
                              "residual_category": "manifest:irregular:SGD"})
            with mock.patch("fincl.planned.forecast.card_plans", return_value=frozenset()):
                fallback = planned_source(self.connection, self.context)
        self.assertEqual(fallback.prospective_card_purchases, [])
        self.assertIn("no settlement plan", fallback.events[0].provenance["notes"][0])

        # The card turns the modelled purchase into a settlement that keeps its treatment and origin.
        conversion = {"contract_version": "v", "cards": [{
            "account_id": account, "label": "Card", "currency": "SGD",
            "plan": {"available": True, "settlements": [{
                "source_id": purchase["id"], "partition": "prospective_purchases", "settle_on": "2026-04-15",
                "amount": "900.00", "currency": "SGD", "basis": "projected_cycle_due_date", "purchased_on": "2026-03-02",
            }]},
            "reconciliation": {"reconciled": True},
        }]}
        [settlement], _, _ = _card_events(conversion, prospective=[purchase])
        self.assertEqual((settlement.on, settlement.label, settlement.residual_category, settlement.provenance["origin"]),
                         ("2026-04-15", "Laptop", "manifest:irregular:SGD", "planned"))
        [extra], _, _ = _card_events(conversion, prospective=[{**purchase, "residual_category": None}])
        self.assertFalse(extra.removes_from_residual)


if __name__ == "__main__":
    unittest.main()
