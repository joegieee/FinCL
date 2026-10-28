"""Controls compose one snapshot and can omit paths without changing the answer."""
from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import patch

from fincl.controls import spending_controls_page
from fincl.controls.queries import baseline_roles
from tests.test_api import ApiTestBase

TODAY = date(2026, 2, 1)


class ControlsReadTests(ApiTestBase):
    def test_a_fund_due_event_reaches_the_one_forecast_controls_build(self) -> None:
        from fincl.controls.queries import cash_timeline
        from fincl.decision_journal import append_decision, default_journal_path
        from fincl.refresh import refresh_ledger

        append_decision(
            default_journal_path(self.database), decision_kind="SINKING_FUND_SET",
            anchors={"fund": "insurance"}, policy_version="test", snapshot="test",
            payload={"name": "Annual insurance", "currency": "SGD", "amount_due": "300.00",
                     "due_on": "2026-04-01", "amount_basis": "evidenced_future_invoice",
                     "date_basis": "evidenced_invoice_date", "source_reference": "invoice:2026",
                     "contribution_dates": ["2026-03-01"], "linked_event_id": None,
                     "reason": "Sourced renewal invoice"},
        )
        refresh_ledger(self.database)
        with (patch("fincl.db.sqlite3.connect", wraps=sqlite3.connect) as connections,
              patch("fincl.controls.queries.cash_timeline", wraps=cash_timeline) as timelines):
            spending_controls_page(self.database, today=TODAY)
        self.assertEqual(connections.call_count, 1)
        self.assertEqual(timelines.call_count, 1)
        timeline = cash_timeline(self.database, today=TODAY)
        sourced = [
            event for event in timeline["by_currency"]["SGD"]["base"]["events"]["items"]
            if event["source_id"] == "fund:insurance:due"
        ]
        self.assertEqual([(event["on"], event["amount"]) for event in sourced], [("2026-04-01", "300.00")])

    def test_controls_open_one_connection_and_build_one_forecast_without_funds(self) -> None:
        from fincl.controls.queries import cash_timeline

        with (patch("fincl.db.sqlite3.connect", wraps=sqlite3.connect) as connections,
              patch("fincl.controls.queries.cash_timeline", wraps=cash_timeline) as timelines):
            page = spending_controls_page(self.database, today=TODAY)
        self.assertTrue(page["available"])
        self.assertEqual(connections.call_count, 1)
        self.assertEqual(timelines.call_count, 1)

    def test_concurrent_generation_change_cannot_mix_controls_panels(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        expected = spending_controls_page(self.database, today=TODAY)

        def refresh_after_first_panel(*args, **kwargs):
            result = baseline_roles(*args, **kwargs)
            # A separate writer publishes a different generation between two
            # composed readers. The ongoing read must retain its old snapshot.
            writer = sqlite3.connect(self.database)
            try:
                writer.execute("UPDATE projection_generation SET generation_token = 'concurrent-generation'")
                writer.commit()
            finally:
                writer.close()
            return result

        with patch("fincl.controls.queries.baseline_roles", side_effect=refresh_after_first_panel):
            actual = spending_controls_page(self.database, today=TODAY)
        self.assertEqual(actual, expected)

    def test_native_and_reported_summary_preserve_answers_and_scenario_comparison(self) -> None:
        csrf = self.bootstrap()
        body = {"month": "2026-02", "currency": "SGD", "category_cuts": [], "date_changes": [],
                "hypothetical_income": [{"amount": "100.00", "currency": "SGD",
                                         "on": "2026-03-01", "label": "Proposed income"}]}
        journal_before = self.journal_len()
        with patch("fincl.clock.today", return_value=TODAY):
            for prefix in ("/api/v1/", "/api/v1/reporting/workspaces/"):
                for scenario in (False, True):
                    with self.subTest(prefix=prefix, scenario=scenario):
                        path = prefix + "spending-controls" + ("/what-if" if scenario else "")
                        query = "?month=2026-02&currency=SGD"
                        full = self.post(path + query, body, csrf) if scenario else self.client.get(path + query)
                        summary = self.post(path + query + "&include_forecast=false", body, csrf) if scenario else self.client.get(path + query + "&include_forecast=false")
                        self.assertEqual(full.status_code, 200, full.text)
                        self.assertEqual(summary.status_code, 200, summary.text)
                        full_page, summary_page = full.json(), summary.json()
                        if "native" in full_page:
                            retained = [item for item in full_page["amounts"]
                                        if item["path"][:2] != ["scenario", "cases"]
                                        and item["path"][:1] != ["forecast_cases"]]
                            self.assertEqual(summary_page["amounts"], retained)
                            full_page, summary_page = full_page["native"], summary_page["native"]
                        self.assertTrue(full_page["available"])
                        full_page.pop("forecast_cases")
                        if scenario:
                            full_page["scenario"].pop("cases")
                            self.assertTrue(summary_page["scenario"]["hypothetical"])
                        self.assertEqual(summary_page, full_page)
                        self.assertLess(len(summary.content), len(full.content) // 2)
        self.assertEqual(self.journal_len(), journal_before)
