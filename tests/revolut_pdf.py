"""Runtime Revolut PDF synthesis for the test suite.

Mirrors the contract of the retired ``tests.test_revolut_import.csv_bytes``: a
call site changes name, not shape. Rows stay in the old CSV column order

    [Type, Product, Started Date, Completed Date, Description, Amount, Fee,
     Currency, State, Balance]

so the parameterised call sites need no data rewrite. The helper renders one
issued-statement PDF that ``fincl.documents.parse_revolut_pdf`` reads back into
the same ledger semantics the CSV adapter produced:

* ``Amount`` keeps the CSV meaning (net of fee for an outflow, gross for a
  top-up); the printed money column is the balance delta ``Amount - Fee`` and a
  ``Fee:`` subline restores the counterpart figure.
* ``effective_at`` becomes the completed date at DAY precision — the PDF prints
  no time of day.
* The event kind is carried by the rendered description and card marker, since
  the PDF has no Type column. The Type value only steers how the row is drawn.
"""

from __future__ import annotations

import io
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from fincl.document_service import DocumentImportService
from fincl.documents import ExactMoney  # noqa: F401  (re-export convenience)

# Same six rows as the retired test_revolut_import.GOOD_ROWS.
GOOD_ROWS: list[list[str]] = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed USD", "100.00", "0.00", "USD", "COMPLETED", "100.00"],
    ["Topup", "Current", "2026-01-01 10:01:00", "2026-01-01 10:01:01", "Seed SGD", "20.00", "0.00", "SGD", "COMPLETED", "20.00"],
    ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01", "Shop, branch", "-10.00", "1.00", "USD", "COMPLETED", "89.00"],
    ["Card Payment", "Current", "2026-01-02 12:00:00", "2026-01-02 12:00:01", "Two-line merchant", "-5.00", "0.50", "SGD", "COMPLETED", "14.50"],
    ["Card Payment", "Current", "2026-01-03 13:00:00", "", "Reverted", "-3.00", "0.25", "USD", "REVERTED", ""],
    ["Card Payment", "Current", "2026-01-04 14:00:00", "", "Pending", "-2.00", "0.00", "USD", "PENDING", ""],
]

_OUT_R = 372.0
_IN_R = 450.0
_BAL_R = 556.0
_DESC_X = 125.0
_DATE_X = 43.0
_PAGE_BOTTOM = 70.0

_COL = {"COMPLETED": "TRANSACTIONS", "PENDING": "PENDING", "REVERTED": "REVERTED"}


def _d(value: str) -> Decimal:
    return Decimal(value or "0")


def _fmt(value: Decimal) -> str:
    return f"{value:.2f}"


def _row_date(row: list[str]) -> datetime:
    stamp = (row[3] or row[2])[:10]
    return datetime.strptime(stamp, "%Y-%m-%d")


def _long_date(moment: datetime) -> str:
    return f"{moment.day} {moment.strftime('%B %Y')}"


def _short_date(moment: datetime) -> str:
    return f"{moment.day} {moment.strftime('%b %Y')}"


def _render_description(row: list[str]) -> tuple[str, bool]:
    """Return ``(printed description, draw a Card: subline)`` for one row.

    The PDF has no Type column, so the event kind is reconstructed from the
    printed description and the card marker (`fincl.documents.
    revolut_pdf_event_kind`). The description text is printed verbatim so tests
    that key on the merchant label keep working; the Type only decides whether
    a ``Card:`` subline is drawn, and Exchange rows get the canonical wording
    the exchange linker needs.
    """
    kind = row[0].strip().upper().replace("-", "_").replace(" ", "_")
    description = " ".join((row[4] or "").split())
    currency = row[7]
    if kind == "EXCHANGE":
        # The event kind is read from the wording, so an Exchange row always
        # prints the canonical phrase regardless of its label column.
        return f"Exchanged to {currency}", False
    if kind in {"TOPUP", "TOP_UP"}:
        # A wire-in top-up prints "Payment from <party>"; the parser reconstructs
        # the TOPUP kind and strips the prefix back off the description.
        return f"Payment from {description or 'Sender'}", False
    card = kind in {"CARD_PAYMENT", "CARD_REFUND", "REFUND"}
    return description or kind.title(), card


