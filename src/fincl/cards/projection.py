"""Fold the extraction-derived card rows into per-statement / per-card views.

No table is written here and no mutable balance is stored. Every figure is an
observed fact at statement-period end (`card_statement_facts`) or the printed
source equation and its pass/fail (`document_reconciliation_checks`). A
"recorded liability as of" view is offered only when a trusted opening value and
a complete run of required activity exist through the as-of date; otherwise the
caller shows the observed closing fact and the coverage gaps and does not roll
anything forward.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from .models import (
    CARD_SOURCE_TYPES,
    DATE_ROLES,
    ROLE_CLOSING_LIABILITY,
    ROLE_OPENING_LIABILITY,
    ROLE_PERIOD_END,
    ROLE_PERIOD_START,
    CardStatementFact,
    ReconciliationStatus,
)


def selected_card_accounts(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Accounts that are credit cards, newest statement first.

    An account qualifies if it has ever produced `card_statement_facts` rows or
    its `source_type` is a known card type. Ordered by account id for a stable
    keyset.
    """
    return connection.execute(
        """
        SELECT a.id, a.institution, a.source_type, a.display_name,
               a.masked_identifier
        FROM accounts a
        WHERE a.source_type IN ({placeholders})
           OR EXISTS (
               SELECT 1 FROM statements s
               JOIN card_statement_facts f ON f.statement_id = s.id
               WHERE s.account_id = a.id
           )
        ORDER BY a.id
        """.format(
            placeholders=",".join("?" for _ in CARD_SOURCE_TYPES) or "NULL"
        ),
        tuple(sorted(CARD_SOURCE_TYPES)),
    ).fetchall()


def selected_statements(
    connection: sqlite3.Connection, account_id: str
) -> list[sqlite3.Row]:
    """SELECTED statements for one card account, earliest period first."""
    return connection.execute(
        """
        SELECT s.id, s.extraction_run_id, s.statement_date, s.period_start,
               s.period_end, s.statement_identity, s.selection_state,
               s.conflict_state, s.evidence_sha256
        FROM statements s
        WHERE s.account_id = ? AND s.selection_state = 'SELECTED'
        ORDER BY COALESCE(s.period_start, s.statement_date), s.id
        """,
        (account_id,),
    ).fetchall()


def statement_facts(
    connection: sqlite3.Connection, statement_id: str
) -> dict[str, CardStatementFact]:
    """Every `card_statement_facts` row for one statement, keyed by field role.

    A single pocket per role is expected for the Mari card; a multi-pocket card
    keeps the last row read per role — callers that need per-pocket separation
    use :func:`statement_facts_by_pocket`.
    """
    facts: dict[str, CardStatementFact] = {}
    for fact in _facts(connection, statement_id):
        facts[fact.field_role] = fact
    return facts


def statement_facts_by_pocket(
    connection: sqlite3.Connection, statement_id: str
) -> dict[str, dict[str, CardStatementFact]]:
    pockets: dict[str, dict[str, CardStatementFact]] = {}
    for fact in _facts(connection, statement_id):
        pockets.setdefault(fact.currency_pocket, {})[fact.field_role] = fact
    return pockets


def _facts(
    connection: sqlite3.Connection, statement_id: str
) -> list[CardStatementFact]:
    rows = connection.execute(
        """
        SELECT field_role, currency_pocket, value_kind, value_text,
               value_coefficient, value_scale, source_locator, parser_version,
               role_version
        FROM card_statement_facts
        WHERE statement_id = ?
        ORDER BY currency_pocket, field_role
        """,
        (statement_id,),
    ).fetchall()
    return [
        CardStatementFact(
            field_role=str(row["field_role"]),
            currency_pocket=str(row["currency_pocket"]),
            value_kind=str(row["value_kind"]),
            value_text=str(row["value_text"]),
            value_coefficient=(
                int(row["value_coefficient"])
                if row["value_coefficient"] is not None
                else None
            ),
            value_scale=(
                int(row["value_scale"]) if row["value_scale"] is not None else None
            ),
            source_locator=str(row["source_locator"]),
            parser_version=str(row["parser_version"]),
            role_version=str(row["role_version"]),
        )
        for row in rows
    ]


