"""Reference FX rates for reporting-currency valuation (F15, W4).

Rates are neither derived from the evidence vault nor originating operator
decisions: they are external reference data, loaded explicitly and carried
forward across a rebuild. A rate says ``1 base_currency = numerator/denominator
quote_currency`` for one calendar month, kept as an exact integer ratio plus the
printed text it came from. No rate is inferred; a month with no loaded rate
leaves the consolidated total unavailable rather than guessed.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from sqlite3 import Connection

from .errors import FinCLError

_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DECIMAL = re.compile(r"^\d+(\.\d+)?$")


def load_fx_rates(connection: Connection, payload: dict[str, object]) -> int:
    """Validate and upsert a rate document; return the number of rates written.

    Expected shape::

        {
          "source": "MAS monthly average",
          "source_version": "mas-monthly-average-2026-v1",
          "quote_currency": "SGD",
          "rates": [
            {"base_currency": "USD", "period": "2026-01", "rate": "1.3402"},
            ...
          ]
        }
    """
    source = _require_str(payload, "source")
    source_version = _require_str(payload, "source_version")
    quote_currency = _require_str(payload, "quote_currency")
    if not _CURRENCY.match(quote_currency):
        raise FinCLError(f"quote_currency is not an ISO code: {quote_currency!r}")
    rates = payload.get("rates")
    if not isinstance(rates, list) or not rates:
        raise FinCLError("fx rate document has no 'rates' array")

    imported_at = datetime.now(UTC).isoformat(timespec="microseconds")
    written = 0
    with connection:
        for index, entry in enumerate(rates):
            if not isinstance(entry, dict):
                raise FinCLError(f"fx rate {index} is not an object")
            base_currency = _require_str(entry, "base_currency")
            period = _require_str(entry, "period")
            rate_text = _require_str(entry, "rate")
            if not _CURRENCY.match(base_currency):
                raise FinCLError(f"fx rate {index} base_currency is not an ISO code: {base_currency!r}")
            if base_currency == quote_currency:
                raise FinCLError(f"fx rate {index} maps {base_currency} to itself")
            if not _PERIOD.match(period):
                raise FinCLError(f"fx rate {index} period is not YYYY-MM: {period!r}")
            if not _DECIMAL.match(rate_text) or Fraction(rate_text) <= 0:
                raise FinCLError(f"fx rate {index} rate is not a positive decimal: {rate_text!r}")
            numerator, denominator = _ratio(rate_text)
            connection.execute(
                """
                INSERT INTO fx_rates(
                    base_currency, quote_currency, period, rate_numerator,
                    rate_denominator, rate_text, source, source_version, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(base_currency, quote_currency, period, source_version)
                DO UPDATE SET
                    rate_numerator = excluded.rate_numerator,
                    rate_denominator = excluded.rate_denominator,
                    rate_text = excluded.rate_text,
                    source = excluded.source,
                    imported_at = excluded.imported_at
                """,
                (
                    base_currency, quote_currency, period, numerator, denominator,
                    rate_text, source, source_version, imported_at,
                ),
            )
            written += 1
    return written


def load_fx_rates_file(connection: Connection, path: Path) -> int:
    try:
        payload = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise FinCLError(f"cannot read fx rate document: {exc}") from exc
    if not isinstance(payload, dict):
        raise FinCLError("fx rate document must be a JSON object")
    return load_fx_rates(connection, payload)


def rate_fraction(
    connection: Connection, base_currency: str, quote_currency: str, period: str
) -> Fraction | None:
    """Latest loaded rate for one month, or None if none is loaded."""
    row = connection.execute(
        """
        SELECT rate_numerator, rate_denominator FROM fx_rates
        WHERE base_currency = ? AND quote_currency = ? AND period = ?
        ORDER BY imported_at DESC LIMIT 1
        """,
        (base_currency, quote_currency, period),
    ).fetchone()
    if row is None:
        return None
    return Fraction(int(row[0]), int(row[1]))


def _ratio(decimal_text: str) -> tuple[int, int]:
    if "." in decimal_text:
        whole, fraction = decimal_text.split(".", 1)
        return int(whole + fraction), 10 ** len(fraction)
    return int(decimal_text), 1


def _require_str(source: dict[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FinCLError(f"fx rate document is missing a non-empty {key!r}")
    return value.strip()
