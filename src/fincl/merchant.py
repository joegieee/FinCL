"""Phase 4 Track A — field projection and merchant normalization.

``ledger_entries`` carries no description column; the text lives on the row's
``raw_records`` / ``document_records`` evidence row. The projection from that row
to the three fields categorization consumes — ``description_text``,
``merchant_raw``, ``paynow_to`` — is a versioned, per-source recipe
(``entry-projection-2026-v1``), because a parser change that alters
``description_raw`` would otherwise silently recategorize history with no
recorded version change. ``merchant-norm-2026-v1`` then turns ``merchant_raw``
into ``merchant_normalized`` and, against the alias projection, into
``merchant_canonical``.

The profile token tables below are the frozen v1 contents; the design states the
shape, this states the contents. Bump the version string to change either.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from .common import canonical_json
from .errors import FinCLError

PROJECTION_VERSION = "entry-projection-2026-v1"
MERCHANT_NORM_VERSION = "merchant-norm-2026-v1"

# Revolut event kinds that carry a merchant in the description (§4.1 step 2).
_REVOLUT_MERCHANT_KINDS = frozenset({"CARD_PAYMENT", "REFUND", "PAYMENT"})

# Leading transaction-type word runs stripped from a DBS / PayLah / Mari-savings
# description to expose the merchant. Longest first so a prefix of another is
# never taken early.
_DOC_LEADING_RUNS: tuple[str, ...] = (
    "ADVICE FAST PAYMENT / RECEIPT",
    "DEBIT CARD TRANSACTION",
    "POINT-OF-SALE TRANSACTION",
    "FUNDS TRANSFER",
    "BILL PAYMENT",
    "QUICK CHEQUE DEPOSIT",
    "INTEREST EARNED",
    "SALARY",
)

# A description in one of these forms is a transfer / GIRO / ATM movement, not a
# merchant purchase: ``merchant_raw`` is NULL and the row is reachable only by
# the tier-5 description / paynow rules.
_DOC_NON_MERCHANT_MARKERS: tuple[str, ...] = (
    "PAYNOW",
    "PAY NOW",
    "FAST PAYMENT",
    "FUNDS TRANSFER",
    "GIRO",
    "ATM",
    "CASH WITHDRAWAL",
    "SEND MONEY",
    "MONEYSEND",
    "MONEE",
    "TOP-UP TO PAYLAH",
    "TRANSFER TO",
    "TRANSFER FROM",
)

# Mari-card rows that are not merchant purchases.
_MARI_CARD_NON_MERCHANT_MARKERS: tuple[str, ...] = (
    "PAYMENT RECEIVED",
    "PAYMENT - THANK YOU",
    "INSTALMENT FINANCING",
    "INSTALLMENT FINANCING",
    "LATE PAYMENT",
    "FINANCE CHARGE",
    "ANNUAL FEE",
    "GST",
)

# Trailing tails removed from a Mari-card merchant description: a foreign-amount
# tail (``USD 12.34`` / ``EUR 45.00``) and an instalment-plan suffix
# (``3/12 Instalment`` / ``12M``).
_MARI_CARD_FOREIGN_TAIL_RE = re.compile(
    r"\s+[A-Z]{3}\s+[\d,]+\.\d{2}\s*$"
)
_MARI_CARD_PLAN_SUFFIX_RE = re.compile(
    r"\s+(?:\d+\s*/\s*\d+\s+INSTAL?LMENT|\d+\s*M)\s*$", re.IGNORECASE
)

# PayNow / GIRO recipient extraction (§4.1 step 3), first match wins.
_RECIPIENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|\n)\s*(?:PAYNOW\s+)?TO:\s*(.+?)\s*(?:\n|$)", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*(?:PAYNOW\s+TO|TRANSFER\s+TO)\s+(.+?)\s*(?:\n|$)", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*GIRO\s*-\s*(.+?)\s*(?:\n|$)", re.IGNORECASE),
)
# A recipient run should not swallow a trailing reference token or the DBS
# "OTHER" purpose word.
_RECIPIENT_TAIL_RE = re.compile(
    r"\s+(?:[A-Z0-9]{12,}|OTHER|PAYNOW TRANSFER|INV-\S+)\s*$", re.IGNORECASE
)

# Merchant-normalization trailing-noise set (§4.2).
_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(?:PTE\.?\s+LT(?:D)?|PTE\s+LTD|LLP|\(S\))\s*$"
)
_LOCATION_TAIL_RE = re.compile(r"^(?P<head>.{6,})\s+-\s+\S.*$")
_TRAILING_SG_RE = re.compile(r"\s+(?:SINGAPORE|SGP|SI\s+SGP|SG)\s*$")
_TRAILING_NUMBER_RE = re.compile(r"\s+\d{3,}\s*$")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class FieldProjection:
    description_text: str | None
    merchant_raw: str | None
    paynow_to: str | None


def _collapse(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = _WS_RE.sub(" ", text).strip()
    return collapsed or None


def _recipient(description_raw: str | None) -> str | None:
    if not description_raw:
        return None
    for pattern in _RECIPIENT_PATTERNS:
        match = pattern.search(description_raw)
        if match is None:
            continue
        run = _RECIPIENT_TAIL_RE.sub("", match.group(1).strip())
        run = _WS_RE.sub(" ", run).strip().upper()
        if run:
            return run
    return None


def _profile_family(source_profile_version: str) -> str:
    for prefix, family in (
        ("revolut-csv-", "revolut"),
        ("dbs-consolidated-", "doc"),
        ("dbs-paylah-", "doc"),
        ("mari-savings-", "doc"),
        ("trust-savings-", "doc"),
        ("mari-card-", "mari-card"),
    ):
        if source_profile_version.startswith(prefix):
            return family
    raise FinCLError(f"no field-projection recipe for profile {source_profile_version!r}")


def _strip_leading_run(text: str) -> str:
    upper = text.upper()
    for run in _DOC_LEADING_RUNS:
        if upper.startswith(run):
            return text[len(run) :].strip(" -:").strip()
    return text


def _doc_merchant_raw(description_text: str | None) -> str | None:
    if description_text is None:
        return None
    upper = description_text.upper()
    if any(marker in upper for marker in _DOC_NON_MERCHANT_MARKERS):
        return None
    stripped = _strip_leading_run(description_text)
    return stripped or None


def _mari_card_merchant_raw(description_text: str | None) -> str | None:
    if description_text is None:
        return None
    upper = description_text.upper()
    if any(marker in upper for marker in _MARI_CARD_NON_MERCHANT_MARKERS):
        return None
    trimmed = _MARI_CARD_FOREIGN_TAIL_RE.sub("", description_text)
    trimmed = _MARI_CARD_PLAN_SUFFIX_RE.sub("", trimmed).strip()
    return trimmed or None


def project_fields(
    *,
    source_profile_version: str,
    event_kind: str,
    description_raw: str | None,
    reference_raw: str | None = None,
) -> FieldProjection:
    """Derive ``(description_text, merchant_raw, paynow_to)`` for one entry.

    ``document_records.reference_raw`` is a reference number, never description
    text, so it is not folded into ``description_text`` and matches no predicate.
    A NULL ``description_raw`` propagates as NULL — never coerced to ``""``.
    """
    description_text = _collapse(description_raw)
    paynow_to = _recipient(description_raw)
    family = _profile_family(source_profile_version)
    if family == "revolut":
        merchant_raw = description_text if event_kind in _REVOLUT_MERCHANT_KINDS else None
    elif family == "mari-card":
        merchant_raw = _mari_card_merchant_raw(description_text)
    else:  # doc
        merchant_raw = _doc_merchant_raw(description_text)
    return FieldProjection(description_text, merchant_raw, paynow_to)


def normalize_merchant(merchant_raw: str | None) -> str | None:
    """``merchant_raw`` -> ``merchant_normalized`` under ``merchant-norm-2026-v1``."""
    if merchant_raw is None:
        return None
    text = _WS_RE.sub(" ", merchant_raw).strip().upper()
    if not text:
        return None
    previous = None
    while previous != text:
        previous = text
        text = _LEGAL_SUFFIX_RE.sub("", text).strip()
        text = _TRAILING_SG_RE.sub("", text).strip()
        text = _TRAILING_NUMBER_RE.sub("", text).strip()
    location = _LOCATION_TAIL_RE.match(text)
    if location is not None:
        text = location.group("head").strip()
    return text or None


# --------------------------------------------------------------------------- #
# Alias resolution (§4.3 / §4.4)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Canonicalization:
    merchant_canonical: str | None
    alias_applied: bool
    alias_ambiguous: bool


def detect_alias_collisions(aliases: dict[str, list[dict[str, str]]]) -> None:
    """Refuse two aliases whose different canonicals carry a byte-identical pattern.

    Nested prefixes are not a collision — longest-prefix-wins resolves them — so
    the only load-time hazard is a duplicate ``exact`` / ``prefix`` / ``regex``
    literal across canonicals (§4.4 step 5).
    """
    seen: dict[str, str] = {}
    for canonical, patterns in aliases.items():
        for pattern in patterns:
            key = canonical_json(pattern)
            if key in seen and seen[key] != canonical:
                raise FinCLError(
                    f"merchant alias pattern {pattern} is registered under both "
                    f"{seen[key]!r} and {canonical!r}"
                )
            seen[key] = canonical


def resolve_canonical(
    merchant_normalized: str | None,
    aliases: dict[str, list[dict[str, str]]],
) -> Canonicalization:
    """Exact beats prefix (longest) beats regex; regex overlap fails closed (§4.4)."""
    if merchant_normalized is None:
        return Canonicalization(None, False, False)
    exact_hits: list[str] = []
    prefix_hits: list[tuple[int, str]] = []
    regex_hits: list[str] = []
    for canonical, patterns in aliases.items():
        for pattern in patterns:
            if "exact" in pattern and pattern["exact"] == merchant_normalized:
                exact_hits.append(canonical)
            elif "prefix" in pattern and merchant_normalized.startswith(pattern["prefix"]):
                prefix_hits.append((len(pattern["prefix"]), canonical))
            elif "regex" in pattern and re.fullmatch(pattern["regex"], merchant_normalized):
                regex_hits.append(canonical)
    if exact_hits:
        if len(set(exact_hits)) > 1:
            return Canonicalization(merchant_normalized, False, True)
        return Canonicalization(exact_hits[0], True, False)
    if prefix_hits:
        longest = max(length for length, _ in prefix_hits)
        winners = {canonical for length, canonical in prefix_hits if length == longest}
        if len(winners) > 1:
            return Canonicalization(merchant_normalized, False, True)
        return Canonicalization(next(iter(winners)), True, False)
    if regex_hits:
        if len(set(regex_hits)) > 1:
            return Canonicalization(merchant_normalized, False, True)
        return Canonicalization(regex_hits[0], True, False)
    return Canonicalization(merchant_normalized, False, False)


def load_alias_projection(connection: sqlite3.Connection) -> dict[str, list[dict[str, str]]]:
    aliases = {
        row["canonical"]: json.loads(row["patterns_json"])
        for row in connection.execute(
            "SELECT canonical, patterns_json FROM merchant_aliases"
        )
    }
    detect_alias_collisions(aliases)
    return aliases


def recompute_merchant_observations(connection: sqlite3.Connection) -> int:
    """Rebuild ``merchant_observations`` for every entry in the selected set.

    A pure function of the selected ledger entries plus the alias projection and
    the two version constants, so a rebuild reproduces it (§3.6).
    """
    aliases = load_alias_projection(connection)
    rows = connection.execute(
        """
        SELECT e.id, e.event_kind, x.source_profile_version,
               COALESCE(r.description_raw, d.description_raw) AS description_raw,
               d.reference_raw AS reference_raw
        FROM ledger_entries e
        JOIN extraction_runs x
          ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'
        LEFT JOIN raw_records r ON r.id = e.raw_record_id
        LEFT JOIN document_records d ON d.id = e.document_record_id
        """
    ).fetchall()
    with connection:
        connection.execute("DELETE FROM merchant_observations")
        for row in rows:
            projection = project_fields(
                source_profile_version=row["source_profile_version"],
                event_kind=row["event_kind"],
                description_raw=row["description_raw"],
                reference_raw=row["reference_raw"],
            )
            normalized = normalize_merchant(projection.merchant_raw)
            canonicalization = resolve_canonical(normalized, aliases)
            connection.execute(
                """
                INSERT INTO merchant_observations(
                    entry_id, description_text, paynow_to, merchant_raw,
                    merchant_normalized, merchant_canonical, alias_applied,
                    alias_ambiguous, norm_version, projection_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    projection.description_text,
                    projection.paynow_to,
                    projection.merchant_raw,
                    normalized,
                    canonicalization.merchant_canonical,
                    int(canonicalization.alias_applied),
                    int(canonicalization.alias_ambiguous),
                    MERCHANT_NORM_VERSION,
                    PROJECTION_VERSION,
                ),
            )
    return len(rows)
