"""The one place the wall clock is read.

Every reader in this codebase takes ``today`` as a parameter and never consults
the clock itself, so that a report is a pure function of its evidence and its
as-of date (§5.3, §2 invariant 7). That rule leaves exactly one question open:
who resolves "today" at the outer edge. This module is the answer, and it is the
only module that calls :meth:`datetime.date.today`.

Having one seam makes the app's own notion of today injectable, which is what a
deterministic browser test needs: synthetic fixtures carry fixed dates, and a
suite that reads the real clock stops agreeing with them the moment enough time
passes. Pinning the clock lets the fixtures and the assertions both stay literal
instead of being recomputed relative to the day the suite happens to run.

The pin is deliberately not reachable from the environment or a request. It is a
constructor argument to :func:`fincl.api.create_app`, so pinning the clock is
something a process does to itself at startup, in code, once — never something
an HTTP caller can ask for.
"""

from __future__ import annotations

from datetime import date

_pinned: date | None = None


def today() -> date:
    """The current date, or the pinned one when a process has fixed it."""
    return _pinned if _pinned is not None else date.today()


def pin(day: date | None) -> None:
    """Fix what :func:`today` returns for this process; ``None`` restores the clock."""
    global _pinned
    _pinned = day


def pinned() -> date | None:
    """The pinned date, if any — for disclosure, never for arithmetic."""
    return _pinned


__all__ = ["pin", "pinned", "today"]
