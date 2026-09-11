"""Exact valuation seam (§3.1, §3.3, §3.6, §8 M2)."""

from __future__ import annotations

import ast
import random
import sqlite3
import tempfile
import unittest
from datetime import date
from fractions import Fraction
from pathlib import Path

from fincl import fx, valuation
from fincl.db import connect_sqlite, initialize_schema
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.money import ExactMoney, format_decimal
from fincl.report import _consolidate_sgd
from fincl.reporting_currency.models import CURRENCY_REGISTRY
from fincl.valuation import (
    FLOW_RATE_V1,
    FORWARD_HOLD_LATEST_V1,
    FORWARD_MONTHLY_CLOSED_V1,
    LATEST_FIRST_IMPORT_V1,
    POSITION_MONTHLY_CLOSED_V1,
    POSITION_RATE_V1,
    ConsolidationStatus,
    EntryPrecision,
    ExactValuation,
    RateBasis,
    RateInput,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    consolidate,
    eligible_month_ceiling,
    quantize,
    to_consolidated_dto,
    to_dto,
    value_many,
    value_one,
)


def _doc(base: str, rates: list[dict[str, str]], quote: str = "SGD", source_version: str = "v1") -> dict[str, object]:
    return {"source": "test", "source_version": source_version, "quote_currency": quote, "rates": rates}


def _money(text: str) -> ExactMoney:
    return ExactMoney.parse(text)


class ValuationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.connection = connect_sqlite(Path(self.temporary.name) / "fincl.sqlite3")
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def _load(self, base: str, period: str, rate: str, *, quote: str = "SGD", source_version: str = "v1") -> None:
        load_fx_rates(
            self.connection,
            _doc(base, [{"base_currency": base, "period": period, "rate": rate}], quote=quote, source_version=source_version),
        )


