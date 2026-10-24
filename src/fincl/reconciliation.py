"""Cross-source transfer reconciliation.

Automatic transfer links are a *projection*, not decision history: every row for
an automatic method is discarded and rebuilt from the currently selected
evidence on each import, ``fincl link-transfers`` / refresh, and rebuild. Bound
journal ``LINK_CONFIRM`` / ``LINK_REJECT`` decisions are then overlaid on that
fresh projection.

This module was carved out of ``document_service`` so the linking logic has one
home independent of the PDF import path. It reads ledger entries and writes only
``transfer_links``; it never parses a statement.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from math import gcd
from pathlib import Path

from .common import canonical_json as _json
from .common import stable_id as _stable_id
from .common import utc_now as _now
from .decisions import replay_journal, resolve_anchor

DBS_PAYLAH_LINK_VERSION = "dbs-paylah-exact-date-amount-description-v1"
DBS_REVOLUT_LEGACY_LINK_VERSION = "dbs-revolut-exact-amount-currency-window-description-v1"
DBS_REVOLUT_CARD_TOPUP_LINK_VERSION = "dbs-revolut-card-topup-exact-amount-window-v1"
DBS_REVOLUT_PAYNOW_TOPUP_LINK_VERSION = "dbs-revolut-paynow-topup-exact-amount-window-v1"
DBS_REVOLUT_OUTBOUND_TRANSFER_LINK_VERSION = (
    "revolut-dbs-outbound-transfer-counterpart-amount-v1"
)
DBS_MARIBANK_PAYNOW_TOPUP_LINK_VERSION = "dbs-maribank-paynow-topup-reference-token-amount-v1"
MARI_SAVINGS_CARD_REPAYMENT_LINK_VERSION = "mari-savings-card-repayment-exact-amount-day-v1"
REVOLUT_EXCHANGE_LINK_VERSION = "revolut-exchange-started-at-pockets-v1"
AMBIGUOUS_MUTUAL_MATCH = "AMBIGUOUS_MUTUAL_MATCH"
SELECTION_LOST = "SELECTION_LOST"


@dataclass(frozen=True, slots=True)
class EligibleLinkLeg:
    """The source account/pocket and canonical shape a linker may consume."""

    source_type: str
    currency_pocket: str | None
    economic_direction: str
    event_kinds: frozenset[str]

    def matches(self, row: sqlite3.Row) -> bool:
        return (
            row["source_type"] == self.source_type
            and (self.currency_pocket is None or row["currency_pocket"] == self.currency_pocket)
            and row["economic_direction"] == self.economic_direction
            and row["event_kind"] in self.event_kinds
        )


@dataclass(frozen=True, slots=True)
class SameCurrencyLinker:
    """A declared automatic linker, including its complete eligible leg pair."""

    method_version: str
    left: EligibleLinkLeg
    right: EligibleLinkLeg
    max_days: int


@dataclass(frozen=True, slots=True)
class ExchangeLinker:
    """A declared cross-currency linker with its complete eligible leg pair."""

    method_version: str
    left: EligibleLinkLeg
    right: EligibleLinkLeg


# These declarations are the eligible-pair table for automatic same-currency
# links. A leg is never matched unless it satisfies one of these narrow scopes.
DBS_PAYLAH_LINKER = SameCurrencyLinker(
    DBS_PAYLAH_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"TOPUP"})),
    EligibleLinkLeg("DBS_PAYLAH", "SGD", "IN", frozenset({"TOPUP"})),
    max_days=0,
)
# The window is measured against the real card-transaction date parsed from the
# DBS merchant line (the trailing ``DDMMM`` token), not the later posting date,
# so two same-amount top-ups a few days apart are told apart by when the card was
# actually charged rather than by when DBS got round to posting each one.
DBS_REVOLUT_CARD_TOPUP_LINKER = SameCurrencyLinker(
    DBS_REVOLUT_CARD_TOPUP_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"CARD_PAYMENT"})),
    EligibleLinkLeg("REVOLUT_PDF", "SGD", "IN", frozenset({"TOPUP", "TRANSFER"})),
    max_days=1,
)
DBS_REVOLUT_PAYNOW_TOPUP_LINKER = SameCurrencyLinker(
    DBS_REVOLUT_PAYNOW_TOPUP_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"TRANSFER"})),
    EligibleLinkLeg("REVOLUT_PDF", "SGD", "IN", frozenset({"TOPUP", "TRANSFER"})),
    max_days=2,
)
# DBS PayNow that funds a MariBank savings inflow.  Both legs print the same
# PayNow reference token; matching on the token plus the exact amount survives
# two same-day, same-counterpart transfers that share one reference.
DBS_MARIBANK_PAYNOW_TOPUP_LINKER = SameCurrencyLinker(
    DBS_MARIBANK_PAYNOW_TOPUP_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"TRANSFER"})),
    EligibleLinkLeg("MARI_SAVINGS", "SGD", "IN", frozenset({"TRANSFER"})),
    max_days=2,
)
# MariBank savings debit that repays the Mari credit card.  The savings row
# names "Mari Credit Card"; the card row is the bucket-B "Repayment" credit.
MARI_SAVINGS_CARD_REPAYMENT_LINKER = SameCurrencyLinker(
    MARI_SAVINGS_CARD_REPAYMENT_LINK_VERSION,
    EligibleLinkLeg("MARI_SAVINGS", "SGD", "OUT", frozenset({"TRANSFER"})),
    EligibleLinkLeg("MARI_CARD", "SGD", "IN", frozenset({"TRANSFER"})),
    max_days=0,
)
REVOLUT_EXCHANGE_LINKER = ExchangeLinker(
    REVOLUT_EXCHANGE_LINK_VERSION,
    EligibleLinkLeg("REVOLUT_PDF", None, "OUT", frozenset({"EXCHANGE"})),
    EligibleLinkLeg("REVOLUT_PDF", None, "IN", frozenset({"EXCHANGE"})),
)
AUTOMATIC_SAME_CURRENCY_LINK_METHODS = (
    DBS_PAYLAH_LINKER.method_version,
    DBS_REVOLUT_LEGACY_LINK_VERSION,
    DBS_REVOLUT_CARD_TOPUP_LINKER.method_version,
    DBS_REVOLUT_PAYNOW_TOPUP_LINKER.method_version,
    DBS_MARIBANK_PAYNOW_TOPUP_LINKER.method_version,
    MARI_SAVINGS_CARD_REPAYMENT_LINKER.method_version,
    DBS_REVOLUT_OUTBOUND_TRANSFER_LINK_VERSION,
)


def link_narrow_transfers(
    connection: sqlite3.Connection, *, journal_path: Path | None = None
) -> int:
    """Refresh all derived same-currency links, then retain exchange links."""
    replay_journal(connection, journal_path)
    return refresh_automatic_transfer_links(connection)


def refresh_automatic_transfer_links(connection: sqlite3.Connection) -> int:
    """Recompute automatic transfer links from currently selected evidence.

    Automatic links are a projection, not decision history: every row for the
    automatic same-currency methods is discarded and rebuilt from the evidence
    currently selected.  The return value is the number of confirmed links that
    are new relative to the prior projection, so an unchanged refresh reports 0.
    """
    placeholders = ", ".join("?" for _ in AUTOMATIC_SAME_CURRENCY_LINK_METHODS)
    before = {
        row[0]
        for row in connection.execute(
            f"SELECT id FROM transfer_links WHERE status = 'CONFIRMED' "
            f"AND method_version IN ({placeholders})",
            AUTOMATIC_SAME_CURRENCY_LINK_METHODS,
        )
    }
    connection.execute(
        f"DELETE FROM transfer_links WHERE method_version IN ({placeholders})",
        AUTOMATIC_SAME_CURRENCY_LINK_METHODS,
    )
    created = link_revolut_exchanges(connection)
    link_revolut_dbs_outbound_transfers(connection)
    dbs_rows = connection.execute(
        """
        SELECT e.id, e.effective_at AS posted_date, e.currency_pocket,
               e.amount_coefficient, e.amount_scale, e.amount_text, e.event_kind,
               e.economic_direction, s.source_type, r.description_raw
        FROM ledger_entries e
        JOIN document_records r ON r.id = e.document_record_id
        JOIN statements s ON s.id = e.statement_id
        WHERE e.source_family = 'DOCUMENT'
          AND s.source_type = 'DBS_CONSOLIDATED'
          AND s.selection_state = 'SELECTED'
          AND e.analytics_eligible = 1
          AND e.economic_direction = 'OUT'
        """
    ).fetchall()
    paylah_rows = connection.execute(
        """
        SELECT e.id, e.effective_at AS posted_date, e.currency_pocket,
               e.amount_coefficient, e.amount_scale, e.amount_text, e.event_kind,
               e.economic_direction, s.source_type, r.description_raw
        FROM ledger_entries e
        JOIN document_records r ON r.id = e.document_record_id
        JOIN statements s ON s.id = e.statement_id
        WHERE e.source_family = 'DOCUMENT'
          AND s.source_type = 'DBS_PAYLAH'
          AND s.selection_state = 'SELECTED'
          AND e.analytics_eligible = 1
          AND e.economic_direction = 'IN'
        """
    ).fetchall()
    revolut_rows = connection.execute(
        """
        SELECT e.id, substr(e.effective_at, 1, 10) AS posted_date,
               e.currency_pocket, e.amount_coefficient, e.amount_scale,
               e.amount_text, e.event_kind, e.economic_direction,
               s.source_type, r.description_raw
        FROM ledger_entries e
        JOIN document_records r ON r.id = e.document_record_id
        JOIN statements s ON s.id = e.statement_id
        WHERE e.source_family = 'DOCUMENT'
          AND s.source_type = 'REVOLUT_PDF'
          AND s.selection_state = 'SELECTED' AND e.analytics_eligible = 1
          AND e.economic_direction = 'IN'
          AND e.event_kind IN ('TOPUP', 'TRANSFER')
        """
    ).fetchall()
    mari_rows = connection.execute(
        """
        SELECT e.id, e.effective_at AS posted_date, e.currency_pocket,
               e.amount_coefficient, e.amount_scale, e.amount_text, e.event_kind,
               e.economic_direction, s.source_type, r.description_raw
        FROM ledger_entries e
        JOIN document_records r ON r.id = e.document_record_id
        JOIN statements s ON s.id = e.statement_id
        WHERE e.source_family = 'DOCUMENT'
          AND s.source_type IN ('MARI_SAVINGS', 'MARI_CARD')
          AND s.selection_state = 'SELECTED'
          AND e.analytics_eligible = 1
        """
    ).fetchall()
    dbs_topups = [
        row
        for row in dbs_rows
        if DBS_PAYLAH_LINKER.left.matches(row)
        and "TOP-UP TO PAYLAH" in (row["description_raw"] or "").upper()
    ]
    paylah_topups = [
        row
        for row in paylah_rows
        if DBS_PAYLAH_LINKER.right.matches(row)
        and "TOP UP WALLET FROM MY ACCOUNT" in (row["description_raw"] or "").upper()
    ]
    card_topups = [
        row
        for row in dbs_rows
        if DBS_REVOLUT_CARD_TOPUP_LINKER.left.matches(row)
        and _is_revolut_card_topup(row["description_raw"])
    ]
    paynow_topups = [
        row
        for row in dbs_rows
        if DBS_REVOLUT_PAYNOW_TOPUP_LINKER.left.matches(row)
        and _is_revolut_paynow_topup(row["description_raw"])
    ]
    card_revolut_rows = [
        row for row in revolut_rows if DBS_REVOLUT_CARD_TOPUP_LINKER.right.matches(row)
    ]
    paynow_revolut_rows = [
        row for row in revolut_rows if DBS_REVOLUT_PAYNOW_TOPUP_LINKER.right.matches(row)
    ]
    dbs_maribank_topups = [
        row
        for row in dbs_rows
        if DBS_MARIBANK_PAYNOW_TOPUP_LINKER.left.matches(row)
        and _is_dbs_maribank_paynow(row["description_raw"])
        and _paynow_reference_token(row["description_raw"]) is not None
    ]
    maribank_savings_inflows = [
        row
        for row in mari_rows
        if DBS_MARIBANK_PAYNOW_TOPUP_LINKER.right.matches(row)
        and _paynow_reference_token(row["description_raw"]) is not None
    ]
    savings_card_repayments = [
        row
        for row in mari_rows
        if MARI_SAVINGS_CARD_REPAYMENT_LINKER.left.matches(row)
        and "MARI CREDIT CARD" in (row["description_raw"] or "").upper()
    ]
    card_repayment_credits = [
        row
        for row in mari_rows
        if MARI_SAVINGS_CARD_REPAYMENT_LINKER.right.matches(row)
        and "REPAYMENT" in (row["description_raw"] or "").upper()
        and "MONEYSEND" not in (row["description_raw"] or "").upper()
    ]
    candidates = [
        *_candidate_pairs(
            dbs_topups,
            paylah_topups,
            DBS_PAYLAH_LINKER.method_version,
            max_days=DBS_PAYLAH_LINKER.max_days,
        ),
        *_candidate_pairs(
            card_topups,
            card_revolut_rows,
            DBS_REVOLUT_CARD_TOPUP_LINKER.method_version,
            max_days=DBS_REVOLUT_CARD_TOPUP_LINKER.max_days,
            left_date=lambda row: _dbs_card_transaction_date(
                row["description_raw"], row["posted_date"]
            ),
        ),
        *_candidate_pairs(
            paynow_topups,
            paynow_revolut_rows,
            DBS_REVOLUT_PAYNOW_TOPUP_LINKER.method_version,
            max_days=DBS_REVOLUT_PAYNOW_TOPUP_LINKER.max_days,
        ),
        *_candidate_pairs(
            dbs_maribank_topups,
            maribank_savings_inflows,
            DBS_MARIBANK_PAYNOW_TOPUP_LINKER.method_version,
            max_days=DBS_MARIBANK_PAYNOW_TOPUP_LINKER.max_days,
            key=lambda row: _paynow_reference_token(row["description_raw"]),
        ),
        *_candidate_pairs(
            savings_card_repayments,
            card_repayment_credits,
            MARI_SAVINGS_CARD_REPAYMENT_LINKER.method_version,
            max_days=MARI_SAVINGS_CARD_REPAYMENT_LINKER.max_days,
        ),
    ]
    candidates = _resolve_balanced_paylah_topups(connection, candidates)
    _store_candidates(connection, candidates)
    _apply_link_decisions(connection)
    after = {
        row[0]
        for row in connection.execute(
            f"SELECT id FROM transfer_links WHERE status = 'CONFIRMED' "
            f"AND method_version IN ({placeholders})",
            AUTOMATIC_SAME_CURRENCY_LINK_METHODS,
        )
    }
    connection.commit()
    return created + len(after - before)


def _apply_link_decisions(connection: sqlite3.Connection) -> None:
    """Overlay bound journal decisions on this freshly-derived link projection."""
    decisions = connection.execute(
        "SELECT * FROM link_decisions WHERE bound = 1 ORDER BY journal_seq"
    ).fetchall()
    for decision in decisions:
        left = resolve_anchor(connection, decision["left_anchor"])
        right = resolve_anchor(connection, decision["right_anchor"])
        if left is None or right is None:
            continue
        row = connection.execute(
            """SELECT id, method_version, evidence_json FROM transfer_links
               WHERE left_entry_id=? AND right_entry_id=?""",
            (left, right),
        ).fetchone()
        if row is None:
            continue  # Confirmation without a candidate is deliberately not guessed.
        evidence = json.loads(row["evidence_json"])
        evidence["decision"] = {
            "seq": decision["journal_seq"],
            "policy_version": decision["policy_version"],
        }
        if decision["decision"] == "REJECT":
            if decision["policy_version"] == row["method_version"]:
                connection.execute("DELETE FROM transfer_links WHERE id = ?", (row["id"],))
            else:
                connection.execute(
                    "UPDATE transfer_links SET status='REJECTED', evidence_json=? WHERE id=?",
                    (_json(evidence), row["id"]),
                )
            continue
        occupied = connection.execute(
            """SELECT 1 FROM transfer_links WHERE status='CONFIRMED' AND id <> ?
               AND (left_entry_id IN (?, ?) OR right_entry_id IN (?, ?))""",
            (row["id"], left, right, left, right),
        ).fetchone()
        if occupied is None:
            connection.execute(
                "UPDATE transfer_links SET status='CONFIRMED', evidence_json=? WHERE id=?",
                (_json(evidence), row["id"]),
            )


_PAYNOW_REFERENCE_TOKEN_RE = re.compile(r"[A-Za-z0-9]{20,30}")


def _paynow_reference_token(description: str | None) -> str | None:
    """The PayNow end-to-end reference both statement sides print, uppercased."""
    for match in _PAYNOW_REFERENCE_TOKEN_RE.finditer(description or ""):
        token = match.group(0)
        if any(character.isdigit() for character in token) and any(
            character.isalpha() for character in token
        ):
            return token.upper()
    return None


def _is_dbs_maribank_paynow(description: str | None) -> bool:
    text = (description or "").upper()
    return "PAYNOW TRANSFER" in text and "TO: MARIBANK SINGAPORE" in text


def _is_revolut_card_topup(description: str | None) -> bool:
    text = (description or "").upper()
    return text.startswith("DEBIT CARD TRANSACTION") and "REVOLUT**" in text and "SINGAPORE SGP" in text


_CARD_TRANSACTION_DATE_RE = re.compile(r"\b(\d{2})([A-Z]{3})\b")
_MONTH_ABBREVIATIONS = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
# A DBS debit-card row posts a few days after the card is charged; a parsed
# transaction date further than this before the posting date is treated as a
# misread and the posting date is used instead.
_CARD_POSTING_LAG_DAYS = 14


def _dbs_card_transaction_date(description: str | None, posted_date: str) -> str:
    """The card-transaction date from the DBS merchant line's ``DDMMM`` token.

    DBS prints the real transaction date (e.g. ``... SINGAPORE SGP 16MAR``) and
    posts the debit a few days later, so two same-amount Revolut top-ups a few
    days apart are only told apart by this token. Falls back to ``posted_date``
    when the token is absent, unparseable, or implies an implausible posting lag.
    """
    posted = date.fromisoformat(posted_date[:10])
    match = _CARD_TRANSACTION_DATE_RE.search((description or "").upper())
    if match is None:
        return posted.isoformat()
    day = int(match.group(1))
    month = _MONTH_ABBREVIATIONS.get(match.group(2))
    if month is None:
        return posted.isoformat()
    for year in (posted.year, posted.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return posted.isoformat()
        if 0 <= (posted - candidate).days <= _CARD_POSTING_LAG_DAYS:
            return candidate.isoformat()
    return posted.isoformat()


def _is_revolut_paynow_topup(description: str | None) -> bool:
    text = (description or "").upper()
    return "PAYNOW TRANSFER" in text and "TO: REVOLUT TECHNOLOGIES SINGAPORE" in text


def link_revolut_dbs_outbound_transfers(connection: sqlite3.Connection) -> int:
    """Link a Revolut transfer-out to its same-day DBS SGD settlement.

    Revolut prints the DBS settlement amount as a counterpart figure below the
    outgoing transfer (and, when applicable, prints its fee separately).  DBS
    records the received SGD amount as a positive debit-card transaction.  The
    printed counterpart amount is required for cross-currency matches; a
    same-currency SGD transfer may instead match its own exact amount.
    """
    revolut_rows = connection.execute(
        """
        SELECT e.id, substr(e.effective_at, 1, 10) AS posted_date,
               e.currency_pocket, e.amount_text, e.amount_coefficient, e.amount_scale,
               r.raw_fields_json
        FROM ledger_entries e
        JOIN statements s ON s.id = e.statement_id
        JOIN document_records r ON r.id = e.document_record_id
        WHERE e.source_family = 'DOCUMENT' AND s.source_type = 'REVOLUT_PDF'
          AND s.selection_state = 'SELECTED' AND e.analytics_eligible = 1
          AND e.lifecycle_state = 'COMPLETED' AND e.economic_direction = 'OUT'
          AND e.event_kind = 'TRANSFER'
        """
    ).fetchall()
    dbs_rows = connection.execute(
        """
        SELECT e.id, substr(e.effective_at, 1, 10) AS posted_date,
               e.currency_pocket, e.amount_text, e.amount_coefficient, e.amount_scale
        FROM ledger_entries e
        JOIN statements s ON s.id = e.statement_id
        WHERE e.source_family = 'DOCUMENT' AND s.source_type = 'DBS_CONSOLIDATED'
          AND s.selection_state = 'SELECTED' AND e.analytics_eligible = 1
          AND e.lifecycle_state = 'COMPLETED' AND e.economic_direction = 'IN'
          AND e.event_kind = 'CARD_PAYMENT' AND e.currency_pocket = 'SGD'
        """
    ).fetchall()
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, str]] = []
    for left in revolut_rows:
        counterpart = _revolut_counterpart_amount(left["raw_fields_json"])
        for right in dbs_rows:
            if left["posted_date"] != right["posted_date"]:
                continue
            expected = counterpart if counterpart is not None else left["amount_text"].lstrip("-")
            if right["amount_text"].lstrip("-") != expected:
                continue
            # A USD (or other foreign-currency) transfer must carry the
            # statement's explicit SGD counterpart; matching numeric amounts
            # across currencies would be accidental.
            if left["currency_pocket"] != "SGD" and counterpart is None:
                continue
            candidates.append((left, right, DBS_REVOLUT_OUTBOUND_TRANSFER_LINK_VERSION))

    left_counts: dict[str, int] = {}
    right_counts: dict[str, int] = {}
    for left, right, _ in candidates:
        left_counts[left["id"]] = left_counts.get(left["id"], 0) + 1
        right_counts[right["id"]] = right_counts.get(right["id"], 0) + 1
    created = 0
    for left, right, method_version in candidates:
        if left_counts[left["id"]] != 1 or right_counts[right["id"]] != 1:
            continue
        created += _insert_cross_currency_transfer_link(connection, left, right, method_version)
    return created


def _insert_cross_currency_transfer_link(
    connection: sqlite3.Connection,
    left: sqlite3.Row,
    right: sqlite3.Row,
    method_version: str,
) -> int:
    same_currency = left["currency_pocket"] == right["currency_pocket"]
    numerator = denominator = None
    if not same_currency:
        numerator = abs(right["amount_coefficient"]) * 10**left["amount_scale"]
        denominator = abs(left["amount_coefficient"]) * 10**right["amount_scale"]
        divisor = gcd(numerator, denominator)
        numerator //= divisor
        denominator //= divisor
    link_id = _stable_id("link", left["id"], right["id"], method_version)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO transfer_links(
            id, left_entry_id, right_entry_id, link_kind,
            left_currency, left_amount_text, left_amount_coefficient, left_amount_scale,
            right_currency, right_amount_text, right_amount_coefficient, right_amount_scale,
            implied_rate_numerator, implied_rate_denominator,
            method_version, status, evidence_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', ?, ?)
        """,
        (
            link_id,
            left["id"], right["id"],
            "SAME_CURRENCY_TRANSFER" if same_currency else "CURRENCY_EXCHANGE",
            left["currency_pocket"], left["amount_text"].lstrip("-"),
            abs(left["amount_coefficient"]), left["amount_scale"],
            right["currency_pocket"], right["amount_text"].lstrip("-"),
            abs(right["amount_coefficient"]), right["amount_scale"],
            numerator, denominator, method_version,
            _json({
                "date_left": left["posted_date"],
                "date_right": right["posted_date"],
                "revolut_counterpart_amount": _revolut_counterpart_amount(left["raw_fields_json"]),
                "unique_pair": True,
            }),
            _now(),
        ),
    )
    return cursor.rowcount


