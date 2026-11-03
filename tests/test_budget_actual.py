"""What a budget counts as spent."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fincl.planning.queries import _budget_actual_cents


class BudgetActualTests(unittest.TestCase):
    def test_an_entry_finer_than_a_cent_still_counts(self) -> None:
        rows = [
            {"amount_coefficient": -12345, "amount_scale": 3, "fee_coefficient": 0, "fee_scale": 0},
            {"amount_coefficient": -500, "amount_scale": 2, "fee_coefficient": -25, "fee_scale": 2},
        ]
        connection = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: rows))

        cents = _budget_actual_cents(
            connection, category_key=None, currency="SGD", start="2026-01-01", end="2026-01-31"
        )

        self.assertEqual(cents, 1760)
