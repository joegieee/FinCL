from __future__ import annotations

import calendar
import hashlib
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pdfplumber

from .money import MAX_ABS_COEFFICIENT, ExactMoney, format_decimal

PDF_PARSER_VERSION = "pdf-geometry-parser-v1"
PDF_EXTRACTION_POLICY_VERSION = "phase2-policy-v1"
# v2 (2026-09): the statement period is now derived from the billing cycle the
# statement date implies, not from the first/last transaction date. A boundary
# day with no transaction no longer shrinks the period, so consecutive
# statements stay exactly adjacent and the F23 coverage contract has no false
# one-day gaps. Bumping the profile version supersedes every v1 extraction run
# and marks the DBS/PayLah evidence for re-extraction (see selection.py).
DBS_PROFILE_VERSION = "dbs-consolidated-2025-v2"
PAYLAH_OLD_PROFILE_VERSION = "dbs-paylah-legacy-2025-v2"
PAYLAH_NEW_PROFILE_VERSION = "dbs-paylah-account-summary-2026-v2"
DBS_BALANCE_EQUATION_VERSION = "dbs-next-equals-prior-minus-debit-plus-credit-v1"
PAYLAH_BALANCE_EQUATION_VERSION = "paylah-closing-equals-opening-plus-cr-minus-db-v1"
MARI_SAVINGS_3COL_PROFILE = "mari-savings-3col-interest-2025-v1"
MARI_SAVINGS_4COL_PROFILE = "mari-savings-4col-rate-2026-v1"
MARI_SAVINGS_BALANCE_EQUATION_VERSION = (
    "mari-savings-ending-equals-starting-minus-outgoing-plus-incoming-v1"
)
MARI_SAVINGS_INTEREST_EQUATION_VERSION = "mari-savings-total-interest-equals-daily-sum-v1"
MARI_SAVINGS_PROFILE_LABEL = "mari-savings"

# Geometry constants, in PDF points unless noted, calibrated against the frozen
# source profiles above. If any of these change, the extracted layout changes
# too: bump the affected *_PROFILE_VERSION so prior extractions stay
# distinguishable.
LINE_GROUP_TOLERANCE_PT = 2.0
"""Max difference in a word's top coordinate for it to join an existing line."""

LINE_GROUP_LOOKBACK = 4
"""How many of the most recent lines `_group_lines` reconsiders for a word."""

DBS_AMOUNT_COLUMN_TOLERANCE_PT = 24
"""Max gap between a transaction amount's right edge and a header column edge."""

DBS_SUPPORTING_COLUMN_TOLERANCE_PT = 28
"""As above, for supporting rows, whose figures are printed slightly wider."""

DBS_DESCRIPTION_MAX_X0_PT = 340
"""Words starting left of this on a transaction line are description, not money."""

PAYLAH_DATE_INFERENCE_WINDOW_DAYS = 40
"""Oldest a day/month PayLah row may resolve to, relative to the statement date."""

_DBS_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_PAYLAH_DAY_RE = re.compile(r"^\d{2}$")
_PAYLAH_MONTH_RE = re.compile(r"^[A-Z][a-z]{2}$")
_MONEY_RE = re.compile(r"^(?:-?\d[\d,]*\.\d{2}|\d[\d,]*\.\d{2}-)$")
_MARI_DAY_RE = re.compile(r"^\d{2}$")
_MARI_MONTH_RE = re.compile(r"^[A-Z]{3}$")
_MARI_FULL_DATE_RE = re.compile(r"\b(\d{2} [A-Z]{3} \d{4})\b")
_MARI_RATE_RE = re.compile(r"^\d+(?:\.\d+)?%$")
_CURRENCY_NAMES = {
    "SINGAPORE DOLLAR": "SGD",
    "THAILAND BAHT": "THB",
    "UNITED STATES DOLLAR": "USD",
}


@dataclass(frozen=True, slots=True)
class DocumentProblem:
    severity: str
    code: str
    message: str
    context: dict[str, object] = field(default_factory=dict)
    source_anchor: str | None = None


@dataclass(slots=True)
class DocumentSection:
    ordinal: int
    section_kind: str
    currency: str | None
    heading_raw: str
    start_page: int
    end_page: int


@dataclass(slots=True)
class DocumentRecord:
    source_anchor: str
    ordinal: int
    page_number: int
    bbox: tuple[float, float, float, float]
    record_kind: str
    raw_text: str
    raw_fields: dict[str, object]
    section_ordinal: int | None = None
    date_raw: str | None = None
    date_value: str | None = None
    description_raw: str | None = None
    reference_raw: str | None = None
    debit: ExactMoney | None = None
    credit: ExactMoney | None = None
    amount: ExactMoney | None = None
    direction_marker: str | None = None
    currency_raw: str | None = None
    balance: ExactMoney | None = None
    confidence: str = "HIGH"
    required_fields_complete: bool = True
    date_precision: str = "DAY"
    """Effective-date precision for the canonical entry: DAY, MONTH, or UNKNOWN."""
    alt_date_role: str | None = None
    """A typed second source date: STARTED or TRANSACTION."""
    alt_date_value: str | None = None
    alt_date_raw: str | None = None
    foreign_currency: str | None = None
    foreign_amount: ExactMoney | None = None


@dataclass(frozen=True, slots=True)
class PageCoverage:
    page_number: int
    classification: str
    word_count: int
    upright_word_count: int
    rotated_ignored_count: int
    transaction_rows: int
    supporting_records: int
    status: str
    context: dict[str, object]


@dataclass(frozen=True, slots=True)
class DocumentCheck:
    currency_pocket: str
    check_type: str
    equation_version: str
    expected: ExactMoney | None
    actual: ExactMoney | None
    difference: ExactMoney | None
    passed: bool | None
    blocking: bool
    context: dict[str, object]


@dataclass(slots=True)
class ParsedDocument:
    institution: str
    source_type: str
    account_id: str
    account_display_name: str
    masked_identifier: str
    source_profile_version: str
    statement_date: str
    period_start: str | None
    period_end: str | None
    statement_identity: str
    sections: list[DocumentSection]
    records: list[DocumentRecord]
    pages: list[PageCoverage]
    checks: list[DocumentCheck]
    problems: list[DocumentProblem]
    # Additive optional field (v0.4 slice 1): the full parsed credit-card
    # statement summary. `parse_mari_card` fills it; every other parser leaves it
    # `None`. Persisted into `card_statement_facts` by
    # `document_service._persist_document`. Not part of the profile version — a
    # richer summary is an additive read of the same evidence, not a re-parse.
    card_summary: _MariCardSummary | None = None

    @property
    def accepted(self) -> bool:
        return not any(problem.severity == "BLOCKING" for problem in self.problems) and not any(
            check.blocking and check.passed is not True for check in self.checks
        )


@dataclass(slots=True)
class _Line:
    words: list[dict[str, Any]]

    @property
    def text(self) -> str:
        return " ".join(word["text"] for word in self.words)

    @property
    def top(self) -> float:
        return min(word["top"] for word in self.words)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (
            min(word["x0"] for word in self.words),
            min(word["top"] for word in self.words),
            max(word["x1"] for word in self.words),
            max(word["bottom"] for word in self.words),
        )


def generic_extraction_identity(evidence_sha256: str, source_profile_version: str) -> str:
    payload = "\0".join(
        (
            evidence_sha256,
            source_profile_version,
            PDF_PARSER_VERSION,
            PDF_EXTRACTION_POLICY_VERSION,
        )
    ).encode("utf-8")
    return "ext_" + hashlib.sha256(payload).hexdigest()


def _shift_one_month_back(anchor: date) -> date:
    """The same day-of-month one calendar month before ``anchor``.

    The day is clamped to the target month's length so 31 March maps to the
    last day of February.
    """
    year = anchor.year - 1 if anchor.month == 1 else anchor.year
    month = 12 if anchor.month == 1 else anchor.month - 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _calendar_month_period(statement_date: date) -> tuple[date, date]:
    """DBS consolidated statements bill the calendar month the statement dates.

    The statement date is the month-end closing date; the period runs from the
    first of that month to the statement date. Anchoring on the date rather than
    the first/last posted transaction keeps successive statements adjacent.
    """
    return statement_date.replace(day=1), statement_date


def _paylah_cycle_period(statement_date: date) -> tuple[date, date]:
    """DBS PayLah! bills a monthly cycle that closes on the statement date.

    The next cycle opens the following day, so the period is
    ``[statement_date - 1 month + 1 day, statement_date]``. Deriving it from the
    statement date (not the first/last wallet transaction) means a boundary day
    with no transaction cannot open a false one-day gap between statements. A
    transaction booked a day or two after the close still appears on the next
    statement and is covered by that statement's interval.
    """
    return _shift_one_month_back(statement_date) + timedelta(days=1), statement_date


