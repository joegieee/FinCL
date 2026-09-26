"""Runtime Mari credit-card statement synthesis for the test suite.

Reproduces the layout of ``fixtures/mari_card.pdf`` closely enough for
``fincl.documents.parse_mari_card`` to read it back: the period line, the account
summary row, the lettered bucket table with its printed equation, and one block
per transaction section. A row prints a label line above its date/amount line and
any further text below, which is how the real statement lays out merchants,
``Split Payment 6M`` plan terms and ``Split Payment 3/6 Instalment`` ordinals.

Buckets and current outstanding are computed from the rows, so a synthesized
statement always reconciles under the ``A-B-C+D+E-F+G+H-I`` equation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

_SECTION_BUCKET = {
    "Repayment/Conversion": "B",
    "Waiver/Refund": "C",
    "Purchase": "D",
    "Cash/Loans": "E",
    "Cashback": "F",
    "Bank Charge": "G",
    "Split Payment": "H",
    "Others": "I",
}
_LETTER_X = {"A": 48, "B": 110, "C": 175, "D": 235, "E": 292, "F": 350, "G": 405, "H": 458, "I": 512}
_SIGNS = {"A": 1, "B": -1, "C": -1, "D": 1, "E": 1, "F": -1, "G": 1, "H": 1, "I": -1}


@dataclass(frozen=True, slots=True)
class MariRow:
    """One printed transaction. ``amount`` is signed as printed: credits positive."""

    posted: date
    amount: Decimal
    above: str
    below: tuple[str, ...] = ()
    transacted: date | None = None


@dataclass(slots=True)
class MariStatement:
    period_start: date
    period_end: date
    statement_date: date
    due_date: date
    previous_balance: Decimal = Decimal("0.00")
    sections: dict[str, list[MariRow]] = field(default_factory=dict)


def _day(value: date) -> str:
    return value.strftime("%d %b").upper()


def _long(value: date) -> str:
    return value.strftime("%d %b %Y").upper()


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def mari_card_pdf_bytes(statement: MariStatement) -> bytes:
    buckets = {letter: Decimal("0.00") for letter in _LETTER_X}
    buckets["A"] = statement.previous_balance
    for heading, rows in statement.sections.items():
        buckets[_SECTION_BUCKET[heading]] += sum((abs(r.amount) for r in rows), Decimal("0.00"))
    outstanding = sum((_SIGNS[k] * v for k, v in buckets.items()), Decimal("0.00"))

    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=A4)
    height = A4[1]
    canvas.setFont("Helvetica", 9)

    def at(x: float, top: float, text: str) -> None:
        canvas.drawString(x, height - top - 7, text)

    def right(x: float, top: float, text: str) -> None:
        canvas.drawRightString(x, height - top - 7, text)

    at(40, 55, f"STATEMENT PERIOD: {_long(statement.period_start)} to {_long(statement.period_end)}")
    at(230, 75, "ACCOUNT SUMMARY")
    at(48, 107, "ACCOUNT STATEMENT DATE CREDIT LIMIT STATEMENT DUE MINIMUM PAYMENT DUE DUE DATE")
    minimum = min(outstanding, Decimal("30.00"))
    at(
        48, 123,
        f"MARI CREDIT CARD {_long(statement.statement_date)} 500.00 {_money(outstanding)} "
        f"{_money(minimum)} {_long(statement.due_date)}",
    )
    at(222, 149, "STATEMENT SUMMARY")
    for letter, x in _LETTER_X.items():
        at(x, 171, f"({letter})")
        right(x + 24, 187, _money(buckets[letter]))
    at(48, 207, f"CURRENT OUTSTANDING (A-B-C+D+E-F+G+H-I) {_money(outstanding)}")
    at(222, 237, "TRANSACTION DETAILS")
    at(48, 253, "POSTED DATE TRANSACTION DATE DESCRIPTION AMOUNT (SGD)")

    top = 275.0
    for heading, rows in statement.sections.items():
        at(250, top, heading)
        top += 16
        for row in rows:
            at(302, top, row.above)
            top += 12
            at(48, top, _day(row.posted))
            if row.transacted is not None:
                at(174, top, _day(row.transacted))
            right(545, top, ("+" if row.amount > 0 else "") + _money(row.amount))
            for text in row.below:
                top += 12
                at(302, top, text)
            top += 24
    at(48, top, "General")
    at(48, top + 12, "This is a system-generated e-Statement.")
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def conversion_statement() -> MariStatement:
    """The statement after ``fixtures/mari_card.pdf``: its 3/6 chain continues and a plan opens.

    The operator converted 150.40 of purchases, so the statement prints that as a
    ``Split Payment Conversion`` credit next to the new chain's 1/6 instalment.
    """
    return MariStatement(
        period_start=date(2026, 7, 21),
        period_end=date(2026, 8, 20),
        statement_date=date(2026, 8, 21),
        due_date=date(2026, 9, 10),
        previous_balance=Decimal("94.00"),
        sections={
            "Repayment/Conversion": [
                MariRow(date(2026, 8, 12), Decimal("150.40"), "Split Payment",
                        ("Conversion",), date(2026, 8, 12)),
            ],
            "Purchase": [
                MariRow(date(2026, 8, 3), Decimal("-150.40"), "MERCHANT GAMMA SYNTHETIC",
                        ("Card Payment",), date(2026, 8, 2)),
            ],
            "Split Payment": [
                MariRow(date(2026, 8, 21), Decimal("-15.00"), "Split Payment 6M",
                        ("Split Payment 4/6 Instalment",), date(2026, 5, 10)),
                MariRow(date(2026, 8, 21), Decimal("-25.07"), "Split Payment 6M",
                        ("Split Payment 1/6 Instalment",), date(2026, 8, 12)),
            ],
        },
    )
