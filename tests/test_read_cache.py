"""One request computes each composed reader once, and nothing leaks out of it."""
from __future__ import annotations

import sqlite3
import unittest
from datetime import date
from typing import cast

from fincl.db import _BorrowedReadConnection
from fincl.read_cache import per_request, request_cache

calls: list[tuple[str, date]] = []


@per_request
def reader(connection: sqlite3.Connection, *, today: date) -> dict[str, list[int]]:
    calls.append(("reader", today))
    return {"rows": [1, 2]}


class RequestCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        calls.clear()
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)

    def test_inside_a_request_each_reader_runs_once_per_argument(self) -> None:
        with request_cache(self.connection):
            reader(self.connection, today=date(2026, 1, 1))
            reader(self.connection, today=date(2026, 1, 1))
            reader(self.connection, today=date(2026, 1, 2))
        self.assertEqual(len(calls), 2)

    def test_each_caller_gets_its_own_copy(self) -> None:
        with request_cache(self.connection):
            first = reader(self.connection, today=date(2026, 1, 1))
            first["rows"].append(99)
            self.assertEqual(reader(self.connection, today=date(2026, 1, 1)), {"rows": [1, 2]})

    def test_nothing_is_cached_outside_a_request_or_across_requests(self) -> None:
        reader(self.connection, today=date(2026, 1, 1))
        reader(self.connection, today=date(2026, 1, 1))
        with request_cache(self.connection):
            reader(self.connection, today=date(2026, 1, 1))
        with request_cache(self.connection):
            reader(self.connection, today=date(2026, 1, 1))
        self.assertEqual(len(calls), 4)

    def test_a_request_pins_one_read_transaction(self) -> None:
        with request_cache(self.connection):
            self.assertTrue(self.connection.in_transaction)

    def test_a_different_connection_is_a_different_snapshot(self) -> None:
        other = sqlite3.connect(":memory:")
        self.addCleanup(other.close)
        with request_cache(self.connection):
            other.execute("BEGIN")
            reader(self.connection, today=date(2026, 1, 1))
            reader(other, today=date(2026, 1, 1))
        self.assertEqual(len(calls), 2)

    def test_borrowed_wrappers_share_the_underlying_snapshot_cache(self) -> None:
        with request_cache(self.connection):
            reader(self.connection, today=date(2026, 1, 1))
            for _ in range(2):
                borrowed = cast(sqlite3.Connection, _BorrowedReadConnection(self.connection))
                result = reader(borrowed, today=date(2026, 1, 1))
                result["rows"].append(99)
                borrowed.close()
            self.assertEqual(reader(self.connection, today=date(2026, 1, 1)), {"rows": [1, 2]})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