def parse_dbs_consolidated(data: bytes) -> ParsedDocument:
    records: list[DocumentRecord] = []
    sections: list[DocumentSection] = []
    coverage: list[PageCoverage] = []
    problems: list[DocumentProblem] = []
    statement_date: date | None = None
    current_currency: str | None = None
    current_section: DocumentSection | None = None
    ordinal = 0
    expected_date_rows = 0

    with pdfplumber.open(io.BytesIO(data), password="") as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            all_words = page.extract_words(
                x_tolerance=2,
                y_tolerance=2,
                extra_attrs=["upright"],
            )
            upright = [word for word in all_words if word.get("upright", True)]
            rotated_count = len(all_words) - len(upright)
            lines = _group_lines(upright)
            page_text = "\n".join(line.text for line in lines)
            if statement_date is None:
                match = re.search(r"Account Summary as (?:at|of) (\d{1,2} \w+ \d{4})", page_text)
                if match:
                    statement_date = datetime.strptime(match.group(1), "%d %b %Y").date()

            page_rows = 0
            page_supporting = 0
            has_summary = "Account Summary" in page_text
            has_information = "For Your Information" in page_text or "Messages For You" in page_text
            has_transactions = "Transaction Details" in page_text or any(
                _DBS_DATE_RE.fullmatch(line.words[0]["text"]) for line in lines if line.words
            )
            classification = _page_classification(has_summary, has_transactions, has_information)
            header_rights: tuple[float, float, float] | None = None
            pending: DocumentRecord | None = None

            def flush_pending() -> None:
                nonlocal pending, ordinal, page_rows
                if pending is not None:
                    records.append(pending)
                    page_rows += pending.record_kind == "TRANSACTION"
                    pending = None

            for line in lines:
                text = line.text.strip()
                if not text:
                    continue
                if _is_dbs_header(line):
                    flush_pending()
                    header_rights = _dbs_header_rights(line)
                    continue
                currency_name = _currency_heading(text)
                if currency_name is not None:
                    flush_pending()
                    current_currency = _CURRENCY_NAMES[currency_name]
                    if current_section is not None:
                        current_section.end_page = page_number
                    current_section = DocumentSection(
                        ordinal=len(sections) + 1,
                        section_kind="CURRENCY_LEDGER",
                        currency=current_currency,
                        heading_raw=text,
                        start_page=page_number,
                        end_page=page_number,
                    )
                    sections.append(current_section)
                    continue
                first = line.words[0]["text"]
                if _DBS_DATE_RE.fullmatch(first):
                    flush_pending()
                    ordinal += 1
                    parsed = _parse_dbs_transaction_line(
                        line,
                        ordinal,
                        page_number,
                        current_section,
                        current_currency,
                        header_rights,
                    )
                    if (
                        parsed.balance is not None
                        and parsed.debit is None
                        and parsed.credit is None
                        and not parsed.description_raw
                    ):
                        parsed.record_kind = "SUPPORTING_BALANCE"
                        parsed.raw_fields = {"supporting_kind": "DATE_CLOSING_BALANCE"}
                        parsed.confidence = "HIGH"
                        parsed.required_fields_complete = True
                        records.append(parsed)
                        page_supporting += 1
                        continue
                    expected_date_rows += 1
                    pending = parsed
                    if not parsed.required_fields_complete:
                        problems.append(
                            DocumentProblem(
                                "BLOCKING",
                                "DBS_TRANSACTION_UNRESOLVED",
                                "dated DBS row is missing an unambiguous amount, balance, currency, or date",
                                {"page_number": page_number, "raw_text": parsed.raw_text},
                                parsed.source_anchor,
                            )
                        )
                    continue
                supporting_kind = _dbs_supporting_kind(text)
                if supporting_kind is not None:
                    flush_pending()
                    ordinal += 1
                    support = _parse_dbs_supporting_line(
                        line,
                        ordinal,
                        page_number,
                        current_section,
                        current_currency,
                        header_rights,
                        supporting_kind,
                    )
                    records.append(support)
                    page_supporting += 1
                    continue
                if pending is not None and _is_dbs_description_continuation(line):
                    pending.raw_text += "\n" + text
                    pending.description_raw = (
                        pending.description_raw + "\n" + text if pending.description_raw else text
                    )
                    pending.reference_raw = _extract_reference(pending.description_raw)
                    pending.bbox = _merge_bbox(pending.bbox, line.bbox)

            flush_pending()
            if current_section is not None:
                current_section.end_page = page_number
            status = "ACCOUNTED" if classification != "UNKNOWN" else "UNRESOLVED"
            if status == "UNRESOLVED":
                problems.append(
                    DocumentProblem(
                        "BLOCKING",
                        "UNCLASSIFIED_PDF_PAGE",
                        "DBS page could not be classified structurally",
                        {"page_number": page_number},
                    )
                )
            coverage.append(
                PageCoverage(
                    page_number,
                    classification,
                    len(all_words),
                    len(upright),
                    rotated_count,
                    page_rows,
                    page_supporting,
                    status,
                    {"profile": DBS_PROFILE_VERSION},
                )
            )

    if statement_date is None:
        problems.append(DocumentProblem("BLOCKING", "STATEMENT_DATE_MISSING", "DBS statement date was not found"))
        statement_date = date.min
    parsed_rows = sum(record.record_kind == "TRANSACTION" for record in records)
    if parsed_rows != expected_date_rows:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "DBS_ROW_COVERAGE_MISMATCH",
                "dated row accounting does not match extracted transaction records",
                {"dated_rows": expected_date_rows, "parsed_rows": parsed_rows},
            )
        )
    checks = _reconcile_dbs(records, sections, problems)
    statement_date_text = statement_date.isoformat()
    if statement_date == date.min:
        period_start_text = period_end_text = None
    else:
        period_start, period_end = _calendar_month_period(statement_date)
        period_start_text = period_start.isoformat()
        period_end_text = period_end.isoformat()
    return ParsedDocument(
        institution="DBS",
        source_type="DBS_CONSOLIDATED",
        account_id="dbs-my-account",
        account_display_name="DBS My Account",
        masked_identifier="DBS deposit account (masked)",
        source_profile_version=DBS_PROFILE_VERSION,
        statement_date=statement_date_text,
        period_start=period_start_text,
        period_end=period_end_text,
        statement_identity=f"dbs-consolidated:dbs-my-account:{statement_date_text}",
        sections=sections,
        records=records,
        pages=coverage,
        checks=checks,
        problems=problems,
    )


def parse_paylah(data: bytes) -> ParsedDocument:
    records: list[DocumentRecord] = []
    coverage: list[PageCoverage] = []
    problems: list[DocumentProblem] = []
    statement_date: date | None = None
    profile = PAYLAH_OLD_PROFILE_VERSION
    ordinal = 0
    opening: ExactMoney | None = None
    closing: ExactMoney | None = None
    expected_rows = 0

    with pdfplumber.open(io.BytesIO(data), password="") as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(x_tolerance=2, y_tolerance=2, extra_attrs=["upright"])
            upright = [word for word in words if word.get("upright", True)]
            lines = _group_lines(upright)
            page_text = "\n".join(line.text for line in lines)
            if statement_date is None:
                full_dates = re.findall(r"\b\d{2} [A-Z][a-z]{2} \d{4}\b", page_text)
                if full_dates:
                    statement_date = datetime.strptime(full_dates[0], "%d %b %Y").date()
            if "Total Balance Carried Forward" in page_text or _has_account_summary(lines):
                profile = PAYLAH_NEW_PROFILE_VERSION

            page_rows = 0
            page_supporting = 0
            pending: DocumentRecord | None = None
            has_information = any(
                marker in page_text
                for marker in (
                    "USEFUL INFORMATION ON YOUR DBS PAYLAH! WALLET",
                    "Important Notice",
                    "Deposit Insurance Scheme",
                    "Terms & Conditions",
                    "Scheme member by law.",
                )
            )
            has_transactions = any(_is_paylah_transaction_line(line) for line in lines)
            has_summary = "Statement of Account" in page_text
            classification = _page_classification(has_summary, has_transactions, has_information)

            def flush_pending() -> None:
                nonlocal pending, page_rows
                if pending is not None:
                    records.append(pending)
                    page_rows += 1
                    pending = None

            for line in lines:
                text = line.text.strip()
                if _is_paylah_transaction_line(line):
                    flush_pending()
                    expected_rows += 1
                    ordinal += 1
                    record = _parse_paylah_transaction_line(
                        line, ordinal, page_number, statement_date
                    )
                    pending = record
                    if not record.required_fields_complete:
                        problems.append(
                            DocumentProblem(
                                "BLOCKING",
                                "PAYLAH_TRANSACTION_UNRESOLVED",
                                "PayLah row is missing an unambiguous amount, marker, or inferred date",
                                {"page_number": page_number, "raw_text": record.raw_text},
                                record.source_anchor,
                            )
                        )
                    continue
                if "PREVIOUS BALANCE" in text:
                    flush_pending()
                    value = _rightmost_money(line)
                    if value is not None:
                        opening = value
                        ordinal += 1
                        records.append(
                            _supporting_record(
                                ordinal,
                                page_number,
                                line,
                                "OPENING_BALANCE",
                                "SGD",
                                balance=value,
                                section_ordinal=1,
                            )
                        )
                        page_supporting += 1
                    continue
                if "Total Balance Carried Forward" in text or re.match(r"^Total\s*:", text):
                    flush_pending()
                    value = _rightmost_money(line)
                    if value is not None:
                        closing = value
                        ordinal += 1
                        records.append(
                            _supporting_record(
                                ordinal,
                                page_number,
                                line,
                                "CLOSING_BALANCE",
                                "SGD",
                                balance=value,
                                section_ordinal=1,
                            )
                        )
                        page_supporting += 1
                    continue
                if pending is not None and "REF NO" in text.upper():
                    pending.raw_text += "\n" + text
                    pending.reference_raw = text
                    pending.bbox = _merge_bbox(pending.bbox, line.bbox)
            flush_pending()
            status = "ACCOUNTED" if classification != "UNKNOWN" else "UNRESOLVED"
            if status == "UNRESOLVED":
                problems.append(
                    DocumentProblem(
                        "BLOCKING",
                        "UNCLASSIFIED_PDF_PAGE",
                        "PayLah page could not be classified structurally",
                        {"page_number": page_number},
                    )
                )
            coverage.append(
                PageCoverage(
                    page_number,
                    classification,
                    len(words),
                    len(upright),
                    len(words) - len(upright),
                    page_rows,
                    page_supporting,
                    status,
                    {"profile": profile},
                )
            )

    if statement_date is None:
        problems.append(DocumentProblem("BLOCKING", "STATEMENT_DATE_MISSING", "PayLah statement date was not found"))
        statement_date = date.min
    parsed_rows = sum(record.record_kind == "TRANSACTION" for record in records)
    if parsed_rows != expected_rows:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "PAYLAH_ROW_COVERAGE_MISMATCH",
                "dated/amount row accounting does not match transaction records",
                {"dated_rows": expected_rows, "parsed_rows": parsed_rows},
            )
        )
    if opening is None or closing is None:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "PAYLAH_BALANCE_BOUNDARY_MISSING",
                "PayLah opening or closing balance is unavailable",
                {"opening_available": opening is not None, "closing_available": closing is not None},
            )
        )
    checks = _reconcile_paylah(records, opening, closing)
    if any(check.blocking and check.passed is not True for check in checks):
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "PAYLAH_BALANCE_MISMATCH",
                "PayLah aggregate balance equation failed",
            )
        )
    statement_date_text = statement_date.isoformat()
    if statement_date == date.min:
        period_start_text = period_end_text = None
    else:
        period_start, period_end = _paylah_cycle_period(statement_date)
        period_start_text = period_start.isoformat()
        period_end_text = period_end.isoformat()
    return ParsedDocument(
        institution="DBS",
        source_type="DBS_PAYLAH",
        account_id="dbs-paylah-wallet",
        account_display_name="DBS PayLah! Wallet",
        masked_identifier="PayLah wallet (masked)",
        source_profile_version=profile,
        statement_date=statement_date_text,
        period_start=period_start_text,
        period_end=period_end_text,
        statement_identity=f"dbs-paylah:dbs-paylah-wallet:{statement_date_text}",
        sections=[DocumentSection(1, "WALLET_LEDGER", "SGD", "PayLah! Wallet", 1, len(coverage))],
        records=records,
        pages=coverage,
        checks=checks,
        problems=problems,
    )


MARI_SAVINGS_OUTGOING_INCOMING_SPLIT_PT = 470
"""Money right-edge below this is the OUTGOING column; above it is INCOMING."""

MARI_COLUMN_MATCH_TOLERANCE_PT = 30
"""Max gap between a money right-edge and a printed column right-edge."""


@dataclass(slots=True)
class _MariSummary:
    starting: ExactMoney | None = None
    total_outgoing: ExactMoney | None = None
    total_incoming: ExactMoney | None = None
    ending: ExactMoney | None = None
    total_restated: ExactMoney | None = None


