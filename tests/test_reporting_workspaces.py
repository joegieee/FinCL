"""Release C read annotations and one-snapshot/native-write boundaries."""
from __future__ import annotations

import unittest

from fincl.db import connect_readonly, read_snapshot
from tests.test_api import ApiTestBase


class WorkspaceReportingTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.csrf = self.bootstrap()

    def test_native_reads_are_preserved_and_annotations_are_separate(self) -> None:
        for resource in ["debts", "cards", "installments", "obligations", "baseline/readiness",
                         "baseline/estimate", "funds", "budgets?month=2026-01", "goals",
                         "recurring", "planning/sufficiency", "spending-controls?month=2026-01"]:
            with self.subTest(resource=resource):
                native = self.client.get("/api/v1/" + resource)
                reported = self.client.get("/api/v1/reporting/workspaces/" + resource)
                self.assertEqual(reported.status_code, 200, reported.text)
                self.assertEqual(reported.json()["native"], native.json())
                self.assertEqual(reported.json()["reporting_currency"], "SGD")
                for annotation in reported.json()["amounts"]:
                    value = reported.json()["native"]
                    for key in annotation["path"]:
                        value = value[key]
                    self.assertEqual(annotation["value"]["native"]["value"], value)

    def test_borrowed_native_reader_cannot_close_the_outer_snapshot(self) -> None:
        with read_snapshot(self.database) as original:
            borrowed = connect_readonly(self.database)
            borrowed.execute("BEGIN")
            borrowed.close()
            self.assertTrue(original.in_transaction)
            self.assertEqual(original.execute("PRAGMA query_only").fetchone()[0], 1)
        standalone = connect_readonly(self.database)
        standalone.close()


if __name__ == "__main__":
    unittest.main()


class WorkspaceValuationTests(ApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        from fincl.db import connect_sqlite
        from fincl.fx import load_fx_rates
        from fincl.refresh import refresh_ledger
        connection = connect_sqlite(self.database)
        load_fx_rates(connection, {"source": "Synthetic", "source_version": "workspace-tests-v1", "quote_currency": "SGD",
                                   "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.20"},
                                             {"base_currency": "USD", "period": "2026-02", "rate": "1.40"},
                                             {"base_currency": "USD", "period": "2026-03", "rate": "9.99"}]})
        connection.commit()
        connection.close()
        refresh_ledger(self.database)

    def annotate(self, native: dict, resource: str) -> dict:
        from datetime import date

        from fincl.reporting_context import ReportedScope, build_reporting_context
        from fincl.reporting_workspaces import annotate_workspace
        with read_snapshot(self.database) as connection:
            context = build_reporting_context(connection, scope=ReportedScope(valuation_as_of=date(2026, 3, 15)))
            result = annotate_workspace(native, context=context, resource=resource)
        self.assertEqual(result.native, native)
        return {tuple(item.path): item.value.model_dump(mode="json") for item in result.amounts}

    def test_principal_marks_and_recorded_events_use_their_own_dates(self) -> None:
        native = {"as_of": "2026-02-15", "i_owe": [{"currency": "USD", "remaining_principal": {"value": "10.00", "currency": "USD"},
                  "principal_history": [{"effective_on": "2026-01-10", "opening_balance": False, "principal": {"value": "10.00", "currency": "USD"}},
                                        {"effective_on": "2026-02-10", "opening_balance": False, "principal": {"value": "10.00", "currency": "USD"}}]}]}
        values = self.annotate(native, "debts")
        self.assertEqual(values[("i_owe", 0, "remaining_principal", "value")]["reported"]["value"], "12.00")
        self.assertEqual(values[("i_owe", 0, "principal_history", 0, "principal", "value")]["reported"]["value"], "12.00")
        self.assertEqual(values[("i_owe", 0, "principal_history", 1, "principal", "value")]["reported"]["value"], "14.00")
        self.assertEqual(values[("i_owe", 0, "principal_history", 1, "principal", "value")]["purpose"], "flow")

    def test_historical_baseline_mean_values_months_before_combining(self) -> None:
        months = [{"month": "2026-01", "owner_cost_recorded": "10.00", "routine_variable_residual": "10.00",
                   "unresolved_irregular": "0", "trip": "0", "income": "0"},
                  {"month": "2026-02", "owner_cost_recorded": "10.00", "routine_variable_residual": "10.00",
                   "unresolved_irregular": "0", "trip": "0", "income": "0"}]
        native = {"by_currency": {"USD": {"months": months, "fixed_recurring_monthly": "0",
                                          "variable_residual_monthly": "10.00", "total_observed_monthly": "10.00",
                                          "net_received_income_monthly": "0", "historical_surplus_monthly": "-10.00"}}}
        values = self.annotate(native, "baseline/estimate")
        residual = values[("by_currency", "USD", "variable_residual_monthly")]
        self.assertEqual(residual["reported"]["value"], "13.00")
        self.assertEqual(residual["purpose"], "flow")
        self.assertIsNone(residual["rate_period"])
        self.assertEqual(values[("by_currency", "USD", "total_observed_monthly")]["reported"]["value"], "13.00")
        self.assertEqual(values[("by_currency", "USD", "historical_surplus_monthly")]["reported"]["value"], "-13.00")

    def test_zero_native_variance_values_actual_and_issued_operands_separately(self) -> None:
        native = {"currency": "USD", "snapshot": {"issued_at": "2026-02-15"}, "as_of": "2026-03-15",
                  "months": [{"month": "2026-02", "net_cash_effect": "0",
                              "actual_cash": {"receipts": "0.00", "payments": "10.00"}, "by_flow_class": [
                                  {"direction": "outflow", "expected": "10.00", "actual": "10.00",
                                   "open_expected": "0.00", "modelled_expected": "0.00", "cash_effect": "0"}]}]}
        values = self.annotate(native, "forecast/variance/snapshot")
        self.assertEqual(values[("months", 0, "by_flow_class", 0, "expected")]["reported"]["value"], "12.00")
        self.assertEqual(values[("months", 0, "by_flow_class", 0, "actual")]["reported"]["value"], "14.00")
        self.assertEqual(values[("months", 0, "net_cash_effect")]["reported"]["value"], "-2.00")
        self.assertEqual(values[("months", 0, "by_flow_class", 0, "cash_effect")]["reported"]["value"], "-2.00")

    def test_missing_rate_retains_native_amount_and_explicit_remedy(self) -> None:
        values = self.annotate({"currency": "EUR", "as_of": "2026-02-15", "remaining_principal": {"currency": "EUR", "value": "10.00"}}, "debts")
        value = values[("remaining_principal", "value")]
        self.assertIsNone(value["reported"])
        self.assertEqual(value["native"]["value"], "10.00")
        self.assertEqual(value["unavailable"]["reason"], "RATE_NOT_IMPORTED")
        self.assertIn("Import", value["unavailable"]["remedy"])