class IdentityAndUnavailableTests(ValuationTestCase):
    def test_identity_when_native_equals_reporting(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(
            native=_money("10.00"), native_currency="SGD", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH, period_month="2026-01",
        )
        result = value_one(self.connection, item, context=context)
        self.assertEqual(result.basis, RateBasis.IDENTITY)
        self.assertEqual(result.reported_exact, Fraction(10))
        self.assertIsNone(result.unavailable)

    def test_unavailable_is_never_zero(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(
            native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH, period_month="2026-01",
        )
        result = value_one(self.connection, item, context=context)
        self.assertIsNone(result.reported_exact)
        self.assertIsNone(result.basis)
        assert result.unavailable is not None
        self.assertEqual(result.unavailable.reason, "RATE_NOT_IMPORTED")

    def test_unknown_precision_flow_entry_is_period_unknown(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(
            native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.UNKNOWN,
        )
        result = value_one(self.connection, item, context=context)
        assert result.unavailable is not None
        self.assertEqual(result.unavailable.reason, "PERIOD_UNKNOWN")

    def test_mismatched_item_purpose_is_rejected(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(
            native=_money("10.00"), native_currency="SGD", purpose=ValuationPurpose.POSITION,
        )
        with self.assertRaises(FinCLError):
            value_one(self.connection, item, context=context)


class RateSelectionPolicyTests(ValuationTestCase):
    """§3.2/§3.4: the ladder is named and versioned, never inferred."""

    def _item(self, purpose: ValuationPurpose = ValuationPurpose.FLOW) -> ValuationItem:
        return ValuationItem(
            native=_money("10.00"), native_currency="USD", purpose=purpose,
            precision=EntryPrecision.MONTH, period_month="2026-01",
        )

    def test_unknown_policy_is_rejected_rather_than_defaulted(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v9",
        )
        with self.assertRaises(FinCLError):
            value_one(self.connection, self._item(), context=context)

    def test_policy_answering_another_purpose_is_rejected(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=POSITION_RATE_V1,
        )
        with self.assertRaises(FinCLError):
            value_one(self.connection, self._item(), context=context)

    def test_direct_or_inverse_policy_prices_a_reciprocal_only_pair(self) -> None:
        self._load("SGD", "2026-01", "25.0", quote="THB")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
        )
        item = ValuationItem(
            native=_money("100.00"), native_currency="THB", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH, period_month="2026-01",
        )
        result = value_one(self.connection, item, context=context)
        self.assertEqual(result.reported_exact, Fraction(4))
        self.assertTrue(result.rate_path[0].inverted)

    def test_legacy_report_policy_takes_no_reciprocal_leg(self) -> None:
        """``latest-first-import-v1`` must resolve exactly the rows the
        pre-seam economic-flow report resolved: a reporting -> native row it
        always ignored may not now light up a total it used to withhold."""
        self._load("SGD", "2026-01", "25.0", quote="THB")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=LATEST_FIRST_IMPORT_V1,
        )
        item = ValuationItem(
            native=_money("100.00"), native_currency="THB", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH, period_month="2026-01",
        )
        result = value_one(self.connection, item, context=context)
        assert result.unavailable is not None
        self.assertEqual(result.unavailable.reason, "RATE_NOT_IMPORTED")


class UnavailabilityRemedyTests(ValuationTestCase):
    """§2 invariant 8 / §9.2: a typed reason *and a remedy*, never a bare gap."""

    def test_missing_rate_names_the_pair_and_the_period_to_import(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
        )
        item = ValuationItem(
            native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH, period_month="2026-03",
        )
        result = value_one(self.connection, item, context=context)
        assert result.unavailable is not None
        self.assertIn("USD -> SGD", result.unavailable.remedy)
        self.assertIn("2026-03", result.unavailable.remedy)

    def test_position_remedy_names_the_as_of_bound_not_a_month(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.POSITION,
            valuation_as_of=date(2026, 2, 15), rate_selection_policy=POSITION_RATE_V1,
        )
        item = ValuationItem(
            native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION
        )
        result = value_one(self.connection, item, context=context)
        assert result.unavailable is not None
        self.assertIn("on or before 2026-02-15", result.unavailable.remedy)

    def test_period_unknown_remedy_asks_for_a_date_not_a_rate(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
        )
        item = ValuationItem(
            native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.UNKNOWN,
        )
        result = value_one(self.connection, item, context=context)
        assert result.unavailable is not None
        self.assertEqual(result.unavailable.reason, "PERIOD_UNKNOWN")
        self.assertIn("date", result.unavailable.remedy)

    def test_every_unavailable_dto_carries_reason_message_and_remedy(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
        )
        items = [
            ValuationItem(
                native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                precision=EntryPrecision.UNKNOWN,
            ),
            ValuationItem(
                native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                precision=EntryPrecision.MONTH, period_month="2026-01",
            ),
        ]
        for item in items:
            dto = to_dto(
                value_one(self.connection, item, context=context),
                currency_registry=CURRENCY_REGISTRY,
            )
            assert dto.unavailable is not None
            self.assertIsNone(dto.reported)
            for field in (dto.unavailable.reason, dto.unavailable.message, dto.unavailable.remedy):
                self.assertTrue(field.strip())


class FlowRateTests(ValuationTestCase):
    def test_exact_period_rate_is_used_with_monthly_proxy_basis(self) -> None:
        self._load("USD", "2026-01", "1.3400")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(
            native=_money("100.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH, period_month="2026-01",
        )
        result = value_one(self.connection, item, context=context)
        self.assertEqual(result.basis, RateBasis.MONTHLY_PROXY)
        self.assertEqual(result.reported_exact, Fraction(134))
        self.assertEqual(len(result.rate_path), 1)
        self.assertFalse(result.rate_path[0].inverted)

    def test_inverse_pair_is_the_exact_reciprocal(self) -> None:
        # Loaded as SGD -> USD, valuing a USD amount into SGD must invert.
        self._load("SGD", "2026-01", "0.7500", quote="USD")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(
            native=_money("75.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH, period_month="2026-01",
        )
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(75) / Fraction(3, 4))
        self.assertTrue(result.rate_path[0].inverted)

    def test_a_later_import_for_a_different_month_does_not_light_up_an_earlier_one(self) -> None:
        self._load("USD", "2026-03", "1.4000")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(
            native=_money("100.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
            precision=EntryPrecision.MONTH, period_month="2026-01",
        )
        result = value_one(self.connection, item, context=context)
        assert result.unavailable is not None
        self.assertEqual(result.unavailable.reason, "RATE_NOT_IMPORTED")


class PositionAndForecastNoLookAheadTests(ValuationTestCase):
    def test_historical_position_never_consumes_a_later_observation(self) -> None:
        self._load("USD", "2026-01", "1.3000")
        self._load("USD", "2026-03", "1.9000", source_version="v2")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.POSITION,
            valuation_as_of=date(2026, 2, 15), rate_selection_policy="position-rate-v1",
        )
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        self.assertEqual(result.basis, RateBasis.CURRENT_MARK)
        self.assertEqual(result.reported_exact, Fraction(13))
        self.assertEqual(result.rate_path[0].period_month, "2026-01")

    def test_forecast_holds_the_position_rate_and_labels_held_latest(self) -> None:
        self._load("USD", "2026-01", "1.5000")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FORECAST,
            valuation_as_of=date(2026, 1, 20), rate_selection_policy="forward-hold-latest-v1",
            forward_policy="forward-hold-latest-v1",
        )
        item = ValuationItem(native=_money("2.00"), native_currency="USD", purpose=ValuationPurpose.FORECAST)
        result = value_one(self.connection, item, context=context)
        self.assertEqual(result.basis, RateBasis.HELD_LATEST)
        self.assertEqual(result.reported_exact, Fraction(3))

    def test_no_rate_at_all_before_as_of_is_unavailable_not_zero(self) -> None:
        self._load("USD", "2026-05", "1.5000")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.POSITION,
            valuation_as_of=date(2026, 2, 15), rate_selection_policy="position-rate-v1",
        )
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.unavailable is not None
        self.assertEqual(result.unavailable.reason, "RATE_NOT_IMPORTED")


class EligibleMonthCeilingTests(unittest.TestCase):
    """§4.2: pure date-math for the closed-month eligible ceiling."""

    def test_permissive_ceiling_is_always_the_requested_month(self) -> None:
        self.assertEqual(
            eligible_month_ceiling(date(2026, 8, 15), closed_month_only=False), "2026-08"
        )
        self.assertEqual(
            eligible_month_ceiling(date(2026, 8, 31), closed_month_only=False), "2026-08"
        )

    def test_closed_month_ceiling_excludes_a_mid_month_date(self) -> None:
        self.assertEqual(
            eligible_month_ceiling(date(2026, 8, 15), closed_month_only=True), "2026-07"
        )

    def test_closed_month_ceiling_includes_the_last_day_of_month(self) -> None:
        self.assertEqual(
            eligible_month_ceiling(date(2026, 8, 31), closed_month_only=True), "2026-08"
        )

    def test_closed_month_ceiling_crosses_a_year_boundary(self) -> None:
        self.assertEqual(
            eligible_month_ceiling(date(2026, 1, 15), closed_month_only=True), "2025-12"
        )

    def test_closed_month_ceiling_handles_leap_february(self) -> None:
        self.assertEqual(
            eligible_month_ceiling(date(2028, 2, 29), closed_month_only=True), "2028-02"
        )
        self.assertEqual(
            eligible_month_ceiling(date(2028, 2, 28), closed_month_only=True), "2028-01"
        )
        self.assertEqual(
            eligible_month_ceiling(date(2028, 3, 1), closed_month_only=True), "2028-02"
        )

    def test_non_leap_february_last_day_is_the_28th(self) -> None:
        self.assertEqual(
            eligible_month_ceiling(date(2026, 2, 28), closed_month_only=True), "2026-02"
        )


class ClosedMonthPolicyTests(ValuationTestCase):
    """§4.2: ``position-monthly-closed-v1``/``forward-monthly-closed-v1``
    actually differ in behavior from the legacy named policies, not merely by
    name (the registration-alone failure mode the prompt calls out)."""

    def _position_context(self, as_of: date, *, policy: str = POSITION_MONTHLY_CLOSED_V1) -> ValuationContext:
        return ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.POSITION,
            valuation_as_of=as_of, rate_selection_policy=policy,
        )

    def test_mid_month_rejects_the_same_month_rate_and_uses_the_prior_month(self) -> None:
        self._load("USD", "2026-07", "1.3000")
        self._load("USD", "2026-08", "9.9999", source_version="v2")
        context = self._position_context(date(2026, 8, 15))
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(13))
        self.assertEqual(result.rate_path[0].period_month, "2026-07")

    def test_month_end_accepts_that_months_own_rate(self) -> None:
        self._load("USD", "2026-08", "1.4000")
        context = self._position_context(date(2026, 8, 31))
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(14))
        self.assertEqual(result.rate_path[0].period_month, "2026-08")

    def test_mid_month_with_no_prior_month_rate_is_unavailable_not_the_same_month(self) -> None:
        self._load("USD", "2026-08", "9.9999")
        context = self._position_context(date(2026, 8, 15))
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.unavailable is not None
        self.assertEqual(result.unavailable.reason, "RATE_NOT_IMPORTED")

    def test_january_boundary_reaches_back_into_prior_december(self) -> None:
        self._load("USD", "2025-12", "1.2000")
        context = self._position_context(date(2026, 1, 10))
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(12))
        self.assertEqual(result.rate_path[0].period_month, "2025-12")

    def test_leap_february_29th_may_use_february_itself(self) -> None:
        self._load("USD", "2028-02", "1.1000")
        context = self._position_context(date(2028, 2, 29))
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(11))
        self.assertEqual(result.rate_path[0].period_month, "2028-02")

    def test_leap_february_28th_is_mid_month_and_falls_back_to_january(self) -> None:
        self._load("USD", "2028-01", "1.0500")
        self._load("USD", "2028-02", "9.9999", source_version="v2")
        context = self._position_context(date(2028, 2, 28))
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(21, 2))
        self.assertEqual(result.rate_path[0].period_month, "2028-01")

    def test_importing_a_future_month_does_not_affect_a_historical_mark(self) -> None:
        self._load("USD", "2026-07", "1.3000")
        context = self._position_context(date(2026, 8, 10))
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        before = value_one(self.connection, item, context=context)
        # Importing August (and even a later September) after the fact must
        # not move a mark already valued against the closed July ceiling.
        self._load("USD", "2026-08", "50.0000", source_version="v2")
        self._load("USD", "2026-09", "99.0000", source_version="v3")
        after = value_one(self.connection, item, context=context)
        self.assertEqual(before.reported_exact, after.reported_exact)
        self.assertEqual(after.rate_path[0].period_month, "2026-07")

    def test_inverse_rate_resolves_under_the_closed_month_ceiling(self) -> None:
        self._load("SGD", "2026-07", "0.7500", quote="USD")
        context = self._position_context(date(2026, 8, 15))
        item = ValuationItem(native=_money("75.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(75) / Fraction(3, 4))
        self.assertTrue(result.rate_path[0].inverted)
        self.assertEqual(result.rate_path[0].period_month, "2026-07")

    def test_identity_needs_no_rate_under_the_closed_month_policy(self) -> None:
        context = self._position_context(date(2026, 8, 15))
        item = ValuationItem(native=_money("10.00"), native_currency="SGD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        self.assertEqual(result.basis, RateBasis.IDENTITY)
        self.assertEqual(result.reported_exact, Fraction(10))
        self.assertEqual(result.rate_path, ())

    def test_forward_monthly_closed_matches_the_position_variant(self) -> None:
        self._load("USD", "2026-07", "1.3000")
        self._load("USD", "2026-08", "9.9999", source_version="v2")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FORECAST,
            valuation_as_of=date(2026, 8, 15), rate_selection_policy=FORWARD_MONTHLY_CLOSED_V1,
            forward_policy=FORWARD_MONTHLY_CLOSED_V1,
        )
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FORECAST)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.basis, RateBasis.HELD_LATEST)
        self.assertEqual(result.reported_exact, Fraction(13))
        self.assertEqual(result.rate_path[0].period_month, "2026-07")

    def test_legacy_position_policy_regression_is_unchanged_mid_month(self) -> None:
        """§4.2 acceptance: a named legacy policy keeps its permissive
        same-month ceiling; only the new closed-month names differ."""
        self._load("USD", "2026-08", "1.4000")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.POSITION,
            valuation_as_of=date(2026, 8, 15), rate_selection_policy=POSITION_RATE_V1,
        )
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.POSITION)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(14))
        self.assertEqual(result.rate_path[0].period_month, "2026-08")

    def test_legacy_forward_policy_regression_is_unchanged_mid_month(self) -> None:
        self._load("USD", "2026-08", "1.5000")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FORECAST,
            valuation_as_of=date(2026, 8, 15), rate_selection_policy=FORWARD_HOLD_LATEST_V1,
            forward_policy=FORWARD_HOLD_LATEST_V1,
        )
        item = ValuationItem(native=_money("2.00"), native_currency="USD", purpose=ValuationPurpose.FORECAST)
        result = value_one(self.connection, item, context=context)
        assert result.reported_exact is not None
        self.assertEqual(result.reported_exact, Fraction(3))
        self.assertEqual(result.rate_path[0].period_month, "2026-08")


