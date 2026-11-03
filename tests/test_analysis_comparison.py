"""Which period an analysis compares against, and how a change reads."""

from __future__ import annotations

import unittest

from fincl.analysis.queries import _pct
from fincl.analysis.scopes import normalize_scope, resolve_comparison


class ComparisonTests(unittest.TestCase):
    def _compare(self, start: str, end: str, requested: str | None = None) -> tuple[str, str, str]:
        scope = normalize_scope(start=start, end=end)
        result = resolve_comparison(scope, [], requested=requested, main_covered=True)
        return result.kind, str(result.start), str(result.end)

    def test_whole_months_compare_with_the_same_number_of_months(self) -> None:
        self.assertEqual(
            self._compare("2026-07-01", "2026-09-30"),
            ("preceding_months", "2026-04-01", "2026-06-30"),
        )
        self.assertEqual(
            self._compare("2026-01-01", "2026-03-31"),
            ("preceding_months", "2025-10-01", "2025-12-31"),
        )

    def test_equal_length_stays_available_when_asked_for(self) -> None:
        self.assertEqual(
            self._compare("2026-07-01", "2026-09-30", "preceding_equal_duration"),
            ("preceding_equal_duration", "2026-03-31", "2026-06-30"),
        )

    def test_a_negative_base_keeps_the_direction_of_the_change(self) -> None:
        self.assertEqual(_pct("-50.00", "-100.00"), "50.0")
        self.assertEqual(_pct("-150.00", "-100.00"), "-50.0")
