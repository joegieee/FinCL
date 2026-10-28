"""The planned-flow forecast adapter, checked against the engine's own event type."""

from __future__ import annotations

import unittest
import uuid

from fincl.decisions import replay_journal
from fincl.forecast.engine import (
    COLUMN_INCOME,
    COLUMN_IRREGULAR,
    TIMING_INFERRED,
    TIMING_KNOWN,
    ForecastEvent,
)
from fincl.planned.forecast import planned_source
from fincl.planned.sources import SourceContext
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


if __name__ == "__main__":
    unittest.main()
