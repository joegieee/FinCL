import unittest

from fincl.money import (
    MAX_ABS_COEFFICIENT,
    MAX_SCALE,
    ExactMoney,
    exact_sum,
    format_decimal,
)


class ExactMoneyTests(unittest.TestCase):
    def test_preserves_printed_text_coefficient_and_scale(self):
        value = ExactMoney.parse("-0012.3400")
        self.assertEqual(value.text, "-0012.3400")
        self.assertEqual(value.coefficient, -123400)
        self.assertEqual(value.scale, 4)

    def test_rejects_binary_float_and_exponent_notation(self):
        with self.assertRaises(TypeError):
            ExactMoney.parse(1.2)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ExactMoney.parse("1e2")

    def test_precision_and_overflow_limits_are_explicit(self):
        self.assertEqual(ExactMoney.parse("0." + "1" * MAX_SCALE).scale, MAX_SCALE)
        with self.assertRaises(ValueError):
            ExactMoney.parse("0." + "1" * (MAX_SCALE + 1))
        self.assertEqual(
            ExactMoney.parse(str(MAX_ABS_COEFFICIENT)).coefficient,
            MAX_ABS_COEFFICIENT,
        )
        with self.assertRaises(OverflowError):
            ExactMoney.parse(str(MAX_ABS_COEFFICIENT + 1))

    def test_mixed_scale_sum_is_exact(self):
        coefficient, scale = exact_sum(
            ExactMoney.parse("1.20"),
            ExactMoney.parse("-0.003"),
            ExactMoney.parse("2"),
        )
        self.assertEqual((coefficient, scale), (3197, 3))
        self.assertEqual(format_decimal(coefficient, scale), "3.197")


if __name__ == "__main__":
    unittest.main()
