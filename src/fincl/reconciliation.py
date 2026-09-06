"""Cross-source transfer reconciliation (DESIGN-application-contract.md §1).

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
DBS_REVOLUT_CARD_TOPUP_LINKER = SameCurrencyLinker(
    DBS_REVOLUT_CARD_TOPUP_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"CARD_PAYMENT"})),
    EligibleLinkLeg("REVOLUT_CSV", "SGD", "IN", frozenset({"TOPUP", "TRANSFER"})),
    max_days=2,
)
DBS_REVOLUT_PAYNOW_TOPUP_LINKER = SameCurrencyLinker(
    DBS_REVOLUT_PAYNOW_TOPUP_LINK_VERSION,
    EligibleLinkLeg("DBS_CONSOLIDATED", "SGD", "OUT", frozenset({"TRANSFER"})),
    EligibleLinkLeg("REVOLUT_CSV", "SGD", "IN", frozenset({"TOPUP", "TRANSFER"})),
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
    EligibleLinkLeg("REVOLUT_CSV", None, "OUT", frozenset({"EXCHANGE"})),
    EligibleLinkLeg("REVOLUT_CSV", None, "IN", frozenset({"EXCHANGE"})),
)
AUTOMATIC_SAME_CURRENCY_LINK_METHODS = (
    DBS_PAYLAH_LINKER.method_version,
    DBS_REVOLUT_LEGACY_LINK_VERSION,
    DBS_REVOLUT_CARD_TOPUP_LINKER.method_version,
    DBS_REVOLUT_PAYNOW_TOPUP_LINKER.method_version,
    DBS_MARIBANK_PAYNOW_TOPUP_LINKER.method_version,
    MARI_SAVINGS_CARD_REPAYMENT_LINKER.method_version,
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
               'REVOLUT_CSV' AS source_type, r.description_raw
        FROM ledger_entries e
        JOIN raw_records r ON r.id = e.raw_record_id
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE e.source_family = 'CSV'
          AND x.selection_state = 'SELECTED' AND e.analytics_eligible = 1
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


def _is_revolut_paynow_topup(description: str | None) -> bool:
    text = (description or "").upper()
    return "PAYNOW TRANSFER" in text and "TO: REVOLUT TECHNOLOGIES SINGAPORE" in text


def link_revolut_exchanges(connection: sqlite3.Connection) -> int:
    """Link complete Revolut exchange pairs and retain their exact implied rate."""
    rows = connection.execute(
        """
        SELECT e.id, e.extraction_run_id,
               COALESCE(e.alt_date_value, e.effective_at) AS started_at,
               e.currency_pocket, e.amount_text, e.amount_coefficient, e.amount_scale,
               e.economic_direction, e.event_kind, a.source_type
        FROM ledger_entries e
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        JOIN accounts a ON a.id = e.account_id
        WHERE e.source_family = 'CSV'
          AND x.selection_state = 'SELECTED' AND e.analytics_eligible = 1
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
        if len(group) != 2:
            continue
        outgoing = [row for row in group if row["economic_direction"] == "OUT"]
        incoming = [row for row in group if row["economic_direction"] == "IN"]
        if len(outgoing) != 1 or len(incoming) != 1:
            continue
        left, right = outgoing[0], incoming[0]
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
                left_amount_scale, right_currency, right_amount_text, right_amount_coefficient,
                right_amount_scale, implied_rate_numerator, implied_rate_denominator,
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


def _candidate_pairs(
    left_rows: list[sqlite3.Row],
    right_rows: list[sqlite3.Row],
    method_version: str,
    *,
    max_days: int,
    key: Callable[[sqlite3.Row], str | None] | None = None,
) -> list[tuple[sqlite3.Row, sqlite3.Row, str]]:
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, str]] = []
    for left in left_rows:
        for right in right_rows:
            if left["currency_pocket"] != right["currency_pocket"]:
                continue
            if not _opposite_exact_amount(left, right):
                continue
            day_difference = abs(
                (date.fromisoformat(left["posted_date"]) - date.fromisoformat(right["posted_date"])).days
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
        link_id = _stable_id("link", left["id"], right["id"], method_version)
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
                "CONFIRMED" if confirmed else "PROPOSED",
                _json(evidence),
                _now(),
            ),
        )
        created += cursor.rowcount if confirmed else 0
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
