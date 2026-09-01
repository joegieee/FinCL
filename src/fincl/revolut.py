from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .errors import ExtractionError
from .money import ExactMoney

SOURCE_PROFILE_VERSION = "revolut-csv-en-sg-2026-v1"
PARSER_VERSION = "revolut-parser-v1"
EXTRACTION_POLICY_VERSION = "phase1-policy-v1"
BALANCE_EQUATION_VERSION = "revolut-next-equals-prior-plus-amount-minus-fee-v1"
EXPECTED_HEADER = (
    "Type",
    "Product",
    "Started Date",
    "Completed Date",
    "Description",
    "Amount",
    "Fee",
    "Currency",
    "State",
    "Balance",
)
LIFECYCLE_STATES = frozenset({"COMPLETED", "PENDING", "REVERTED"})
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class ProfileMismatch(ExtractionError):
    def __init__(
        self,
        message: str,
        *,
        header_text: str | None = None,
        header_cells: list[str] | None = None,
        physical_data_row_count: int | None = None,
    ):
        super().__init__(message)
        self.header_text = header_text
        self.header_cells = header_cells
        self.physical_data_row_count = physical_data_row_count


@dataclass(frozen=True, slots=True)
class RowProblem:
    severity: str
    code: str
    message: str
    context: dict[str, object]


@dataclass(frozen=True, slots=True)
class RevolutRow:
    ordinal: int
    physical_line_start: int
    physical_line_end: int
    raw_record_text: str
    cells: tuple[str, ...]
    source_anchor: str
    type_raw: str | None
    product_raw: str | None
    started_at_raw: str | None
    completed_at_raw: str | None
    description_raw: str | None
    amount_text: str | None
    amount: ExactMoney | None
    fee_text: str | None
    fee: ExactMoney | None
    currency_raw: str | None
    state_raw: str | None
    balance_text: str | None
    balance: ExactMoney | None
    problems: tuple[RowProblem, ...]

    @property
    def required_fields_complete(self) -> bool:
        return not any(problem.severity == "BLOCKING" for problem in self.problems)


@dataclass(frozen=True, slots=True)
class RevolutExport:
    header_text: str
    header_cells: tuple[str, ...]
    header_sha256: str
    rows: tuple[RevolutRow, ...]

    @property
    def physical_data_row_count(self) -> int:
        return len(self.rows)


def extraction_identity(evidence_sha256: str) -> str:
    payload = "\0".join(
        (
            evidence_sha256,
            SOURCE_PROFILE_VERSION,
            PARSER_VERSION,
            EXTRACTION_POLICY_VERSION,
        )
    ).encode("utf-8")
    return "ext_" + hashlib.sha256(payload).hexdigest()


def parse_revolut_csv(data: bytes) -> RevolutExport:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProfileMismatch("Revolut CSV is not valid UTF-8") from exc

    physical_lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ProfileMismatch("Revolut CSV is empty") from exc
    except csv.Error as exc:
        raise ProfileMismatch(f"cannot parse CSV header: {exc}") from exc

    header_end = reader.line_num
    header_text = "".join(physical_lines[:header_end])
    header_sha256 = hashlib.sha256(header_text.encode("utf-8")).hexdigest()
    if tuple(header) != EXPECTED_HEADER:
        row_count = _count_remaining_records(reader)
        raise ProfileMismatch(
            "header does not match the frozen Revolut source profile",
            header_text=header_text,
            header_cells=header,
            physical_data_row_count=row_count,
        )

    rows: list[RevolutRow] = []
    previous_end = header_end
    occurrences: dict[str, int] = {}
    try:
        for ordinal, cells in enumerate(reader, start=1):
            line_end = reader.line_num
            line_start = previous_end + 1
            raw_record_text = "".join(physical_lines[previous_end:line_end])
            previous_end = line_end
            record_digest = hashlib.sha256(raw_record_text.encode("utf-8")).hexdigest()
            occurrence = occurrences.get(record_digest, 0) + 1
            occurrences[record_digest] = occurrence
            source_anchor = f"csv:{header_sha256}:{record_digest}:{occurrence}"
            rows.append(
                _parse_row(
                    ordinal=ordinal,
                    line_start=line_start,
                    line_end=line_end,
                    raw_record_text=raw_record_text,
                    cells=cells,
                    source_anchor=source_anchor,
                )
            )
    except csv.Error as exc:
        raise ProfileMismatch(
            f"CSV syntax error near physical line {reader.line_num}: {exc}",
            header_text=header_text,
            header_cells=header,
            physical_data_row_count=len(rows),
        ) from exc
    return RevolutExport(
        header_text=header_text,
        header_cells=tuple(header),
        header_sha256=header_sha256,
        rows=tuple(rows),
    )


def _count_remaining_records(reader: Iterable[list[str]]) -> int | None:
    try:
        return sum(1 for _ in reader)
    except csv.Error:
        return None


