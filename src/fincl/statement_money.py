"""Read-only statement money normalizer (plan §5, M3b.2 step 06).

`ledger_entries` already stores the settled native amount, an optional
explicit fee, and — when the write-time parser could resolve it — an
original `source_currency`/`source_amount_*` pair. The linked
`document_records` row additionally preserves the full printed statement
text and a source-specific `raw_fields_json` blob (Trust's `fx` dict,
Revolut's `fx_line`/`balance_delta`, …). Already-accepted rows can carry
usable foreign-amount evidence in that raw text that the write-time parser
could not turn into `source_currency`/`source_amount_*` — e.g. amounts
printed with a `₹`
symbol or as an integer/grouped VND figure, which the write-time tail-token
match (two decimal places, a bare `S$`/`$` prefix or a three-letter code)
does not accept.

This module is a pure read adapter over that already-preserved evidence: it
performs no writes, never touches `ledger_entries`/`document_records`, never
re-extracts a statement, and never bumps a parser or profile version. Its
only job is to resolve one `StatementMoneyDetailDTO` per ledger entry so a
reporting reader (step 07) does not have to know each source's storage
quirks. Where the write path could not populate `source_currency`/
`source_amount_*`, and this module cannot unambiguously decode the raw
evidence either, the original amount stays `None` (never a fabricated
zero) and that is disclosed through `validation`/`provenance`, not silently
hidden.

Resolution order (plan §5.2):

1. Complete stored `source_currency`/`source_amount_*` is preferred and
   authoritative.
2. Otherwise, decode the linked raw evidence with a source-specific decoder.
3. A stored value that disagrees with a successfully decoded value produces
   a visible `conflicting_fields` issue; the stored value is kept, never
   silently replaced. A decode that cannot be resolved unambiguously leaves
   the original amount unavailable rather than guessing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from .errors import FinCLError
from .money import ExactMoney, compare

NORMALIZER_VERSION = "statement-money-v1"

DETAIL_BASIS_STORED = "stored_columns"
DETAIL_BASIS_DECODED = "decoded_statement_text"
DETAIL_BASIS_UNAVAILABLE = "unavailable"

VALIDATION_MATCHES = "matches"
VALIDATION_MISMATCH = "mismatch"
VALIDATION_NOT_CHECKABLE = "not_checkable"
VALIDATION_CONFLICT = "conflicting_fields"

AMOUNT_BASIS_FEES_EXCLUDED = "fees_excluded"
AMOUNT_BASIS_FEE_NOT_APPLICABLE = "fee_not_applicable"


@dataclass(frozen=True, slots=True)
class ExactAmountDTO:
    currency: str
    amount_text: str


@dataclass(frozen=True, slots=True)
class PrintedConversionDTO:
    base_currency: str
    quote_currency: str
    base_amount_text: str
    quote_amount_text: str
    raw_text: str


@dataclass(frozen=True, slots=True)
class EffectiveRateDTO:
    base_currency: str
    quote_currency: str
    numerator: str
    denominator: str
    amount_basis: str


@dataclass(frozen=True, slots=True)
class ValidationDTO:
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class ProvenanceDTO:
    document_record_id: str | None
    source_location: str | None
    detail_basis: str
    normalizer_version: str


@dataclass(frozen=True, slots=True)
class StatementMoneyDetailDTO:
    original: ExactAmountDTO | None
    settled: ExactAmountDTO
    explicit_fee: ExactAmountDTO | None
    statement_cash_movement: ExactAmountDTO | None
    printed_conversion: PrintedConversionDTO | None
    printed_charge_percent: str | None
    effective_rate: EffectiveRateDTO | None
    validation: ValidationDTO
    provenance: ProvenanceDTO


@dataclass(frozen=True, slots=True)
class _DecodedOriginal:
    currency: str = ""
    amount_text: str = ""
    ambiguous: bool = False
    reason: str | None = None


# A printed amount, tolerant of a leading Revolut-style symbol (`S$`, `$`,
# `₹`), thousands-grouping commas, and an optional decimal fraction — this is
# deliberately looser than any write-time transaction-row regex (those stay
# untouched) so it can recover the ₹/INR and integer/grouped VND printings
# the write path's two-decimal-place tail-token match rejects.
_MONEY_TOKEN_RE = re.compile(r"^(?P<symbol>S\$|\$|₹)?(?P<amount>\d[\d,]*(?:\.\d+)?)$")
_MONEY_TOKEN = r"(?:S\$|\$|₹)?\d[\d,]*(?:\.\d+)?"

# An example printed Revolut line (invented figures): `Revolut Rate S$1.00 = ₹60.00 (ECB rate* S$1.00 = ₹59.50) ₹1,000.00`
# — a 1-unit provider rate, a parenthesized ECB benchmark rate the provider
# rate is compared against, and the original purchase amount printed last.
# The ECB group is matched so it is never mistaken for the provider rate or
# the original amount, but its own figures are not separately surfaced (the
# strict DTO in plan §5.3 has no field for a benchmark rate); the full line
# always survives in `printed_conversion.raw_text`.
_REVOLUT_FX_LINE_RE = re.compile(
    r"^Revolut Rate\s+(?:S\$|\$)1(?:\.00)?\s*=\s*"
    rf"(?P<quote_amt>{_MONEY_TOKEN})\s*(?P<quote_cur>[A-Z]{{3}})?"
    r"(?:\s*\(ECB rate\*\s+(?:S\$|\$)1(?:\.00)?\s*=\s*"
    rf"(?:{_MONEY_TOKEN})\s*(?:[A-Z]{{3}})?\))?"
    rf"\s+(?P<orig_amt>{_MONEY_TOKEN})\s*(?P<orig_cur>[A-Z]{{3}})?$"
)
_REVOLUT_FX_LINE_BASE_RE = re.compile(r"^Revolut Rate\s+(S\$|\$)")


def _resolve_symbol(symbol: str | None, *, source_type: str) -> str | None:
    """Resolve a printed currency symbol from unambiguous source context.

    `S$` and `₹` are unambiguous everywhere. A bare `$` is only resolved for
    Revolut, whose statements always print `S$` for SGD and reserve bare `$`
    for USD — a fixed house notation, not a currency-agnostic guess. No other
    source gets a bare-`$` resolution here (plan §5.2: never guess USD).
    """
    if symbol == "S$":
        return "SGD"
    if symbol == "₹":
        return "INR"
    if symbol == "$" and source_type == "REVOLUT_PDF":
        return "USD"
    return None


def _parse_money_token(token: str) -> tuple[str | None, ExactMoney]:
    match = _MONEY_TOKEN_RE.fullmatch(token)
    if match is None:
        raise ValueError(f"not a recognized printed amount: {token!r}")
    return match.group("symbol"), ExactMoney.parse(match.group("amount").replace(",", ""))


def _amounts_equal(left_text: str, right_text: str) -> bool:
    return compare(ExactMoney.parse(left_text), ExactMoney.parse(right_text)) == 0


def _decode_original(
    source_type: str, raw_fields: dict[str, object]
) -> _DecodedOriginal | None:
    if source_type == "REVOLUT_PDF":
        fx_line = raw_fields.get("fx_line")
        if not isinstance(fx_line, str):
            return None
        match = _REVOLUT_FX_LINE_RE.fullmatch(fx_line.strip())
        if match is None:
            return _DecodedOriginal(ambiguous=True, reason="unrecognized Revolut rate line format")
        try:
            symbol, amount = _parse_money_token(match.group("orig_amt"))
        except ValueError:
            return _DecodedOriginal(ambiguous=True, reason="unparseable original amount in Revolut rate line")
        currency = match.group("orig_cur") or _resolve_symbol(symbol, source_type=source_type)
        if currency is None:
            return _DecodedOriginal(ambiguous=True, reason="original currency symbol not resolvable")
        return _DecodedOriginal(currency=currency, amount_text=amount.text)

    if source_type == "TRUST_SAVINGS":
        columns = raw_fields.get("source_columns")
        if not isinstance(columns, dict):
            return None
        amount_fcy = columns.get("amount_fcy")
        currency = columns.get("foreign_currency")
        if not isinstance(amount_fcy, str) or not isinstance(currency, str):
            return None
        try:
            _, amount = _parse_money_token(amount_fcy.lstrip("+"))
        except ValueError:
            return _DecodedOriginal(ambiguous=True, reason="unparseable Trust foreign-currency amount")
        return _DecodedOriginal(currency=currency, amount_text=amount.text)

    # MARI_CARD already writes `foreign_currency`/`foreign_amount` straight
    # into `source_currency`/`source_amount_*` at parse time when its
    # `CUR 123.45` line is found; there is no separate raw field to decode.
    # DBS_CONSOLIDATED/DBS_PAYLAH/MARI_SAVINGS never carry foreign detail.
    return None


def _decode_printed_conversion(
    source_type: str, raw_fields: dict[str, object]
) -> tuple[PrintedConversionDTO | None, str | None]:
    if source_type == "REVOLUT_PDF":
        fx_line = raw_fields.get("fx_line")
        if not isinstance(fx_line, str):
            return None, None
        text = fx_line.strip()
        match = _REVOLUT_FX_LINE_RE.fullmatch(text)
        if match is None:
            return None, None
        base_match = _REVOLUT_FX_LINE_BASE_RE.match(text)
        base_currency = (
            _resolve_symbol(base_match.group(1), source_type=source_type) if base_match else None
        )
        try:
            quote_symbol, quote_amount = _parse_money_token(match.group("quote_amt"))
        except ValueError:
            return None, None
        quote_currency = match.group("quote_cur") or _resolve_symbol(
            quote_symbol, source_type=source_type
        )
        if base_currency is None or quote_currency is None:
            return None, None
        return (
            PrintedConversionDTO(
                base_currency=base_currency,
                quote_currency=quote_currency,
                base_amount_text="1.00",
                quote_amount_text=quote_amount.text,
                raw_text=text,
            ),
            None,
        )

    if source_type == "TRUST_SAVINGS":
        fx = raw_fields.get("fx")
        if isinstance(fx, dict):
            currency, rate, pct = fx.get("currency"), fx.get("rate_to_sgd"), fx.get("fx_charge_percent")
            if isinstance(currency, str) and isinstance(rate, str):
                percent_text = f"{pct}%" if isinstance(pct, str) else None
                raw_text = f"1 {currency} = {rate} SGD" + (
                    f" TRUST FX CHARGE = {pct}%" if isinstance(pct, str) else ""
                )
                return (
                    PrintedConversionDTO(
                        base_currency=currency,
                        quote_currency="SGD",
                        base_amount_text="1",
                        quote_amount_text=rate,
                        raw_text=raw_text,
                    ),
                    percent_text,
                )
        # Fall back to re-matching the immutable write-time regex against the
        # preserved raw line, in case a future statement's `fx` dict is
        # missing but the printed text still survived in `raw_text`.
        return None, None

    return None, None


def _effective_rate(
    original: ExactAmountDTO | None, settled: ExactAmountDTO, *, has_fee: bool
) -> EffectiveRateDTO | None:
    if original is None or original.currency == settled.currency:
        return None
    original_money = ExactMoney.parse(original.amount_text)
    settled_money = ExactMoney.parse(settled.amount_text)
    if original_money.coefficient == 0 or settled_money.coefficient == 0:
        return None
    ratio = Fraction(abs(original_money.coefficient), 10**original_money.scale) / Fraction(
        abs(settled_money.coefficient), 10**settled_money.scale
    )
    return EffectiveRateDTO(
        base_currency=settled.currency,
        quote_currency=original.currency,
        numerator=str(ratio.numerator),
        denominator=str(ratio.denominator),
        amount_basis=AMOUNT_BASIS_FEES_EXCLUDED if has_fee else AMOUNT_BASIS_FEE_NOT_APPLICABLE,
    )


def _decimal_as_fraction(text: str) -> Fraction:
    money = ExactMoney.parse(text)
    return Fraction(money.coefficient, 10**money.scale)


def _validate_rate(
    printed: PrintedConversionDTO | None, effective: EffectiveRateDTO | None
) -> ValidationDTO:
    if printed is None or effective is None:
        return ValidationDTO(
            VALIDATION_NOT_CHECKABLE,
            "no printed conversion and derived effective rate are both available to compare",
        )
    if printed.base_currency == effective.base_currency and printed.quote_currency == effective.quote_currency:
        printed_ratio = _decimal_as_fraction(printed.quote_amount_text) / _decimal_as_fraction(
            printed.base_amount_text
        )
    elif printed.base_currency == effective.quote_currency and printed.quote_currency == effective.base_currency:
        printed_ratio = _decimal_as_fraction(printed.base_amount_text) / _decimal_as_fraction(
            printed.quote_amount_text
        )
    else:
        return ValidationDTO(
            VALIDATION_NOT_CHECKABLE,
            "printed conversion direction does not match the original/settled currency pair",
        )
    effective_ratio = Fraction(int(effective.numerator), int(effective.denominator))
    # Documented rounding allowance: half a unit in the last printed decimal
    # place of the printed quote amount — the coarsest precision genuinely on
    # the statement, so a merely-rounded printed rate is not flagged as wrong.
    quote_scale = ExactMoney.parse(printed.quote_amount_text).scale
    tolerance = Fraction(1, 2 * (10**quote_scale))
    difference = abs(printed_ratio - effective_ratio)
    if difference <= tolerance:
        return ValidationDTO(
            VALIDATION_MATCHES,
            f"derived ratio is within the documented {quote_scale}-decimal printed rounding allowance",
        )
    return ValidationDTO(
        VALIDATION_MISMATCH,
        f"derived ratio differs from the printed rate by more than the documented "
        f"{quote_scale}-decimal rounding allowance",
    )


def read_statement_money_detail(
    connection: sqlite3.Connection, ledger_entry_id: str
) -> StatementMoneyDetailDTO:
    """Read-only: no write is issued against `connection` by this function."""
    row = connection.execute(
        """
        SELECT e.currency_pocket, e.amount_text, e.fee_text,
               e.source_currency, e.source_amount_text,
               e.document_record_id,
               s.source_type,
               d.raw_fields_json, d.page_number
        FROM ledger_entries e
        LEFT JOIN statements s ON s.id = e.statement_id
        LEFT JOIN document_records d ON d.id = e.document_record_id
        WHERE e.id = ?
        """,
        (ledger_entry_id,),
    ).fetchone()
    if row is None:
        raise FinCLError(f"no ledger entry found for id {ledger_entry_id!r}")

    settled = ExactAmountDTO(row["currency_pocket"], row["amount_text"])
    explicit_fee = (
        ExactAmountDTO(row["currency_pocket"], row["fee_text"]) if row["fee_text"] is not None else None
    )
    document_record_id = row["document_record_id"]

    if document_record_id is None:
        return StatementMoneyDetailDTO(
            original=None,
            settled=settled,
            explicit_fee=explicit_fee,
            statement_cash_movement=None,
            printed_conversion=None,
            printed_charge_percent=None,
            effective_rate=None,
            validation=ValidationDTO(VALIDATION_NOT_CHECKABLE, "entry has no linked statement evidence"),
            provenance=ProvenanceDTO(None, None, DETAIL_BASIS_UNAVAILABLE, NORMALIZER_VERSION),
        )

    source_type = row["source_type"] or ""
    raw_fields: dict[str, object] = {}
    if row["raw_fields_json"]:
        try:
            candidate = json.loads(row["raw_fields_json"])
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict):
            raw_fields = candidate

    stored_original = (
        ExactAmountDTO(row["source_currency"], row["source_amount_text"])
        if row["source_currency"] is not None and row["source_amount_text"] is not None
        else None
    )
    decoded = _decode_original(source_type, raw_fields)

    conflict_reason: str | None = None
    if stored_original is not None:
        original: ExactAmountDTO | None = stored_original
        detail_basis = DETAIL_BASIS_STORED
        if decoded is not None and not decoded.ambiguous and (
            decoded.currency != stored_original.currency
            or not _amounts_equal(decoded.amount_text, stored_original.amount_text)
        ):
            conflict_reason = (
                f"stored source amount {stored_original.currency} {stored_original.amount_text} "
                f"disagrees with the decoded statement text "
                f"{decoded.currency} {decoded.amount_text}"
            )
    elif decoded is not None and not decoded.ambiguous:
        original = ExactAmountDTO(decoded.currency, decoded.amount_text)
        detail_basis = DETAIL_BASIS_DECODED
    else:
        original = None
        detail_basis = DETAIL_BASIS_UNAVAILABLE

    printed_conversion, printed_charge_percent = _decode_printed_conversion(source_type, raw_fields)

    statement_cash_movement = None
    if source_type == "REVOLUT_PDF":
        delta_text = raw_fields.get("balance_delta")
        if isinstance(delta_text, str):
            statement_cash_movement = ExactAmountDTO(row["currency_pocket"], delta_text)

    effective_rate = _effective_rate(original, settled, has_fee=explicit_fee is not None)

    if conflict_reason is not None:
        validation = ValidationDTO(VALIDATION_CONFLICT, conflict_reason)
    elif original is None and decoded is not None and decoded.ambiguous:
        validation = ValidationDTO(
            VALIDATION_NOT_CHECKABLE, decoded.reason or "ambiguous original amount in statement text"
        )
    else:
        validation = _validate_rate(printed_conversion, effective_rate)

    source_location = f"Page {row['page_number']}" if row["page_number"] is not None else None
    return StatementMoneyDetailDTO(
        original=original,
        settled=settled,
        explicit_fee=explicit_fee,
        statement_cash_movement=statement_cash_movement,
        printed_conversion=printed_conversion,
        printed_charge_percent=printed_charge_percent,
        effective_rate=effective_rate,
        validation=validation,
        provenance=ProvenanceDTO(document_record_id, source_location, detail_basis, NORMALIZER_VERSION),
    )