def reconciliation(
    connection: sqlite3.Connection, extraction_run_id: str, pocket: str
) -> dict[str, Any]:
    """The printed source equation and its reconciliation status for a pocket.

    Folded from `document_reconciliation_checks`: the recognized equation text
    comes from the `SUMMARY_EQUATION` check's context, and the status is
    RECONCILED only when every blocking check for the pocket passed.
    """
    rows = connection.execute(
        """
        SELECT check_type, equation_version, passed, blocking, context_json,
               expected_text, actual_text, difference_text
        FROM document_reconciliation_checks
        WHERE extraction_run_id = ? AND currency_pocket = ?
        ORDER BY id
        """,
        (extraction_run_id, pocket),
    ).fetchall()

    equation: str | None = None
    checks: list[dict[str, Any]] = []
    blocking_total = 0
    blocking_passed = 0
    for row in rows:
        context = json.loads(row["context_json"]) if row["context_json"] else {}
        if row["check_type"] == "SUMMARY_EQUATION" and isinstance(
            context.get("equation"), str
        ):
            equation = context["equation"]
        if int(row["blocking"]):
            blocking_total += 1
            if row["passed"] == 1:
                blocking_passed += 1
        checks.append(
            {
                "check_type": str(row["check_type"]),
                "equation_version": str(row["equation_version"]),
                "passed": (None if row["passed"] is None else bool(row["passed"])),
                "blocking": bool(row["blocking"]),
                "expected": row["expected_text"],
                "actual": row["actual_text"],
                "difference": row["difference_text"],
                "context": context,
            }
        )

    if equation is None:
        status = ReconciliationStatus.NO_EQUATION
    elif blocking_total == 0:
        status = ReconciliationStatus.UNVERIFIED
    elif blocking_passed == blocking_total:
        status = ReconciliationStatus.RECONCILED
    else:
        status = ReconciliationStatus.FAILED

    return {
        "status": status,
        "printed_equation": equation,
        "blocking_total": blocking_total,
        "blocking_passed": blocking_passed,
        "checks": checks,
    }


def _fact_shape(fact: CardStatementFact) -> dict[str, Any]:
    shape: dict[str, Any] = {
        "role": fact.field_role,
        "kind": fact.value_kind,
        "value": fact.value_text,
        "currency": fact.currency_pocket if fact.is_money else None,
        "source_locator": fact.source_locator,
        "parser_version": fact.parser_version,
        "role_version": fact.role_version,
    }
    return shape


def statement_view(
    connection: sqlite3.Connection, account_id: str, statement: sqlite3.Row
) -> dict[str, Any]:
    """One observed statement: activity terms, due fields and reconciliation.

    Statement activity is presented as the issuer's own equation
    (opening liability + purchases + fees/interest − repayments − credits =
    closing liability) using the versioned card adapter's bucket signs, not
    merchant labels. Missing fields stay absent rather than zero.
    """
    pockets = statement_facts_by_pocket(connection, str(statement["id"]))
    if not pockets:
        pockets = {"SGD": {}}
    pocket_views: list[dict[str, Any]] = []
    for pocket, facts in sorted(pockets.items()):
        recon = reconciliation(
            connection, str(statement["extraction_run_id"]), pocket
        )
        closing = facts.get(ROLE_CLOSING_LIABILITY)
        opening = facts.get(ROLE_OPENING_LIABILITY)
        buckets = {
            role[len("summary_bucket_") :]: _fact_shape(fact)
            for role, fact in sorted(facts.items())
            if role.startswith("summary_bucket_")
        }
        pocket_views.append(
            {
                "currency": pocket,
                "opening_liability": _fact_shape(opening) if opening else None,
                "closing_liability": _fact_shape(closing) if closing else None,
                "summary_buckets": buckets,
                "due_fields": {
                    role: _fact_shape(facts[role])
                    for role in ("payment_due", "minimum_payment", "payment_due_date")
                    if role in facts
                },
                "credit_limit": (
                    _fact_shape(facts["credit_limit"])
                    if "credit_limit" in facts
                    else None
                ),
                "reconciliation": recon,
                "fields_present": sorted(facts),
            }
        )
    return {
        "statement_id": str(statement["id"]),
        "statement_identity": str(statement["statement_identity"]),
        "statement_date": str(statement["statement_date"]),
        "period_start": (
            str(statement["period_start"])
            if statement["period_start"] is not None
            else None
        ),
        "period_end": (
            str(statement["period_end"])
            if statement["period_end"] is not None
            else None
        ),
        "evidence_sha256": str(statement["evidence_sha256"]),
        "pockets": pocket_views,
    }