def _parse_row(
    *,
    ordinal: int,
    line_start: int,
    line_end: int,
    raw_record_text: str,
    cells: list[str],
    source_anchor: str,
) -> RevolutRow:
    problems: list[RowProblem] = []
    padded: list[str | None] = list(cells[: len(EXPECTED_HEADER)])
    padded.extend([None] * (len(EXPECTED_HEADER) - len(padded)))
    if len(cells) != len(EXPECTED_HEADER):
        problems.append(
            RowProblem(
                "BLOCKING",
                "CSV_COLUMN_COUNT",
                f"expected {len(EXPECTED_HEADER)} columns but found {len(cells)}",
                {"expected": len(EXPECTED_HEADER), "actual": len(cells)},
            )
        )
    (
        type_raw,
        product_raw,
        started_at_raw,
        completed_at_raw,
        description_raw,
        amount_text,
        fee_text,
        currency_raw,
        state_raw,
        balance_text,
    ) = padded

    for field_name, value in (
        ("Type", type_raw),
        ("Product", product_raw),
        ("Started Date", started_at_raw),
        ("Amount", amount_text),
        ("Fee", fee_text),
        ("Currency", currency_raw),
        ("State", state_raw),
    ):
        if value is None or value == "":
            problems.append(
                RowProblem(
                    "BLOCKING",
                    "REQUIRED_FIELD_MISSING_" + _code_name(field_name),
                    f"required field {field_name!r} is blank",
                    {"field": field_name},
                )
            )

    _validate_timestamp("Started Date", started_at_raw, False, problems)
    _validate_timestamp("Completed Date", completed_at_raw, True, problems)
    amount = _parse_money("Amount", amount_text, problems)
    fee = _parse_money("Fee", fee_text, problems)
    balance = _parse_money("Balance", balance_text, problems)

    if state_raw not in LIFECYCLE_STATES:
        problems.append(
            RowProblem(
                "BLOCKING",
                "UNKNOWN_LIFECYCLE_STATE",
                "lifecycle state is not supported by this source profile",
                {"state": state_raw},
            )
        )
    if product_raw not in (None, "", "Current"):
        problems.append(
            RowProblem(
                "BLOCKING",
                "UNSUPPORTED_PRODUCT",
                "product is outside the frozen Revolut source profile",
                {"product": product_raw, "supported_product": "Current"},
            )
        )
    if currency_raw is not None and not _CURRENCY_RE.fullmatch(currency_raw):
        problems.append(
            RowProblem(
                "BLOCKING",
                "INVALID_CURRENCY",
                "currency is not a three-letter uppercase code",
                {"currency": currency_raw},
            )
        )
    if state_raw == "COMPLETED":
        if not completed_at_raw:
            problems.append(
                RowProblem(
                    "BLOCKING",
                    "COMPLETED_TIMESTAMP_MISSING",
                    "a completed row must have its completed timestamp",
                    {},
                )
            )
        if not balance_text:
            problems.append(
                RowProblem(
                    "BLOCKING",
                    "COMPLETED_BALANCE_MISSING",
                    "a completed row must have a running balance",
                    {},
                )
            )
    if state_raw in {"PENDING", "REVERTED"} and fee is not None and not fee.is_zero():
        problems.append(
            RowProblem(
                "WARNING",
                "NON_COMPLETED_NONZERO_FEE",
                "non-completed row has a non-zero fee; it remains analytics-ineligible",
                {"state": state_raw, "fee": fee.text},
            )
        )

    return RevolutRow(
        ordinal=ordinal,
        physical_line_start=line_start,
        physical_line_end=line_end,
        raw_record_text=raw_record_text,
        cells=tuple(cells),
        source_anchor=source_anchor,
        type_raw=type_raw,
        product_raw=product_raw,
        started_at_raw=started_at_raw,
        completed_at_raw=completed_at_raw,
        description_raw=description_raw,
        amount_text=amount_text,
        amount=amount,
        fee_text=fee_text,
        fee=fee,
        currency_raw=currency_raw,
        state_raw=state_raw,
        balance_text=balance_text,
        balance=balance,
        problems=tuple(_deduplicate_problems(problems)),
    )


def _parse_money(
    field_name: str,
    value: str | None,
    problems: list[RowProblem],
) -> ExactMoney | None:
    if value is None or value == "":
        return None
    try:
        return ExactMoney.parse(value)
    except (ValueError, OverflowError) as exc:
        problems.append(
            RowProblem(
                "BLOCKING",
                "INVALID_MONEY_" + _code_name(field_name),
                f"{field_name} is not an accepted exact decimal",
                {"field": field_name, "value": value, "reason": str(exc)},
            )
        )
        return None


def _validate_timestamp(
    field_name: str,
    value: str | None,
    nullable: bool,
    problems: list[RowProblem],
) -> None:
    if value is None or value == "":
        return
    if not _TIMESTAMP_RE.fullmatch(value):
        valid = False
    else:
        try:
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            valid = True
        except ValueError:
            valid = False
    if not valid:
        problems.append(
            RowProblem(
                "BLOCKING",
                "INVALID_TIMESTAMP_" + _code_name(field_name),
                f"{field_name} is not a timezone-unassigned Revolut timestamp",
                {"field": field_name, "value": value, "nullable": nullable},
            )
        )


def _code_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _deduplicate_problems(problems: list[RowProblem]) -> Iterable[RowProblem]:
    seen: set[str] = set()
    for problem in problems:
        if problem.code not in seen:
            seen.add(problem.code)
            yield problem


def cells_json(cells: tuple[str, ...] | list[str]) -> str:
    return json.dumps(cells, ensure_ascii=False, separators=(",", ":"))
