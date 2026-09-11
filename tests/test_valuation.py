"""Exact valuation seam (§3.1, §3.3, §3.6, §8 M2)."""

from __future__ import annotations

import random
import re
import tempfile
import unittest
from datetime import date
from fractions import Fraction
from pathlib import Path

from fincl.db import connect_sqlite, initialize_schema
from fincl.errors import FinCLError
from fincl.fx import load_fx_rates
from fincl.money import ExactMoney
from fincl.reporting_currency.models import CURRENCY_REGISTRY
from fincl.valuation import (
    ConsolidationStatus,
    EntryPrecision,
    RateBasis,
    ValuationContext,
    ValuationItem,
    ValuationPurpose,
    consolidate,
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


class NoFloatInValuationTests(unittest.TestCase):
    """§2 invariant 10: exact arithmetic is retained; no ``float`` anywhere in valuation."""

    def test_no_float_literal_or_constructor_in_the_module(self) -> None:
        path = Path(__file__).resolve().parent.parent / "src" / "fincl" / "valuation.py"
        text = path.read_text()
        self.assertNotRegex(text, re.compile(r"\bfloat\("))


if __name__ == "__main__":
    unittest.main()
