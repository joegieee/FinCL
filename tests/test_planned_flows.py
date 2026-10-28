"""Planned flows: the PLANNED_* kinds, their state checks, the read model and
the forecast adapter."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal, resolve_anchor
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.planned import check_planned_sequence, validate_planned
from fincl.planned.lifecycle import Occurrence, Slice, assess, envelope
from fincl.planned.queries import planned_page_from
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
    ["Card Payment", "Current", "2026-01-02 10:00:00", "2026-01-02 10:00:01", "Laptop shop", "-120.00", "0.00", "SGD", "COMPLETED", "380.00"],
    ["Card Payment", "Current", "2026-01-20 10:00:00", "2026-01-20 10:00:01", "Grocer", "-51.25", "0.00", "SGD", "COMPLETED", "328.75"],
]


def _occurrence(key: str, earliest: str, amount: str, latest: str | None = None) -> dict[str, str]:
    return {"key": key, "earliest": earliest, "latest": latest or earliest, "amount": amount}


def _flow(**changes: Any) -> dict[str, Any]:
    return {
        "name": "Laptop", "direction": "outflow", "currency": "SGD", "account_id": None, "category": None,
        "occurrences": [_occurrence("a", "2026-01-02", "120.00"), _occurrence("b", "2026-03-01", "300.00", "2026-03-31")],
        "certainty": "sure", "baseline_treatment": "extra", "link": None, "reason": "planned",
        **changes,
    }


class _PlannedLedger(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "revolut.csv"
        source.write_bytes(revolut_pdf_bytes(_ROWS))
        self.database = root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, root / "evidence").import_revolut_pdf(source)
        refresh_ledger(self.database)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.flow = str(uuid.uuid4())
        self.laptop = self._anchor("-12000")
        self.topup = self._anchor("50000")

    def _anchor(self, coefficient: str) -> str:
        return str(self.connection.execute(
            "SELECT natural_anchor FROM ledger_entries WHERE amount_coefficient = ?", (int(coefficient),)
        ).fetchone()[0])

    def _append(self, kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
        append_decision(
            default_journal_path(self.database), decision_kind=kind, anchors=anchors,
            payload=payload, policy_version="planned-flow-v1", snapshot=f"{kind} {anchors}",
        )

    def _check(self, *items: tuple[str, dict[str, str], dict[str, Any]]) -> None:
        check_planned_sequence(self.connection, list(items), lambda a: resolve_anchor(self.connection, a))

    def _match(self, key: str = "a", amount: str = "120.00", entry: str | None = None) -> tuple[str, dict[str, str], dict[str, Any]]:
        return ("PLANNED_MATCH_SET", {"flow": self.flow, "occurrence": key, "entry": entry or self.laptop},
                {"amount": amount, "reason": "paid"})


class PlannedLedgerTests(_PlannedLedger):

    def test_a_flow_its_matches_and_outcomes_project_and_replay(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow())
        self._append(*self._match(amount="100.00"))
        self._append("PLANNED_OCCURRENCE_RESOLVE_SET", {"flow": self.flow, "occurrence": "a"}, {"outcome": "done", "reason": "rest waived"})
        replay_journal(self.connection)
        replay_journal(self.connection)

        flow = self.connection.execute("SELECT name, direction, currency, certainty FROM planned_flows").fetchone()
        self.assertEqual(tuple(flow), ("Laptop", "outflow", "SGD", "sure"))
        occurrences = self.connection.execute(
            "SELECT occurrence, earliest, latest, amount_coefficient FROM planned_occurrences ORDER BY position"
        ).fetchall()
        self.assertEqual([tuple(row) for row in occurrences],
                         [("a", "2026-01-02", "2026-01-02", 12000), ("b", "2026-03-01", "2026-03-31", 30000)])
        match = self.connection.execute("SELECT entry_anchor, amount_coefficient FROM planned_matches").fetchone()
        self.assertEqual(tuple(match), (self.laptop, 10000))
        self.assertEqual(self.connection.execute("SELECT outcome FROM planned_resolutions").fetchone()[0], "done")

    def test_currency_and_direction_cannot_change(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow())
        replay_journal(self.connection)
        with self.assertRaisesRegex(FinCLError, "cannot change currency"):
            self._check(("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(currency="USD")))
        with self.assertRaisesRegex(FinCLError, "between spending and income"):
            self._check(("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(direction="inflow")))
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(currency="USD"))
        with self.assertRaisesRegex(FinCLError, "cannot change currency"):
            replay_journal(self.connection)

    def test_a_match_must_fit_the_transaction(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow())
        replay_journal(self.connection)
        self._check(self._match())
        with self.assertRaisesRegex(FinCLError, "Only money going out"):
            self._check(self._match(amount="10.00", entry=self.topup))
        with self.assertRaisesRegex(FinCLError, "left to match"):
            self._check(self._match(amount="120.01"))
        # One payment split across two occurrences may not exceed the payment.
        with self.assertRaisesRegex(FinCLError, "left to match"):
            self._check(self._match(amount="100.00"), self._match(key="b", amount="20.01"))
        self._check(self._match(amount="100.00"), self._match(key="b", amount="20.00"))
        with self.assertRaisesRegex(FinCLError, "no longer resolves"):
            self._check(self._match(entry="nothing:here"))

    def test_a_trip_budget_refuses_manual_matches(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(link={"kind": "trip", "id": str(uuid.uuid4())}))
        replay_journal(self.connection)
        with self.assertRaisesRegex(FinCLError, "trip's own transactions"):
            self._check(self._match())

    def test_a_link_must_name_an_existing_trip_when_written(self) -> None:
        with self.assertRaisesRegex(FinCLError, "trip does not exist"):
            self._check(("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(link={"kind": "trip", "id": str(uuid.uuid4())})))

    def test_matches_hold_their_occurrence_and_flow(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow())
        self._append(*self._match())
        replay_journal(self.connection)
        retire = ("PLANNED_FLOW_RETIRE", {"flow": self.flow}, {"reason": "not buying"})
        with self.assertRaisesRegex(FinCLError, "retire them in the same batch"):
            self._check(retire)
        with self.assertRaisesRegex(FinCLError, "Occurrence\\(s\\) a have matches"):
            self._check(("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(occurrences=[_occurrence("b", "2026-03-01", "300.00")])))
        with self.assertRaisesRegex(FinCLError, "mark it done instead"):
            self._check(("PLANNED_OCCURRENCE_RESOLVE_SET", {"flow": self.flow, "occurrence": "a"}, {"outcome": "cancelled", "reason": "x"}))
        self._check(("PLANNED_MATCH_RETIRE", {"flow": self.flow, "occurrence": "a", "entry": self.laptop}, {"reason": "wrong"}), retire)

    def test_an_occurrence_with_an_outcome_takes_no_new_match(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow())
        self._append("PLANNED_OCCURRENCE_RESOLVE_SET", {"flow": self.flow, "occurrence": "a"}, {"outcome": "cancelled", "reason": "x"})
        replay_journal(self.connection)
        with self.assertRaisesRegex(FinCLError, "reopen it first"):
            self._check(self._match())

    def test_the_read_model_judges_occurrences_against_the_cash_cutoff(self) -> None:
        self._append("PLANNED_FLOW_SET", {"flow": self.flow}, _flow(occurrences=[
            _occurrence("a", "2026-01-02", "120.00"), _occurrence("b", "2026-01-10", "40.00"),
            _occurrence("c", "2026-03-01", "300.00", "2026-03-31"),
        ]))
        self._append(*self._match(amount="120.00"))
        replay_journal(self.connection)
        page = planned_page_from(self.connection, today=date(2026, 2, 10))
        self.assertEqual(page["cutoff"], "2026-01-20")
        cells = {cell["key"]: cell for cell in page["flows"][0]["occurrences"]}
        self.assertEqual({key: cell["state"] for key, cell in cells.items()},
                         {"a": "matched", "b": "overdue", "c": "upcoming"})
        self.assertFalse(cells["a"]["in_forecast"])
        self.assertEqual(cells["b"]["forecast"]["placements"],
                         [{"earliest": "2026-01-21", "latest": "2026-01-21", "amount": "40.00",
                           "cases": ["base", "cautious"], "overdue": True}])
        self.assertEqual(cells["a"]["matches"][0]["on"], "2026-01-02")
        self.assertEqual([item["occurrence"] for item in page["attention"]], ["b"])
        self.assertEqual(page["flows"][0]["remaining"], "340.00")
        self.assertEqual(page["months"][0]["totals"], {"SGD": {"outflows": "40.00", "inflows": "0.00"}})


class LifecycleTests(unittest.TestCase):
    def _assess(self, direction: str, certainty: str, occurrence: Occurrence, slices: list[Slice] | None = None):
        return assess(occurrence, slices or [], direction=direction, certainty=certainty,
                      reference="2026-01-31", horizon_end="2026-07-31")

    def test_certainty_decides_which_cases_count_it(self) -> None:
        later = Occurrence("a", "2026-03-15", "2026-03-15", Decimal("100"))
        cases = {
            (direction, certainty): self._assess(direction, certainty, later).placements
            for direction in ("outflow", "inflow") for certainty in ("sure", "expected", "maybe")
        }
        self.assertEqual({key: value[0].cases if value else () for key, value in cases.items()}, {
            ("outflow", "sure"): ("base", "cautious"), ("outflow", "expected"): ("base", "cautious"),
            ("outflow", "maybe"): ("cautious",), ("inflow", "sure"): ("base", "cautious"),
            ("inflow", "expected"): ("base",), ("inflow", "maybe"): (),
        })

    def test_overdue_spending_stays_and_overdue_income_leaves(self) -> None:
        due = Occurrence("a", "2026-01-10", "2026-01-31", Decimal("100"))
        spending = self._assess("outflow", "maybe", due, [Slice("2026-01-12", Decimal("30"))])
        self.assertEqual((spending.state, spending.remaining), ("overdue", Decimal("70")))
        self.assertEqual([(p.earliest, p.amount, p.cases, p.overdue) for p in spending.placements],
                         [("2026-02-01", Decimal("70"), ("base", "cautious"), True)])
        income = self._assess("inflow", "sure", due)
        self.assertEqual((income.state, income.placements), ("overdue", ()))

    def test_a_window_is_clipped_to_the_horizon_and_beyond_it_is_left_out(self) -> None:
        straddling = Occurrence("a", "2026-01-20", "2026-02-10", Decimal("50"))
        placement = self._assess("outflow", "sure", straddling).placements[0]
        self.assertEqual((placement.earliest, placement.latest), ("2026-02-01", "2026-02-10"))
        beyond = Occurrence("b", "2026-08-01", "2026-08-01", Decimal("50"))
        self.assertEqual(self._assess("outflow", "sure", beyond).placements, ())

    def test_matched_and_resolved_place_nothing(self) -> None:
        item = Occurrence("a", "2026-03-15", "2026-03-15", Decimal("100"))
        self.assertEqual(self._assess("outflow", "sure", item, [Slice("2026-03-14", Decimal("100"))]).state, "matched")
        done = Occurrence("a", "2026-03-15", "2026-03-15", Decimal("100"), "done")
        self.assertEqual(self._assess("outflow", "sure", done).placements, ())

    def test_trip_spend_fills_occurrences_in_date_order(self) -> None:
        occurrences = [
            Occurrence("feb", "2026-02-01", "2026-02-28", Decimal("100")),
            Occurrence("mar", "2026-03-01", "2026-03-31", Decimal("100"), "cancelled"),
            Occurrence("apr", "2026-04-01", "2026-04-30", Decimal("100")),
        ]
        slices, over = envelope(occurrences, [Slice("2026-02-03", Decimal("150")), Slice("2026-02-01", Decimal("80"))])
        self.assertEqual(slices["feb"], [Slice("2026-02-01", Decimal("80")), Slice("2026-02-03", Decimal("20"))])
        self.assertEqual(slices["mar"], [])
        self.assertEqual(slices["apr"], [Slice("2026-02-03", Decimal("100"))])
        self.assertEqual(over, Decimal("30"))


class PlannedValidationTests(unittest.TestCase):
    def test_payload_shape_is_closed(self) -> None:
        flow = {"flow": str(uuid.uuid4())}
        validate_planned("PLANNED_FLOW_SET", flow, _flow())
        for change, message in [
            ({"direction": "sideways"}, "outflow or inflow"),
            ({"direction": "inflow", "baseline_treatment": "irregular_allowance"}, "always extra"),
            ({"occurrences": []}, "1-60 occurrences"),
            ({"occurrences": [_occurrence("a", "2026-03-02", "1.00", "2026-03-01")]}, "ends before it starts"),
            ({"occurrences": [_occurrence("a", "2026-03-02", "1.00")] * 2}, "used twice"),
            ({"occurrences": [_occurrence("a", "2026-03-02", "0.00")]}, "greater than zero"),
            ({"link": {"kind": "debt", "id": "x"}}, "can link only to: trip"),
        ]:
            with self.subTest(change=change), self.assertRaisesRegex(FinCLError, message):
                validate_planned("PLANNED_FLOW_SET", flow, _flow(**change))
        with self.assertRaisesRegex(FinCLError, "lowercase UUID"):
            validate_planned("PLANNED_FLOW_RETIRE", {"flow": "Laptop"}, {"reason": "x"})


if __name__ == "__main__":
    unittest.main()