def link_revolut_exchanges(connection: sqlite3.Connection) -> int:
    """Link complete Revolut exchange pairs and retain their exact implied rate."""
    # The retired CSV export carried a started/completed timestamp pair, so
    # exchange legs paired on the exact `Started Date`. The PDF prints one date
    # per row at DAY precision, so the two legs of a same-day exchange pair on
    # the completed date within one statement's extraction run.
    rows = connection.execute(
        """
        SELECT e.id, e.extraction_run_id,
               substr(e.effective_at, 1, 10) AS started_at,
               e.currency_pocket, e.amount_text, e.amount_coefficient, e.amount_scale,
               e.economic_direction, e.event_kind, s.source_type, r.raw_fields_json
        FROM ledger_entries e
        JOIN statements s ON s.id = e.statement_id
        LEFT JOIN document_records r ON r.id = e.document_record_id
        WHERE e.source_family = 'DOCUMENT'
          AND s.source_type = 'REVOLUT_PDF'
          AND s.selection_state = 'SELECTED' AND e.analytics_eligible = 1
          AND e.lifecycle_state = 'COMPLETED'
        ORDER BY e.extraction_run_id, started_at, e.id
        """
    ).fetchall()
    rows = [
        row
        for row in rows
        if REVOLUT_EXCHANGE_LINKER.left.matches(row)
        or REVOLUT_EXCHANGE_LINKER.right.matches(row)
    ]
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault((row["extraction_run_id"], row["started_at"]), []).append(row)

    created = 0
    for group in groups.values():
        outgoing = [row for row in group if row["economic_direction"] == "OUT"]
        incoming = [row for row in group if row["economic_direction"] == "IN"]
        for left, right in _pair_revolut_exchange_legs(outgoing, incoming):
            if left["currency_pocket"] == right["currency_pocket"]:
                continue
            numerator = abs(right["amount_coefficient"]) * 10**left["amount_scale"]
            denominator = abs(left["amount_coefficient"]) * 10**right["amount_scale"]
            divisor = gcd(numerator, denominator)
            link_id = _stable_id(
                "link", left["id"], right["id"], REVOLUT_EXCHANGE_LINKER.method_version
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO transfer_links(
                    id, left_entry_id, right_entry_id,
                    link_kind, left_currency, left_amount_text, left_amount_coefficient,
                    left_amount_scale, right_currency, right_amount_text,
                    right_amount_coefficient, right_amount_scale,
                    implied_rate_numerator, implied_rate_denominator,
                    method_version, status, evidence_json, created_at
                ) VALUES (?, ?, ?, 'CURRENCY_EXCHANGE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'CONFIRMED', ?, ?)
                """,
                (
                    link_id,
                    left["id"],
                    right["id"],
                    left["currency_pocket"],
                    left["amount_text"].lstrip("-"),
                    abs(left["amount_coefficient"]),
                    left["amount_scale"],
                    right["currency_pocket"],
                    right["amount_text"].lstrip("-"),
                    abs(right["amount_coefficient"]),
                    right["amount_scale"],
                    numerator // divisor,
                    denominator // divisor,
                    REVOLUT_EXCHANGE_LINKER.method_version,
                    _json({"started_at": left["started_at"], "unique_pair": True}),
                    _now(),
                ),
            )
            created += cursor.rowcount
    return created


def _revolut_counterpart_amount(raw_fields_json: str | None) -> str | None:
    if not raw_fields_json:
        return None
    try:
        fields = json.loads(raw_fields_json)
    except (TypeError, ValueError):
        return None
    counterpart = fields.get("counterpart")
    if not isinstance(counterpart, str):
        return None
    return counterpart.replace("S$", "").replace("$", "").replace(",", "").lstrip("-")


def _pair_revolut_exchange_legs(
    outgoing: list[sqlite3.Row], incoming: list[sqlite3.Row]
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    """Match the OUT and IN legs of same-day Revolut exchanges.

    With one leg of each direction the pairing is unambiguous. When a day holds
    more than one exchange — the CSV export told them apart by their exact
    ``Started Date``, which the PDF does not print — pair a leg to the opposite
    leg whose printed counterpart figure equals this leg's own posted amount.
    """
    if len(outgoing) == 1 and len(incoming) == 1:
        return [(outgoing[0], incoming[0])]
    pairs: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    remaining = list(incoming)
    for out_row in outgoing:
        out_amount = out_row["amount_text"].lstrip("-")
        match = next(
            (
                in_row
                for in_row in remaining
                if _revolut_counterpart_amount(in_row["raw_fields_json"]) == out_amount
                or _revolut_counterpart_amount(out_row["raw_fields_json"])
                == in_row["amount_text"].lstrip("-")
            ),
            None,
        )
        if match is not None:
            remaining.remove(match)
            pairs.append((out_row, match))
    return pairs


def _candidate_pairs(
    left_rows: list[sqlite3.Row],
    right_rows: list[sqlite3.Row],
    method_version: str,
    *,
    max_days: int,
    key: Callable[[sqlite3.Row], str | None] | None = None,
    left_date: Callable[[sqlite3.Row], str] | None = None,
) -> list[tuple[sqlite3.Row, sqlite3.Row, str]]:
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, str]] = []
    for left in left_rows:
        left_anchor_date = left_date(left) if left_date is not None else left["posted_date"]
        for right in right_rows:
            if left["currency_pocket"] != right["currency_pocket"]:
                continue
            if not _opposite_exact_amount(left, right):
                continue
            day_difference = abs(
                (date.fromisoformat(left_anchor_date) - date.fromisoformat(right["posted_date"])).days
            )
            if day_difference > max_days:
                continue
            if key is not None:
                left_key, right_key = key(left), key(right)
                if left_key is None or right_key is None or not _reference_keys_match(
                    left_key, right_key
                ):
                    continue
            candidates.append((left, right, method_version))
    return candidates


def _reference_keys_match(left: str, right: str) -> bool:
    """Equal, or a shared >=20-char prefix (one statement truncated the token)."""
    return left == right or (
        min(len(left), len(right)) >= 20
        and (left.startswith(right) or right.startswith(left))
    )


def _resolve_balanced_paylah_topups(
    connection: sqlite3.Connection,
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, str]],
) -> list[tuple[sqlite3.Row, sqlite3.Row, str]]:
    """Auto-confirm same-day, same-amount DBS->PayLah top-up groups.

    The DBS consolidated ``TOP-UP TO PAYLAH!`` line carries no reference, and the
    PayLah ``TOP UP WALLET FROM MY ACCOUNT`` line's ``REF NO:. PLPE...`` never
    appears on the DBS side, so two identical top-ups on one day have no shared
    key and today land as ``AMBIGUOUS_MUTUAL_MATCH`` proposals.

    Both legs are the owner's own SGD accounts, the amounts within a group are
    exactly equal, the day is identical (``max_days=0``), and the fee is zero, so
    every perfect matching is economically identical and is excluded from
    spending analysis as a self-transfer. When a group is a complete bipartite
    block with an equal number of legs on each side (k on each, k >= 2), confirm
    one deterministic bijection -- both sides ordered by the rebuild-stable ledger
    entry id and zipped -- instead of proposing k*k pairs. Unbalanced groups
    (a missing or extra leg) stay ``PROPOSED``: that asymmetry is a real review
    signal. A group any leg of which is named by a bound journal
    ``LINK_CONFIRM`` / ``LINK_REJECT`` also stays ``PROPOSED`` so that the
    explicit operator pairing is the one overlaid by ``_apply_link_decisions``.
    """
    paylah = [c for c in candidates if c[2] == DBS_PAYLAH_LINKER.method_version]
    if not paylah:
        return candidates

    decided_entries: set[str] = set()
    for decision in connection.execute(
        "SELECT left_anchor, right_anchor FROM link_decisions WHERE bound = 1"
    ).fetchall():
        for anchor in (decision["left_anchor"], decision["right_anchor"]):
            resolved_entry = resolve_anchor(connection, anchor)
            if resolved_entry is not None:
                decided_entries.add(resolved_entry)

    groups: dict[tuple[str, int, int], list[tuple[sqlite3.Row, sqlite3.Row]]] = {}
    for left, right, _ in paylah:
        key = (right["posted_date"], abs(right["amount_coefficient"]), right["amount_scale"])
        groups.setdefault(key, []).append((left, right))

    resolved: set[tuple[str, str]] = set()
    for pairs in groups.values():
        lefts = {left["id"]: left for left, _ in pairs}
        rights = {right["id"]: right for _, right in pairs}
        if len(lefts) < 2 or len(lefts) != len(rights):
            continue
        if len(pairs) != len(lefts) * len(rights):
            continue  # not a complete block; leave the whole group for review
        if decided_entries & (lefts.keys() | rights.keys()):
            continue  # an explicit journal decision pairs this group by hand
        ordered_left = sorted(lefts.values(), key=lambda row: row["id"])
        ordered_right = sorted(rights.values(), key=lambda row: row["id"])
        for position, (left, right) in enumerate(zip(ordered_left, ordered_right, strict=True)):
            _insert_link(
                connection,
                left,
                right,
                DBS_PAYLAH_LINKER.method_version,
                status="CONFIRMED",
                evidence={
                    "date_left": left["posted_date"],
                    "date_right": right["posted_date"],
                    "description_evidence": True,
                    "unique_candidate": False,
                    "balanced_group": {
                        "size": len(ordered_left),
                        "position": position,
                        "arbitrary_pairing": True,
                    },
                },
            )
            resolved.add((left["id"], right["id"]))

    if not resolved:
        return candidates
    return [
        c
        for c in candidates
        if c[2] != DBS_PAYLAH_LINKER.method_version
        or (c[0]["id"], c[1]["id"]) not in resolved
    ]


def _insert_link(
    connection: sqlite3.Connection,
    left: sqlite3.Row,
    right: sqlite3.Row,
    method_version: str,
    *,
    status: str,
    evidence: dict[str, object],
) -> int:
    link_id = _stable_id("link", left["id"], right["id"], method_version)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO transfer_links(
            id, left_entry_id, right_entry_id, link_kind,
            left_currency, left_amount_text, left_amount_coefficient, left_amount_scale,
            right_currency, right_amount_text, right_amount_coefficient, right_amount_scale,
            method_version, status, evidence_json, created_at
        ) VALUES (?, ?, ?, 'SAME_CURRENCY_TRANSFER', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            link_id,
            left["id"],
            right["id"],
            left["currency_pocket"],
            left["amount_text"].lstrip("-"),
            abs(left["amount_coefficient"]),
            left["amount_scale"],
            right["currency_pocket"],
            right["amount_text"].lstrip("-"),
            abs(right["amount_coefficient"]),
            right["amount_scale"],
            method_version,
            status,
            _json(evidence),
            _now(),
        ),
    )
    return cursor.rowcount


def _store_candidates(
    connection: sqlite3.Connection,
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, str]],
) -> int:
    left_counts: dict[str, int] = {}
    right_counts: dict[str, int] = {}
    for left, right, _ in candidates:
        left_counts[left["id"]] = left_counts.get(left["id"], 0) + 1
        right_counts[right["id"]] = right_counts.get(right["id"], 0) + 1
    created = 0
    for left, right, method_version in candidates:
        left_count = left_counts[left["id"]]
        right_count = right_counts[right["id"]]
        confirmed = left_count == 1 and right_count == 1
        evidence: dict[str, object] = {
            "date_left": left["posted_date"],
            "date_right": right["posted_date"],
            "description_evidence": True,
            "unique_candidate": confirmed,
        }
        if not confirmed:
            evidence["rejection"] = {
                "code": AMBIGUOUS_MUTUAL_MATCH,
                "left_candidates": left_count,
                "right_candidates": right_count,
            }
        rowcount = _insert_link(
            connection,
            left,
            right,
            method_version,
            status="CONFIRMED" if confirmed else "PROPOSED",
            evidence=evidence,
        )
        created += rowcount if confirmed else 0
    return created


def _opposite_exact_amount(left: sqlite3.Row, right: sqlite3.Row) -> bool:
    scale = max(left["amount_scale"], right["amount_scale"])
    left_value = left["amount_coefficient"] * 10 ** (scale - left["amount_scale"])
    right_value = right["amount_coefficient"] * 10 ** (scale - right["amount_scale"])
    return left_value == -right_value


def revoke_links_for_extraction(
    connection: sqlite3.Connection, extraction_run_id: str, code: str
) -> int:
    """Reject links whose legs lost eligibility; ``code`` is a centralized reason."""
    entry_ids = {
        row[0]
        for row in connection.execute(
            "SELECT id FROM ledger_entries "
            "WHERE extraction_run_id = ? AND source_family = 'DOCUMENT'",
            (extraction_run_id,),
        )
    }
    if not entry_ids:
        return 0
    revoked = 0
    rows = connection.execute(
        "SELECT id, left_entry_id, right_entry_id, evidence_json FROM transfer_links WHERE status = 'CONFIRMED'"
    ).fetchall()
    for row in rows:
        if row["left_entry_id"] not in entry_ids and row["right_entry_id"] not in entry_ids:
            continue
        evidence = json.loads(row["evidence_json"])
        evidence["revocation"] = {"code": code, "recorded_at": _now()}
        connection.execute(
            "UPDATE transfer_links SET status = 'REJECTED', evidence_json = ? WHERE id = ?",
            (_json(evidence), row["id"]),
        )
        revoked += 1
    return revoked
