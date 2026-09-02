"""Replay the originating decision journal into disposable SQLite projections."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import NamedTuple

from .categorization import (
    CATEGORIZATION_DECISION_KINDS,
    CATEGORY_OVERRIDE,
    CATEGORY_RULE_RETIRE,
    CATEGORY_RULE_SET,
    MERCHANT_ALIAS_RETIRE,
    MERCHANT_ALIAS_SET,
    canonical_match,
    validate_alias_patterns,
    validate_payload,
)
from .common import canonical_json, resolve_category
from .decision_journal import JournalRecord, default_journal_path, read_records
from .errors import FinCLError

NATURAL_ANCHOR_VERSION = "natural-anchor-v1"

# The decision-journal kind vocabulary is closed (Confirmed 32, ARCHITECTURE.md
# §14.2). Replay refuses an ``decision_kind`` it does not recognize rather than
# skipping it: a silent skip would let this build read a journal written by a
# newer FinCL, drop every record of a kind it has not learned yet, and report a
# confident but different projection — the §3.4 silent-wrong-number failure. Add
# a kind here in the same change that teaches ``_apply_record`` to project it.
# ``EXTRACTION_REPAIR`` is recognized but deliberately has no projection: its
# effect is applied directly by ``recovery.py`` and it stays in the journal only
# as an audit record, so replay skips it rather than refusing it.
KNOWN_DECISION_KINDS = frozenset(
    {
        "LINK_CONFIRM",
        "LINK_REJECT",
        "INVARIANT_ESCAPE",
        "INBOUND_CLASSIFICATION",
        "EXTRACTION_REPAIR",
    }
    | CATEGORIZATION_DECISION_KINDS
)

# Anchor recipes that this build once wrote but has since superseded. A record
# written under one of these is legitimately stale: it is projected as dangling
# with a reason so the §14.3 rebind migration can find and re-adjudicate it,
# never refused. Only ``natural-anchor-v1`` has ever existed, so this is empty;
# the first anchor-recipe change moves the old version string in here.
SUPERSEDED_ANCHOR_VERSIONS: frozenset[str] = frozenset()


def journal_path_for(connection: sqlite3.Connection) -> Path:
    row = connection.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        raise FinCLError("cannot determine the SQLite database path for decision replay")
    return default_journal_path(Path(row[2]))


def entry_anchor(connection: sqlite3.Connection, entry_id: str) -> str:
    row = connection.execute(
        "SELECT natural_anchor FROM ledger_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise FinCLError("entry does not resolve to a natural anchor")
    return str(row[0])


class _AnchorResolution(NamedTuple):
    entry_id: str | None
    dangling_reason: str | None  # set only when entry_id is None


def _resolve(connection: sqlite3.Connection, anchor: str | None) -> _AnchorResolution:
    """Resolve a ``natural_anchor`` to the one entry a decision binds to.

    A ``natural_anchor`` is unique only within an extraction run (Confirmed 30),
    so a blob with a ``SELECTED`` run and a retained ``SUPERSEDED`` run carries
    the anchor twice. The ``SELECTED`` entry wins — it is the analytically real
    one an override must bind to. Only when no ``SELECTED`` entry exists (a run
    still ``QUARANTINED`` awaiting an invariant escape, or ``FAILED``) does the
    lookup fall back to the other runs. More than one candidate at the winning
    level dangles with a distinct reason rather than an arbitrary
    ``ORDER BY id LIMIT 1`` pick, which would let an override bind to one of two
    identical twins.
    """
    if not anchor:
        return _AnchorResolution(None, "decision carries no anchor")
    rows = connection.execute(
        """
        SELECT e.id, x.selection_state FROM ledger_entries e
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE e.natural_anchor = ?
        ORDER BY e.id
        """,
        (anchor,),
    ).fetchall()
    if not rows:
        return _AnchorResolution(None, "anchor no longer resolves")
    selected = [row for row in rows if row["selection_state"] == "SELECTED"]
    pool = selected or rows
    if len(pool) > 1:
        return _AnchorResolution(None, "anchor resolves to multiple entries")
    return _AnchorResolution(str(pool[0][0]), None)


def resolve_anchor(connection: sqlite3.Connection, anchor: str) -> str | None:
    """The selected entry an anchor binds to, or ``None`` when it does not bind.

    An anchor that resolves to more than one selected entry returns ``None``
    here too — a caller that needs the distinct reason uses ``_resolve``.
    """
    return _resolve(connection, anchor).entry_id


def invariant_violation_is_live(
    connection: sqlite3.Connection, code: str, entry_ids: list[str]
) -> bool:
    """True when ``code`` is a currently-raised BLOCKING issue on one of these entries.

    An ``INVARIANT_ESCAPE`` may only downgrade a check that is failing right now
    (ARCHITECTURE.md §17.1). A violation is anchored through the offending raw
    record, so the escape's entry anchors must map back to a raw record that
    carries a live ``BLOCKING`` ``extraction_issues`` row for that exact code.
    """
    if not entry_ids:
        return False
    entry_placeholders = ",".join("?" for _ in entry_ids)
    record_ids = [
        row[0]
        for row in connection.execute(
            f"SELECT raw_record_id FROM ledger_entries "
            f"WHERE id IN ({entry_placeholders}) AND raw_record_id IS NOT NULL",
            tuple(entry_ids),
        )
    ]
    if not record_ids:
        return False
    record_placeholders = ",".join("?" for _ in record_ids)
    hit = connection.execute(
        f"SELECT 1 FROM extraction_issues "
        f"WHERE code = ? AND severity = 'BLOCKING' "
        f"AND raw_record_id IN ({record_placeholders}) LIMIT 1",
        (code, *record_ids),
    ).fetchone()
    return hit is not None


def replay_journal(connection: sqlite3.Connection, journal_path: Path | None = None) -> tuple[int, int]:
    """Verify and project the complete journal; return bound and dangling counts."""
    records = read_records(journal_path or journal_path_for(connection))
    with connection:
        for table in (
            "link_decisions",
            "invariant_escapes",
            "inbound_classifications",
            "merchant_aliases",
            "category_rules",
            "category_overrides",
        ):
            connection.execute(f"DELETE FROM {table}")
        for record in records:
            _apply_record(connection, record)
        connection.execute("UPDATE decision_watermark SET applied_seq = ? WHERE id = 1", (len(records),))
    bound = connection.execute(
        "SELECT (SELECT count(*) FROM link_decisions WHERE bound=1) + "
        "(SELECT count(*) FROM invariant_escapes WHERE bound=1) + "
        "(SELECT count(*) FROM inbound_classifications WHERE bound=1) + "
        "(SELECT count(*) FROM category_overrides WHERE bound=1)"
    ).fetchone()[0]
    dangling = connection.execute(
        "SELECT (SELECT count(*) FROM link_decisions WHERE bound=0) + "
        "(SELECT count(*) FROM invariant_escapes WHERE bound=0) + "
        "(SELECT count(*) FROM inbound_classifications WHERE bound=0) + "
        "(SELECT count(*) FROM category_overrides WHERE bound=0)"
    ).fetchone()[0]
    return bound, dangling


def _apply_record(connection: sqlite3.Connection, record: JournalRecord) -> None:
    if record.decision_kind not in KNOWN_DECISION_KINDS:
        raise FinCLError(
            f"decision journal record {record.seq} has an unrecognized "
            f"decision_kind {record.decision_kind!r}; this build cannot replay it "
            "without silently dropping the decision — upgrade fincl"
        )
    override: str | None = None
    if record.anchor_version != NATURAL_ANCHOR_VERSION:
        if record.anchor_version not in SUPERSEDED_ANCHOR_VERSIONS:
            raise FinCLError(
                f"decision journal record {record.seq} was written under unknown "
                f"anchor_version {record.anchor_version!r}; this build cannot "
                "resolve its anchors — upgrade fincl"
            )
        # A known-older recipe: project the decision as dangling, carrying its
        # snapshot, so the §14.3 rebind migration can re-adjudicate it.
        override = f"anchor recipe {record.anchor_version} superseded; awaiting rebind"
    if record.decision_kind in CATEGORIZATION_DECISION_KINDS:
        try:
            validate_payload(record.decision_kind, record.anchors, record.payload)
        except FinCLError as exc:
            raise FinCLError(f"decision journal record {record.seq}: {exc}") from exc
    if record.decision_kind in {"LINK_CONFIRM", "LINK_REJECT"}:
        _apply_link(connection, record, override)
    elif record.decision_kind == "INVARIANT_ESCAPE":
        _apply_escape(connection, record, override)
    elif record.decision_kind == "INBOUND_CLASSIFICATION":
        _apply_inbound(connection, record, override)
    elif record.decision_kind == MERCHANT_ALIAS_SET:
        _apply_alias_set(connection, record)
    elif record.decision_kind == MERCHANT_ALIAS_RETIRE:
        connection.execute(
            "DELETE FROM merchant_aliases WHERE canonical = ?",
            (record.anchors["canonical"],),
        )
    elif record.decision_kind == CATEGORY_RULE_SET:
        _apply_rule_set(connection, record)
    elif record.decision_kind == CATEGORY_RULE_RETIRE:
        connection.execute(
            "DELETE FROM category_rules WHERE rule_id = ?",
            (record.anchors["rule_id"],),
        )
    elif record.decision_kind == CATEGORY_OVERRIDE:
        _apply_override(connection, record, override)


def _binding(reason: str | None, override: str | None) -> tuple[int, str | None]:
    """``(bound, dangling_reason)`` — ``reason`` is ``None`` when every anchor resolved."""
    if override is not None:
        return 0, override
    return (1, None) if reason is None else (0, reason)


def _apply_link(connection: sqlite3.Connection, record: JournalRecord, override: str | None) -> None:
    left, right = record.anchors.get("left"), record.anchors.get("right")
    left_res, right_res = _resolve(connection, left), _resolve(connection, right)
    reason = left_res.dangling_reason or right_res.dangling_reason
    bound, dangling_reason = _binding(reason, override)
    connection.execute(
        """INSERT INTO link_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(left_anchor,right_anchor,anchor_version,link_kind) DO UPDATE SET
           decision=excluded.decision, policy_version=excluded.policy_version, journal_seq=excluded.journal_seq,
           snapshot=excluded.snapshot, bound=excluded.bound, dangling_reason=excluded.dangling_reason""",
        (left or "", right or "", record.anchor_version,
         record.payload.get("link_kind", "SAME_CURRENCY_TRANSFER"),
         "CONFIRM" if record.decision_kind == "LINK_CONFIRM" else "REJECT", record.policy_version,
         record.seq, record.snapshot, bound, dangling_reason),
    )


def _apply_escape(connection: sqlite3.Connection, record: JournalRecord, override: str | None) -> None:
    anchors_json = json.dumps(record.anchors, sort_keys=True, separators=(",", ":"))
    resolutions = [_resolve(connection, anchor) for anchor in record.anchors.values()]
    reason = next((r.dangling_reason for r in resolutions if r.dangling_reason), None)
    bound, dangling_reason = _binding(reason, override)
    connection.execute(
        """INSERT INTO invariant_escapes VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(invariant_code,anchors_json,anchor_version) DO UPDATE SET policy_version=excluded.policy_version,
           journal_seq=excluded.journal_seq, snapshot=excluded.snapshot, bound=excluded.bound, dangling_reason=excluded.dangling_reason""",
        (record.payload.get("code", ""), anchors_json, record.anchor_version, record.policy_version,
         record.seq, record.snapshot, bound, dangling_reason),
    )


def _apply_inbound(connection: sqlite3.Connection, record: JournalRecord, override: str | None) -> None:
    anchor = record.anchors.get("entry", "")
    reason = _resolve(connection, anchor).dangling_reason
    bound, dangling_reason = _binding(reason, override)
    connection.execute(
        """INSERT INTO inbound_classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(anchor,anchor_version) DO UPDATE SET classification=excluded.classification,
           policy_version=excluded.policy_version, journal_seq=excluded.journal_seq, snapshot=excluded.snapshot,
           bound=excluded.bound, dangling_reason=excluded.dangling_reason""",
        (anchor, record.anchor_version, record.payload.get("classification", "other"), record.policy_version,
         record.seq, record.snapshot, bound, dangling_reason),
    )


def _apply_alias_set(connection: sqlite3.Connection, record: JournalRecord) -> None:
    patterns = validate_alias_patterns(record.payload["patterns"])
    connection.execute(
        """INSERT INTO merchant_aliases
               (canonical, patterns_json, display_name, reason, journal_seq, record_hash)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(canonical) DO UPDATE SET
               patterns_json=excluded.patterns_json, display_name=excluded.display_name,
               reason=excluded.reason, journal_seq=excluded.journal_seq,
               record_hash=excluded.record_hash""",
        (
            record.anchors["canonical"],
            canonical_json(patterns),
            record.payload.get("display_name"),
            record.payload["reason"],
            record.seq,
            record.record_hash,
        ),
    )


def _apply_rule_set(connection: sqlite3.Connection, record: JournalRecord) -> None:
    group, category, migrated_from = resolve_category(str(record.payload["category"]))
    connection.execute(
        """INSERT INTO category_rules
               (rule_id, match_json, group_name, category, migrated_from, reason,
                journal_seq, record_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(rule_id) DO UPDATE SET
               match_json=excluded.match_json, group_name=excluded.group_name,
               category=excluded.category, migrated_from=excluded.migrated_from,
               reason=excluded.reason, journal_seq=excluded.journal_seq,
               record_hash=excluded.record_hash""",
        (
            record.anchors["rule_id"],
            canonical_json(canonical_match(record.payload["match"])),
            group,
            category,
            migrated_from,
            record.payload["reason"],
            record.seq,
            record.record_hash,
        ),
    )


def _apply_override(connection: sqlite3.Connection, record: JournalRecord, override: str | None) -> None:
    anchor = record.anchors.get("entry", "")
    reason = _resolve(connection, anchor).dangling_reason
    bound, dangling_reason = _binding(reason, override)
    group, category, migrated_from = resolve_category(str(record.payload["category"]))
    connection.execute(
        """INSERT INTO category_overrides
               (entry_anchor, anchor_version, group_name, category, migrated_from,
                reason, journal_seq, record_hash, bound, dangling_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(entry_anchor) DO UPDATE SET
               anchor_version=excluded.anchor_version, group_name=excluded.group_name,
               category=excluded.category, migrated_from=excluded.migrated_from,
               reason=excluded.reason, journal_seq=excluded.journal_seq,
               record_hash=excluded.record_hash, bound=excluded.bound,
               dangling_reason=excluded.dangling_reason""",
        (
            anchor,
            record.anchor_version,
            group,
            category,
            migrated_from,
            record.payload["reason"],
            record.seq,
            record.record_hash,
            bound,
            dangling_reason,
        ),
    )
