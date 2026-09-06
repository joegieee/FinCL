"""Explicit ledger preparation for the MVP read surface (plan M1a / M1b §3).

A query path opens the ledger read-only and reads whatever the last preparation
committed (`report.py`, and the future screen queries). Turning the journal and
the currently selected evidence into the SQLite projection the queries read is
this module's job, run as its own step: at startup, before a CLI query, or on an
explicit ``fincl refresh``.

Preparation ends by publishing the ``projection_generation`` row (M1b §3): the
generation token is stable across a no-op refresh and changes when any input
does. The bounded writer lock and command receipts are the rest of M1b.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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


def _dangling_and_bound(connection) -> tuple[int, int]:
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
    """Rebuild the disposable projection the read surface serves.

    Under the one ledger writer lock: initialize the schema, replay the decision
    journal, recompute extraction selection, refresh the automatic transfer
    links, recompute the categorization projection, then publish the generation
    token. Returns a small summary of the committed state.
    """
    with ledger_writer(database_path):
        connection = connect_sqlite(database_path)
        try:
            initialize_schema(connection)
            # link_narrow_transfers replays the journal and rebuilds the automatic
            # link projection; categorize replays again, recomputes selection and
            # merchant observations, then the entry categories.
            link_narrow_transfers(connection)
            has_selected = connection.execute(
                "SELECT 1 FROM ledger_entries e JOIN extraction_runs x "
                "ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED' LIMIT 1"
            ).fetchone()
            run_id: str | None = None
            categorized = 0
            if has_selected is not None:
                run_id = categorize(connection)
                categorized = int(
                    connection.execute(
                        "SELECT count(*) FROM entry_categories"
                    ).fetchone()[0]
                )
            token = publish_generation(connection)
            connection.commit()
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