class _Writer:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        # invariant=1 fixes the embedded timestamp and document id so the same
        # rows always render byte-for-byte, which the rebuild/replay tests that
        # import twice and compare rely on.
        self.canvas = Canvas(
            self.buffer, pagesize=A4, pageCompression=0, invariant=1
        )
        self.canvas.setFont("Helvetica", 8)
        self.y = 800.0

    def line(self, x: float, text: str) -> None:
        self.canvas.drawString(x, self.y, text)

    def right(self, x: float, text: str) -> None:
        self.canvas.drawRightString(x, self.y, text)

    def feed(self, delta: float = 14.0) -> None:
        self.y -= delta

    def page_break_if_needed(self, banner: str, header: str) -> None:
        if self.y > _PAGE_BOTTOM:
            return
        self.canvas.showPage()
        self.canvas.setFont("Helvetica", 8)
        self.y = 800.0
        self.line(_DATE_X, banner)
        self.feed()
        self._column_header(header)

    def _column_header(self, header: str) -> None:
        if header == "TRANSACTIONS":
            self.line(_DATE_X, "Date Description")
            self.right(_OUT_R, "out")
            self.right(_IN_R, "in")
            self.right(_BAL_R, "Balance")
        else:
            self.line(_DATE_X, "Start date Description")
            self.right(_OUT_R, "out")
            self.right(_IN_R, "in")
        # A "Money" token precedes each of out/in on the real statement. Keep it
        # clear of the right-aligned column word so pdfplumber never merges them.
        self.canvas.drawString(_OUT_R - 66, self.y, "Money")
        self.canvas.drawString(_IN_R - 66, self.y, "Money")
        self.feed()

    def column_header(self, header: str) -> None:
        self._column_header(header)

    def save(self) -> bytes:
        self.canvas.save()
        return self.buffer.getvalue()


def revolut_pdf_bytes(
    rows: list[list[str]] = GOOD_ROWS, *, period: tuple[datetime, datetime] | None = None
) -> bytes:
    """Render ``rows``. ``period`` prints that span; the default is the rows' own dates.

    A row may carry an eleventh column, ``"12.00 EUR"``: the amount it paid abroad.
    """
    writer = _Writer()
    by_currency: dict[str, list[list[str]]] = {}
    for row in rows:
        by_currency.setdefault(row[7], []).append(row)

    for currency, currency_rows in by_currency.items():
        banner = f"{currency} Statement"
        writer.line(_DATE_X, banner)
        writer.feed()

        completed = [r for r in currency_rows if r[8] == "COMPLETED"]
        money_out = sum((abs(_d(r[5]) - _d(r[6])) for r in completed if _d(r[5]) - _d(r[6]) < 0), Decimal(0))
        money_in = sum((_d(r[5]) - _d(r[6]) for r in completed if _d(r[5]) - _d(r[6]) > 0), Decimal(0))
        if completed:
            first = completed[0]
            opening = _d(first[9]) - (_d(first[5]) - _d(first[6]))
            closing = _d(completed[-1][9])
        else:
            opening = closing = Decimal(0)

        writer.line(_DATE_X, "Balance summary")
        writer.feed()
        writer.line(_DATE_X, "Total")
        writer.canvas.drawString(_DATE_X + 40, writer.y, _fmt(opening))
        writer.right(_OUT_R, _fmt(money_out))
        writer.right(_IN_R, _fmt(money_in))
        writer.right(_BAL_R, _fmt(closing))
        writer.feed()

        dates = [_row_date(r) for r in currency_rows]
        first_day, last_day = period or (min(dates), max(dates))
        span = f"from {_long_date(first_day)} to {_long_date(last_day)}"

        for state in ("COMPLETED", "PENDING", "REVERTED"):
            section_rows = [r for r in currency_rows if r[8] == state]
            if not section_rows:
                continue
            section = _COL[state]
            heading = {
                "TRANSACTIONS": "Account transactions",
                "PENDING": "Pending",
                "REVERTED": "Reverted",
            }[section]
            writer.page_break_if_needed(banner, section)
            writer.line(_DATE_X, f"{heading} {span}")
            writer.feed()
            writer.column_header(section)

            for row in section_rows:
                writer.page_break_if_needed(banner, section)
                amount, fee = _d(row[5]), _d(row[6])
                delta = amount - fee
                description, card = _render_description(row)
                writer.line(_DATE_X, _short_date(_row_date(row)))
                writer.canvas.drawString(_DESC_X, writer.y, description)
                if section == "TRANSACTIONS":
                    if delta < 0:
                        writer.right(_OUT_R, _fmt(-delta))
                    else:
                        writer.right(_IN_R, _fmt(delta))
                    writer.right(_BAL_R, _fmt(_d(row[9])))
                else:
                    if delta < 0:
                        writer.right(_OUT_R, _fmt(-delta))
                    else:
                        writer.right(_IN_R, _fmt(delta))
                writer.feed(12.0)
                if fee != 0:
                    counterpart = amount if delta < 0 else amount  # merchant / gross
                    writer.canvas.drawString(_DESC_X, writer.y, f"Fee: {_fmt(abs(fee))} {_fmt(abs(counterpart))}")
                    writer.feed(12.0)
                if len(row) > 10:
                    writer.canvas.drawString(_DESC_X, writer.y, f"Revolut Rate {row[10]}")
                    writer.feed(12.0)
                if card:
                    writer.canvas.drawString(_DESC_X, writer.y, "Card: 400000******1234")
                    writer.feed(12.0)

        writer.feed()

    writer.line(_DATE_X, "Report lost or stolen card")
    return writer.save()


def seed_revolut(database: Path, evidence: Path, rows: list[list[str]] = GOOD_ROWS):
    """Write ``rows`` as a PDF and import it through the document path."""
    source = Path(database).parent / "revolut.pdf"
    source.write_bytes(revolut_pdf_bytes(rows))
    return DocumentImportService.plaintext_phase2(Path(database), Path(evidence)).import_revolut_pdf(
        source
    )
