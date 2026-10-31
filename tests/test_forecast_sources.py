"""Forecast source adapters: one seam every reader of the cash timeline shares."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fincl.db import connect_sqlite
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import entry_anchor
from fincl.document_service import DocumentImportService
from fincl.forecast import sources
from fincl.forecast.engine import (
    COLUMN_IRREGULAR,
    DIRECTION_OUT,
    ForecastEvent,
    cash_timeline,
)
from fincl.forecast.sources import SourceContext, SourceResult
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes
from tests.test_obligations import _rows
from tests.test_planned_flows import _flow, _occurrence

TODAY = date(2026, 7, 1)


def _decide(database: Path, kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    append_decision(
        default_journal_path(database), decision_kind=kind, anchors=anchors,
        payload=payload, policy_version="test", snapshot=f"{kind} {anchors}",
    )


def rent_ledger(*, bind_rent: bool = True, schedule: dict[str, Any] | None = None) -> Path:
    """Six months of rent 1200, groceries 300 and salary 5000, cut off 2026-06-25.

    Rent is a confirmed monthly commitment; ``bind_rent`` links its six past
    payments to their occurrences.
    """
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(_rows()))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    connection = connect_sqlite(database)
    try:
        anchors = {
            merchant: [
                entry_anchor(connection, row["id"])
                for row in connection.execute(
                    "SELECT e.id FROM ledger_entries e "
                    "JOIN document_records d ON d.id = e.document_record_id "
                    "WHERE d.description_raw = ? ORDER BY e.effective_at",
                    (merchant,),
                )
            ]
            for merchant in ("Landlord", "Corner Grocer", "Acme Payroll")
        }
    finally:
        connection.close()
    for anchor in anchors["Acme Payroll"]:
        _decide(database, "INBOUND_CLASSIFICATION", {"entry": anchor},
                {"classification": "income", "reason": "payslip"})
    for merchant, category in (
        ("Landlord", "Housing / Rent"),
        ("Corner Grocer", "Food & Dining / Groceries"),
    ):
        for anchor in anchors[merchant]:
            _decide(database, "CATEGORY_OVERRIDE", {"entry": anchor},
                    {"category": category, "reason": "receipts"})
    _decide(database, "RECURRING_COMMITMENT_SET", {"commitment": "rent"}, {
        "name": "Rent", "currency": "SGD", "merchant_key": "Landlord",
        "account_ids": [], "seed_entry_anchors": anchors["Landlord"][:3],
        "schedule": schedule or {"kind": "monthly", "day": 5, "anchor_on": "2026-01-05"},
        "expected_amount": "1200.00", "amount_tolerance": "0.00",
        "status": "active", "ended_on": None, "reason": "lease",
    })
    if bind_rent:
        for index, anchor in enumerate(anchors["Landlord"]):
            _decide(database, "RECURRING_OCCURRENCE_SET",
                    {"commitment": "rent", "scheduled_on": f"2026-0{index + 1}-05"},
                    {"entry": anchor, "reason": "statement"})
    refresh_ledger(database)
    return database


def decide(database: Path, kind: str, anchors: dict[str, str], payload: dict[str, Any]) -> None:
    _decide(database, kind, anchors, payload)
    refresh_ledger(database)


def base_events(timeline: dict[str, Any], prefix: str) -> list[tuple[str, str]]:
    return [
        (event["on"], event["amount"])
        for event in timeline["by_currency"]["SGD"]["base"]["events"]["items"]
        if event["source_id"].startswith(prefix)
    ]


class SourceSeamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = rent_ledger()

    def _adapter(self, *, generation: str | None) -> sources.SourceAdapter:
        def adapter(connection: sqlite3.Connection, context: SourceContext) -> SourceResult:
            self.seen = context
            return SourceResult(
                events=[ForecastEvent(
                    event_id="test:boiler", source_id="test:boiler", currency="SGD",
                    amount="900.00", column=COLUMN_IRREGULAR, direction=DIRECTION_OUT,
                    label="New boiler", on="2026-09-10",
                )],
                action_items=[{"kind": "needs_review", "output": "test", "currency": "SGD",
                               "rank": 50, "action": "Check the boiler", "detail": "boiler"}],
                generation=generation,
                manifest_source={"input": "test_source", "module": "tests"},
                limitations=["the boiler quote is provisional"],
            )
        return adapter

    def test_an_adapter_reaches_the_timeline_its_manifest_and_its_action_items(self) -> None:
        token = cash_timeline(self.database, today=TODAY)["generation"]
        with patch.object(sources, "EXTRA_SOURCES", [*sources.EXTRA_SOURCES, self._adapter(generation=token)]):
            timeline = cash_timeline(self.database, today=TODAY)

        self.assertEqual(base_events(timeline, "test:"), [("2026-09-10", "900.00")])
        self.assertIn("test_source", [source["input"] for source in timeline["provenance"]])
        self.assertIn("boiler", [item["detail"] for item in timeline["action_items"]])
        self.assertIn("the boiler quote is provisional", timeline["limitations"])
        self.assertEqual(self.seen.cutoff, "2026-06-25")
        self.assertEqual(self.seen.start, "2026-06-26")
        self.assertIn("recurring:rent:2026-07-05", self.seen.existing_source_ids)
        self.assertIn("manifest:irregular:SGD", self.seen.residual_category_ids)

    def test_an_adapter_from_another_generation_refuses_the_timeline(self) -> None:
        with patch.object(sources, "EXTRA_SOURCES", [self._adapter(generation="stale")]):
            timeline = cash_timeline(self.database, today=TODAY)

        self.assertFalse(timeline["available"])
        self.assertEqual(timeline["reported_generations"]["test_source"], "stale")


class FundSourceTests(unittest.TestCase):
    def _fund(self, database: Path, *, linked: str | None, due_on: str = "2026-08-05") -> None:
        decide(database, "SINKING_FUND_SET", {"fund": "lease"}, {
            "name": "Lease renewal", "currency": "SGD", "amount_due": "1200.00",
            "due_on": due_on, "amount_basis": "evidenced_future_invoice",
            "date_basis": "evidenced_invoice_date", "source_reference": "lease:2026",
            "contribution_dates": [], "linked_event_id": linked, "reason": "renewal notice",
        })

    def test_a_fund_reaches_the_forecast_every_tab_reads(self) -> None:
        database = rent_ledger()
        self._fund(database, linked=None)

        timeline = cash_timeline(database, today=TODAY)

        self.assertEqual(base_events(timeline, "fund:"), [("2026-08-05", "1200.00")])

    def test_a_fund_linked_to_a_recurring_obligation_adds_no_second_payment(self) -> None:
        database = rent_ledger()
        self._fund(database, linked="recurring:rent")

        timeline = cash_timeline(database, today=TODAY)

        self.assertEqual(
            [
                (event["source_id"], event["amount"])
                for event in timeline["by_currency"]["SGD"]["base"]["events"]["items"]
                if event["on"] == "2026-08-05" and not event["source_id"].startswith("residual:")
            ],
            [("recurring:rent:2026-08-05", "1200.00")],
        )
        self.assertEqual(base_events(timeline, "fund:"), [])

    def test_an_unreleased_fund_past_the_cutoff_is_named_not_dropped(self) -> None:
        database = rent_ledger()
        self._fund(database, linked=None, due_on="2026-06-01")

        timeline = cash_timeline(database, today=TODAY)

        self.assertEqual(base_events(timeline, "fund:"), [])
        self.assertTrue(any("Lease renewal was due on 2026-06-01" in item for item in timeline["limitations"]))


class AnswerFreshnessTests(unittest.TestCase):
    def test_the_answer_reads_each_accounts_freshness_and_asks_only_for_stale_ones(self) -> None:
        from fincl.planning.sufficiency import sufficiency_page

        database = rent_ledger()
        fresh = sufficiency_page(database, today=TODAY)["combined_answer"]
        stale = sufficiency_page(database, today=date(2026, 8, 1))["combined_answer"]

        [account] = fresh["pockets"][0]["accounts"]
        self.assertEqual((account["evidence_through"], account["age_days"], account["stale"]),
                         ("2026-06-25", 6, False))
        self.assertNotIn("recent-statements", [todo["key"] for todo in fresh["todos"]])
        recent = next(todo for todo in stale["todos"] if todo["key"] == "recent-statements")
        self.assertEqual((recent["page"], recent["anchor"]), ("Setup", "setup-coverage"))
        self.assertIn("proven through 2026-06-25 (37 days ago)", recent["detail"])


class PlannedInTheAnswerTests(unittest.TestCase):
    def test_planned_money_reaches_the_breakdown_the_upside_and_the_todos(self) -> None:
        from fincl.planned.forecast import planned_source
        from fincl.planning.sufficiency import sufficiency_page

        database = rent_ledger()
        for name, direction, certainty, occurrence in (
            ("Laptop", "outflow", "sure", _occurrence("a", "2026-08-10", "600.00")),
            ("Parking fine", "outflow", "sure", _occurrence("a", "2026-06-10", "50.00")),
            ("Tax refund", "inflow", "maybe", _occurrence("a", "2026-09-01", "200.00")),
        ):
            _decide(database, "PLANNED_FLOW_SET", {"flow": str(uuid.uuid4())}, _flow(
                name=name, direction=direction, certainty=certainty, occurrences=[occurrence],
            ))
        refresh_ledger(database)

        answer = sufficiency_page(database, today=TODAY)["combined_answer"]

        planned = next(row for row in answer["where_it_goes"] if row["origin"] == "planned")
        self.assertEqual(planned["label"], "Planned spending")
        self.assertEqual(planned["total"], "650.00")
        self.assertEqual([(row["label"], row["amount"]) for row in answer["upside"]], [("Tax refund", "200.00")])
        overdue = next(todo for todo in answer["todos"] if todo["key"] == "planned-overdue")
        self.assertEqual(overdue["page"], "Planned")
        self.assertIn("Parking fine was due by 2026-06-10", overdue["detail"])
        self.assertNotIn("Tax refund", [row["label"] for row in answer["income"]])


if __name__ == "__main__":
    unittest.main()