def parse_maribank_savings(data: bytes) -> ParsedDocument:
    """Extract one MariBank deposit-and-investment savings statement.

    Savings activity and the daily-interest schedule are separate tables: the
    daily rows are supporting records and only the single credited-interest line
    (printed with month precision) is a ledger candidate.  Investment sections
    are preserved as separate content and never folded into savings activity.
    """
    problems: list[DocumentProblem] = []
    records: list[DocumentRecord] = []
    ordinal = 0
    statement_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    account_number: str | None = None
    summary = _MariSummary()

    with pdfplumber.open(io.BytesIO(data), password="") as pdf:
        page_lines: list[tuple[int, list[_Line], dict[str, int]]] = []
        for page_number, page in enumerate(pdf.pages, start=1):
            all_words = page.extract_words(x_tolerance=2, y_tolerance=2, extra_attrs=["upright"])
            upright = [word for word in all_words if word.get("upright", True)]
            lines = _group_lines(upright)
            counts = {
                "all": len(all_words),
                "upright": len(upright),
                "rotated": len(all_words) - len(upright),
            }
            page_lines.append((page_number, lines, counts))
            page_text = "\n".join(line.text for line in lines)
            if statement_date is None:
                for line in lines:
                    if _MARI_FULL_DATE_RE.fullmatch(line.text.strip()):
                        statement_date = datetime.strptime(line.text.strip(), "%d %b %Y").date()
                        break
            period_match = re.search(
                r"STATEMENT PERIOD:\s*(\d{2} [A-Z]{3} \d{4})\s+to\s+(\d{2} [A-Z]{3} \d{4})",
                page_text,
            )
            if period_match and period_start is None:
                period_start = datetime.strptime(period_match.group(1), "%d %b %Y").date()
                period_end = datetime.strptime(period_match.group(2), "%d %b %Y").date()
            if account_number is None:
                account_match = re.search(
                    r"SAVINGS - ACCOUNT SUMMARY.*?ACCOUNT:\s*([0-9 ]+)", page_text, re.DOTALL
                )
                if account_match:
                    account_number = account_match.group(1).strip()

        flat: list[tuple[int, _Line]] = [
            (page_number, line) for page_number, lines, _ in page_lines for line in lines
        ]
        _parse_mari_account_summary(flat, summary, problems)
        profile, unknown_interest_profile = _mari_interest_profile(flat)
        if unknown_interest_profile:
            problems.append(
                DocumentProblem(
                    "BLOCKING",
                    "MARI_SAVINGS_UNKNOWN_INTEREST_PROFILE",
                    "MariBank savings interest table uses an unrecognized column arrangement",
                )
            )
        ordinal = _parse_mari_savings_transactions(
            flat, records, ordinal, period_start, period_end, problems
        )
        ordinal, total_interest_credited = _parse_mari_interest_schedule(
            flat, records, ordinal, profile, problems
        )
        ordinal = _parse_mari_investment_sections(flat, records, ordinal, problems)

    if statement_date is None:
        problems.append(
            DocumentProblem("BLOCKING", "STATEMENT_DATE_MISSING", "MariBank statement date was not found")
        )
        statement_date = date.min
    if period_start is None or period_end is None:
        problems.append(
            DocumentProblem(
                "BLOCKING", "MARI_STATEMENT_PERIOD_MISSING", "MariBank statement period was not found"
            )
        )

    transactions = [record for record in records if record.record_kind == "TRANSACTION"]
    checks = _reconcile_mari_savings(
        transactions, records, summary, total_interest_credited, problems
    )

    coverage = _mari_page_coverage(page_lines, records, MARI_SAVINGS_PROFILE_LABEL)
    for page_cov in coverage:
        if page_cov.status == "UNRESOLVED":
            problems.append(
                DocumentProblem(
                    "BLOCKING",
                    "UNCLASSIFIED_PDF_PAGE",
                    "MariBank savings page could not be classified structurally",
                    {"page_number": page_cov.page_number},
                )
            )

    statement_date_text = statement_date.isoformat()
    return ParsedDocument(
        institution="MariBank",
        source_type="MARI_SAVINGS",
        account_id="maribank-savings",
        account_display_name="MariBank Savings Account",
        masked_identifier=f"MariBank savings {account_number}" if account_number else "MariBank savings",
        source_profile_version=profile,
        statement_date=statement_date_text,
        period_start=(period_start.isoformat() if period_start else None),
        period_end=(period_end.isoformat() if period_end else None),
        statement_identity=f"mari-savings:maribank-savings:{statement_date_text}",
        sections=[
            DocumentSection(1, "SAVINGS_LEDGER", "SGD", "SAVINGS - TRANSACTION DETAILS", 1, len(coverage)),
            DocumentSection(2, "INTEREST_SCHEDULE", "SGD", "SAVINGS - INTEREST DETAILS", 1, len(coverage)),
            DocumentSection(3, "INVESTMENT", None, "INVESTMENTS", 1, len(coverage)),
        ],
        records=records,
        pages=coverage,
        checks=checks,
        problems=problems,
    )


_MARI_INTEREST_HEADER_3COL = "DATE PREVIOUS DAY BALANCE (SGD) INTEREST (SGD)"
_MARI_INTEREST_HEADER_4COL = "DATE PREVIOUS DAY BALANCE (SGD) INTEREST RATE* INTEREST (SGD)"


def _mari_interest_profile(flat: list[tuple[int, _Line]]) -> tuple[str, bool]:
    """Return (profile version, unknown-arrangement seen).

    A printed interest header that matches neither frozen arrangement is a
    profile refusal per the MariBank savings contract, not a best-effort shift.
    """
    header_seen = False
    for _, line in flat:
        norm = " ".join(line.text.split())
        if norm == _MARI_INTEREST_HEADER_3COL:
            return MARI_SAVINGS_3COL_PROFILE, False
        if norm == _MARI_INTEREST_HEADER_4COL:
            return MARI_SAVINGS_4COL_PROFILE, False
        if norm.startswith("DATE") and "BALANCE" in norm and "INTEREST" in norm:
            header_seen = True
    return MARI_SAVINGS_3COL_PROFILE, header_seen


def _parse_mari_account_summary(
    flat: list[tuple[int, _Line]],
    summary: _MariSummary,
    problems: list[DocumentProblem],
) -> None:
    in_summary = False
    for _, line in flat:
        text = line.text.strip()
        if "SAVINGS - ACCOUNT SUMMARY" in text:
            in_summary = True
            continue
        if not in_summary:
            continue
        if text.startswith("INVESTMENTS -"):
            break
        monies = [word for word in line.words if _MONEY_RE.fullmatch(word["text"])]
        if text.startswith("SAVINGS") and len(monies) == 4:
            summary.starting = _parse_printed_money(monies[0]["text"])
            summary.total_outgoing = _parse_printed_money(monies[1]["text"])
            summary.total_incoming = _parse_printed_money(monies[2]["text"])
            summary.ending = _parse_printed_money(monies[3]["text"])
        elif text.startswith("TOTAL:") and monies:
            summary.total_restated = _parse_printed_money(monies[-1]["text"])
    if summary.ending is None:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "MARI_SAVINGS_SUMMARY_MISSING",
                "MariBank savings account-summary figures were not found",
            )
        )


def _is_mari_transaction_anchor(line: _Line) -> tuple[str, str, str] | None:
    """Return (day, month, precision) if this line opens a savings transaction."""
    words = line.words
    if not words:
        return None
    if (
        len(words) >= 2
        and _MARI_DAY_RE.fullmatch(words[0]["text"])
        and _MARI_MONTH_RE.fullmatch(words[1]["text"])
        and _mari_row_money(line)
    ):
        return words[0]["text"], words[1]["text"], "DAY"
    if (
        _MARI_MONTH_RE.fullmatch(words[0]["text"])
        and _mari_row_money(line)
        and not _MARI_DAY_RE.fullmatch(words[0]["text"])
    ):
        return "", words[0]["text"], "MONTH"
    return None


def _mari_row_money(line: _Line) -> list[dict[str, Any]]:
    return [
        word
        for word in line.words
        if _MONEY_RE.fullmatch(word["text"]) and word["x1"] > 380
    ]


def _parse_mari_savings_transactions(
    flat: list[tuple[int, _Line]],
    records: list[DocumentRecord],
    ordinal: int,
    period_start: date | None,
    period_end: date | None,
    problems: list[DocumentProblem],
) -> int:
    region: list[tuple[int, _Line]] = []
    active = False
    for page_number, line in flat:
        text = line.text.strip()
        if "SAVINGS - TRANSACTION DETAILS" in text:
            active = True
            continue
        if not active:
            continue
        if text.startswith("SAVINGS - INTEREST DETAILS") or text.startswith("INVESTMENTS -"):
            active = False
            continue
        if _is_mari_header(text) or _is_mari_page_furniture(text):
            continue
        region.append((page_number, line))

    anchor_positions = [
        index for index, (_, line) in enumerate(region) if _is_mari_transaction_anchor(line)
    ]
    expected_rows = len(anchor_positions)
    # A transaction row is a tight visual cluster: its wrapping description sits
    # both above and below the dated amount baseline.  Assign every non-anchor
    # line on a page to the vertically nearest anchor on that same page.
    above: dict[int, list[_Line]] = {position: [] for position in anchor_positions}
    below: dict[int, list[_Line]] = {position: [] for position in anchor_positions}
    anchors_by_page: dict[int, list[int]] = {}
    for position in anchor_positions:
        anchors_by_page.setdefault(region[position][0], []).append(position)
    for index, (page_number, line) in enumerate(region):
        if index in above:
            continue
        candidates = anchors_by_page.get(page_number)
        if not candidates:
            continue
        nearest = min(candidates, key=lambda pos: abs(region[pos][1].top - line.top))
        (above if line.top < region[nearest][1].top else below)[nearest].append(line)

    for position in anchor_positions:
        page_number, anchor_line = region[position]
        day, month, precision = _is_mari_transaction_anchor(anchor_line)  # type: ignore[misc]
        ordinal += 1
        records.append(
            _build_mari_savings_record(
                anchor_line,
                sorted(above[position], key=lambda line: line.top),
                sorted(below[position], key=lambda line: line.top),
                ordinal,
                page_number,
                day,
                month,
                precision,
                period_start,
                period_end,
                problems,
            )
        )

    parsed_rows = sum(
        record.record_kind == "TRANSACTION" and record.section_ordinal == 1 for record in records
    )
    if parsed_rows != expected_rows:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "MARI_SAVINGS_ROW_COVERAGE_MISMATCH",
                "dated savings-row accounting does not match extracted transaction records",
                {"anchor_rows": expected_rows, "parsed_rows": parsed_rows},
            )
        )
    return ordinal


