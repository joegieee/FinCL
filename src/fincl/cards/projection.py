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

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    CARD_FACILITY_RETIRE,
    CARD_FACILITY_SET,
    CARD_LIMIT_OBSERVATION_RETIRE,
    CARD_LIMIT_OBSERVATION_SET,
    CARD_PAYMENT_ALLOCATION_RETIRE,
    CARD_PAYMENT_ALLOCATION_SET,
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


# --------------------------------------------------------------------------- #
# Slice 2: facility / limit-observation journal projection.
# --------------------------------------------------------------------------- #


def _component_money(value: object) -> tuple[int, int]:
    money = ExactMoney.parse(str(value))
    return money.coefficient, money.scale


def _is_card_account(connection: sqlite3.Connection, account_id: str) -> bool:
    row = connection.execute(
        "SELECT source_type FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return False
    if str(row["source_type"]) in CARD_SOURCE_TYPES:
        return True
    return (
        connection.execute(
            """
            SELECT 1 FROM statements s
            JOIN card_statement_facts f ON f.statement_id = s.id
            WHERE s.account_id = ? LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        is not None
    )


def _source_ref_is_statement_evidence(
    connection: sqlite3.Connection, source_ref: str
) -> bool:
    """True when ``source_ref`` names preserved statement evidence.

    Accepts a `statements.id`, a `statement_identity`, or a 64-hex evidence
    sha256 that a selected statement carries.
    """
    return (
        connection.execute(
            """
            SELECT 1 FROM statements
            WHERE id = ?1 OR statement_identity = ?1 OR evidence_sha256 = ?1
            LIMIT 1
            """,
            (source_ref,),
        ).fetchone()
        is not None
    )


def apply_card(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
    batch_index: int | None,
    resolve_anchor: Any,
) -> None:
    """Project one ``CARD_FACILITY_*`` / ``CARD_LIMIT_OBSERVATION_*`` record."""
    if decision_kind == CARD_FACILITY_SET:
        facility_id = anchors["facility"]
        account_ids = list(payload["account_ids"])
        missing = [a for a in account_ids if not _is_card_account(connection, a)]
        bound = 0 if missing else 1
        dangling = (
            None
            if bound
            else "account(s) are not credit-liability card accounts: "
            + ", ".join(sorted(missing))
        )
        connection.execute(
            """
            INSERT INTO card_facilities
                (facility_id, label, currency, journal_seq, batch_index, bound,
                 dangling_reason, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(facility_id) DO UPDATE SET
                label = excluded.label,
                currency = excluded.currency,
                journal_seq = excluded.journal_seq,
                batch_index = excluded.batch_index,
                bound = excluded.bound,
                dangling_reason = excluded.dangling_reason,
                reason = excluded.reason
            """,
            (
                facility_id,
                payload["label"],
                payload["currency"],
                journal_seq,
                batch_index,
                bound,
                dangling,
                payload["reason"],
            ),
        )
        connection.execute(
            "DELETE FROM card_facility_accounts WHERE facility_id = ?", (facility_id,)
        )
        for account_id in account_ids:
            connection.execute(
                "INSERT INTO card_facility_accounts(facility_id, account_id, bound) "
                "VALUES (?, ?, ?)",
                (facility_id, account_id, 1 if _is_card_account(connection, account_id) else 0),
            )
        return

    if decision_kind == CARD_FACILITY_RETIRE:
        facility_id = anchors["facility"]
        remaining = connection.execute(
            "SELECT count(*) FROM card_limit_observations WHERE facility_id = ?",
            (facility_id,),
        ).fetchone()[0]
        if remaining:
            raise FinCLError(
                f"card facility {facility_id} still has {remaining} limit "
                "observation(s); retire those first"
            )
        connection.execute(
            "DELETE FROM card_facility_accounts WHERE facility_id = ?", (facility_id,)
        )
        connection.execute(
            "DELETE FROM card_facilities WHERE facility_id = ?", (facility_id,)
        )
        return

    if decision_kind == CARD_LIMIT_OBSERVATION_SET:
        facility_id, observation_id = anchors["facility"], anchors["observation"]
        facility = connection.execute(
            "SELECT currency FROM card_facilities WHERE facility_id = ?",
            (facility_id,),
        ).fetchone()
        if facility is None:
            raise FinCLError(
                f"limit observation {observation_id} references facility "
                f"{facility_id} before it was set"
            )
        limit = ExactMoney.parse(str(payload["limit"]))
        source_ref = payload["source_ref"]
        source_kind = (
            "statement-evidence"
            if source_ref is not None
            and _source_ref_is_statement_evidence(connection, str(source_ref))
            else "operator-declared"
        )
        connection.execute(
            """
            INSERT INTO card_limit_observations
                (facility_id, observation_id, observed_on, limit_coefficient,
                 limit_scale, currency, source_ref, source_kind, journal_seq,
                 batch_index, bound, dangling_reason, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
            ON CONFLICT(facility_id, observation_id) DO UPDATE SET
                observed_on = excluded.observed_on,
                limit_coefficient = excluded.limit_coefficient,
                limit_scale = excluded.limit_scale,
                currency = excluded.currency,
                source_ref = excluded.source_ref,
                source_kind = excluded.source_kind,
                journal_seq = excluded.journal_seq,
                batch_index = excluded.batch_index,
                bound = 1,
                dangling_reason = NULL,
                reason = excluded.reason
            """,
            (
                facility_id,
                observation_id,
                payload["observed_on"],
                limit.coefficient,
                limit.scale,
                str(facility["currency"]),
                source_ref,
                source_kind,
                journal_seq,
                batch_index,
                payload["reason"],
            ),
        )
        return

    if decision_kind == CARD_LIMIT_OBSERVATION_RETIRE:
        connection.execute(
            "DELETE FROM card_limit_observations WHERE facility_id = ? AND observation_id = ?",
            (anchors["facility"], anchors["observation"]),
        )
        return

    if decision_kind == CARD_PAYMENT_ALLOCATION_SET:
        anchor = anchors["card_entry"]
        entry = connection.execute(
            """
            SELECT e.id, e.economic_direction, e.currency_pocket
            FROM ledger_entries e
            JOIN extraction_runs x
              ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
            WHERE e.natural_anchor = ?
            LIMIT 1
            """,
            (anchor,),
        ).fetchone()
        connection.execute(
            "DELETE FROM card_payment_allocations WHERE card_entry_anchor = ?", (anchor,)
        )
        missing_statements = [
            str(item["statement_id"])
            for item in payload["allocations"]
            if connection.execute(
                "SELECT 1 FROM statements WHERE id = ?", (item["statement_id"],)
            ).fetchone()
            is None
        ]
        bound = 1
        dangling = None
        if entry is None:
            bound, dangling = 0, "the source repayment does not resolve to one selected entry"
        elif str(entry["economic_direction"]) == "ZERO":
            bound, dangling = 0, "the source entry has no repayment component"
        elif missing_statements:
            bound, dangling = 0, "statement(s) not preserved: " + ", ".join(missing_statements)
        currency = str(entry["currency_pocket"]) if entry is not None else ""
        for seq, item in enumerate(payload["allocations"]):
            coefficient, scale = _component_money(item["amount"])
            connection.execute(
                """
                INSERT INTO card_payment_allocations
                    (card_entry_anchor, seq_in_entry, statement_id, entry_id,
                     amount_coefficient, amount_scale, currency, journal_seq,
                     batch_index, bound, dangling_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    anchor,
                    seq,
                    item["statement_id"],
                    entry["id"] if entry is not None else None,
                    coefficient,
                    scale,
                    currency,
                    journal_seq,
                    batch_index,
                    bound,
                    dangling,
                ),
            )
        return

    if decision_kind == CARD_PAYMENT_ALLOCATION_RETIRE:
        connection.execute(
            "DELETE FROM card_payment_allocations WHERE card_entry_anchor = ?",
            (anchors["card_entry"],),
        )
        return

    raise FinCLError(f"{decision_kind} is not a credit-card decision")  # pragma: no cover


def resolve_cards(connection: sqlite3.Connection) -> None:
    """Revalidate facility membership exclusivity after every replay (§2).

    One account / pocket belongs to at most one active facility. Facility bound
    status is recomputed from current account existence and kind so a retroactive
    account removal is caught at replay time rather than trusting a stale flag.
    """
    seen: dict[str, str] = {}
    for row in connection.execute(
        """
        SELECT fa.account_id, fa.facility_id
        FROM card_facility_accounts fa
        JOIN card_facilities f ON f.facility_id = fa.facility_id
        ORDER BY fa.facility_id, fa.account_id
        """
    ).fetchall():
        account_id = str(row["account_id"])
        facility_id = str(row["facility_id"])
        if account_id in seen and seen[account_id] != facility_id:
            raise FinCLError(
                f"account {account_id} is claimed by more than one active card "
                f"facility ({seen[account_id]}, {facility_id})"
            )
        seen[account_id] = facility_id

    for row in connection.execute(
        "SELECT facility_id FROM card_facilities"
    ).fetchall():
        facility_id = str(row["facility_id"])
        accounts = [
            str(r["account_id"])
            for r in connection.execute(
                "SELECT account_id FROM card_facility_accounts WHERE facility_id = ?",
                (facility_id,),
            )
        ]
        missing = [a for a in accounts if not _is_card_account(connection, a)]
        connection.execute(
            "UPDATE card_facilities SET bound = ?, dangling_reason = ? "
            "WHERE facility_id = ?",
            (
                0 if missing else 1,
                (
                    None
                    if not missing
                    else "account(s) are not credit-liability card accounts: "
                    + ", ".join(sorted(missing))
                ),
                facility_id,
            ),
        )
        for account_id in accounts:
            connection.execute(
                "UPDATE card_facility_accounts SET bound = ? "
                "WHERE facility_id = ? AND account_id = ?",
                (1 if _is_card_account(connection, account_id) else 0, facility_id, account_id),
            )

    _check_payment_allocation_partitions(connection)


def _check_payment_allocation_partitions(connection: sqlite3.Connection) -> None:
    """A manual bill allocation annotates an observed repayment component only.

    Its allocations sum against the same source entry as installment settlements
    and debt allocations, so it flows through the shared component-claim
    registry — the bank and card legs are never counted twice.
    """
    from ..analysis.allocations import assert_partition

    for (anchor,) in connection.execute(
        "SELECT DISTINCT card_entry_anchor FROM card_payment_allocations WHERE bound = 1"
    ).fetchall():
        assert_partition(connection, str(anchor), role="amount")


def manual_payment_allocations(
    connection: sqlite3.Connection, account_id: str
) -> list[dict[str, Any]]:
    """Operator-entered bill allocations for the card's repayments (§2).

    Shown as "manually allocated payments", separate from any issuer-stated
    remaining due — an issuer-confirmed remaining due needs new source evidence.
    """
    from ..money import format_decimal

    rows = connection.execute(
        """
        SELECT p.card_entry_anchor, p.statement_id, p.amount_coefficient,
               p.amount_scale, p.currency, p.bound, p.dangling_reason
        FROM card_payment_allocations p
        JOIN statements s ON s.id = p.statement_id
        WHERE s.account_id = ?
        ORDER BY p.card_entry_anchor, p.seq_in_entry
        """,
        (account_id,),
    ).fetchall()
    return [
        {
            "card_entry_anchor": str(r["card_entry_anchor"]),
            "statement_id": str(r["statement_id"]),
            "amount": {
                "value": format_decimal(
                    int(r["amount_coefficient"]), int(r["amount_scale"])
                ),
                "currency": str(r["currency"]),
            },
            "manual": True,
            "bound": bool(r["bound"]),
            "dangling_reason": r["dangling_reason"],
        }
        for r in rows
    ]


def facility_for_account(
    connection: sqlite3.Connection, account_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT f.facility_id, f.label, f.currency, f.bound
        FROM card_facilities f
        JOIN card_facility_accounts fa ON fa.facility_id = f.facility_id
        WHERE fa.account_id = ?
        ORDER BY f.facility_id LIMIT 1
        """,
        (account_id,),
    ).fetchone()


def limit_as_of(
    connection: sqlite3.Connection, facility_id: str, as_of: str
) -> dict[str, Any] | None:
    """The latest non-retired limit observation on/before ``as_of``.

    Its observation date is carried so the caller can display exactly when the
    limit was read. No FX conversion is ever applied.
    """
    row = connection.execute(
        """
        SELECT observation_id, observed_on, limit_coefficient, limit_scale,
               currency, source_ref, source_kind
        FROM card_limit_observations
        WHERE facility_id = ? AND observed_on <= ? AND bound = 1
        ORDER BY observed_on DESC, observation_id DESC
        LIMIT 1
        """,
        (facility_id, as_of),
    ).fetchone()
    if row is None:
        return None
    from ..money import format_decimal

    return {
        "observation_id": str(row["observation_id"]),
        "observed_on": str(row["observed_on"]),
        "limit": {
            "value": format_decimal(
                int(row["limit_coefficient"]), int(row["limit_scale"])
            ),
            "currency": str(row["currency"]),
        },
        "source_ref": row["source_ref"],
        "source_kind": str(row["source_kind"]),
    }


__all__ = [
    "selected_card_accounts",
    "selected_statements",
    "statement_facts",
    "statement_facts_by_pocket",
    "reconciliation",
    "statement_view",
    "coverage_gaps",
    "latest_statement_summary",
    "apply_card",
    "resolve_cards",
    "facility_for_account",
    "manual_payment_allocations",
    "limit_as_of",
    "DATE_ROLES",
    "ROLE_PERIOD_START",
    "ROLE_PERIOD_END",
]
