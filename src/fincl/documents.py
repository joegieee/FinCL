from __future__ import annotations

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
DBS_PROFILE_VERSION = "dbs-consolidated-2025-v1"
PAYLAH_OLD_PROFILE_VERSION = "dbs-paylah-legacy-2025-v1"
PAYLAH_NEW_PROFILE_VERSION = "dbs-paylah-account-summary-2026-v1"
DBS_BALANCE_EQUATION_VERSION = "dbs-next-equals-prior-minus-debit-plus-credit-v1"
PAYLAH_BALANCE_EQUATION_VERSION = "paylah-closing-equals-opening-plus-cr-minus-db-v1"

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
    transaction_dates = [
        date.fromisoformat(record.date_value)
        for record in records
        if record.record_kind == "TRANSACTION" and record.date_value
    ]
    checks = _reconcile_dbs(records, sections, problems)
    statement_date_text = statement_date.isoformat()
    return ParsedDocument(
        institution="DBS",
        source_type="DBS_CONSOLIDATED",
        account_id="dbs-my-account",
        account_display_name="DBS My Account",
        masked_identifier="DBS deposit account (masked)",
        source_profile_version=DBS_PROFILE_VERSION,
        statement_date=statement_date_text,
        period_start=min(transaction_dates).isoformat() if transaction_dates else None,
        period_end=max(transaction_dates).isoformat() if transaction_dates else None,
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
    transaction_dates = [
        date.fromisoformat(record.date_value)
        for record in records
        if record.record_kind == "TRANSACTION" and record.date_value
    ]
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
    return ParsedDocument(
        institution="DBS",
        source_type="DBS_PAYLAH",
        account_id="dbs-paylah-wallet",
        account_display_name="DBS PayLah! Wallet",
        masked_identifier="PayLah wallet (masked)",
        source_profile_version=profile,
        statement_date=statement_date_text,
        period_start=min(transaction_dates).isoformat() if transaction_dates else None,
        period_end=max(transaction_dates).isoformat() if transaction_dates else None,
        statement_identity=f"dbs-paylah:dbs-paylah-wallet:{statement_date_text}",
        sections=[DocumentSection(1, "WALLET_LEDGER", "SGD", "PayLah! Wallet", 1, len(coverage))],
        records=records,
        pages=coverage,
        checks=checks,
        problems=problems,
    )


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
