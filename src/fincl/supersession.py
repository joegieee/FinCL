"""One statement's rows already carried by another statement of the same account.

A re-download with different bytes, or a mid-month export inside the month's
statement, is the same evidence again. Exactly one copy is selected and counted;
the other stays as evidence, marked with a ``STATEMENT_SUPERSEDED`` warning
naming the statement that carries its rows.

Decisions follow the rows, not the file. A decision anchored on a superseded
statement's row is replayed against the matching row of the statement that
carries it (:func:`anchor_redirects`). The match is by pocket, day, amount and
description, paired in statement order, so it is the same on every replay and
whichever copy was imported first. Categorising a mid-month export and then
importing the month-end statement therefore keeps the category.

:func:`resolve_statement_overlaps` runs on every refresh. It finds overlaps
already in a ledger, re-selects a statement whose superseding statement is gone,
and settles an old same-statement conflict whose copies have identical rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any

from .common import canonical_json as _json
from .common import utc_now as _now
from .decision_journal import JournalRecord

__all__ = [
    "SUPERSEDED",
    "anchor_redirects",
    "containing_statement",
    "redirect_record",
    "resolve_statement_overlaps",
    "supersede_contained",
    "supersession_of",
]

SUPERSEDED = "STATEMENT_SUPERSEDED"

_Row = tuple[str, str, str, str]


def _rows(
    connection: sqlite3.Connection, statement_id: str, start: str, end: str
) -> list[tuple[_Row, str]]:
    """``(comparable row, natural anchor)`` for the statement's movements in
    ``start..end``, in statement order."""
    return [
        ((row[0], row[1], row[2], row[3]), row[4])
        for row in connection.execute(
            """
            SELECT e.currency_pocket, substr(e.effective_at, 1, 10), e.amount_text,
                   coalesce(d.description_raw, ''), e.natural_anchor
            FROM ledger_entries e
            LEFT JOIN document_records d ON d.id = e.document_record_id
            WHERE e.statement_id = ? AND substr(e.effective_at, 1, 10) BETWEEN ? AND ?
            ORDER BY e.effective_at, d.extraction_ordinal, e.id
            """,
            (statement_id, start, end),
        )
    ]


def _carries(
    connection: sqlite3.Connection, container: str, contained: str, start: str, end: str
) -> bool:
    """Whether ``container`` holds exactly ``contained``'s rows over its period."""
    return Counter(row for row, _ in _rows(connection, container, start, end)) == Counter(
        row for row, _ in _rows(connection, contained, start, end)
    )


def _statement(connection: sqlite3.Connection, statement_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, account_id, evidence_sha256, extraction_run_id, period_start, period_end "
        "FROM statements WHERE id = ?",
        (statement_id,),
    ).fetchone()


