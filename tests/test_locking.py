"""The single ledger-directory writer lock."""
from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fincl.errors import FinCLError
from fincl.locking import WRITER_LOCK_NAME, LedgerBusyError, ledger_writer


class LedgerWriterLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger_dir = Path(temporary.name)
        self.database = self.ledger_dir / "fincl.sqlite3"
        self.lock_path = self.ledger_dir / WRITER_LOCK_NAME

    def _hold_lock(self) -> int:
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def test_uncontended_acquire_and_release(self) -> None:
        with ledger_writer(self.database) as context:
            self.assertTrue(context.held)
            self.assertEqual(context.database_path, self.database.resolve())
        # The lock file is kept, never unlinked.
        self.assertTrue(self.lock_path.exists())
        # And is immediately reacquirable.
        with ledger_writer(self.database) as again:
            self.assertTrue(again.held)

    def test_contended_acquire_times_out_with_ledger_busy(self) -> None:
        held = self._hold_lock()
        try:
            with mock.patch("fincl.locking.LOCK_DEADLINE_S", 0.3):
                started = time.monotonic()
                with self.assertRaises(LedgerBusyError):  # noqa: SIM117
                    with ledger_writer(self.database):
                        pass
                elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.3)
            self.assertLess(elapsed, 3.0)
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
            os.close(held)

    def test_acquire_succeeds_once_the_holder_releases(self) -> None:
        held = self._hold_lock()
        acquired: list[bool] = []

        def _worker() -> None:
            with ledger_writer(self.database):
                acquired.append(True)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=0.5)
        self.assertEqual(acquired, [])  # still blocked

        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        thread.join(timeout=3.0)
        self.assertEqual(acquired, [True])

    def test_nested_context_reuses_the_held_lock(self) -> None:
        with ledger_writer(self.database) as outer:
            with ledger_writer(self.database, existing=outer) as inner:  # noqa: SIM117
                self.assertIs(inner, outer)
                self.assertEqual(outer._depth, 2)
            self.assertEqual(outer._depth, 1)
            self.assertTrue(outer.held)

    def test_nested_context_reuses_across_sibling_databases(self) -> None:
        # The lock is per directory, so a rebuild staging file in the same dir
        # legitimately reuses an outer context.
        with ledger_writer(self.database) as outer:  # noqa: SIM117
            with ledger_writer(self.ledger_dir / ".staging.sqlite3", existing=outer) as inner:
                self.assertIs(inner, outer)

    def test_nested_context_for_a_different_directory_is_rejected(self) -> None:
        other_dir = self.ledger_dir / "other"
        other_dir.mkdir()
        with ledger_writer(self.database) as outer:  # noqa: SIM117
            with self.assertRaises(FinCLError):
                with ledger_writer(other_dir / "fincl.sqlite3", existing=outer):
                    pass


if __name__ == "__main__":
    unittest.main()
