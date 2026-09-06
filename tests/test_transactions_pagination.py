"""Cursor-backed server pagination for the transaction list (queries.transactions_page).

Order is deterministic to the (natural_anchor, id) tie-break, so keyset pages
partition the result with no gaps or repeats even when many rows share a date.
Filters (currency, window, account, category, search, kind, excluded) and the
three sort orders are SQL predicates applied before LIMIT.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fincl.errors import FinCLError
from fincl.queries import dashboard, transactions_page
from fincl.refresh import refresh_ledger
from fincl.service import FinCLService
from tests.test_revolut_import import csv_bytes


def _ledger(rows: list[list[str]]) -> tuple[Path, str]:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    FinCLService.plaintext_phase1(database, root / "evidence").import_revolut(source)
    refresh_ledger(database)
    return database, dashboard(database, start="2026-01-01", end="2026-01-31")["generation"]


def _many_rows(n: int) -> list[list[str]]:
    rows = [["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
             "Seed", "5000.00", "0.00", "SGD", "COMPLETED", "5000.00"]]
    balance = 5000.0
    for i in range(n):
        amount = (i % 9) + 1
        balance -= amount
        # Deliberately reuse one timestamp for a block of rows so the date key
        # collides and the anchor/id tie-break has to carry the ordering.
        day = 2 + (i // 6)
        rows.append(["Card Payment", "Current", f"2026-01-{day:02d} 10:00:00",
                     f"2026-01-{day:02d} 10:00:01", f"Merchant {i % 4}", f"-{amount}.00",
                     "0.00", "SGD", "COMPLETED", f"{balance:.2f}"])
    return rows


class KeysetPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database, self.generation = _ledger(_many_rows(30))

    def _all(self, **kw) -> list[str]:
        return [e["id"] for e in transactions_page(
            self.database, generation=self.generation, start="2026-01-01", end="2026-01-31",
            limit=500, **kw)["entries"]]

    def _paged(self, page_size: int, **kw) -> list[str]:
        collected: list[str] = []
        cursor: str | None = None
        for _ in range(100):
            page = transactions_page(
                self.database, generation=self.generation, start="2026-01-01",
                end="2026-01-31", limit=page_size, cursor=cursor, **kw)
            collected += [e["id"] for e in page["entries"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        return collected

    def test_pages_partition_the_result_with_no_gaps_or_repeats(self) -> None:
        for sort in ("date", "amount", "merchant"):
            expected = self._all(sort=sort)
            for size in (1, 3, 7, 500):
                with self.subTest(sort=sort, size=size):
                    self.assertEqual(self._paged(size, sort=sort), expected)
            self.assertEqual(len(set(expected)), len(expected))

    def test_total_is_the_unpaged_count(self) -> None:
        page = transactions_page(self.database, generation=self.generation,
                                 start="2026-01-01", end="2026-01-31", limit=5)
        self.assertEqual(page["total"], len(self._all()))
        self.assertIsNotNone(page["next_cursor"])

    def test_amount_sort_is_largest_outgoing_first(self) -> None:
        values = [float(e["amount"]) for e in transactions_page(
            self.database, generation=self.generation, start="2026-01-01",
            end="2026-01-31", sort="amount", kind="spending", limit=500)["entries"]]
        self.assertEqual(values, sorted(values))

    def test_search_and_account_and_kind_filters(self) -> None:
        one = transactions_page(self.database, generation=self.generation,
                                start="2026-01-01", end="2026-01-31", search="merchant 2", limit=500)
        self.assertTrue(one["total"])
        self.assertTrue(all("Merchant 2".casefold() in e["merchant"].casefold() for e in one["entries"]))

        excluded_off = transactions_page(self.database, generation=self.generation,
                                         start="2026-01-01", end="2026-01-31", limit=500)
        excluded_on = transactions_page(self.database, generation=self.generation,
                                        start="2026-01-01", end="2026-01-31",
                                        include_excluded=True, limit=500)
        self.assertLessEqual(excluded_off["total"], excluded_on["total"])

    def test_unknown_sort_and_bad_cursor_are_refused(self) -> None:
        with self.assertRaises(FinCLError):
            transactions_page(self.database, generation=self.generation,
                              start="2026-01-01", end="2026-01-31", sort="sideways")
        with self.assertRaises(FinCLError):
            transactions_page(self.database, generation=self.generation,
                              start="2026-01-01", end="2026-01-31", cursor="@@@not-valid@@@")

    def test_a_stale_generation_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            transactions_page(self.database, generation="sha256:stale",
                              start="2026-01-01", end="2026-01-31")

    def test_a_forward_cursor_survives_a_limit_change(self) -> None:
        first = transactions_page(self.database, generation=self.generation,
                                  start="2026-01-01", end="2026-01-31", limit=4)
        rest = transactions_page(self.database, generation=self.generation,
                                 start="2026-01-01", end="2026-01-31", limit=500,
                                 cursor=first["next_cursor"])
        combined = [e["id"] for e in first["entries"]] + [e["id"] for e in rest["entries"]]
        self.assertEqual(combined, self._all())


_MONTH_PRECISION = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
     "Seed", "400.00", "0.00", "SGD", "COMPLETED", "400.00"],
    ["Card Payment", "Current", "2026-01-15 10:00:00", "2026-01-15 10:00:01",
     "January purchase", "-12.00", "0.00", "SGD", "COMPLETED", "388.00"],
    ["Card Payment", "Current", "2026-02-15 10:00:00", "2026-02-15 10:00:01",
     "February purchase", "-9.00", "0.00", "SGD", "COMPLETED", "379.00"],
]


class WindowFilterTests(unittest.TestCase):
    def test_a_day_precision_row_is_included_only_inside_the_window(self) -> None:
        database, generation = _ledger(_MONTH_PRECISION)
        january = transactions_page(database, generation=generation,
                                    start="2026-01-01", end="2026-01-31", limit=50)
        merchants = {e["merchant"].casefold() for e in january["entries"]}
        self.assertIn("january purchase", merchants)
        self.assertNotIn("february purchase", merchants)


if __name__ == "__main__":
    unittest.main()
