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

# MAS weekly exchange-rate CSV: "S$ Per Unit of US Dollar" column headers, month
# names abbreviated, one currency per column block. Extend the name map as new
# currencies appear in a real export.
_MAS_CURRENCY_NAMES = {
    "US DOLLAR": "USD",
    "EURO": "EUR",
    "POUND STERLING": "GBP",
    "AUSTRALIAN DOLLAR": "AUD",
    "CANADIAN DOLLAR": "CAD",
    "SWISS FRANC": "CHF",
    "NEW ZEALAND DOLLAR": "NZD",
    "MALAYSIAN RINGGIT": "MYR",
    "HONG KONG DOLLAR": "HKD",
    "CHINESE RENMINBI": "CNY",
}
_MAS_HEADER_RE = re.compile(
    r"S\$\s+Per\s+(?:(\d+)\s+)?(?:Units?\s+of\s+)?(.+?)\s*$", re.IGNORECASE
)
_MAS_MONTHS = {
    name: index
    for index, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def load_fx_rates(connection: Connection, payload: dict[str, object]) -> int:
    """Import immutable rates; identical rows are no-ops. Return new row count.

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
        frozen_keys = {
            tuple(row) for row in connection.execute(
                "SELECT base_currency, quote_currency, period FROM fx_rates WHERE source_version = ?",
                (source_version,),
            )
        }
        supplied_keys: set[tuple[str, str, str]] = set()
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
            key = (base_currency, quote_currency, period)
            supplied_keys.add(key)
            if frozen_keys and key not in frozen_keys:
                raise FinCLError(f"immutable FX version {source_version!r}: use a new source_version")
            numerator, denominator = _ratio(rate_text)
            existing = connection.execute(
                "SELECT rate_text, source FROM fx_rates WHERE base_currency = ? "
                "AND quote_currency = ? AND period = ? AND source_version = ?",
                (base_currency, quote_currency, period, source_version),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (rate_text, source):
                    raise FinCLError(
                        f"immutable FX version {source_version!r} conflicts for "
                        f"{base_currency}/{quote_currency} {period}; use a new source_version"
                    )
                continue
            connection.execute(
                """
                INSERT INTO fx_rates(
                    base_currency, quote_currency, period, rate_numerator,
                    rate_denominator, rate_text, source, source_version, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    base_currency, quote_currency, period, numerator, denominator,
                    rate_text, source, source_version, imported_at,
                ),
            )
            written += 1
        if frozen_keys and supplied_keys != frozen_keys:
            raise FinCLError(f"immutable FX version {source_version!r}: incomplete reimport")
    return written


def load_fx_rates_file(connection: Connection, path: Path) -> int:
    try:
        payload = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise FinCLError(f"cannot read fx rate document: {exc}") from exc
    if not isinstance(payload, dict):
        raise FinCLError("fx rate document must be a JSON object")
    return load_fx_rates(connection, payload)


def parse_mas_weekly_csv(text: str, *, source_version: str) -> dict[str, object]:
    """Reduce a MAS *weekly end of period* exchange-rate CSV to a month keyed
    rate document.

    MAS publishes weekly figures as end-of-period (Friday) values. The rate for
    a calendar month is taken as the **last weekly value printed within that
    month** — a month-end reading, which the design permits over the monthly
    average (Default 8). The output has exactly the shape ``load_fx_rates``
    expects, so it flows through the same validation and exact-ratio storage.
    """
    base_currency: str | None = None
    year: int | None = None
    month: int | None = None
    latest: dict[tuple[str, int, int], tuple[int, str]] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith('"')
            or line.startswith("MAS:")
            or line.startswith("Exchange Rates")
        ):
            continue
        cells = [cell.strip() for cell in line.split(",")]
        if cells[0].lower().startswith("end of period"):
            match = _MAS_HEADER_RE.search(cells[-1])
            if not match:
                raise FinCLError(f"unrecognized MAS column header: {cells[-1]!r}")
            multiplier, name = match.group(1), match.group(2).strip().upper()
            if multiplier and multiplier != "1":
                raise FinCLError(
                    f"MAS column {name!r} is quoted per {multiplier} units; "
                    "per-N currencies are not supported by this loader yet"
                )
            base_currency = _MAS_CURRENCY_NAMES.get(name)
            if base_currency is None:
                raise FinCLError(f"unmapped MAS currency name: {name!r}")
            year = month = None
            continue
        if base_currency is None or len(cells) < 4:
            continue
        year_cell, month_cell, day_cell, rate_cell = cells[0], cells[1], cells[2], cells[3]
        if year_cell:
            if not year_cell.isdigit():
                continue
            year = int(year_cell)
        if month_cell:
            key = month_cell[:3].title()
            if key not in _MAS_MONTHS:
                raise FinCLError(f"unrecognized MAS month: {month_cell!r}")
            month = _MAS_MONTHS[key]
        if year is None or month is None or not day_cell.isdigit():
            continue
        if not _DECIMAL.match(rate_cell) or Fraction(rate_cell) <= 0:
            raise FinCLError(f"MAS rate cell is not a positive decimal: {rate_cell!r}")
        day = int(day_cell)
        group = (base_currency, year, month)
        if group not in latest or day > latest[group][0]:
            latest[group] = (day, rate_cell)

    if not latest:
        raise FinCLError("no MAS weekly rate rows were parsed")
    rates = [
        {"base_currency": base, "period": f"{yr:04d}-{mo:02d}", "rate": rate_text}
        for (base, yr, mo), (_day, rate_text) in sorted(latest.items())
    ]
    return {
        "source": "Monetary Authority of Singapore (weekly end of period)",
        "source_version": source_version,
        "quote_currency": "SGD",
        "rates": rates,
    }


def load_mas_weekly_csv_file(
    connection: Connection, path: Path, *, source_version: str
) -> int:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise FinCLError(f"cannot read MAS rate CSV: {exc}") from exc
    return load_fx_rates(connection, parse_mas_weekly_csv(text, source_version=source_version))


def rate_fraction(
    connection: Connection, base_currency: str, quote_currency: str, period: str
) -> Fraction | None:
    """Latest loaded rate for one month, or None if none is loaded."""
    row = selected_rate(connection, base_currency, quote_currency, period)
    if row is None:
        return None
    return Fraction(int(row["rate_numerator"]), int(row["rate_denominator"]))


def selected_rate(
    connection: Connection, base_currency: str, quote_currency: str, period: str
) -> dict[str, str] | None:
    """Exact selected input, including provenance, for a report manifest."""
    cursor = connection.execute(
        """
        SELECT base_currency, quote_currency, period, rate_numerator,
               rate_denominator, rate_text, source, source_version, imported_at FROM fx_rates
        WHERE base_currency = ? AND quote_currency = ? AND period = ?
        ORDER BY imported_at DESC, source_version DESC LIMIT 1
        """,
        (base_currency, quote_currency, period),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {column[0]: str(value) for column, value in zip(cursor.description, row, strict=True)}


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