def coverage_gaps(statements: list[sqlite3.Row]) -> list[dict[str, str]]:
    """Missing statement periods between consecutive selected statements.

    A gap is any gap of more than one day between one statement's period end and
    the next statement's period start. Statements missing a printed period are
    reported separately by the caller and skipped here.
    """
    dated = [
        (str(s["period_start"]), str(s["period_end"]))
        for s in statements
        if s["period_start"] is not None and s["period_end"] is not None
    ]
    dated.sort()
    gaps: list[dict[str, str]] = []
    for (_, prev_end), (next_start, _) in zip(dated, dated[1:], strict=False):
        try:
            gap_from = date.fromisoformat(prev_end) + timedelta(days=1)
            gap_to = date.fromisoformat(next_start) - timedelta(days=1)
        except ValueError:
            continue
        if gap_from <= gap_to:
            gaps.append(
                {"from": gap_from.isoformat(), "to": gap_to.isoformat()}
            )
    return gaps


def latest_statement_summary(
    connection: sqlite3.Connection, statements: list[sqlite3.Row]
) -> dict[str, Any] | None:
    """The landing-row summary for a card: the newest statement's observed facts.

    No timeless balance label: the closing figure is stamped with the statement
    period end it was observed at.
    """
    if not statements:
        return None
    latest = statements[-1]
    facts = statement_facts(connection, str(latest["id"]))
    recon = reconciliation(
        connection, str(latest["extraction_run_id"]), _first_pocket(connection, latest)
    )
    return {
        "statement_id": str(latest["id"]),
        "statement_date": str(latest["statement_date"]),
        "period_end": (
            str(latest["period_end"])
            if latest["period_end"] is not None
            else None
        ),
        "closing_liability": (
            _fact_shape(facts[ROLE_CLOSING_LIABILITY])
            if ROLE_CLOSING_LIABILITY in facts
            else None
        ),
        "payment_due": (
            _fact_shape(facts["payment_due"]) if "payment_due" in facts else None
        ),
        "minimum_payment": (
            _fact_shape(facts["minimum_payment"])
            if "minimum_payment" in facts
            else None
        ),
        "payment_due_date": (
            _fact_shape(facts["payment_due_date"])
            if "payment_due_date" in facts
            else None
        ),
        "credit_limit": (
            _fact_shape(facts["credit_limit"]) if "credit_limit" in facts else None
        ),
        "reconciliation_status": recon["status"],
    }


def _first_pocket(connection: sqlite3.Connection, statement: sqlite3.Row) -> str:
    row = connection.execute(
        "SELECT currency_pocket FROM card_statement_facts WHERE statement_id = ? "
        "ORDER BY currency_pocket LIMIT 1",
        (str(statement["id"]),),
    ).fetchone()
    return str(row["currency_pocket"]) if row is not None else "SGD"


__all__ = [
    "selected_card_accounts",
    "selected_statements",
    "statement_facts",
    "statement_facts_by_pocket",
    "reconciliation",
    "statement_view",
    "coverage_gaps",
    "latest_statement_summary",
    "DATE_ROLES",
    "ROLE_PERIOD_START",
    "ROLE_PERIOD_END",
]
