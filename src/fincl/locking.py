"""The single ledger-directory writer lock (DESIGN-application-contract.md §2).

Every process that writes a ledger — import, backup, rebuild, migrate, FX load,
rule/review/repair/adoption, and the startup refresh — serializes on one
advisory lock file in the ledger directory: ``.fincl.writer.lock``. Acquisition
is nonblocking with a bounded retry so a contended writer fails cleanly with
``LedgerBusyError`` instead of hanging.

Lock order is **ledger lock → journal file lock → SQLite write transaction**.
This module owns only the first. The journal's own ``flock`` in
``decision_journal`` is a lower-level nested integrity guard and is never a
substitute for this lock. A nested application call receives the already-held
``WriterContext`` and must not reacquire.

Only the ledger-directory lock lives here; schema-9 generation identity and
command receipts are separate M1b work and are not introduced in this module.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import FinCLError

WRITER_LOCK_NAME = ".fincl.writer.lock"

# Nonblocking acquire, retried every 50 ms against a 5 s monotonic deadline.
LOCK_RETRY_INTERVAL_S = 0.05
LOCK_DEADLINE_S = 5.0


class LedgerBusyError(FinCLError):
    """Another writer holds the ledger lock; the bounded retry gave up."""


@dataclass(slots=True)
class WriterContext:
    """A held ledger-directory write lock, passed to nested application calls."""

    database_path: Path
    ledger_dir: Path
    lock_path: Path
    _lock_fd: int
    _depth: int = 1

    @property
    def held(self) -> bool:
        return self._lock_fd >= 0


def _acquire(lock_path: Path) -> int:
    """Open and flock the lock file, or raise ``LedgerBusyError`` past the deadline.

    The lock file is created if missing and **never unlinked** — unlinking it
    would let a second writer create a different inode and hold a lock the first
    writer cannot see.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + LOCK_DEADLINE_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise LedgerBusyError(
                    f"ledger {lock_path.parent} is busy: another writer held the "
                    f"lock for more than {LOCK_DEADLINE_S:g}s"
                ) from None
            time.sleep(LOCK_RETRY_INTERVAL_S)


@contextmanager
def ledger_writer(
    database_path: Path, *, existing: WriterContext | None = None
) -> Iterator[WriterContext]:
    """Hold the one ledger-directory writer lock for the duration of the block.

    Pass ``existing`` from an outer writer so a nested call reuses the held lock
    instead of reacquiring it (and deadlocking against itself, since ``flock`` is
    not recursive within a process on the same fd). The lock is released only
    when the outermost context exits.
    """
    database_path = Path(database_path).resolve()
    ledger_dir = database_path.parent

    if existing is not None:
        # The lock file is one per ledger directory, so a nested call for a
        # sibling database (a rebuild staging file) legitimately reuses it.
        if existing.ledger_dir != ledger_dir:
            raise FinCLError(
                "nested writer context is for a different ledger directory: "
                f"{existing.ledger_dir} vs {ledger_dir}"
            )
        existing._depth += 1
        try:
            yield existing
        finally:
            existing._depth -= 1
        return

    lock_path = ledger_dir / WRITER_LOCK_NAME
    fd = _acquire(lock_path)
    context = WriterContext(database_path, ledger_dir, lock_path, fd)
    try:
        yield context
    finally:
        context._lock_fd = -1
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