def _build_mari_savings_record(
    anchor_line: _Line,
    above_lines: list[_Line],
    below_lines: list[_Line],
    ordinal: int,
    page_number: int,
    day: str,
    month: str,
    precision: str,
    period_start: date | None,
    period_end: date | None,
    problems: list[DocumentProblem],
) -> DocumentRecord:
    monies = _mari_row_money(anchor_line)
    amount_word = monies[-1]
    debit = credit = None
    value = _parse_printed_money(amount_word["text"])
    if amount_word["x1"] < MARI_SAVINGS_OUTGOING_INCOMING_SPLIT_PT:
        debit = value
    else:
        credit = value
    money_ids = {id(word) for word in monies}
    first = anchor_line.words[0]["text"]
    inline_words = [
        word["text"]
        for index, word in enumerate(anchor_line.words)
        if id(word) not in money_ids
        and not (index == 0 and (word["text"] == day or _MARI_MONTH_RE.fullmatch(word["text"])))
        and not (index == 1 and precision == "DAY" and _MARI_MONTH_RE.fullmatch(word["text"]))
        and not (index == 1 and first == day and _MARI_MONTH_RE.fullmatch(word["text"]))
    ]
    description = " ".join(
        [
            *(" ".join(word["text"] for word in line.words) for line in above_lines),
            *inline_words,
            *(" ".join(word["text"] for word in line.words) for line in below_lines),
        ]
    ).strip()

    date_raw = f"{day} {month}".strip()
    if precision == "MONTH":
        date_value = period_end.isoformat() if period_end else None
    else:
        date_value = _infer_mari_date(int(day), month, period_start, period_end)
    complete = bool(date_value and description and (debit is not None) != (credit is not None))
    if not complete:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "MARI_SAVINGS_TRANSACTION_UNRESOLVED",
                "MariBank savings row is missing an unambiguous amount, description, or date",
                {"page_number": page_number, "raw_text": anchor_line.text},
                _document_anchor("mari-savings", page_number, anchor_line.bbox, anchor_line.text),
            )
        )
    bbox = anchor_line.bbox
    for line in (*above_lines, *below_lines):
        bbox = _merge_bbox(bbox, line.bbox)
    return DocumentRecord(
        source_anchor=_document_anchor("mari-savings", page_number, anchor_line.bbox, anchor_line.text),
        ordinal=ordinal,
        page_number=page_number,
        bbox=bbox,
        record_kind="TRANSACTION" if complete else "UNRESOLVED",
        raw_text="\n".join(
            line.text for line in (*above_lines, anchor_line, *below_lines)
        ),
        raw_fields={
            "source_columns": {
                "outgoing": debit.text if debit else None,
                "incoming": credit.text if credit else None,
            }
        },
        section_ordinal=1,
        date_raw=date_raw,
        date_value=date_value,
        description_raw=description or None,
        reference_raw=_extract_reference(description),
        debit=debit,
        credit=credit,
        currency_raw="SGD",
        confidence="HIGH" if complete else "UNRESOLVED",
        required_fields_complete=complete,
        date_precision=precision,
    )


def _infer_mari_date(
    day: int, month: str, period_start: date | None, period_end: date | None
) -> str | None:
    if period_start is None or period_end is None:
        return None
    for year in (period_end.year, period_end.year - 1, period_start.year):
        try:
            candidate = datetime.strptime(f"{day:02d} {month} {year}", "%d %b %Y").date()
        except ValueError:
            continue
        if period_start - timedelta(days=5) <= candidate <= period_end + timedelta(days=2):
            return candidate.isoformat()
    return None


def _parse_mari_interest_schedule(
    flat: list[tuple[int, _Line]],
    records: list[DocumentRecord],
    ordinal: int,
    profile: str,
    problems: list[DocumentProblem],
) -> tuple[int, ExactMoney | None]:
    active = False
    total_interest_credited: ExactMoney | None = None
    for page_number, line in flat:
        text = line.text.strip()
        if text.startswith("SAVINGS - INTEREST DETAILS"):
            active = True
            continue
        if not active:
            continue
        if text.startswith("INVESTMENTS -"):
            active = False
            continue
        if text.startswith("TOTAL INTEREST CREDITED"):
            monies = [word for word in line.words if _MONEY_RE.fullmatch(word["text"])]
            if monies:
                total_interest_credited = _parse_printed_money(monies[-1]["text"])
                ordinal += 1
                records.append(
                    DocumentRecord(
                        source_anchor=_document_anchor(
                            "mari-savings", page_number, line.bbox, line.text
                        ),
                        ordinal=ordinal,
                        page_number=page_number,
                        bbox=line.bbox,
                        record_kind="SUMMARY",
                        raw_text=line.text,
                        raw_fields={"supporting_kind": "TOTAL_INTEREST_CREDITED"},
                        section_ordinal=2,
                        currency_raw="SGD",
                        credit=total_interest_credited,
                    )
                )
            active = False
            continue
        if _is_mari_header(text) or _is_mari_page_furniture(text):
            continue
        words = line.words
        if not (
            len(words) >= 3
            and _MARI_DAY_RE.fullmatch(words[0]["text"])
            and _MARI_MONTH_RE.fullmatch(words[1]["text"])
        ):
            continue
        monies = [word for word in words if _MONEY_RE.fullmatch(word["text"])]
        rates = [word for word in words if _MARI_RATE_RE.fullmatch(word["text"])]
        if len(monies) < 2:
            continue
        ordinal += 1
        records.append(
            DocumentRecord(
                source_anchor=_document_anchor("mari-savings", page_number, line.bbox, line.text),
                ordinal=ordinal,
                page_number=page_number,
                bbox=line.bbox,
                record_kind="SUPPORTING_BALANCE",
                raw_text=line.text,
                raw_fields={
                    "supporting_kind": "DAILY_INTEREST",
                    "previous_day_balance": monies[0]["text"],
                    "interest": monies[-1]["text"],
                    "interest_rate": rates[0]["text"] if rates else None,
                    "profile": profile,
                },
                section_ordinal=2,
                date_raw=f"{words[0]['text']} {words[1]['text']}",
                currency_raw="SGD",
                debit=_parse_printed_money(monies[0]["text"]),
                credit=_parse_printed_money(monies[-1]["text"]),
            )
        )
    return ordinal, total_interest_credited


def _parse_mari_investment_sections(
    flat: list[tuple[int, _Line]],
    records: list[DocumentRecord],
    ordinal: int,
    problems: list[DocumentProblem],
) -> int:
    regions = {
        "INVESTMENTS - ACCOUNT SUMMARY": ("INVESTMENTS_HOLDINGS", "NO HOLDINGS"),
        "INVESTMENTS - TRANSACTION DETAILS": ("INVESTMENTS_TRANSACTIONS", "NO RECENT TRANSACTION"),
    }
    for heading, (region_name, empty_marker) in regions.items():
        collecting = False
        body: list[tuple[int, _Line]] = []
        for page_number, line in flat:
            text = line.text.strip()
            if text.startswith(heading):
                collecting = True
                anchor_page = page_number
                anchor_line = line
                continue
            if not collecting:
                continue
            if text.startswith("INVESTMENTS -") or text.startswith("SAVINGS -") or text.startswith("General"):
                break
            body.append((page_number, line))
        if not collecting:
            continue
        body_text = " ".join(line.text for _, line in body)
        empty = empty_marker in body_text
        ordinal += 1
        records.append(
            DocumentRecord(
                source_anchor=_document_anchor(
                    "mari-savings", anchor_page, anchor_line.bbox, anchor_line.text
                ),
                ordinal=ordinal,
                page_number=anchor_page,
                bbox=anchor_line.bbox,
                record_kind="RECOGNIZED_REGION" if empty else "UNRESOLVED",
                raw_text=anchor_line.text + ("\n" + body_text if body_text else ""),
                raw_fields={"region": region_name, "empty": empty},
                section_ordinal=3,
                confidence="HIGH" if empty else "UNRESOLVED",
                required_fields_complete=empty,
            )
        )
        if not empty:
            problems.append(
                DocumentProblem(
                    "WARNING",
                    "MARI_INVESTMENT_ACTIVITY_UNRESOLVED",
                    "MariBank investment section shows activity that is not folded into savings",
                    {"region": region_name},
                    _document_anchor("mari-savings", anchor_page, anchor_line.bbox, anchor_line.text),
                )
            )
    return ordinal


def _reconcile_mari_savings(
    transactions: list[DocumentRecord],
    all_records: list[DocumentRecord],
    summary: _MariSummary,
    total_interest_credited: ExactMoney | None,
    problems: list[DocumentProblem],
) -> list[DocumentCheck]:
    checks: list[DocumentCheck] = []
    outgoing = [record.debit for record in transactions if record.debit is not None]
    incoming = [record.credit for record in transactions if record.credit is not None]
    schedule_check = _mari_interest_schedule_check(all_records, total_interest_credited)
    if schedule_check is not None:
        checks.append(schedule_check)
    if summary.starting and summary.total_incoming and summary.total_outgoing and summary.ending:
        expected = _money_expression(summary.starting, summary.total_incoming, summary.total_outgoing)
        checks.append(
            _make_check(
                "SGD",
                "STATEMENT_BALANCE",
                MARI_SAVINGS_BALANCE_EQUATION_VERSION,
                expected,
                summary.ending,
                True,
                {"outgoing_rows": len(outgoing), "incoming_rows": len(incoming)},
            )
        )
    if summary.total_outgoing is not None:
        checks.append(
            _make_check(
                "SGD",
                "SUMMARY_OUTGOING_TOTAL",
                MARI_SAVINGS_BALANCE_EQUATION_VERSION,
                _money_total(outgoing),
                summary.total_outgoing,
                True,
                {"rows": len(outgoing)},
            )
        )
    if summary.total_incoming is not None:
        checks.append(
            _make_check(
                "SGD",
                "SUMMARY_INCOMING_TOTAL",
                MARI_SAVINGS_BALANCE_EQUATION_VERSION,
                _money_total(incoming),
                summary.total_incoming,
                True,
                {"rows": len(incoming)},
            )
        )
    if summary.ending is not None and summary.total_restated is not None:
        checks.append(
            _make_check(
                "SGD",
                "ENDING_BALANCE_RESTATED",
                MARI_SAVINGS_BALANCE_EQUATION_VERSION,
                summary.ending,
                summary.total_restated,
                True,
                {},
            )
        )
    credited_rows = [
        record
        for record in transactions
        if record.date_precision == "MONTH" and record.credit is not None
    ]
    if total_interest_credited is not None:
        if total_interest_credited.coefficient != 0:
            if len(credited_rows) == 1:
                checks.append(
                    _make_check(
                        "SGD",
                        "CREDITED_INTEREST_MATCHES_TOTAL",
                        MARI_SAVINGS_INTEREST_EQUATION_VERSION,
                        total_interest_credited,
                        credited_rows[0].credit,  # type: ignore[arg-type]
                        True,
                        {},
                    )
                )
            else:
                problems.append(
                    DocumentProblem(
                        "BLOCKING",
                        "MARI_SAVINGS_INTEREST_ROW_AMBIGUOUS",
                        "printed total interest is non-zero but a single credited-interest ledger row was not found",
                        {"credited_interest_rows": len(credited_rows)},
                    )
                )
        elif credited_rows:
            problems.append(
                DocumentProblem(
                    "BLOCKING",
                    "MARI_SAVINGS_INTEREST_ROW_AMBIGUOUS",
                    "printed total interest is zero but a credited-interest ledger row is present",
                    {"credited_interest_rows": len(credited_rows)},
                )
            )
    if any(check.blocking and check.passed is not True for check in checks):
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "MARI_SAVINGS_RECONCILIATION_FAILED",
                "one or more MariBank savings exact equations failed",
            )
        )
    return checks


