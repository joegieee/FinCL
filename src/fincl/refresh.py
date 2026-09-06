"""Explicit ledger preparation for the MVP read surface (plan M1a / M1b §§2-3).

A query path opens the ledger read-only and reads whatever the last preparation
committed (`report.py`, and the future screen queries). Turning the journal and
the currently selected evidence into the SQLite projection the queries read is
this module's job, run as its own step: at startup, before a CLI query, or on an
explicit ``fincl refresh``.

The whole projection rebuild — journal replay, selection, automatic links,
merchant and category projections, and the generation token — runs inside **one**
SQLite transaction under the ledger writer lock. A failure rolls the batch back
whole, so a reader keeps seeing the previous generation and its matching
projection rather than a half-written one; a retry reruns deterministically.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from .db import connect_sqlite, initialize_schema
from .engine import categorize
from .generation import publish_generation
from .locking import ledger_writer
from .reconciliation import link_narrow_transfers


@dataclass(frozen=True, slots=True)
class RefreshResult:
    decisions_bound: int
    decisions_dangling: int
    confirmed_transfer_links: int
    categorization_run_id: str | None
    categorized_entries: int
    generation_token: str


class _BatchConnection:
    """A ``sqlite3.Connection`` view whose nested helpers cannot end the batch.

    ``commit`` / ``rollback`` are swallowed and ``with conn:`` becomes a
    savepoint, so every domain helper (``replay_journal``,
    ``refresh_automatic_transfer_links``, ``categorize`` …) keeps its
    roll-back-this-step semantics while the one real transaction is committed —
    or rolled back — exactly once by :func:`refresh_ledger`.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self._depth = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def commit(self) -> None:  # noqa: D401 - swallowed on purpose
        pass

    def rollback(self) -> None:
        pass

    def __enter__(self) -> _BatchConnection:
        self._real.execute(f"SAVEPOINT _refresh_{self._depth}")
        self._depth += 1
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        self._depth -= 1
        name = f"_refresh_{self._depth}"
        if exc_type is None:
            self._real.execute(f"RELEASE SAVEPOINT {name}")
        else:
            self._real.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._real.execute(f"RELEASE SAVEPOINT {name}")
        return False


def rebuild_projection(connection: sqlite3.Connection) -> tuple[str | None, str]:
    """Rebuild the whole projection and generation in one transaction.

    The caller owns ``connection`` and the ledger writer lock, and
    ``initialize_schema`` has already run. Returns ``(categorization_run_id,
    generation_token)``. On any failure the batch is rolled back whole and the
    exception propagates — the previous generation and its projection stay
    readable.
    """
    connection.isolation_level = None
    connection.execute("BEGIN IMMEDIATE")
    try:
        batch = cast(sqlite3.Connection, _BatchConnection(connection))
        link_narrow_transfers(batch)
        has_selected = connection.execute(
            "SELECT 1 FROM ledger_entries e JOIN extraction_runs x "
            "ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED' LIMIT 1"
        ).fetchone()
        run_id = categorize(batch) if has_selected is not None else None
        token = publish_generation(batch)
        connection.execute("COMMIT")
        return run_id, token
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _dangling_and_bound(connection: sqlite3.Connection) -> tuple[int, int]:
    bound, dangling = connection.execute(
        "SELECT (SELECT count(*) FROM link_decisions WHERE bound=1) + "
        "(SELECT count(*) FROM invariant_escapes WHERE bound=1) + "
        "(SELECT count(*) FROM inbound_classifications WHERE bound=1) + "
        "(SELECT count(*) FROM category_overrides WHERE bound=1), "
        "(SELECT count(*) FROM link_decisions WHERE bound=0) + "
        "(SELECT count(*) FROM invariant_escapes WHERE bound=0) + "
        "(SELECT count(*) FROM inbound_classifications WHERE bound=0) + "
        "(SELECT count(*) FROM category_overrides WHERE bound=0)"
    ).fetchone()
    return int(bound), int(dangling)


def refresh_ledger(database_path: Path) -> RefreshResult:
    """Rebuild the disposable projection the read surface serves, atomically."""
    with ledger_writer(database_path):
        connection = connect_sqlite(database_path)
        try:
            # Schema setup is not part of the refresh batch: executescript would
            # commit the transaction out from under it.
            initialize_schema(connection)

            run_id, token = rebuild_projection(connection)

            categorized = 0
            if run_id is not None:
                categorized = int(
                    connection.execute(
                        "SELECT count(*) FROM entry_categories"
                    ).fetchone()[0]
                )
            bound, dangling = _dangling_and_bound(connection)
            confirmed_links = int(
                connection.execute(
                    "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
                ).fetchone()[0]
            )
            return RefreshResult(
                decisions_bound=bound,
                decisions_dangling=dangling,
                confirmed_transfer_links=confirmed_links,
                categorization_run_id=run_id,
                categorized_entries=categorized,
                generation_token=token,
            )
        finally:
            connection.close()