class ConsolidateTests(ValuationTestCase):
    def _flow_context(self, as_of: date = date(2026, 3, 31)) -> ValuationContext:
        return ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=as_of, rate_selection_policy="flow-rate-v1",
        )

    def test_rejects_a_mixed_purpose_batch(self) -> None:
        context = self._flow_context()
        items = [
            ValuationItem(native=_money("1.00"), native_currency="SGD", purpose=ValuationPurpose.FLOW),
            ValuationItem(native=_money("1.00"), native_currency="SGD", purpose=ValuationPurpose.POSITION),
        ]
        with self.assertRaises(FinCLError):
            consolidate(self.connection, items, context=context)

    def test_complete_batch_is_available(self) -> None:
        self._load("USD", "2026-01", "1.3000")
        context = self._flow_context()
        items = [
            ValuationItem(native=_money("100.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
            ValuationItem(native=_money("50.00"), native_currency="SGD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
        ]
        result = consolidate(self.connection, items, context=context)
        self.assertEqual(result.status, ConsolidationStatus.AVAILABLE)
        self.assertEqual(result.total, Fraction(180))
        self.assertIsNone(result.partial_total)
        self.assertEqual(result.excluded_entry_count, 0)

    def test_one_missing_month_yields_a_partial_total_naming_the_exact_gap(self) -> None:
        self._load("USD", "2026-01", "1.3000")
        context = self._flow_context()
        items = [
            ValuationItem(native=_money("100.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
            ValuationItem(native=_money("100.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-02"),
        ]
        result = consolidate(self.connection, items, context=context)
        self.assertEqual(result.status, ConsolidationStatus.PARTIAL)
        self.assertIsNone(result.total)
        self.assertEqual(result.partial_total, Fraction(130))
        self.assertEqual(result.excluded_entry_count, 1)
        self.assertEqual([(s.currency, s.period) for s in result.excluded], [("USD", "2026-02")])
        self.assertEqual([(s.currency, s.period) for s in result.included], [("USD", "2026-01")])
        self.assertEqual(result.unavailable_reasons, ("RATE_NOT_IMPORTED",))

    def test_a_missing_month_does_not_exclude_every_other_month_of_the_same_currency(self) -> None:
        self._load("USD", "2026-01", "1.0000")
        self._load("USD", "2026-03", "1.0000", source_version="v3")
        context = self._flow_context()
        items = [
            ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
            ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-02"),
            ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-03"),
        ]
        result = consolidate(self.connection, items, context=context)
        self.assertEqual(result.status, ConsolidationStatus.PARTIAL)
        self.assertEqual([(s.currency, s.period) for s in result.excluded], [("USD", "2026-02")])
        self.assertEqual(
            sorted((s.currency, s.period) for s in result.included),
            [("USD", "2026-01"), ("USD", "2026-03")],
        )

    def test_fully_unavailable_batch_has_no_partial_total(self) -> None:
        context = self._flow_context()
        items = [
            ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
        ]
        result = consolidate(self.connection, items, context=context)
        self.assertEqual(result.status, ConsolidationStatus.UNAVAILABLE)
        self.assertIsNone(result.total)
        self.assertIsNone(result.partial_total)


class QuantizationTests(ValuationTestCase):
    def test_half_away_from_zero_at_two_decimal_places(self) -> None:
        self.assertEqual(quantize(Fraction(5, 1000), scale=2).text, "0.01")
        self.assertEqual(quantize(Fraction(-5, 1000), scale=2).text, "-0.01")

    def test_half_away_from_zero_at_scale_zero_for_jpy(self) -> None:
        self.assertEqual(quantize(Fraction(5, 10), scale=0).text, "1")
        self.assertEqual(quantize(Fraction(-5, 10), scale=0).text, "-1")

    def test_half_away_from_zero_at_scale_three_for_kwd(self) -> None:
        self.assertEqual(quantize(Fraction(1, 2000), scale=3).text, "0.001")

    def test_property_rational_fold_equals_one_quantization(self) -> None:
        """§3.6: the internal fold equals a direct rational fold of inputs, and
        the DTO equals *one* quantization of that fraction — never a sum of
        independently rounded rows."""
        self._load("USD", "2026-01", "1.37")
        rng = random.Random(20260911)
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        for _ in range(25):
            n = rng.randint(1, 6)
            coefficients = [rng.randint(-999_99, 999_99) for _ in range(n)]
            items = [
                ValuationItem(
                    native=ExactMoney(c, 2, ""), native_currency="USD", purpose=ValuationPurpose.FLOW,
                    precision=EntryPrecision.MONTH, period_month="2026-01",
                )
                for c in coefficients
            ]
            result = consolidate(self.connection, items, context=context)
            self.assertEqual(result.status, ConsolidationStatus.AVAILABLE)
            direct_fold = sum((Fraction(c, 100) * Fraction(137, 100) for c in coefficients), Fraction(0))
            assert result.total is not None
            self.assertEqual(result.total, direct_fold)
            dto = to_consolidated_dto(result, currency_registry=CURRENCY_REGISTRY)
            assert dto.total is not None
            self.assertEqual(dto.total.value, quantize(direct_fold, scale=2).text)
            # It must NOT equal a sum of independently-rounded per-row strings
            # whenever that sum would actually diverge from one quantization.
            per_row_rounded = sum(
                quantize(Fraction(c, 100) * Fraction(137, 100), scale=2).coefficient for c in coefficients
            )
            once_rounded = quantize(direct_fold, scale=2).coefficient
            if per_row_rounded != once_rounded:
                self.assertNotEqual(dto.total.value, quantize(Fraction(per_row_rounded, 100), scale=2).text)


class DtoRenderingTests(ValuationTestCase):
    def test_available_valuation_renders_reported_and_period_basis(self) -> None:
        self._load("USD", "2026-01", "1.3000")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                              precision=EntryPrecision.MONTH, period_month="2026-01")
        result = value_one(self.connection, item, context=context)
        dto = to_dto(result, currency_registry=CURRENCY_REGISTRY)
        self.assertEqual(dto.native.value, "10.00")
        self.assertEqual(dto.native.currency, "USD")
        assert dto.reported is not None
        self.assertEqual(dto.reported.value, "13.00")
        self.assertEqual(dto.reported.currency, "SGD")
        self.assertEqual(dto.basis, RateBasis.MONTHLY_PROXY)
        self.assertEqual(dto.rate_period, "2026-01")
        self.assertIsNone(dto.rate_as_of)
        self.assertIsNone(dto.unavailable)

    def test_unavailable_valuation_still_renders_native_and_a_typed_reason(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                              precision=EntryPrecision.MONTH, period_month="2026-01")
        result = value_one(self.connection, item, context=context)
        dto = to_dto(result, currency_registry=CURRENCY_REGISTRY)
        self.assertEqual(dto.native.value, "10.00")
        self.assertIsNone(dto.reported)
        self.assertIsNone(dto.basis)
        assert dto.unavailable is not None
        self.assertEqual(dto.unavailable.reason, "RATE_NOT_IMPORTED")

    def test_identity_valuation_has_no_rate_as_of_or_period(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(native=_money("10.00"), native_currency="SGD", purpose=ValuationPurpose.FLOW,
                              precision=EntryPrecision.MONTH, period_month="2026-01")
        result = value_one(self.connection, item, context=context)
        dto = to_dto(result, currency_registry=CURRENCY_REGISTRY)
        self.assertEqual(dto.basis, RateBasis.IDENTITY)
        self.assertIsNone(dto.rate_as_of)
        self.assertIsNone(dto.rate_period)

    def test_minor_unit_scale_is_not_hard_coded_to_two(self) -> None:
        self._load("USD", "2026-01", "150.0000", quote="JPY")
        context = ValuationContext(
            reporting_currency="JPY", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        item = ValuationItem(native=_money("2.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                              precision=EntryPrecision.MONTH, period_month="2026-01")
        result = value_one(self.connection, item, context=context)
        dto = to_dto(result, currency_registry=CURRENCY_REGISTRY)
        assert dto.reported is not None
        self.assertEqual(dto.reported.value, "300")

    def test_consolidated_dto_status_and_scopes(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        items = [
            ValuationItem(native=_money("10.00"), native_currency="SGD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
        ]
        result = consolidate(self.connection, items, context=context)
        dto = to_consolidated_dto(result, currency_registry=CURRENCY_REGISTRY)
        self.assertEqual(dto.status, ConsolidationStatus.AVAILABLE)
        assert dto.total is not None
        self.assertEqual(dto.total.value, "10.00")
        self.assertIsNone(dto.partial_total)
        self.assertEqual([(s.currency, s.period) for s in dto.included], [("SGD", "2026-01")])


class RateInputDtoExactRatioTests(ValuationTestCase):
    """§4.1 M3b.1 step 03: exact applied numerator/denominator, direction, and
    imported text separated from a synthesized reciprocal display string."""

    def test_direct_rate_exposes_the_exact_ratio_and_its_own_imported_text(self) -> None:
        self._load("USD", "2026-01", "1.3456")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
        )
        item = ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                              precision=EntryPrecision.MONTH, period_month="2026-01")
        result = value_one(self.connection, item, context=context)
        dto = to_dto(result, currency_registry=CURRENCY_REGISTRY)
        rate = dto.rate_path[0]
        self.assertEqual(rate.direction, "direct")
        self.assertEqual(rate.applied_numerator, "13456")
        self.assertEqual(rate.applied_denominator, "10000")
        self.assertEqual(int(rate.applied_numerator) / int(rate.applied_denominator), 1.3456)
        self.assertEqual(rate.imported_rate_text, "1.3456")

    def test_inverse_rate_exposes_the_exact_reciprocal_and_the_pre_inversion_text(self) -> None:
        # Imported as SGD -> USD "0.7500"; valuing USD -> SGD must take the
        # exact reciprocal 4/3, never a rounded finite decimal like "1.33".
        self._load("SGD", "2026-01", "0.7500", quote="USD")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
        )
        item = ValuationItem(native=_money("75.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                              precision=EntryPrecision.MONTH, period_month="2026-01")
        result = value_one(self.connection, item, context=context)
        dto = to_dto(result, currency_registry=CURRENCY_REGISTRY)
        rate = dto.rate_path[0]
        self.assertEqual(rate.direction, "inverse")
        self.assertTrue(rate.inverted)
        # The exact reciprocal of 3/4 is 4/3: a finite decimal ("1.33...")
        # would silently misstate this ratio; the DTO must carry it exact.
        self.assertEqual(Fraction(int(rate.applied_numerator), int(rate.applied_denominator)), Fraction(4, 3))
        # The genuinely imported text is the *other* direction's own printed
        # decimal, never the synthesized "4/3" fallback that used to be
        # ``rate_text`` for an inverted leg.
        self.assertEqual(rate.imported_rate_text, "0.7500")

    def test_large_exact_ratio_survives_as_a_plain_digit_string_not_a_float(self) -> None:
        """A rate precise enough that its numerator/denominator individually
        exceed JavaScript's safe integer range (2**53) must still round-trip
        exactly through the ``str``-typed wire field (§4.1)."""
        self._load("USD", "2026-01", "1.23456789012345678")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
        )
        item = ValuationItem(native=_money("1.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                              precision=EntryPrecision.MONTH, period_month="2026-01")
        result = value_one(self.connection, item, context=context)
        dto = to_dto(result, currency_registry=CURRENCY_REGISTRY)
        rate = dto.rate_path[0]
        self.assertIsInstance(rate.applied_numerator, str)
        self.assertIsInstance(rate.applied_denominator, str)
        expected_numerator = 123456789012345678
        expected_denominator = 10**17
        self.assertGreater(expected_numerator, 2**53)
        self.assertEqual(rate.applied_numerator, str(expected_numerator))
        self.assertEqual(rate.applied_denominator, str(expected_denominator))
        # A JS ``Number`` parse of this string would silently round; asserting
        # it against the same string (never through ``float``/``int`` JS-side
        # arithmetic) is exactly what the wire contract requires here.
        self.assertNotEqual(str(float(expected_numerator)), rate.applied_numerator)

    def test_identity_valuation_has_no_rate_path_entries_to_render_a_ratio_for(self) -> None:
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
        )
        item = ValuationItem(native=_money("10.00"), native_currency="SGD", purpose=ValuationPurpose.FLOW,
                              precision=EntryPrecision.MONTH, period_month="2026-01")
        result = value_one(self.connection, item, context=context)
        dto = to_dto(result, currency_registry=CURRENCY_REGISTRY)
        self.assertEqual(dto.rate_path, [])


class ExcludedScopeDetailTests(ValuationTestCase):
    """§4.1/§4.3: per-scope reasons/remedies alongside the flat legacy fields."""

    def _flow_context(self, as_of: date = date(2026, 3, 31)) -> ValuationContext:
        return ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=as_of, rate_selection_policy=FLOW_RATE_V1,
        )

    def test_each_excluded_scope_carries_its_own_reason_and_remedy(self) -> None:
        self._load("USD", "2026-01", "1.3000")
        context = self._flow_context()
        items = [
            ValuationItem(native=_money("100.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
            ValuationItem(native=_money("50.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-02"),
            ValuationItem(native=_money("20.00"), native_currency="EUR", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
        ]
        result = consolidate(self.connection, items, context=context)
        dto = to_consolidated_dto(result, currency_registry=CURRENCY_REGISTRY)
        self.assertEqual(len(dto.excluded_detail), 2)
        by_currency = {(d.currency, d.period): d for d in dto.excluded_detail}
        usd_feb = by_currency[("USD", "2026-02")]
        self.assertEqual(usd_feb.entry_count, 1)
        self.assertEqual(usd_feb.reasons, ["RATE_NOT_IMPORTED"])
        self.assertIn("2026-02", usd_feb.remedies[0])
        eur_jan = by_currency[("EUR", "2026-01")]
        self.assertEqual(eur_jan.entry_count, 1)
        self.assertEqual(eur_jan.reasons, ["RATE_NOT_IMPORTED"])
        self.assertIn("EUR", eur_jan.remedies[0])
        # The flat legacy fields still discharge their own contract unchanged.
        self.assertEqual(sorted(dto.unavailable_reasons), ["RATE_NOT_IMPORTED"])
        self.assertEqual(dto.excluded_entry_count, 2)

    def test_a_scope_with_multiple_excluded_entries_counts_and_dedupes_reasons(self) -> None:
        context = self._flow_context()
        items = [
            ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
            ValuationItem(native=_money("20.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
        ]
        result = consolidate(self.connection, items, context=context)
        dto = to_consolidated_dto(result, currency_registry=CURRENCY_REGISTRY)
        self.assertEqual(len(dto.excluded_detail), 1)
        detail = dto.excluded_detail[0]
        self.assertEqual(detail.entry_count, 2)
        self.assertEqual(detail.reasons, ["RATE_NOT_IMPORTED"])

    def test_no_excluded_detail_when_the_batch_is_fully_available(self) -> None:
        context = self._flow_context()
        items = [
            ValuationItem(native=_money("10.00"), native_currency="SGD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
        ]
        result = consolidate(self.connection, items, context=context)
        dto = to_consolidated_dto(result, currency_registry=CURRENCY_REGISTRY)
        self.assertEqual(dto.excluded_detail, [])


class ValueManyRateInventoryTests(ValuationTestCase):
    def test_distinct_rates_are_deduplicated(self) -> None:
        self._load("USD", "2026-01", "1.3000")
        context = ValuationContext(
            reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
            valuation_as_of=date(2026, 1, 31), rate_selection_policy="flow-rate-v1",
        )
        items = [
            ValuationItem(native=_money("10.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
            ValuationItem(native=_money("20.00"), native_currency="USD", purpose=ValuationPurpose.FLOW,
                          precision=EntryPrecision.MONTH, period_month="2026-01"),
        ]
        _values, inventory = value_many(self.connection, items, context=context)
        self.assertEqual(len(inventory.rate_inputs), 1)


class BasisClassificationTests(unittest.TestCase):
    """§3.3: which of ``rate_as_of`` / ``rate_period`` applies is implied by basis.

    The classification must stay total over ``RateBasis``. A basis in none of
    the three arms would render with neither rate field populated and no error,
    which is precisely the silent branch §3.3 chose two fields to avoid.
    """

    def test_every_rate_basis_is_classified_exactly_once(self) -> None:
        arms = (valuation._DATED_BASES, valuation._PERIOD_BASES, valuation._RATELESS_BASES)
        for basis in RateBasis:
            containing = [arm for arm in arms if basis in arm]
            self.assertEqual(len(containing), 1, f"{basis} is classified {len(containing)} times")


class RateInventoryKeyTests(unittest.TestCase):
    """The inventory key must be totally ordered over nullable rate metadata."""

    def _rate(self, *, period_month: str | None, effective_on: date | None, kind: str) -> RateInput:
        return RateInput(
            base_currency="USD", quote_currency="SGD", period_month=period_month,
            effective_on=effective_on, observation_kind=kind, source="t",
            source_version="v1", inverted=False, rate_numerator=13,
            rate_denominator=10, rate_text="1.3",
        )

    def test_a_dated_row_and_a_monthly_row_sort_and_survive_together(self) -> None:
        """§3.4 admits a daily observation and a monthly proxy for the same
        month. Sorting a mix of ``None`` and ``str`` raises, and keying only by
        month would collapse the two into one inventory row."""
        dated = self._rate(period_month=None, effective_on=date(2026, 1, 14), kind="DAILY_CLOSE")
        monthly = self._rate(period_month="2026-01", effective_on=None, kind="MONTHLY_PROXY")
        values = [
            ExactValuation(
                native=_money("1.00"), native_currency="USD", reported_exact=Fraction(13, 10),
                context=ValuationContext(
                    reporting_currency="SGD", purpose=ValuationPurpose.FLOW,
                    valuation_as_of=date(2026, 1, 31), rate_selection_policy=FLOW_RATE_V1,
                ),
                basis=RateBasis.MONTHLY_PROXY, rate_path=(rate,), unavailable=None,
            )
            for rate in (dated, monthly, dated)
        ]
        inventory = valuation._rate_inventory(values)
        self.assertEqual(len(inventory.rate_inputs), 2)


def _pre_seam_consolidate_sgd(
    connection: sqlite3.Connection,
    netting: list[tuple[str, int, int, str]],
    fx_inputs: dict[tuple[str, str], dict[str, str]],
) -> tuple[str | None, str]:
    """``report.py::_consolidate_sgd`` exactly as it stood before the M2 seam.

    Kept verbatim as the oracle for the differential test below: §8 M2 requires
    the move behind the seam to leave this report's SGD result and its retained
    ``fx_inputs`` field byte-equivalent, and only the old code can prove that.
    """
    total = Fraction(0)
    for currency, coefficient, scale, period in netting:
        amount = Fraction(coefficient, 10**scale)
        if currency == "SGD":
            total += amount
            continue
        key = (currency, period)
        selected = fx_inputs.get(key) or fx.selected_rate(connection, currency, "SGD", period)
        if selected is None:
            return None, "UNAVAILABLE_FX_RATES_NOT_IMPORTED"
        fx_inputs[key] = selected
        total += amount * Fraction(
            int(selected["rate_numerator"]), int(selected["rate_denominator"])
        )
    cents = total * 100
    rounded = int(cents + Fraction(1, 2)) if cents >= 0 else -int(-cents + Fraction(1, 2))
    return format_decimal(rounded, 2), "AVAILABLE"


class ConsolidateSgdEquivalenceTests(ValuationTestCase):
    """§8 M2: the seam move changes neither the SGD total nor ``fx_inputs``."""

    WINDOW = {"start": "2026-01-01", "end": "2026-03-31"}

    def _assert_equivalent(self, netting: list[tuple[str, int, int, str]]) -> None:
        old_inputs: dict[tuple[str, str], dict[str, str]] = {}
        new_inputs: dict[tuple[str, str], dict[str, str]] = {}
        old_result = _pre_seam_consolidate_sgd(self.connection, netting, old_inputs)
        new_result = _consolidate_sgd(
            self.connection, netting, new_inputs, analysis_window=self.WINDOW
        )
        self.assertEqual(new_result, old_result)
        # The payload renders `[fx_inputs[key] for key in sorted(fx_inputs)]`,
        # so both the key set and each provenance dict must match.
        self.assertEqual(sorted(new_inputs), sorted(old_inputs))
        for key in sorted(old_inputs):
            self.assertEqual(new_inputs[key], old_inputs[key])

    def test_identity_and_converted_rows(self) -> None:
        self._load("USD", "2026-01", "1.3500")
        self._assert_equivalent([("SGD", 10000, 2, "2026-01"), ("USD", 5000, 2, "2026-01")])

    def test_repeated_currency_month_is_recorded_once(self) -> None:
        self._load("USD", "2026-01", "1.3500")
        self._assert_equivalent([("USD", 5000, 2, "2026-01"), ("USD", 2500, 2, "2026-01")])

    def test_a_reciprocal_only_pair_stays_unavailable(self) -> None:
        """The pre-seam report never read a reporting -> native row. The seam
        must not turn that withheld status into a priced total."""
        self._load("SGD", "2026-01", "25.0", quote="THB")
        self._assert_equivalent([("THB", 100000, 2, "2026-01")])

    def test_missing_month_after_a_priced_row_publishes_only_the_priced_one(self) -> None:
        self._load("USD", "2026-01", "1.3500")
        self._assert_equivalent([("USD", 5000, 2, "2026-01"), ("USD", 5000, 2, "2026-03")])

    def test_missing_month_before_a_priced_row_publishes_nothing(self) -> None:
        """The old loop returned at the first missing rate, so a later row's
        rate never reached the payload. Order is observable here."""
        self._load("USD", "2026-01", "1.3500")
        self._assert_equivalent([("USD", 5000, 2, "2026-03"), ("USD", 5000, 2, "2026-01")])

    def test_negative_rows_round_half_away_from_zero_identically(self) -> None:
        self._load("USD", "2026-02", "1.3333")
        self._assert_equivalent([("USD", -1250, 2, "2026-02"), ("SGD", -375, 2, "2026-02")])

    def test_empty_netting_is_an_available_zero_with_no_inputs(self) -> None:
        self._assert_equivalent([])


class NoFloatInValuationTests(unittest.TestCase):
    """§2 invariant 10: exact arithmetic is retained; no ``float`` anywhere in valuation."""

    def test_no_float_constructor_or_literal_in_the_module(self) -> None:
        """A regex for ``float(`` misses the likelier mistake — a bare decimal
        literal such as ``1.37``, which is a float the moment it is parsed. The
        AST catches both."""
        path = Path(__file__).resolve().parent.parent / "src" / "fincl" / "valuation.py"
        tree = ast.parse(path.read_text())
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"float literal {node.value!r} at line {node.lineno}")
            if isinstance(node, ast.Name) and node.id == "float":
                offenders.append(f"reference to float at line {node.lineno}")
        self.assertEqual(offenders, [])

    def test_true_division_is_never_applied_to_module_values(self) -> None:
        """``Fraction / Fraction`` is exact, but ``int / int`` is a float. The
        only division in the module is the quantization step's, and it must
        stay rational."""
        result = quantize(Fraction(1, 3), scale=2)
        self.assertIsInstance(result.coefficient, int)
        self.assertNotIsInstance(result.coefficient, float)


if __name__ == "__main__":
    unittest.main()
