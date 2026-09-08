"""Analytical selection of one extraction run per evidence blob (Confirmed 30).

``extraction_runs`` is unique on ``(evidence, profile, parser, policy)`` and a
``natural_anchor`` is unique only *within* a run. Nothing demoted a superseded
run, so a parser-version bump could re-extract one evidence blob into two
``ACCEPTED`` runs and leave two ledger entries sharing one anchor in the
analytical set — double-counting totals and letting a ``CATEGORY_OVERRIDE`` bind
to one twin arbitrarily.

The fix: exactly one ``ACCEPTED`` run per evidence blob is ``SELECTED`` — the one
whose ``(source_profile_version, parser_version, extraction_policy_version)``
triple is one the *current* code can still emit. Runs at superseded versions are
retained as history but marked ``SUPERSEDED``; everything non-analytical (a
``QUARANTINED`` / ``FAILED`` / ``RUNNING`` run) is ``QUARANTINED``. A partial
unique index on ``extraction_runs(evidence_sha256) WHERE selection_state =
'SELECTED'`` holds the one-per-blob invariant in the schema.
"""

from __future__ import annotations

import sqlite3

from .documents import (
    DBS_PROFILE_VERSION,
    MARI_CARD_PROFILE_MINUS_I,
    MARI_CARD_PROFILE_PLUS_I,
    MARI_SAVINGS_3COL_PROFILE,
    MARI_SAVINGS_4COL_PROFILE,
    PAYLAH_NEW_PROFILE_VERSION,
    PAYLAH_OLD_PROFILE_VERSION,
    PDF_EXTRACTION_POLICY_VERSION,
    PDF_PARSER_VERSION,
    TRUST_SAVINGS_PROFILE,
)
from .revolut import (
    EXTRACTION_POLICY_VERSION as REVOLUT_POLICY_VERSION,
)
from .revolut import (
    PARSER_VERSION as REVOLUT_PARSER_VERSION,
)
from .revolut import (
    SOURCE_PROFILE_VERSION as REVOLUT_PROFILE_VERSION,
)

# Every ``(source_profile_version, parser_version, extraction_policy_version)``
# triple the current importers can produce. A run whose triple is not in here was
# written by superseded code: it stays as history but is demoted out of the
# analytical set. Content-variant profiles (PayLah legacy vs. account-summary,
# the two Mari-card equations, the two Mari-savings interest layouts) are all
# current — the version history axis is the parser/policy, not the content
# variant.
_PDF_PROFILES = (
    DBS_PROFILE_VERSION,
    PAYLAH_OLD_PROFILE_VERSION,
    PAYLAH_NEW_PROFILE_VERSION,
    MARI_SAVINGS_3COL_PROFILE,
    MARI_SAVINGS_4COL_PROFILE,
    MARI_CARD_PROFILE_MINUS_I,
    MARI_CARD_PROFILE_PLUS_I,
    TRUST_SAVINGS_PROFILE,
)
CURRENT_EXTRACTION_TRIPLES: frozenset[tuple[str, str, str]] = frozenset(
    {(REVOLUT_PROFILE_VERSION, REVOLUT_PARSER_VERSION, REVOLUT_POLICY_VERSION)}
    | {
        (profile, PDF_PARSER_VERSION, PDF_EXTRACTION_POLICY_VERSION)
        for profile in _PDF_PROFILES
    }
)


def is_current_triple(profile: str, parser: str, policy: str) -> bool:
    return (profile, parser, policy) in CURRENT_EXTRACTION_TRIPLES


def recompute_selection(connection: sqlite3.Connection) -> None:
    """Set ``extraction_runs.selection_state`` for every run.

    Idempotent and independent of import order — a pure function of the runs on
    disk plus the current code constants, so a rebuild reproduces it (§3.6).
    """
    runs = connection.execute(
        """
        SELECT id, evidence_sha256, status, source_profile_version,
               parser_version, extraction_policy_version
        FROM extraction_runs
        ORDER BY id
        """
    ).fetchall()
    selected_ids: set[str] = set()
    chosen_per_blob: dict[str, str] = {}
    for run in runs:
        if run["status"] != "ACCEPTED":
            continue
        if not is_current_triple(
            run["source_profile_version"],
            run["parser_version"],
            run["extraction_policy_version"],
        ):
            continue
        blob = run["evidence_sha256"]
        # Deterministic tiebreak: the lowest id wins if the same blob somehow has
        # two current ACCEPTED runs. In practice re-extraction is idempotent
        # (identical triple -> identical id), so this never fires.
        if blob not in chosen_per_blob or run["id"] < chosen_per_blob[blob]:
            chosen_per_blob[blob] = run["id"]
    selected_ids = set(chosen_per_blob.values())

    with connection:
        # Two passes so the partial unique index is never transiently violated:
        # first demote everything, then promote exactly one run per blob.
        for run in runs:
            if run["id"] in selected_ids:
                state = "SUPERSEDED" if run["status"] == "ACCEPTED" else "QUARANTINED"
            elif run["status"] == "ACCEPTED":
                state = "SUPERSEDED"
            else:
                state = "QUARANTINED"
            connection.execute(
                "UPDATE extraction_runs SET selection_state = ? WHERE id = ?",
                (state, run["id"]),
            )
        for run_id in sorted(selected_ids):
            connection.execute(
                "UPDATE extraction_runs SET selection_state = 'SELECTED' WHERE id = ?",
                (run_id,),
            )


def needs_reextraction(connection: sqlite3.Connection) -> list[dict[str, str]]:
    """Evidence blobs whose only ``ACCEPTED`` runs are at superseded versions.

    Surfaced in ``coverage`` so a stale-version blob is neither silently included
    nor silently dropped (§8.8).
    """
    rows = connection.execute(
        """
        SELECT s.sha256, s.original_path,
               group_concat(DISTINCT x.source_profile_version) AS profiles
        FROM source_content s
        JOIN extraction_runs x ON x.evidence_sha256 = s.sha256
        WHERE x.status = 'ACCEPTED'
        GROUP BY s.sha256
        HAVING sum(x.selection_state = 'SELECTED') = 0
        ORDER BY s.original_path
        """
    ).fetchall()
    return [
        {
            "evidence_sha256": row["sha256"],
            "original_path": row["original_path"],
            "profiles": row["profiles"] or "",
        }
        for row in rows
    ]