def _mari_interest_schedule_check(
    records: list[DocumentRecord], total_interest_credited: ExactMoney | None
) -> DocumentCheck | None:
    daily = [
        _parse_printed_money(str(record.raw_fields["interest"]))
        for record in records
        if record.raw_fields.get("supporting_kind") == "DAILY_INTEREST"
    ]
    if total_interest_credited is None or not daily:
        return None
    return _make_check(
        "SGD",
        "INTEREST_SCHEDULE_TOTAL",
        MARI_SAVINGS_INTEREST_EQUATION_VERSION,
        _money_total(daily),
        total_interest_credited,
        True,
        {"daily_rows": len(daily)},
    )


def _is_mari_header(text: str) -> bool:
    upper = text.upper()
    if upper.startswith("DATE") and ("TRANSACTION" in upper or "PREVIOUS" in upper):
        return True
    if "ACCOUNT STARTING BALANCE" in upper:
        return True
    return upper.startswith("ACCOUNT:") or upper in {"DATE"}


def _is_mari_page_furniture(text: str) -> bool:
    upper = text.upper()
    if (
        upper.startswith("DEPOSIT AND INVESTMENT STATEMENT")
        or upper.startswith("MARI CREDIT CARD STATEMENT")
        or upper.startswith("S/N ")
    ):
        return True
    if _MARI_FULL_DATE_RE.fullmatch(text.strip()):
        return True
    if re.match(r"^page \d+ of \d+$", text.strip(), re.IGNORECASE):
        return True
    return upper.startswith("*INTEREST RATES DISPLAYED")


def _mari_page_coverage(
    page_lines: list[tuple[int, list[_Line], dict[str, int]]],
    records: list[DocumentRecord],
    profile_label: str,
) -> list[PageCoverage]:
    rows_by_page: dict[int, int] = {}
    supporting_by_page: dict[int, int] = {}
    for record in records:
        if record.record_kind == "TRANSACTION":
            rows_by_page[record.page_number] = rows_by_page.get(record.page_number, 0) + 1
        else:
            supporting_by_page[record.page_number] = supporting_by_page.get(record.page_number, 0) + 1
    coverage: list[PageCoverage] = []
    for page_number, lines, counts in page_lines:
        page_text = "\n".join(line.text for line in lines)
        has_summary = "ACCOUNT SUMMARY" in page_text
        has_transactions = "TRANSACTION DETAILS" in page_text or any(
            _is_mari_transaction_anchor(line) for line in lines
        )
        has_interest = "INTEREST DETAILS" in page_text or "PREVIOUS DAY BALANCE" in page_text
        has_information = "Deposit Insurance Scheme" in page_text or page_text.strip().startswith(
            "DEPOSIT AND INVESTMENT STATEMENT\nS/N"
        ) and "General" in page_text
        present = sum((has_summary, has_transactions, has_interest, has_information))
        if present > 1:
            classification = "MIXED"
        elif has_transactions:
            classification = "TRANSACTION"
        elif has_interest:
            classification = "INTEREST"
        elif has_summary:
            classification = "SUMMARY"
        elif has_information:
            classification = "INFORMATION"
        else:
            classification = "UNKNOWN"
        coverage.append(
            PageCoverage(
                page_number,
                classification,
                counts["all"],
                counts["upright"],
                counts["rotated"],
                rows_by_page.get(page_number, 0),
                supporting_by_page.get(page_number, 0),
                "ACCOUNTED" if classification != "UNKNOWN" else "UNRESOLVED",
                {"profile": profile_label},
            )
        )
    return coverage


MARI_CARD_PROFILE_MINUS_I = "mari-card-summary-A-B-C+D+E-F+G+H-I-v1"
MARI_CARD_PROFILE_PLUS_I = "mari-card-summary-A-B-C+D+E-F+G+H+I-v1"
MARI_CARD_RECONCILIATION_VERSION = "mari-card-printed-equation-and-section-totals-v1"
MARI_CARD_PROFILE_LABEL = "mari-card"

_MARI_CARD_MONEY_RE = re.compile(r"^[+-]?\d[\d,]*\.\d{2}$")
_MARI_CARD_INSTALMENT_RE = re.compile(r"^(.*?)\s+(\d+)/(\d+)\s+Instalment$")
_MARI_CARD_PLAN_TERM_RE = re.compile(r"^(.*?)\s+(\d+)M$")
_MARI_CARD_FOREIGN_RE = re.compile(r"^([A-Z]{3})\s+([\d,]+\.\d{2})$")
_MARI_CARD_EQUATION_MINUS_I = "A-B-C+D+E-F+G+H-I"
_MARI_CARD_EQUATION_PLUS_I = "A-B-C+D+E-F+G+H+I"

# Each printed transaction section reproduces exactly one summary bucket.
_MARI_CARD_SECTION_BUCKET = {
    "REPAYMENT/CONVERSION": "B",
    "WAIVER/REFUND": "C",
    "WAIVER": "C",
    "REFUND": "C",
    "PURCHASE": "D",
    "CASH/LOANS": "E",
    "CASH": "E",
    "LOANS": "E",
    "CASHBACK": "F",
    "BANK CHARGE": "G",
    "SPLIT PAYMENT": "H",
    "SPLIT BILL/SPLIT PAYMENT": "H",
    "OTHERS": "I",
}
_MARI_CARD_BUCKET_SIGNS_MINUS_I = {
    "A": 1, "B": -1, "C": -1, "D": 1, "E": 1, "F": -1, "G": 1, "H": 1, "I": -1
}
_MARI_CARD_BUCKET_SIGNS_PLUS_I = {**_MARI_CARD_BUCKET_SIGNS_MINUS_I, "I": 1}


@dataclass(slots=True)
class _MariCardSummary:
    statement_date: str | None = None
    payment_due_date: str | None = None
    credit_limit: ExactMoney | None = None
    statement_due: ExactMoney | None = None
    minimum_payment: ExactMoney | None = None
    buckets: dict[str, ExactMoney] = field(default_factory=dict)
    equation_raw: str | None = None
    current_outstanding: ExactMoney | None = None


def parse_mari_card(data: bytes) -> ParsedDocument:
    """Extract one Mari credit-card e-Statement.

    Posted and printed transaction dates are captured separately; each source
    section reproduces one printed summary bucket, and applying the statement's
    own printed equation over the buckets reproduces current outstanding.  A
    summary equation that matches neither observed formula blocks generation.
    Installment occurrences are preserved as facts; chaining them into plans is
    an interpretation step handled elsewhere.
    """
    problems: list[DocumentProblem] = []
    records: list[DocumentRecord] = []
    summary = _MariCardSummary()
    period_start: date | None = None
    period_end: date | None = None
    account_masked: str | None = None

    with pdfplumber.open(io.BytesIO(data), password="") as pdf:
        page_lines: list[tuple[int, list[_Line], dict[str, int]]] = []
        for page_number, page in enumerate(pdf.pages, start=1):
            all_words = page.extract_words(x_tolerance=2, y_tolerance=2, extra_attrs=["upright"])
            upright = [word for word in all_words if word.get("upright", True)]
            lines = _group_lines(upright)
            page_lines.append(
                (
                    page_number,
                    lines,
                    {
                        "all": len(all_words),
                        "upright": len(upright),
                        "rotated": len(all_words) - len(upright),
                    },
                )
            )
            page_text = "\n".join(line.text for line in lines)
            period_match = re.search(
                r"STATEMENT PERIOD:\s*(\d{2} [A-Z]{3} \d{4})\s+to\s+(\d{2} [A-Z]{3} \d{4})",
                page_text,
            )
            if period_match and period_start is None:
                period_start = datetime.strptime(period_match.group(1), "%d %b %Y").date()
                period_end = datetime.strptime(period_match.group(2), "%d %b %Y").date()
            if account_masked is None:
                account_match = re.search(r"ACCOUNT:\s*([0-9*]{6,})", page_text)
                if account_match:
                    account_masked = account_match.group(1)

        flat: list[tuple[int, _Line]] = [
            (page_number, line) for page_number, lines, _ in page_lines for line in lines
        ]
        _parse_mari_card_account_summary(flat, summary)
        _parse_mari_card_statement_summary(flat, summary, problems)
        _parse_mari_card_transactions(flat, records, period_start, period_end, problems)

    if summary.statement_date is None:
        problems.append(
            DocumentProblem("BLOCKING", "STATEMENT_DATE_MISSING", "Mari card statement date was not found")
        )
    if period_start is None or period_end is None:
        problems.append(
            DocumentProblem(
                "BLOCKING", "MARI_CARD_STATEMENT_PERIOD_MISSING", "Mari card statement period was not found"
            )
        )

    profile, checks = _reconcile_mari_card(records, summary, problems)

    headings: list[str] = []
    for record in records:
        heading = str(record.raw_fields.get("section_heading", ""))
        if heading and heading not in headings:
            headings.append(heading)
    sections = [
        DocumentSection(index + 1, "CARD_LEDGER", "SGD", heading, 1, len(page_lines))
        for index, heading in enumerate(headings)
    ]

    coverage = _mari_card_page_coverage(page_lines, records)
    for page_cov in coverage:
        if page_cov.status == "UNRESOLVED":
            problems.append(
                DocumentProblem(
                    "BLOCKING",
                    "UNCLASSIFIED_PDF_PAGE",
                    "Mari card page could not be classified structurally",
                    {"page_number": page_cov.page_number},
                )
            )

    statement_date_text = summary.statement_date or date.min.isoformat()
    return ParsedDocument(
        institution="MariBank",
        source_type="MARI_CARD",
        account_id="mari-credit-card",
        account_display_name="Mari Credit Card",
        masked_identifier=f"Mari Credit Card {account_masked}" if account_masked else "Mari Credit Card",
        source_profile_version=profile,
        statement_date=statement_date_text,
        period_start=(period_start.isoformat() if period_start else None),
        period_end=(period_end.isoformat() if period_end else None),
        statement_identity=f"mari-card:mari-credit-card:{statement_date_text}",
        sections=sections,
        records=records,
        pages=coverage,
        checks=checks,
        problems=problems,
        card_summary=summary,
    )


def _parse_mari_card_account_summary(
    flat: list[tuple[int, _Line]], summary: _MariCardSummary
) -> None:
    for _, line in flat:
        text = line.text.strip()
        if not (text.startswith("MARI CREDIT") and "20" in text):
            continue
        dates = re.findall(r"\d{2} [A-Z]{3} \d{4}", text)
        monies = [
            _parse_printed_money(word["text"])
            for word in line.words
            if _MONEY_RE.fullmatch(word["text"])
        ]
        if len(dates) >= 2 and len(monies) >= 3:
            summary.statement_date = datetime.strptime(dates[0], "%d %b %Y").date().isoformat()
            summary.payment_due_date = datetime.strptime(dates[1], "%d %b %Y").date().isoformat()
            summary.credit_limit, summary.statement_due, summary.minimum_payment = monies[:3]
            return


