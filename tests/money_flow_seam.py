"""Shared builders for the M3a snapshot-safe reader seam (§9.1, §5.5).

Named once and built once, per §9.1: the connection-counting and
``concurrent_refresh`` checks in ``tests/test_money_flow_seam.py`` both need a
real ledger reachable through the money-flow call graph, and the
``concurrent_refresh`` shape additionally needs a way to publish a *second*,
distinct generation without touching ledger evidence.
"""

from __future__ import annotations

import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fincl.db import connect_sqlite
from fincl.document_service import DocumentImportService
from fincl.fx import load_fx_rates
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

# Seed top-up, one grocery spend, one rent payment: SGD 900.00 -> 860.00 -> 360.00.
ROWS: list[list[str]] = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-02-05 10:00:00", "2026-02-05 10:00:01",
     "Groceries", "-40.00", "0.00", "SGD", "COMPLETED", "860.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "360.00"],
]


def build_ledger(rows: list[list[str]] = ROWS) -> Path:
    """A real ledger through the Revolut import path and ``refresh_ledger``."""
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def publish_new_generation(database: Path) -> str:
    """Change one manifest input and republish — a distinct generation.

    Loading one new FX rate row moves ``fx_row_inventory_digest`` (§4,
    build_manifest) without touching any ledger evidence or reconciliation
    state, so this is the cheapest real state change that is guaranteed to
    produce a generation token different from the one ``build_ledger``
    published — the same shape as "a rate import" in §5.6, and exactly the
    kind of write a concurrent refresh represents for the ``concurrent_refresh``
    fixture (§9.1).
    """
    connection = connect_sqlite(database)
    try:
        load_fx_rates(
            connection,
            {
                "source": "money-flow-seam-fixture",
                "source_version": f"seam-fixture-{uuid.uuid4().hex}",
                "quote_currency": "SGD",
                "rates": [{"base_currency": "USD", "period": "2026-01", "rate": "1.3000"}],
            },
        )
    finally:
        connection.close()
    return refresh_ledger(database).generation_token


@contextmanager
def count_sqlite_connections() -> Iterator[dict[str, int]]:
    """Count every ``sqlite3.connect`` call made inside the ``with`` block.

    Patches the ``sqlite3`` module's own ``connect`` attribute rather than
    ``fincl.db.connect_readonly``: every reader module did ``import sqlite3``
    and calls ``sqlite3.connect`` through that live module reference at call
    time, so a patch on the module attribute is visible to all of them —
    unlike ``fincl.db.connect_readonly``, which each module bound to its own
    name via ``from ..db import connect_readonly`` at import time.
    """
    counter = {"n": 0}
    real_connect = sqlite3.connect

    def counting_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        counter["n"] += 1
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    with patch("sqlite3.connect", side_effect=counting_connect):
        yield counter