def _selected_of_account(
    connection: sqlite3.Connection, account_id: str, excluding_sha: str
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT s.id, s.extraction_run_id, s.period_start, s.period_end
        FROM statements s JOIN extraction_runs x ON x.id = s.extraction_run_id
        WHERE s.account_id = ? AND s.evidence_sha256 <> ? AND s.selection_state = 'SELECTED'
          AND x.status = 'ACCEPTED'
          AND s.period_start IS NOT NULL AND s.period_end IS NOT NULL
        ORDER BY s.period_start, s.period_end, s.id
        """,
        (account_id, excluding_sha),
    ).fetchall()


def _period(row: sqlite3.Row) -> str:
    return f"{row['period_start']}..{row['period_end']}"


def _mark_superseded(
    connection: sqlite3.Connection, statement: sqlite3.Row, by: sqlite3.Row, message: str
) -> None:
    from .reconciliation import SELECTION_LOST, revoke_links_for_extraction

    connection.execute(
        "UPDATE statements SET selection_state = 'CANDIDATE', conflict_state = 'NONE' "
        "WHERE id = ?",
        (statement["id"],),
    )
    revoke_links_for_extraction(connection, statement["extraction_run_id"], SELECTION_LOST)
    connection.execute(
        "UPDATE ledger_entries SET analytics_eligible = 0 WHERE statement_id = ?",
        (statement["id"],),
    )
    connection.execute(
        """
        INSERT INTO extraction_issues(
            extraction_run_id, raw_record_id, severity, code, message, context_json, created_at
        ) VALUES (?, NULL, 'WARNING', ?, ?, ?, ?)
        """,
        (
            statement["extraction_run_id"], SUPERSEDED, message,
            _json({"superseded_by": by["id"], "period": _period(by)}), _now(),
        ),
    )


def containing_statement(
    connection: sqlite3.Connection, statement_id: str
) -> sqlite3.Row | None:
    """A selected statement of the same account that carries every row of this one."""
    this = _statement(connection, statement_id)
    if this is None or this["period_start"] is None or this["period_end"] is None:
        return None
    start, end = this["period_start"], this["period_end"]
    for other in _selected_of_account(connection, this["account_id"], this["evidence_sha256"]):
        if other["period_start"] <= start and other["period_end"] >= end and _carries(
            connection, other["id"], statement_id, start, end
        ):
            return other
    return None


def supersede(connection: sqlite3.Connection, statement_id: str, by: sqlite3.Row) -> None:
    """Stop counting a just-imported statement whose rows ``by`` already carries."""
    this = _statement(connection, statement_id)
    assert this is not None
    _mark_superseded(
        connection, this, by,
        "an already imported statement for this account carries every row of this one",
    )


def supersede_contained(connection: sqlite3.Connection, statement_id: str) -> None:
    """Stop counting selected statements whose rows this one fully carries."""
    this = _statement(connection, statement_id)
    if this is None or this["period_start"] is None or this["period_end"] is None:
        return
    for other in _selected_of_account(connection, this["account_id"], this["evidence_sha256"]):
        start, end = other["period_start"], other["period_end"]
        if not (this["period_start"] <= start and this["period_end"] >= end):
            continue
        if (start, end) == (this["period_start"], this["period_end"]):
            continue  # an equal period is the newcomer's to give way
        if not _carries(connection, statement_id, other["id"], start, end):
            continue
        _mark_superseded(
            connection, other, this,
            "a later statement for this account carries every row of this one",
        )


def _latest_supersession(connection: sqlite3.Connection) -> dict[str, str]:
    """Superseded statement id -> the statement its latest warning names."""
    result: dict[str, str] = {}
    for row in connection.execute(
        f"""
        SELECT s.id, q.context_json
        FROM statements s
        JOIN extraction_issues q ON q.extraction_run_id = s.extraction_run_id AND q.code = '{SUPERSEDED}'
        WHERE s.selection_state = 'CANDIDATE' AND s.conflict_state = 'NONE'
        ORDER BY q.id
        """
    ):
        result[row[0]] = str(json.loads(row[1])["superseded_by"])
    return result


def supersession_of(
    connection: sqlite3.Connection, extraction_run_id: str
) -> tuple[str | None, tuple[str, ...]]:
    """(the period that supersedes this import's statement, periods it supersedes)."""
    latest = _latest_supersession(connection)
    mine = [row[0] for row in connection.execute(
        "SELECT id FROM statements WHERE extraction_run_id = ?", (extraction_run_id,)
    )]
    by = None
    for statement_id in mine:
        if statement_id in latest:
            other = _statement(connection, latest[statement_id])
            by = _period(other) if other is not None else None
    replaced = sorted({
        _period(row) for statement_id, target in latest.items() if target in mine
        if (row := _statement(connection, statement_id)) is not None
    })
    return by, tuple(replaced)


def anchor_redirects(connection: sqlite3.Connection) -> dict[str, str]:
    """Superseded row anchor -> the matching row anchor of its carrying statement.

    Only a carrying statement that is itself selected is followed, so a
    decision never lands on a row that does not count.
    """
    redirects: dict[str, str] = {}
    for statement_id, target_id in _latest_supersession(connection).items():
        target = connection.execute(
            "SELECT id FROM statements WHERE id = ? AND selection_state = 'SELECTED'",
            (target_id,),
        ).fetchone()
        this = _statement(connection, statement_id)
        if target is None or this is None or this["period_start"] is None:
            continue
        start, end = this["period_start"], this["period_end"]
        available: dict[_Row, list[str]] = defaultdict(list)
        for row, anchor in _rows(connection, target_id, start, end):
            available[row].append(anchor)
        for row, anchor in _rows(connection, statement_id, start, end):
            if available[row]:
                redirects[anchor] = available[row].pop(0)
    return redirects


def _redirect_value(value: Any, redirects: dict[str, str]) -> Any:
    if isinstance(value, str):
        return redirects.get(value, value)
    if isinstance(value, list):
        return [_redirect_value(item, redirects) for item in value]
    if isinstance(value, dict):
        return {key: _redirect_value(item, redirects) for key, item in value.items()}
    return value


def redirect_record(record: JournalRecord, redirects: dict[str, str]) -> JournalRecord:
    """The record as projected: every superseded row anchor it names, in its
    anchors or anywhere in its payload, replaced by the carrying row's."""
    if not redirects:
        return record
    anchors = _redirect_value(record.anchors, redirects)
    payload = _redirect_value(record.payload, redirects)
    if anchors == record.anchors and payload == record.payload:
        return record
    return replace(record, anchors=anchors, payload=payload)


def _restore(connection: sqlite3.Connection, statement_id: str) -> None:
    connection.execute(
        "UPDATE statements SET selection_state = 'SELECTED', conflict_state = 'NONE' WHERE id = ?",
        (statement_id,),
    )
    connection.execute(
        """
        UPDATE ledger_entries SET analytics_eligible = 1
        WHERE statement_id = ? AND source_family = 'DOCUMENT'
          AND extraction_run_id IN (
              SELECT id FROM extraction_runs WHERE status = 'ACCEPTED' AND selection_state = 'SELECTED'
          )
        """,
        (statement_id,),
    )


def resolve_statement_overlaps(connection: sqlite3.Connection) -> None:
    """Bring an existing ledger's statement selection in line with supersession.

    Idempotent: a ledger already in line is left exactly as it is.
    """
    # A statement whose carrying statement is gone or no longer counted counts
    # again, until something else carries it.
    for statement_id, target_id in _latest_supersession(connection).items():
        selected = connection.execute(
            "SELECT 1 FROM statements WHERE id = ? AND selection_state = 'SELECTED'",
            (target_id,),
        ).fetchone()
        if selected is None:
            _restore(connection, statement_id)

    # An old same-statement conflict whose copies have identical rows was a
    # re-download: the earliest imported copy counts.
    groups = connection.execute(
        """
        SELECT statement_identity FROM statements
        WHERE conflict_state = 'CONFLICT' GROUP BY statement_identity
        """
    ).fetchall()
    for (identity,) in groups:
        copies = connection.execute(
            """
            SELECT s.id, s.period_start, s.period_end, x.status,
                   (SELECT min(i.started_at) FROM import_runs i
                     WHERE i.evidence_sha256 = s.evidence_sha256) AS imported_at
            FROM statements s JOIN extraction_runs x ON x.id = s.extraction_run_id
            WHERE s.statement_identity = ?
            ORDER BY imported_at, s.evidence_sha256
            """,
            (identity,),
        ).fetchall()
        first = copies[0]
        if first["status"] != "ACCEPTED" or first["period_start"] is None:
            continue
        start, end = first["period_start"], first["period_end"]
        if all(
            (copy["period_start"], copy["period_end"]) == (start, end)
            and _carries(connection, first["id"], copy["id"], start, end)
            for copy in copies[1:]
        ):
            _restore(connection, first["id"])
            by = _statement(connection, first["id"])
            assert by is not None
            for copy in copies[1:]:
                other = _statement(connection, copy["id"])
                assert other is not None
                _mark_superseded(
                    connection, other, by,
                    "an already imported statement for this account carries every row of this one",
                )

    # Overlaps imported before supersession existed: the statement that carries
    # another's rows counts, the carried one gives way.
    selected = connection.execute(
        """
        SELECT s.id FROM statements s JOIN extraction_runs x ON x.id = s.extraction_run_id
        WHERE s.selection_state = 'SELECTED' AND x.status = 'ACCEPTED'
          AND s.period_start IS NOT NULL AND s.period_end IS NOT NULL
        ORDER BY s.period_end DESC, s.period_start, s.id
        """
    ).fetchall()
    for (statement_id,) in selected:
        still = connection.execute(
            "SELECT 1 FROM statements WHERE id = ? AND selection_state = 'SELECTED'",
            (statement_id,),
        ).fetchone()
        if still is None:
            continue
        carrier = containing_statement(connection, statement_id)
        if carrier is not None:
            this = _statement(connection, statement_id)
            assert this is not None
            _mark_superseded(
                connection, this, carrier,
                "another statement for this account carries every row of this one",
            )