def _parse_mari_card_statement_summary(
    flat: list[tuple[int, _Line]],
    summary: _MariCardSummary,
    problems: list[DocumentProblem],
) -> None:
    letters_line: _Line | None = None
    for index, (_, line) in enumerate(flat):
        if re.fullmatch(r"\(A\)( \([B-I]\))+", line.text.strip()):
            letters_line = line
            values_line = flat[index + 1][1] if index + 1 < len(flat) else None
            if values_line is not None:
                centers = [
                    (word["text"].strip("()"), (word["x0"] + word["x1"]) / 2)
                    for word in letters_line.words
                ]
                values = [
                    word for word in values_line.words if _MONEY_RE.fullmatch(word["text"])
                ]
                for letter, center in centers:
                    nearest = min(
                        values, key=lambda word: abs((word["x0"] + word["x1"]) / 2 - center)
                    )
                    summary.buckets[letter] = _parse_printed_money(nearest["text"])
        if "CURRENT OUTSTANDING" in line.text:
            equation_match = re.search(r"\(([A-Z0-9()+\-]+)\)", line.text)
            if equation_match:
                summary.equation_raw = equation_match.group(1)
            monies = [word for word in line.words if _MONEY_RE.fullmatch(word["text"])]
            if monies:
                summary.current_outstanding = _parse_printed_money(monies[-1]["text"])
    if letters_line is None or summary.current_outstanding is None:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "MARI_CARD_SUMMARY_MISSING",
                "Mari card statement-summary bucket table or current outstanding was not found",
            )
        )


def _is_mari_card_anchor(line: _Line) -> tuple[str, str | None] | None:
    """Return (posted 'DD MON', txn 'DD MON' or None) if this line is a row."""
    words = line.words
    if len(words) < 3:
        return None
    if not (_MARI_DAY_RE.fullmatch(words[0]["text"]) and _MARI_MONTH_RE.fullmatch(words[1]["text"])):
        return None
    if not any(
        _MARI_CARD_MONEY_RE.fullmatch(word["text"]) and word["x1"] > 470 for word in words
    ):
        return None
    posted = f"{words[0]['text']} {words[1]['text']}"
    txn = None
    if (
        len(words) >= 4
        and _MARI_DAY_RE.fullmatch(words[2]["text"])
        and _MARI_MONTH_RE.fullmatch(words[3]["text"])
        and words[2]["x0"] > 150
    ):
        txn = f"{words[2]['text']} {words[3]['text']}"
    return posted, txn


def _parse_mari_card_transactions(
    flat: list[tuple[int, _Line]],
    records: list[DocumentRecord],
    period_start: date | None,
    period_end: date | None,
    problems: list[DocumentProblem],
) -> None:
    region: list[tuple[int, _Line]] = []
    active = False
    for page_number, line in flat:
        text = line.text.strip()
        if "TRANSACTION DETAILS" in text and "POSTED" not in text:
            active = True
            continue
        if not active:
            continue
        if text.startswith(("General", "DISCLOSURE TABLE", "Important Information")):
            active = False
            continue
        if text.startswith("POSTED DATE") or _is_mari_page_furniture(text):
            continue
        region.append((page_number, line))

    anchor_positions = [
        index for index, (_, line) in enumerate(region) if _is_mari_card_anchor(line)
    ]
    header_positions = {
        index
        for index, (_, line) in enumerate(region)
        if _mari_card_section_heading(line) is not None
    }
    above: dict[int, list[_Line]] = {position: [] for position in anchor_positions}
    below: dict[int, list[_Line]] = {position: [] for position in anchor_positions}
    anchors_by_page: dict[int, list[int]] = {}
    for position in anchor_positions:
        anchors_by_page.setdefault(region[position][0], []).append(position)
    for index, (page_number, line) in enumerate(region):
        if index in above or index in header_positions:
            continue
        candidates = anchors_by_page.get(page_number)
        if not candidates:
            continue
        nearest = min(candidates, key=lambda pos: abs(region[pos][1].top - line.top))
        (above if line.top < region[nearest][1].top else below)[nearest].append(line)

    current_section = "UNSECTIONED"
    anchor_order = {position: order for order, position in enumerate(anchor_positions)}
    for index, (page_number, line) in enumerate(region):
        heading = _mari_card_section_heading(line)
        if heading is not None:
            current_section = heading
            if heading not in _MARI_CARD_SECTION_BUCKET:
                problems.append(
                    DocumentProblem(
                        "BLOCKING",
                        "MARI_CARD_UNKNOWN_SECTION",
                        "Mari card transaction section heading is not a known summary bucket",
                        {"heading": heading},
                    )
                )
            continue
        if index not in anchor_order:
            continue
        records.append(
            _build_mari_card_record(
                line,
                sorted(above[index], key=lambda item: item.top),
                sorted(below[index], key=lambda item: item.top),
                len(records) + 1,
                page_number,
                current_section,
                period_start,
                period_end,
                problems,
            )
        )


def _mari_card_section_heading(line: _Line) -> str | None:
    if not line.words:
        return None
    if any(_MARI_CARD_MONEY_RE.fullmatch(word["text"]) for word in line.words):
        return None
    if min(word["x0"] for word in line.words) >= 300:
        return None
    normalized = " ".join(line.text.split()).upper()
    return normalized if normalized in _MARI_CARD_SECTION_BUCKET else None


def _build_mari_card_record(
    anchor_line: _Line,
    above_lines: list[_Line],
    below_lines: list[_Line],
    ordinal: int,
    page_number: int,
    section_heading: str,
    period_start: date | None,
    period_end: date | None,
    problems: list[DocumentProblem],
) -> DocumentRecord:
    posted, txn = _is_mari_card_anchor(anchor_line)  # type: ignore[misc]
    amount_word = next(
        word
        for word in reversed(anchor_line.words)
        if _MARI_CARD_MONEY_RE.fullmatch(word["text"]) and word["x1"] > 470
    )
    signed = _parse_printed_money(amount_word["text"].lstrip("+"))
    marker = "CR" if not amount_word["text"].startswith("-") else "DB"
    amount = ExactMoney(abs(signed.coefficient), signed.scale, amount_word["text"].lstrip("+-"))

    date_token_count = 4 if txn is not None else 2
    frag = [
        word["text"]
        for word in anchor_line.words[date_token_count:]
        if id(word) != id(amount_word) and not _MARI_CARD_MONEY_RE.fullmatch(word["text"])
    ]

    foreign_currency: str | None = None
    foreign_amount: ExactMoney | None = None
    installment: dict[str, object] | None = None
    label_parts: list[str] = []
    plan_term_printed: int | None = None
    for line in (*above_lines, *below_lines):
        text = " ".join(line.text.split())
        foreign_match = _MARI_CARD_FOREIGN_RE.fullmatch(text)
        instalment_match = _MARI_CARD_INSTALMENT_RE.fullmatch(text)
        plan_match = _MARI_CARD_PLAN_TERM_RE.fullmatch(text)
        if foreign_match:
            foreign_currency = foreign_match.group(1)
            foreign_amount = _parse_printed_money(foreign_match.group(2))
        elif instalment_match:
            installment = {
                "family": instalment_match.group(1),
                "ordinal": int(instalment_match.group(2)),
                "term": int(instalment_match.group(3)),
            }
        elif plan_match and plan_match.group(1):
            plan_term_printed = int(plan_match.group(2))
        else:
            label_parts.append(text)
    description = " ".join([*label_parts, *frag]).strip()
    if installment is not None:
        if plan_term_printed is not None:
            installment["plan_term_printed"] = plan_term_printed
        if not description:
            description = (
                f"{installment['family']} {installment['ordinal']}/{installment['term']} Instalment"
            )

    posted_value = _infer_mari_date(int(posted[:2]), posted[3:], period_start, period_end)
    alt_role = alt_value = alt_raw = None
    if txn is not None:
        alt_role = "TRANSACTION"
        alt_raw = txn
        alt_value = _infer_mari_date(int(txn[:2]), txn[3:], period_start, period_end)
        if alt_value is None:
            problems.append(
                DocumentProblem(
                    "WARNING",
                    "MARI_CARD_PARTIAL_TRANSACTION_DATE",
                    "Mari card printed transaction date could not be resolved to an unambiguous year",
                    {"raw_text": anchor_line.text},
                    _document_anchor("mari-card", page_number, anchor_line.bbox, anchor_line.text),
                )
            )
    else:
        problems.append(
            DocumentProblem(
                "WARNING",
                "MARI_CARD_BLANK_TRANSACTION_DATE",
                "Mari card row has a blank printed transaction date; posted date is retained",
                {"raw_text": anchor_line.text},
                _document_anchor("mari-card", page_number, anchor_line.bbox, anchor_line.text),
            )
        )

    complete = bool(posted_value and description and section_heading != "UNSECTIONED")
    if not complete:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "MARI_CARD_TRANSACTION_UNRESOLVED",
                "Mari card row is missing a posted date, description, or section",
                {"page_number": page_number, "raw_text": anchor_line.text},
                _document_anchor("mari-card", page_number, anchor_line.bbox, anchor_line.text),
            )
        )
    raw_fields: dict[str, object] = {
        "section_heading": section_heading,
        "summary_bucket": _MARI_CARD_SECTION_BUCKET.get(section_heading),
        "signed_amount": amount_word["text"],
    }
    if installment is not None:
        raw_fields["installment"] = installment
    bbox = anchor_line.bbox
    for line in (*above_lines, *below_lines):
        bbox = _merge_bbox(bbox, line.bbox)
    return DocumentRecord(
        source_anchor=_document_anchor("mari-card", page_number, anchor_line.bbox, anchor_line.text),
        ordinal=ordinal,
        page_number=page_number,
        bbox=bbox,
        record_kind="TRANSACTION" if complete else "UNRESOLVED",
        raw_text="\n".join(line.text for line in (*above_lines, anchor_line, *below_lines)),
        raw_fields=raw_fields,
        section_ordinal=1,
        date_raw=posted,
        date_value=posted_value,
        description_raw=description or None,
        reference_raw=None,
        amount=amount,
        direction_marker=marker,
        currency_raw="SGD",
        confidence="HIGH" if complete else "UNRESOLVED",
        required_fields_complete=complete,
        alt_date_role=alt_role,
        alt_date_value=alt_value,
        alt_date_raw=alt_raw,
        foreign_currency=foreign_currency,
        foreign_amount=foreign_amount,
    )


