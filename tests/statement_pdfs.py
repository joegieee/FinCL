"""Runtime statement synthesis for DBS, PayLah!, MariBank savings and Trust.

The layouts follow ``fixtures/generate_pdf_fixtures.py``, closely enough for the
parsers in ``fincl.documents`` to read them back, with the rows as parameters.
Balances and printed totals are computed from the rows, so a synthesized statement
always reconciles. See ``revolut_pdf`` and ``mari_card_pdf`` for the other two.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

_ZERO = Decimal("0.00")
_PAGE_TOP = 790.0
_PAGE_BOTTOM = 70.0


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _short(value: date) -> str:
    return f"{value.day:02d} {value:%b %Y}"


def _upper(value: date) -> str:
    return f"{value:%d %b %Y}".upper()


def _canvas(buffer: io.BytesIO) -> Canvas:
    canvas = Canvas(buffer, pagesize=A4, pageCompression=0, invariant=1)
    canvas.setFont("Helvetica", 9)
    return canvas


def _new_page(canvas: Canvas) -> None:
    canvas.showPage()
    canvas.setFont("Helvetica", 9)


def _in_out(rows: Sequence[object]) -> tuple[Decimal, Decimal]:
    amounts = [row.amount for row in rows]  # type: ignore[attr-defined]
    return (
        sum((amount for amount in amounts if amount > 0), _ZERO),
        sum((-amount for amount in amounts if amount < 0), _ZERO),
    )


@dataclass(frozen=True, slots=True)
class Row:
    """One printed line. ``amount`` is signed: credits positive."""

    on: date
    description: str
    amount: Decimal
    detail: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DbsPocket:
    heading: str
    opening: Decimal
    rows: tuple[Row, ...]


_DBS_WITHDRAWAL_R, _DBS_DEPOSIT_R, _DBS_BALANCE_R = 402, 475, 551


def _dbs_header(canvas: Canvas, y: float) -> None:
    for x, label in ((40, "Date"), (105, "Description"), (325, "Withdrawal"), (389, "(-)"),
                     (420, "Deposit"), (462, "(+)"), (510, "Balance")):
        canvas.drawString(x, y, label)


def dbs_pdf_bytes(statement_date: date, pockets: Sequence[DbsPocket]) -> bytes:
    """A consolidated statement. A section that runs out of page carries its balance over."""
    buffer = io.BytesIO()
    canvas = _canvas(buffer)
    canvas.drawString(40, 805, f"Account Summary as at {_short(statement_date)}")
    canvas.drawString(40, 790, f"Transaction Details as at {_short(statement_date)}")
    y = 770.0

    for pocket in pockets:
        if y < _PAGE_BOTTOM + 120:
            _new_page(canvas)
            y = _PAGE_TOP
            canvas.drawString(40, y, f"Transaction Details as at {_short(statement_date)}")
            y -= 18
        canvas.drawString(40, y, f"CURRENCY: {pocket.heading}")
        y -= 18
        _dbs_header(canvas, y)
        y -= 18
        balance = pocket.opening
        canvas.drawString(105, y, "Balance Brought Forward")
        canvas.drawRightString(_DBS_BALANCE_R, y, _money(balance))
        y -= 18
        for row in pocket.rows:
            if y - 15 * len(row.detail) < _PAGE_BOTTOM:
                canvas.drawString(105, y, "Balance Carried Forward")
                canvas.drawRightString(_DBS_BALANCE_R, y, _money(balance))
                _new_page(canvas)
                y = _PAGE_TOP
                canvas.drawString(40, y, f"Transaction Details as at {_short(statement_date)}")
                y -= 18
                _dbs_header(canvas, y)
                y -= 18
                canvas.drawString(105, y, "Balance Brought Forward")
                canvas.drawRightString(_DBS_BALANCE_R, y, _money(balance))
                y -= 18
            balance += row.amount
            canvas.drawString(40, y, f"{row.on:%d/%m/%Y}")
            canvas.drawString(105, y, row.description)
            column = _DBS_WITHDRAWAL_R if row.amount < 0 else _DBS_DEPOSIT_R
            canvas.drawRightString(column, y, _money(abs(row.amount)))
            canvas.drawRightString(_DBS_BALANCE_R, y, _money(balance))
            y -= 15
            for text in row.detail:
                canvas.drawString(115, y, text)
                y -= 13
            y -= 4
        money_in, money_out = _in_out(pocket.rows)
        canvas.drawString(105, y, "Total Balance Carried Forward")
        canvas.drawRightString(_DBS_WITHDRAWAL_R, y, _money(money_out))
        canvas.drawRightString(_DBS_DEPOSIT_R, y, _money(money_in))
        canvas.drawRightString(_DBS_BALANCE_R, y, _money(balance))
        y -= 28
    canvas.save()
    return buffer.getvalue()


def paylah_pdf_bytes(statement_date: date, opening: Decimal, rows: Sequence[Row]) -> bytes:
    buffer = io.BytesIO()
    canvas = _canvas(buffer)
    canvas.drawString(40, 805, f"Statement of Account {_short(statement_date)}")
    canvas.drawString(40, 780, "PREVIOUS BALANCE")
    canvas.drawRightString(550, 780, _money(opening))
    y = 750.0
    for index, row in enumerate(rows, start=1):
        if y < _PAGE_BOTTOM:
            _new_page(canvas)
            y = _PAGE_TOP
        marker = "CR" if row.amount > 0 else "DB"
        canvas.drawString(40, y, f"{row.on:%d %b} {row.description} {_money(abs(row.amount))} {marker}")
        canvas.drawString(110, y - 15, f"REF NO: WALLET-{statement_date:%y%m}-{index:04d}")
        y -= 33
    money_in, money_out = _in_out(rows)
    canvas.drawString(40, y, "Total :")
    canvas.drawRightString(550, y, _money(opening + money_in - money_out))
    canvas.save()
    return buffer.getvalue()


@dataclass(frozen=True, slots=True)
class SavingsRow:
    """``above`` wraps over the dated line; ``detail`` runs under it. ``on`` None prints month-only."""

    on: date | None
    above: str
    amount: Decimal
    detail: tuple[str, ...] = ()


def mari_savings_pdf_bytes(
    period_start: date, period_end: date, statement_date: date, opening: Decimal,
    rows: Sequence[SavingsRow], interest: Decimal,
) -> bytes:
    """``rows`` excludes the credited-interest line, which is printed with month precision."""
    money_in, money_out = _in_out(rows)
    money_in += interest
    closing = opening + money_in - money_out

    buffer = io.BytesIO()
    canvas = _canvas(buffer)
    canvas.drawString(490, 800, _upper(statement_date))
    canvas.drawString(40, 780, f"STATEMENT PERIOD: {_upper(period_start)} to {_upper(period_end)}")
    canvas.drawString(195, 760, "SAVINGS - ACCOUNT SUMMARY")
    canvas.drawString(244, 745, "ACCOUNT: 111 222 333")
    canvas.drawString(48, 728, "ACCOUNT STARTING BALANCE (SGD) TOTAL OUTGOING (SGD) "
                               "TOTAL INCOMING (SGD) ENDING BALANCE (SGD)")
    canvas.drawString(48, 712, "SAVINGS")
    for right, value in ((222, opening), (331, money_out), (439, money_in), (545, closing)):
        canvas.drawRightString(right, 712, _money(value))
    canvas.drawString(490, 696, "TOTAL:")
    canvas.drawRightString(545, 696, _money(closing))

    canvas.drawString(187, 668, "SAVINGS - TRANSACTION DETAILS")
    canvas.drawString(244, 653, "ACCOUNT: 111 222 333")
    canvas.drawString(48, 636, "DATE TRANSACTION OUTGOING (SGD) INCOMING (SGD)")
    y = 616.0

    def _row(row: SavingsRow) -> None:
        nonlocal y
        if y < _PAGE_BOTTOM + 40:
            _new_page(canvas)
            y = _PAGE_TOP
        canvas.drawString(174, y, row.above)
        y -= 12
        canvas.drawString(48, y, f"{row.on:%d %b}".upper() if row.on else f"{period_end:%b}".upper())
        canvas.drawRightString(545 if row.amount > 0 else 420, y, _money(abs(row.amount)))
        for text in row.detail:
            y -= 12
            canvas.drawString(174, y, text)
        y -= 22

    if interest > 0:
        _row(SavingsRow(None, "Interest", interest, ("Interest",)))
    for row in rows:
        _row(row)

    if y < _PAGE_BOTTOM + 150:
        _new_page(canvas)
        y = _PAGE_TOP
    canvas.drawString(200, y, "SAVINGS - INTEREST DETAILS*")
    canvas.drawString(244, y - 15, "ACCOUNT: 111 222 333")
    y -= 32
    canvas.drawString(48, y, "DATE PREVIOUS DAY BALANCE (SGD) INTEREST (SGD)")
    y -= 17
    canvas.drawString(48, y, f"{period_start:%d %b}".upper())
    canvas.drawRightString(293, y, _money(opening))
    canvas.drawRightString(545, y, _money(interest))
    y -= 19
    canvas.drawString(48, y, "TOTAL INTEREST CREDITED:")
    canvas.drawRightString(545, y, _money(interest))
    y -= 30

    if y < _PAGE_BOTTOM + 80:
        _new_page(canvas)
        y = _PAGE_TOP
    canvas.drawString(177, y, "INVESTMENTS - ACCOUNT SUMMARY")
    canvas.drawString(244, y - 18, "NO HOLDINGS")
    canvas.drawString(177, y - 38, "INVESTMENTS - TRANSACTION DETAILS")
    canvas.drawString(244, y - 56, "NO RECENT TRANSACTION")
    canvas.save()
    return buffer.getvalue()


@dataclass(frozen=True, slots=True)
class TrustRow:
    """``amount`` is the SGD posting. ``foreign`` is ``(amount, code, rate)`` when it paid abroad."""

    on: date
    description: str
    amount: Decimal
    foreign: tuple[Decimal, str, str] | None = None


_TRUST_SGD_R = 543


def trust_pdf_bytes(
    period_start: date, period_end: date, statement_date: date, opening: Decimal, rows: Sequence[TrustRow],
) -> bytes:
    """Interest is an ordinary row: ``+`` marks a credit, as the statement prints it."""
    interest = sum((row.amount for row in rows if row.description == "Interest"), _ZERO)
    money_in, money_out = _in_out([row for row in rows if row.description != "Interest"])
    closing = opening + money_in - money_out + interest

    buffer = io.BytesIO()
    canvas = _canvas(buffer)
    canvas.drawString(53, 800, "ALEX SYNTHETIC TAN")
    canvas.drawString(300, 800, "Account Statement 01-2345678-9")
    canvas.drawString(53, 782, f"Statement period {period_start.day} {period_start:%b %Y} - "
                               f"{period_end.day} {period_end:%b %Y}")
    canvas.drawString(53, 764, f"Statement date {statement_date.day} {statement_date:%b %Y}")
    canvas.drawString(53, 740, "ACTIVITY SUMMARY")

    width = canvas.stringWidth
    pen = 145.0
    rights: dict[str, float] = {}
    for token, key in (("Opening", None), ("balance", "opening"), ("+", None), ("Total", None),
                       ("incoming", "incoming"), ("-", None), ("Total", None), ("outgoing", "outgoing"),
                       ("+", None), ("Interest*", "interest"), ("=", None), ("Closing", None),
                       ("balance", "closing")):
        canvas.drawString(pen, 722, token)
        pen += width(token, "Helvetica", 9)
        if key is not None:
            rights[key] = pen
        pen += 7.0
    canvas.drawString(53, 722, "Savings Account")

    def _summary(y: float, label: str, values: dict[str, Decimal]) -> None:
        canvas.drawString(53, y, label)
        for key, value in values.items():
            canvas.drawRightString(rights[key], y, f"S${value:,.2f}")

    main = {"opening": opening, "incoming": money_in, "outgoing": money_out,
            "interest": interest, "closing": closing}
    _summary(704, "Main Account", main)
    _summary(688, "Savings", {"opening": _ZERO, "incoming": _ZERO, "outgoing": _ZERO, "closing": _ZERO})
    _summary(672, "Total", main)
    canvas.drawString(53, 656, "*All interest earned will be credited to your Main Account.")

    canvas.drawString(53, 596, "TRANSACTION DETAILS")
    canvas.drawString(53, 578, "Main Account")
    canvas.drawString(53, 562, "Posting date Description Amount in FCY Amount in SGD")

    def _line(y: float, on: date, text: str, amount: str) -> None:
        canvas.drawString(53, y, f"{on.day} {on:%b} {text}")
        canvas.drawRightString(_TRUST_SGD_R, y, amount)

    y = 544.0
    _line(y, period_start, "Previous balance", _money(opening))
    y -= 18
    for row in rows:
        _line(y, row.on, row.description, ("+" if row.amount > 0 else "") + _money(abs(row.amount)))
        if row.foreign is not None:
            amount, code, rate = row.foreign
            canvas.drawRightString(367 + width(_money(amount), "Helvetica", 9), y, _money(amount))
            canvas.drawString(405, y, code)
            y -= 14
            canvas.drawString(152, y, f"1 {code} = {rate} SGD TRUST FX CHARGE = 0%")
        y -= 18
    _line(y, period_end, "Closing balance", _money(closing))
    y -= 24
    canvas.drawString(53, y, "Savings")
    canvas.drawString(53, y - 16, "Posting date Description Amount in FCY Amount in SGD")
    _line(y - 34, period_start, "Previous balance", _money(_ZERO))
    _line(y - 52, period_end, "Closing balance", _money(_ZERO))
    canvas.save()
    return buffer.getvalue()
