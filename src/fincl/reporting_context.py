"""The shared reporting request context every reported reader builds from
(§3.1, §6.2; plan M3b.1 step 03).

One :class:`ReportingContext` is built once, at a composite response's outer
edge, from an already-open connection — never rebuilt or re-resolved per
owner/adapter. That is what makes §2 invariant 10 ("one connection, one read
transaction, one generation") hold across a response that composes several
owners: every one of them receives this same object plus the same
connection, so two panels of one response cannot silently observe two
generations or two reporting policies.

This module owns none of the money arithmetic (that stays in ``valuation.py``)
and no route (``api_reporting.py`` calls it). It is the connective tissue
step 04+'s reported routes assemble around: build one context, thread it
through every adapter, translate its typed staleness refusal into HTTP 409.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from .db import database_path
from .decision_journal import default_journal_path, read_records
from .errors import FinCLError
from .generation import build_manifest, current_generation, generation_token
from .reporting_currency.queries import ReportingPolicy, reporting_policy

__all__ = [
    "STALE_GENERATION_REFRESH_REQUIRED",
    "ReportedScope",
    "ReportingContext",
    "build_reporting_context",
]

# The exact prefix ``api.py``'s existing write-path handlers already match on
# (``str(exc).startswith('STALE_GENERATION')``) to translate a raised
# ``FinCLError`` into ``HTTPException(409, ...)``. Reusing it here means a
# reported route's own translation is the same one-line pattern already used
# everywhere else in this codebase, not a second convention.
STALE_GENERATION_REFRESH_REQUIRED = "STALE_GENERATION_REFRESH_REQUIRED"


@dataclass(frozen=True, slots=True)
class ReportedScope:
    """§6.2's resolved scope: explicit dates and record filters, never inferred.

    ``valuation_as_of`` is mandatory (§2 invariant 7: no reader in the money
    -flow call graph consults the wall clock; a route resolves "today" at its
    own outer edge and passes it in, exactly like step 02's
    ``dated_reporting_coverage``). ``history_start``/``history_end`` are
    ``None`` where a surface has no history window at all (a single as-of
    snapshot). ``pocket_currency`` is ``None`` unless the caller deliberately
    narrowed to one Account-currency filter — reporting currency and record
    scope stay independent (§1 invariant 2); this field is never inferred
    from the resolved reporting currency. ``filters`` carries whatever
    additional narrowing a specific surface defines (accounts, categories,
    merchant, ...): its meaning is owned by that surface, not by this shared
    context, so it stays a plain opaque mapping here.
    """

    valuation_as_of: date
    history_start: str | None = None
    history_end: str | None = None
    pocket_currency: str | None = None
    filters: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReportingContext:
    """One pinned generation/policy/scope for a whole composite response (§6.2).

    Built once by :func:`build_reporting_context` from a connection the
    caller opened and will close; this object never opens or closes anything
    itself, and carries no reference the caller does not already own.
    """

    connection: sqlite3.Connection
    generation_token: str
    rate_inventory_digest: str
    policy: ReportingPolicy
    scope: ReportedScope

    @property
    def reporting_currency(self) -> str:
        return self.policy.currency


def _generation_is_stale(connection: sqlite3.Connection, *, token: str, manifest: dict[str, object]) -> bool:
    """Whether the connection's own snapshot no longer matches the published
    generation it claims (§6.2 composite protocol step 2, §2 invariant 11).

    Two independent checks, both against this same connection's snapshot,
    matching the definition ``analysis_page_from`` already established for
    the native seam (this module duplicates that definition rather than
    importing it, since the native reader's *soft* "freshness: stale" flag
    and this context's *hard* refusal are deliberately different behaviors
    built on the same check, not one behavior two callers share):

    1. Recomputing the manifest right now from this same snapshot must
       reproduce the same token the generation row claims — otherwise some
       manifest input (evidence, FX rows, policy, categorization run, ...)
       has changed since the last ``fincl refresh``.
    2. The on-disk decision journal's own last record hash must still match
       what that manifest recorded as ``journal_head`` — guarding against a
       journal file that has moved independently of what got replayed into
       this database.
    """
    if generation_token(build_manifest(connection)) != token:
        return True
    records = read_records(default_journal_path(database_path(connection)))
    last_hash = records[-1].record_hash if records else "0" * 64
    return last_hash != manifest["journal_head"]


def build_reporting_context(
    connection: sqlite3.Connection, *, scope: ReportedScope
) -> ReportingContext:
    """Pin one generation/policy for ``scope`` (§6.2 composite protocol steps 1-3).

    The caller owns ``connection``: opening it, starting its read transaction
    before or immediately after this call, and closing it in ``finally``. This
    function only reads through it — it begins a transaction if none is
    already open, but never writes, and returns before any owner/adapter is
    invoked, so a stale refusal never dirties the connection with partial
    reported work.

    Raises a typed ``FinCLError`` whose message starts with
    ``STALE_GENERATION_REFRESH_REQUIRED`` when :func:`_generation_is_stale`
    is true — the caller's route translates that into HTTP 409 with no
    reported total composed (§6.2: "no newly composed reported totals").
    This is a hard refusal, unlike the legacy ``analysis_page_from``'s soft
    ``freshness: 'stale'`` flag which still serves a (labelled) response: a
    *new* reported route must never mix newly resolved FX with a manifest it
    cannot currently reproduce (§2 invariant 11).
    """
    if not connection.in_transaction:
        connection.execute("BEGIN")
    generation = current_generation(connection)
    if generation is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    token = str(generation["generation_token"])
    manifest = json.loads(generation["manifest_json"])
    if _generation_is_stale(connection, token=token, manifest=manifest):
        raise FinCLError(
            f"{STALE_GENERATION_REFRESH_REQUIRED}: the ledger has changed since the last "
            "refresh; run `fincl refresh` before reading this reported view."
        )
    policy = reporting_policy(connection)
    return ReportingContext(
        connection=connection,
        generation_token=token,
        rate_inventory_digest=str(manifest["fx_row_inventory_digest"]),
        policy=policy,
        scope=scope,
    )