def _reconcile_mari_card(
    records: list[DocumentRecord],
    summary: _MariCardSummary,
    problems: list[DocumentProblem],
) -> tuple[str, list[DocumentCheck]]:
    if summary.equation_raw == _MARI_CARD_EQUATION_MINUS_I:
        profile, signs = MARI_CARD_PROFILE_MINUS_I, _MARI_CARD_BUCKET_SIGNS_MINUS_I
    elif summary.equation_raw == _MARI_CARD_EQUATION_PLUS_I:
        profile, signs = MARI_CARD_PROFILE_PLUS_I, _MARI_CARD_BUCKET_SIGNS_PLUS_I
    else:
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "MARI_CARD_UNKNOWN_SUMMARY_EQUATION",
                "Mari card statement-summary equation matches neither observed formula",
                {"equation": summary.equation_raw},
            )
        )
        return MARI_CARD_PROFILE_MINUS_I, []

    checks: list[DocumentCheck] = []
    transactions = [record for record in records if record.record_kind == "TRANSACTION"]
    if len(summary.buckets) == 9 and summary.current_outstanding is not None:
        scale = max(value.scale for value in summary.buckets.values())
        coefficient = sum(
            signs[letter] * summary.buckets[letter].rescaled_coefficient(scale)
            for letter in "ABCDEFGHI"
        )
        expected = ExactMoney(coefficient, scale, format_decimal(coefficient, scale))
        checks.append(
            _make_check(
                "SGD",
                "SUMMARY_EQUATION",
                MARI_CARD_RECONCILIATION_VERSION,
                expected,
                summary.current_outstanding,
                True,
                {"equation": summary.equation_raw},
            )
        )

    by_bucket: dict[str, list[DocumentRecord]] = {}
    for record in transactions:
        letter = record.raw_fields.get("summary_bucket")
        if isinstance(letter, str):
            by_bucket.setdefault(letter, []).append(record)
    for letter, bucket_records in sorted(by_bucket.items()):
        if letter not in summary.buckets:
            continue
        scale = max(
            [record.amount.scale for record in bucket_records if record.amount]
            + [summary.buckets[letter].scale]
        )
        total = 0
        for record in bucket_records:
            assert record.amount is not None
            sign = 1 if record.direction_marker == "CR" else -1
            total += sign * record.amount.rescaled_coefficient(scale)
        observed = ExactMoney(abs(total), scale, format_decimal(abs(total), scale))
        checks.append(
            _make_check(
                "SGD",
                "SECTION_BUCKET_TOTAL",
                MARI_CARD_RECONCILIATION_VERSION,
                summary.buckets[letter],
                observed,
                True,
                {"bucket": letter, "rows": len(bucket_records)},
            )
        )

    if summary.statement_due is not None and summary.current_outstanding is not None:
        checks.append(
            _make_check(
                "SGD",
                "STATEMENT_DUE_EQUALS_OUTSTANDING",
                MARI_CARD_RECONCILIATION_VERSION,
                summary.current_outstanding,
                summary.statement_due,
                True,
                {},
            )
        )

    if any(check.blocking and check.passed is not True for check in checks):
        problems.append(
            DocumentProblem(
                "BLOCKING",
                "MARI_CARD_RECONCILIATION_FAILED",
                "one or more Mari card exact equations failed",
            )
        )
    return profile, checks


def _mari_card_page_coverage(
    page_lines: list[tuple[int, list[_Line], dict[str, int]]],
    records: list[DocumentRecord],
) -> list[PageCoverage]:
    rows_by_page: dict[int, int] = {}
    for record in records:
        rows_by_page[record.page_number] = rows_by_page.get(record.page_number, 0) + 1
    coverage: list[PageCoverage] = []
    for page_number, lines, counts in page_lines:
        page_text = "\n".join(line.text for line in lines)
        has_summary = "ACCOUNT SUMMARY" in page_text or "STATEMENT SUMMARY" in page_text
        has_transactions = "TRANSACTION DETAILS" in page_text or any(
            _is_mari_card_anchor(line) for line in lines
        )
        has_information = any(
            marker in page_text
            for marker in (
                "DISCLOSURE TABLE",
                "General",
                "Important Information",
                "Deposit Insurance Scheme",
                "Apple Wallet",
                "Google Wallet",
            )
        )
        present = sum((has_summary, has_transactions, has_information))
        if present > 1:
            classification = "MIXED"
        elif has_transactions:
            classification = "TRANSACTION"
        elif has_summary:
            classification = "SUMMARY"
        elif has_information:
            classification = "INFORMATION"
        else:
            classification = "UNKNOWN"
        coverage.append(
            PageCoverage(
                page_number,
                classification,
                counts["all"],
                counts["upright"],
                counts["rotated"],
                rows_by_page.get(page_number, 0),
                0,
                "ACCOUNTED" if classification != "UNKNOWN" else "UNRESOLVED",
                {"profile": MARI_CARD_PROFILE_LABEL},
            )
        )
    return coverage


def _group_lines(
    words: list[dict[str, Any]], tolerance: float = LINE_GROUP_TOLERANCE_PT
) -> list[_Line]:
    groups: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        target = None
        for group in reversed(groups[-LINE_GROUP_LOOKBACK:]):
            if abs(group[0]["top"] - word["top"]) <= tolerance:
                target = group
                break
        if target is None:
            groups.append([word])
        else:
            target.append(word)
    for group in groups:
        group.sort(key=lambda item: item["x0"])
    return [_Line(group) for group in groups]


def _page_classification(summary: bool, transactions: bool, information: bool) -> str:
    present = sum((summary, transactions, information))
    if present > 1:
        return "MIXED"
    if transactions:
        return "TRANSACTION"
    if summary:
        return "SUMMARY"
    if information:
        return "INFORMATION"
    return "UNKNOWN"


def _is_dbs_header(line: _Line) -> bool:
    text = line.text
    return all(label in text for label in ("Date", "Description", "Withdrawal", "Deposit", "Balance"))


def _dbs_header_rights(line: _Line) -> tuple[float, float, float]:
    groups: dict[str, list[dict[str, Any]]] = {"Withdrawal": [], "Deposit": [], "Balance": []}
    active: str | None = None
    for word in line.words:
        if word["text"] in groups:
            active = word["text"]
            groups[active].append(word)
        elif word["text"] in {"(-)", "(+)"} and active is not None:
            groups[active].append(word)
    return tuple(max(word["x1"] for word in groups[name]) for name in ("Withdrawal", "Deposit", "Balance"))


def _currency_heading(text: str) -> str | None:
    if not text.startswith("CURRENCY:"):
        return None
    name = text.partition(":")[2].strip()
    return name if name in _CURRENCY_NAMES else None


def _parse_dbs_transaction_line(
    line: _Line,
    ordinal: int,
    page_number: int,
    section: DocumentSection | None,
    currency: str | None,
    header_rights: tuple[float, float, float] | None,
) -> DocumentRecord:
    date_raw = line.words[0]["text"]
    date_value: str | None
    try:
        date_value = datetime.strptime(date_raw, "%d/%m/%Y").date().isoformat()
    except ValueError:
        date_value = None
    debit = credit = balance = None
    amount_word_ids: set[int] = set()
    if header_rights is not None:
        for index, word in enumerate(line.words[1:], start=1):
            if not _MONEY_RE.fullmatch(word["text"]):
                continue
            distances = [abs(word["x1"] - right) for right in header_rights]
            nearest = min(range(3), key=distances.__getitem__)
            if distances[nearest] <= DBS_AMOUNT_COLUMN_TOLERANCE_PT:
                value = _parse_printed_money(word["text"])
                if nearest == 0:
                    debit = value
                elif nearest == 1:
                    credit = value
                else:
                    balance = value
                amount_word_ids.add(index)
    description = " ".join(
        word["text"]
        for index, word in enumerate(line.words[1:], start=1)
        if index not in amount_word_ids and word["x0"] < DBS_DESCRIPTION_MAX_X0_PT
    ).strip()
    complete = (
        section is not None
        and currency is not None
        and date_value is not None
        and balance is not None
        and ((debit is None) != (credit is None))
    )
    anchor = _document_anchor("dbs", page_number, line.bbox, line.text)
    return DocumentRecord(
        anchor,
        ordinal,
        page_number,
        line.bbox,
        "TRANSACTION" if complete else "UNRESOLVED",
        line.text,
        {"source_columns": {"debit": debit.text if debit else None, "credit": credit.text if credit else None, "balance": balance.text if balance else None}},
        section.ordinal if section else None,
        date_raw,
        date_value,
        description or None,
        _extract_reference(description),
        debit,
        credit,
        None,
        None,
        currency,
        balance,
        "HIGH" if complete else "UNRESOLVED",
        complete,
    )


def _dbs_supporting_kind(text: str) -> str | None:
    if "Balance Brought Forward" in text:
        return "OPENING_BALANCE"
    if "Balance Carried Forward" in text and not text.startswith("Total"):
        return "PAGE_CLOSING_BALANCE"
    if text.startswith("Total Balance Carried Forward"):
        return "SECTION_TOTAL"
    return None


def _parse_dbs_supporting_line(
    line: _Line,
    ordinal: int,
    page_number: int,
    section: DocumentSection | None,
    currency: str | None,
    header_rights: tuple[float, float, float] | None,
    supporting_kind: str,
) -> DocumentRecord:
    debit = credit = balance = None
    if header_rights is not None:
        for word in line.words:
            if not _MONEY_RE.fullmatch(word["text"]):
                continue
            value = _parse_printed_money(word["text"])
            distances = [abs(word["x1"] - right) for right in header_rights]
            nearest = min(range(3), key=distances.__getitem__)
            if distances[nearest] <= DBS_SUPPORTING_COLUMN_TOLERANCE_PT:
                if nearest == 0:
                    debit = value
                elif nearest == 1:
                    credit = value
                else:
                    balance = value
    if balance is None:
        balance = _rightmost_money(line)
    kind = "SUMMARY" if supporting_kind == "SECTION_TOTAL" else "SUPPORTING_BALANCE"
    return DocumentRecord(
        _document_anchor("dbs", page_number, line.bbox, line.text),
        ordinal,
        page_number,
        line.bbox,
        kind,
        line.text,
        {"supporting_kind": supporting_kind},
        section.ordinal if section else None,
        debit=debit,
        credit=credit,
        currency_raw=currency,
        balance=balance,
        confidence="HIGH" if currency and balance else "UNRESOLVED",
        required_fields_complete=bool(currency and balance),
    )


def _is_dbs_description_continuation(line: _Line) -> bool:
    if not line.words:
        return False
    text = line.text
    if any(marker in text for marker in ("Transaction Details as", "Page ", "Messages For", "Total Interest")):
        return False
    return min(word["x0"] for word in line.words) >= 100 and max(word["x1"] for word in line.words) < 410


def _is_paylah_transaction_line(line: _Line) -> bool:
    words = line.words
    return (
        len(words) >= 5
        and _PAYLAH_DAY_RE.fullmatch(words[0]["text"]) is not None
        and _PAYLAH_MONTH_RE.fullmatch(words[1]["text"]) is not None
        and words[-1]["text"] in {"DB", "CR"}
        and _MONEY_RE.fullmatch(words[-2]["text"]) is not None
    )