class WorkspaceAuditTests(WorkspaceValuationTests):
    """Release C audit: each amount is valued at its own date and purpose."""

    def test_cost_income_months_are_recorded_flows_at_their_own_month(self) -> None:
        native = {"months": [{"month": "2026-01", "by_currency": {"USD": {"gross_cost": "10.00", "owner_cost_recorded": "10.00", "owner_cost_baseline": "10.00", "unmatched_receipts": "0"}}},
                             {"month": "2026-02", "by_currency": {"USD": {"gross_cost": "10.00", "owner_cost_recorded": "10.00", "owner_cost_baseline": "5.00", "unmatched_receipts": "0"}}}],
                  "reconciliation": {"USD": {"owner_cost_recorded_total": "20.00", "owner_cost_baseline_total": "15.00",
                                             "unmatched_receipts_total": "0", "recorded_minus_baseline": "5.00", "cross_month_refund_in": "5.00"}}}
        values = self.annotate(native, "baseline/cost-income")
        january = values[("months", 0, "by_currency", "USD", "gross_cost")]
        self.assertEqual((january["purpose"], january["reported"]["value"]), ("flow", "12.00"))
        self.assertEqual(values[("months", 1, "by_currency", "USD", "gross_cost")]["reported"]["value"], "14.00")
        self.assertEqual(values[("reconciliation", "USD", "owner_cost_recorded_total")]["reported"]["value"], "26.00")
        self.assertEqual(values[("reconciliation", "USD", "recorded_minus_baseline")]["reported"]["value"], "7.00")
        # No dated breakdown exists for a cross-month refund total: never one rate.
        refund = values[("reconciliation", "USD", "cross_month_refund_in")]
        self.assertIsNone(refund["reported"])
        self.assertEqual(refund["unavailable"]["reason"], "NO_DATED_BREAKDOWN")
        self.assertEqual(values[("reconciliation", "USD", "unmatched_receipts_total")]["native"]["value"], "0")

    def test_multi_month_totals_compose_from_their_dated_parts(self) -> None:
        native = {"as_of": "2026-03-15", "currency": "USD",
                  "income_streams": [{"currency": "USD", "total_received": "30.00", "min_amount": "10.00", "max_amount": "20.00",
                                      "occurrences": [{"month": "2026-01", "amount": "20.00"}, {"month": "2026-02", "amount": "10.00"}]}],
                  "groups": [{"currency": "USD", "amount": "30.00", "mean_monthly": "15.00", "highest_observed_monthly": "20.00",
                              "months": [{"month": "2026-01", "amount": "20.00"}, {"month": "2026-02", "amount": "10.00"}]}]}
        values = self.annotate(native, "baseline/roles")
        self.assertEqual(values[("income_streams", 0, "total_received")]["reported"]["value"], "38.00")
        self.assertEqual(values[("income_streams", 0, "max_amount")]["reported"]["value"], "24.00")
        self.assertEqual(values[("income_streams", 0, "min_amount")]["reported"]["value"], "14.00")
        self.assertEqual(values[("groups", 0, "amount")]["reported"]["value"], "38.00")
        self.assertEqual(values[("groups", 0, "mean_monthly")]["reported"]["value"], "19.00")
        self.assertEqual(values[("groups", 0, "highest_observed_monthly")]["purpose"], "flow")

    def test_obligation_payments_and_lanes_compose_from_their_parts(self) -> None:
        native = {"as_of": "2026-02-15", "obligations": [
            {"currency": "USD", "direction": "outflow", "stays_visible": True, "cash_effect": "bank_withdrawal",
             "actual_payments": {"total": "20.00", "entries": [{"on": "2026-01-10", "amount": "10.00"}, {"on": "2026-02-10", "amount": "10.00"}]},
             "remaining_amount": {"value": "10.00", "currency": "USD"}}],
            "totals_by_currency": {"USD": {"bank_withdrawal": "10.00", "via_card_statement": "0.00"}}}
        values = self.annotate(native, "obligations")
        self.assertEqual(values[("obligations", 0, "actual_payments", "total")]["reported"]["value"], "26.00")
        lane = values[("totals_by_currency", "USD", "bank_withdrawal")]
        self.assertEqual((lane["purpose"], lane["reported"]["value"]), ("position", "12.00"))

    def test_statement_stocks_use_statement_dates(self) -> None:
        native = {"as_of": "2026-03-15", "statement_activity": [{"statement_date": "2026-02-21", "period_start": "2026-01-21", "pockets": [
            {"currency": "USD", "opening_liability": {"kind": "money", "value": "10.00", "currency": "USD"},
             "due_fields": {"payment_due": {"kind": "money", "value": "10.00", "currency": "USD"}},
             "reconciliation": {"checks": [{"expected": "1.00", "actual": "1.00"}]}}]}]}
        values = self.annotate(native, "cards/card")
        opening = values[("statement_activity", 0, "pockets", 0, "opening_liability", "value")]
        self.assertEqual((opening["purpose"], opening["valuation_as_of"]), ("position", "2026-01-20"))
        due = values[("statement_activity", 0, "pockets", 0, "due_fields", "payment_due", "value")]
        self.assertEqual((due["purpose"], due["valuation_as_of"]), ("position", "2026-02-21"))
        self.assertFalse(any("checks" in path for path in values))

    def test_variance_summaries_reuse_their_events(self) -> None:
        event = {"event_id": "e1", "direction": "inflow", "expected": "10.00", "actual": "10.00",
                 "matches": [{"landed_on": "2026-02-10", "amount": "10.00"}]}
        native = {"currency": "USD", "snapshot": {"issued_at": "2026-02-15"}, "as_of": "2026-03-15", "events": [event],
                  "biggest_misses": [{"event_id": "e1", "modelled_month": "2026-01", "expected": "10.00", "actual": "10.00", "cash_effect": "0"}],
                  "likely_income_realization": {"expected_amount": "10.00", "realized_amount": "10.00",
                                                "observations": [{"event_id": "e1", "expected": "10.00", "actual": "10.00"}]},
                  "match_suggestions": [{"outstanding": "1.00", "candidates": [{"on": "2026-02-01", "amount": "1.00", "amount_difference": "0.00", "suggested_amount": "1.00"}]}]}
        values = self.annotate(native, "forecast/variance/snapshot")
        self.assertEqual(values[("biggest_misses", 0, "actual")]["reported"]["value"], "14.00")
        self.assertEqual(values[("biggest_misses", 0, "cash_effect")]["reported"]["value"], "2.00")
        self.assertEqual(values[("likely_income_realization", "realized_amount")]["reported"]["value"], "14.00")
        self.assertEqual(values[("likely_income_realization", "expected_amount")]["purpose"], "forecast")
        # Matching compares native amounts; only the candidate's own amount is annotated.
        self.assertIn(("match_suggestions", 0, "candidates", 0, "amount"), values)
        for field in ("outstanding", "amount_difference", "suggested_amount"):
            self.assertFalse(any(path[-1] == field for path in values), field)

    def test_rule_inputs_and_unrendered_model_internals_stay_native(self) -> None:
        native = {"currency": "USD", "as_of": "2026-03-15", "candidates": [{"currency": "USD", "amount_tolerance": "1.00", "expected_amount": "10.00"}],
                  "irregular_risk_entries": [{"month": "2026-01", "amount": "200.00", "threshold": "150"}],
                  "forecast_cases": {"base": {"closing_balance": "5.00"}},
                  "starting_balance": "10.00", "starting_balance_as_of": "2026-02-10"}
        values = self.annotate(native, "planning/sufficiency")
        self.assertFalse(any(path[-1] in {"amount_tolerance", "threshold"} for path in values))
        self.assertFalse(any("forecast_cases" in path for path in values))
        start = values[("starting_balance",)]
        self.assertEqual((start["purpose"], start["valuation_as_of"]), ("position", "2026-02-10"))

    def test_owner_currency_reaches_amounts_that_omit_it(self) -> None:
        native = {"as_of": "2026-03-15", "plan": {"plan_id": "missing", "currency": "USD", "opened_on": "2026-01-05"},
                  "conversion": None, "suggested_conversion": {"effective_on": "2026-01-05", "amount": "10.00"},
                  "views": {"recognized_purchase_cost": {"recognized_principal": {"value": "10.00", "currency": "USD"}}}}
        values = self.annotate(native, "installments/missing")
        self.assertEqual(values[("suggested_conversion", "amount")]["reported"]["value"], "12.00")
        recognized = values[("views", "recognized_purchase_cost", "recognized_principal", "value")]
        self.assertEqual((recognized["purpose"], recognized["reported"]["value"]), ("flow", "12.00"))

    def test_trip_totals_value_each_day_at_its_own_month(self) -> None:
        native = {"trip": {"trip_id": "t", "totals_by_currency": {"USD": {"gross_cost": "20.00", "refunds": "10.00", "net_cost": "10.00"}},
                           "cost_parts": [{"on": "2026-01-31", "currency": "USD", "kind": "cost", "amount": "10.00"},
                                          {"on": "2026-02-01", "currency": "USD", "kind": "cost", "amount": "10.00"},
                                          {"on": "2026-02-02", "currency": "USD", "kind": "refund", "amount": "10.00"}]},
                  "by_category": [{"currency": "USD", "category_key": "Travel", "cost_amount": "20.00"}],
                  "contributors": [{"currency": "USD", "category_key": "Travel", "on": day, "cost_amount": "10.00",
                                    "categories": [{"category_key": "Travel", "cost_amount": "10.00"}]}
                                   for day in ("2026-01-31", "2026-02-01")]}
        values = self.annotate(native, "trips/t")
        totals = ("trip", "totals_by_currency", "USD")
        self.assertEqual(values[(*totals, "gross_cost")]["reported"]["value"], "26.00")
        self.assertEqual(values[(*totals, "refunds")]["reported"]["value"], "14.00")
        self.assertEqual(values[(*totals, "net_cost")]["reported"]["value"], "12.00")
        self.assertEqual(values[("by_category", 0, "cost_amount")]["reported"]["value"], "26.00")

    def test_a_report_row_with_no_refunds_reports_zero_and_a_full_net(self) -> None:
        native = {"rows": [{"currency": "USD", "gross_cost": "20.00", "refunds": "0.00", "net_cost": "20.00",
                            "cost_parts": [{"on": "2026-01-31", "currency": "USD", "kind": "cost", "amount": "10.00"},
                                           {"on": "2026-02-01", "currency": "USD", "kind": "cost", "amount": "10.00"}]}],
                  "totals_by_currency": {"USD": {"gross_cost": "20.00", "refunds": "0.00", "net_cost": "20.00"}}}
        values = self.annotate(native, "reports/r/result")
        self.assertIsNone(values[("rows", 0, "refunds")]["unavailable"])
        self.assertEqual(values[("rows", 0, "gross_cost")]["reported"]["value"], "26.00")
        self.assertEqual(values[("rows", 0, "refunds")]["reported"]["value"], "0.00")
        self.assertEqual(values[("rows", 0, "net_cost")]["reported"]["value"], "26.00")
        self.assertEqual(values[("totals_by_currency", "USD", "net_cost")]["reported"]["value"], "26.00")