def _parse_paylah_transaction_line(
    line: _Line,
    ordinal: int,
    page_number: int,
    statement_date: date | None,
) -> DocumentRecord:
    raw_date = f"{line.words[0]['text']} {line.words[1]['text']}"
    date_value = _infer_paylah_date(raw_date, statement_date)
    marker = line.words[-1]["text"]
    amount = _parse_printed_money(line.words[-2]["text"])
    description = " ".join(word["text"] for word in line.words[2:-2]).strip()
    complete = bool(statement_date and date_value and description and amount and marker in {"DB", "CR"})
    return DocumentRecord(
        _document_anchor("paylah", page_number, line.bbox, line.text),
        ordinal,
        page_number,
        line.bbox,
        "TRANSACTION" if complete else "UNRESOLVED",
        line.text,
        {"amount": amount.text, "direction_marker": marker},
        1,
        raw_date,
        date_value,
        description or None,
        None,
        None,
        None,
        amount,
        marker,
        "SGD",
        None,
        "HIGH" if complete else "UNRESOLVED",
        complete,
    )


def _infer_paylah_date(raw_date: str, statement_date: date | None) -> str | None:
    if statement_date is None:
        return None
    for year in (statement_date.year, statement_date.year - 1):
        try:
            candidate = datetime.strptime(f"{raw_date} {year}", "%d %b %Y").date()
        except ValueError:
            continue
        window_start = statement_date - timedelta(days=PAYLAH_DATE_INFERENCE_WINDOW_DAYS)
        if window_start <= candidate <= statement_date:
            return candidate.isoformat()
    return None


def _has_account_summary(lines: list[_Line]) -> bool:
    return any("Account No." in line.text and "Balance" in line.text for line in lines)


def _rightmost_money(line: _Line) -> ExactMoney | None:
    candidates = [word for word in line.words if _MONEY_RE.fullmatch(word["text"])]
    return _parse_printed_money(max(candidates, key=lambda word: word["x1"])["text"]) if candidates else None


def _parse_printed_money(text: str) -> ExactMoney:
    normalized = text.replace(",", "")
    if normalized.endswith("-"):
        normalized = "-" + normalized[:-1]
    parsed = ExactMoney.parse(normalized)
    return ExactMoney(parsed.coefficient, parsed.scale, text)


def _supporting_record(
    ordinal: int,
    page_number: int,
    line: _Line,
    supporting_kind: str,
    currency: str,
    *,
    balance: ExactMoney,
    section_ordinal: int,
) -> DocumentRecord:
    return DocumentRecord(
        _document_anchor("paylah", page_number, line.bbox, line.text),
        ordinal,
        page_number,
        line.bbox,
        "SUPPORTING_BALANCE",
        line.text,
        {"supporting_kind": supporting_kind},
        section_ordinal,
        currency_raw=currency,
        balance=balance,
    )


def _reconcile_dbs(
    records: list[DocumentRecord],
    sections: list[DocumentSection],
    problems: list[DocumentProblem],
) -> list[DocumentCheck]:
    checks: list[DocumentCheck] = []
    for section in sections:
        section_records = [record for record in records if record.section_ordinal == section.ordinal]
        last_balance: ExactMoney | None = None
        debits: list[ExactMoney] = []
        credits: list[ExactMoney] = []
        summary: DocumentRecord | None = None
        for record in section_records:
            supporting_kind = record.raw_fields.get("supporting_kind")
            if supporting_kind == "OPENING_BALANCE":
                if last_balance is not None and record.balance is not None:
                    checks.append(
                        _make_check(
                            section.currency or "UNKNOWN",
                            "PAGE_BALANCE_CONTINUITY",
                            DBS_BALANCE_EQUATION_VERSION,
                            last_balance,
                            record.balance,
                            True,
                            {"page_number": record.page_number, "source_anchor": record.source_anchor},
                        )
                    )
                last_balance = record.balance
            elif record.record_kind == "TRANSACTION":
                if record.debit:
                    debits.append(record.debit)
                if record.credit:
                    credits.append(record.credit)
                operands_available = record.balance is not None and ((record.debit is None) != (record.credit is None))
                if last_balance is not None and operands_available:
                    assert record.balance is not None  # implied by operands_available
                    expected = _money_expression(last_balance, record.credit, record.debit)
                    checks.append(
                        _make_check(
                            section.currency or "UNKNOWN",
                            "RUNNING_BALANCE",
                            DBS_BALANCE_EQUATION_VERSION,
                            expected,
                            record.balance,
                            True,
                            {"page_number": record.page_number, "source_anchor": record.source_anchor},
                        )
                    )
                    last_balance = record.balance
                elif last_balance is None and operands_available:
                    problems.append(
                        DocumentProblem(
                            "BLOCKING",
                            "DBS_OPENING_BALANCE_MISSING",
                            "the first running-balance equation is unavailable; subsequent checks resume from this printed balance",
                            {"currency": section.currency, "page_number": record.page_number},
                            record.source_anchor,
                        )
                    )
                    last_balance = record.balance
                else:
                    problems.append(
                        DocumentProblem(
                            "BLOCKING",
                            "DBS_BALANCE_CHECK_UNAVAILABLE",
                            "running balance check lacks a required operand",
                            {"currency": section.currency, "page_number": record.page_number},
                            record.source_anchor,
                        )
                    )
            elif supporting_kind in {"PAGE_CLOSING_BALANCE", "DATE_CLOSING_BALANCE"} and last_balance and record.balance:
                checks.append(
                    _make_check(
                        section.currency or "UNKNOWN",
                        "PAGE_CLOSING_BALANCE",
                        DBS_BALANCE_EQUATION_VERSION,
                        last_balance,
                        record.balance,
                        True,
                        {"page_number": record.page_number, "source_anchor": record.source_anchor},
                    )
                )
            elif supporting_kind == "SECTION_TOTAL":
                summary = record
        if summary is not None:
            if summary.debit is not None:
                checks.append(
                    _make_check(
                        section.currency or "UNKNOWN",
                        "SECTION_DEBIT_TOTAL",
                        DBS_BALANCE_EQUATION_VERSION,
                        _money_total(debits),
                        summary.debit,
                        True,
                        {"section_ordinal": section.ordinal},
                    )
                )
            if summary.credit is not None:
                checks.append(
                    _make_check(
                        section.currency or "UNKNOWN",
                        "SECTION_CREDIT_TOTAL",
                        DBS_BALANCE_EQUATION_VERSION,
                        _money_total(credits),
                        summary.credit,
                        True,
                        {"section_ordinal": section.ordinal},
                    )
                )
            if summary.balance is not None and last_balance is not None:
                checks.append(
                    _make_check(
                        section.currency or "UNKNOWN",
                        "SECTION_CLOSING_BALANCE",
                        DBS_BALANCE_EQUATION_VERSION,
                        last_balance,
                        summary.balance,
                        True,
                        {"section_ordinal": section.ordinal},
                    )
                )
    if any(check.blocking and check.passed is not True for check in checks):
        problems.append(DocumentProblem("BLOCKING", "DBS_RECONCILIATION_FAILED", "one or more DBS exact equations failed"))
    return checks


def _reconcile_paylah(
    records: list[DocumentRecord], opening: ExactMoney | None, closing: ExactMoney | None
) -> list[DocumentCheck]:
    transactions = [record for record in records if record.record_kind == "TRANSACTION"]
    debits = [record.amount for record in transactions if record.direction_marker == "DB" and record.amount]
    credits = [record.amount for record in transactions if record.direction_marker == "CR" and record.amount]
    checks: list[DocumentCheck] = []
    if opening is not None and closing is not None:
        expected = _money_expression(opening, _money_total(credits), _money_total(debits))
        checks.append(
            _make_check(
                "SGD",
                "STATEMENT_BALANCE",
                PAYLAH_BALANCE_EQUATION_VERSION,
                expected,
                closing,
                True,
                {"debit_rows": len(debits), "credit_rows": len(credits)},
            )
        )
    debit_multiset = sorted((value.coefficient, value.scale) for value in debits)
    credit_multiset = sorted((value.coefficient, value.scale) for value in credits)
    checks.append(
        DocumentCheck(
            "SGD",
            "AMOUNT_PAIRING_DIAGNOSTIC",
            "paylah-amount-multiset-diagnostic-v1",
            None,
            None,
            None,
            debit_multiset == credit_multiset,
            False,
            {"debit_rows": len(debits), "credit_rows": len(credits), "review_only": True},
        )
    )
    return checks


def _make_check(
    currency: str,
    check_type: str,
    equation_version: str,
    expected: ExactMoney,
    actual: ExactMoney,
    blocking: bool,
    context: dict[str, object],
) -> DocumentCheck:
    scale = max(expected.scale, actual.scale)
    expected_coefficient = expected.rescaled_coefficient(scale)
    actual_coefficient = actual.rescaled_coefficient(scale)
    difference_coefficient = actual_coefficient - expected_coefficient
    if abs(difference_coefficient) > MAX_ABS_COEFFICIENT:
        raise OverflowError("reconciliation difference exceeds exact-money limits")
    expected_value = ExactMoney(expected_coefficient, scale, format_decimal(expected_coefficient, scale))
    actual_value = ExactMoney(actual_coefficient, scale, actual.text)
    difference = ExactMoney(difference_coefficient, scale, format_decimal(difference_coefficient, scale))
    return DocumentCheck(
        currency,
        check_type,
        equation_version,
        expected_value,
        actual_value,
        difference,
        difference_coefficient == 0,
        blocking,
        context,
    )


def _money_expression(
    opening: ExactMoney,
    credit: ExactMoney | None,
    debit: ExactMoney | None,
) -> ExactMoney:
    values = [value for value in (opening, credit, debit) if value is not None]
    scale = max(value.scale for value in values)
    coefficient = opening.rescaled_coefficient(scale)
    if credit is not None:
        coefficient += credit.rescaled_coefficient(scale)
    if debit is not None:
        coefficient -= debit.rescaled_coefficient(scale)
    if abs(coefficient) > MAX_ABS_COEFFICIENT:
        raise OverflowError("money expression exceeds exact-money limits")
    return ExactMoney(coefficient, scale, format_decimal(coefficient, scale))


def _money_total(values: Iterable[ExactMoney]) -> ExactMoney:
    values = list(values)
    if not values:
        return ExactMoney(0, 2, "0.00")
    scale = max(value.scale for value in values)
    coefficient = sum(value.rescaled_coefficient(scale) for value in values)
    if abs(coefficient) > MAX_ABS_COEFFICIENT:
        raise OverflowError("money total exceeds exact-money limits")
    return ExactMoney(coefficient, scale, format_decimal(coefficient, scale))


def _document_anchor(
    prefix: str,
    page_number: int,
    bbox: tuple[float, float, float, float],
    text: str,
) -> str:
    payload = f"{prefix}\0{page_number}\0" + "\0".join(f"{value:.3f}" for value in bbox) + "\0" + text
    return f"pdf:{prefix}:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _merge_bbox(
    left: tuple[float, float, float, float], right: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    return (min(left[0], right[0]), min(left[1], right[1]), max(left[2], right[2]), max(left[3], right[3]))


def _extract_reference(description: str | None) -> str | None:
    if not description:
        return None
    lines = description.splitlines()
    candidates = [
        line.strip()
        for line in lines
        if "REF" in line.upper() or re.fullmatch(r"[A-Z0-9-]{8,}", line.strip())
    ]
    return "\n".join(candidates) if candidates else None
