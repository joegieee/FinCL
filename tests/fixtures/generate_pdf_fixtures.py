"""Generate privacy-safe PDF parser fixtures with invented financial data."""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))  # tests/ — for `import revolut_pdf`

# Curated Revolut rows (CSV column order) exercising both currencies, every
# continuation subline, a fee, an exchange pair, and one Pending + one Reverted
# row. The "unbalanced" twin nudges a single printed balance.
_REVOLUT_FIXTURE_ROWS = [
    ["Topup", "Current", "2026-01-02 09:00:00", "2026-01-02 09:00:01", "ACME CLIENT LTD", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
    ["Card Payment", "Current", "2026-01-03 10:00:00", "2026-01-03 10:00:01", "Acme Supermarket", "-12.00", "0.00", "SGD", "COMPLETED", "488.00"],
    ["Card Payment", "Current", "2026-01-05 11:00:00", "2026-01-05 11:00:01", "Grab", "-9.60", "0.10", "SGD", "COMPLETED", "478.30"],
    ["Exchange", "Current", "2026-01-08 12:00:00", "2026-01-08 12:00:01", "SGD to USD", "-100.00", "0.00", "SGD", "COMPLETED", "378.30"],
    ["Card Refund", "Current", "2026-01-10 13:00:00", "2026-01-10 13:00:01", "Acme Supermarket", "12.00", "0.00", "SGD", "COMPLETED", "390.30"],
    ["Card Payment", "Current", "2026-01-12 14:00:00", "", "Pending Cafe", "-4.00", "0.00", "SGD", "PENDING", ""],
    ["Card Payment", "Current", "2026-01-11 14:00:00", "", "Reverted Shop", "-7.00", "0.00", "SGD", "REVERTED", ""],
    ["Exchange", "Current", "2026-01-08 12:00:00", "2026-01-08 12:00:01", "SGD to USD", "75.00", "0.00", "USD", "COMPLETED", "75.00"],
    ["Card Payment", "Current", "2026-01-15 15:00:00", "2026-01-15 15:00:01", "Steam", "-20.00", "0.00", "USD", "COMPLETED", "55.00"],
]


def generate_revolut() -> None:
    from revolut_pdf import revolut_pdf_bytes

    (ROOT / "revolut_statement.pdf").write_bytes(revolut_pdf_bytes(_REVOLUT_FIXTURE_ROWS))


def generate_revolut_unbalanced() -> None:
    from revolut_pdf import revolut_pdf_bytes

    rows = [list(row) for row in _REVOLUT_FIXTURE_ROWS]
    rows[1][9] = "487.00"  # nudge one printed SGD balance by a dollar
    (ROOT / "revolut_statement_unbalanced.pdf").write_bytes(revolut_pdf_bytes(rows))


def _text(canvas: Canvas, x: float, y: float, value: str) -> None:
    canvas.drawString(x, y, value)


def _dbs_header(canvas: Canvas, y: float) -> tuple[float, float, float]:
    _text(canvas, 40, y, "Date")
    _text(canvas, 105, y, "Description")
    _text(canvas, 325, y, "Withdrawal")
    _text(canvas, 389, y, "(-)")
    _text(canvas, 420, y, "Deposit")
    _text(canvas, 462, y, "(+)")
    _text(canvas, 510, y, "Balance")
    return (402, 475, 551)


def _money(canvas: Canvas, right: float, y: float, value: str) -> None:
    canvas.drawRightString(right, y, value)


def generate_dbs() -> None:
    path = ROOT / "dbs_multicurrency.pdf"
    canvas = Canvas(str(path), pagesize=A4, pageCompression=0)
    canvas.setFont("Helvetica", 9)
    _text(canvas, 40, 805, "Account Summary as at 31 Jul 2026")
    _text(canvas, 40, 785, "Transaction Details as at 31 Jul 2026")
    _text(canvas, 40, 765, "CURRENCY: SINGAPORE DOLLAR")
    withdrawal, deposit, balance = _dbs_header(canvas, 745)
    _text(canvas, 105, 725, "Balance Brought Forward")
    _money(canvas, balance, 725, "100.00")
    _text(canvas, 40, 705, "01/07/2026")
    _text(canvas, 105, 705, "SYNTHETIC PURCHASE")
    _money(canvas, withdrawal, 705, "10.00")
    _money(canvas, balance, 705, "90.00")
    _text(canvas, 115, 690, "REF TEST-SGD-0001")
    _text(canvas, 40, 670, "02/07/2026")
    _text(canvas, 105, 670, "SYNTHETIC CREDIT")
    _money(canvas, deposit, 670, "5.00")
    _money(canvas, balance, 670, "95.00")
    _text(canvas, 105, 650, "Balance Carried Forward")
    _money(canvas, balance, 650, "95.00")
    _text(canvas, 105, 630, "Total Balance Carried Forward")
    _money(canvas, withdrawal, 630, "10.00")
    _money(canvas, deposit, 630, "5.00")
    _money(canvas, balance, 630, "95.00")

    _text(canvas, 40, 600, "CURRENCY: UNITED STATES DOLLAR")
    withdrawal, deposit, balance = _dbs_header(canvas, 580)
    _text(canvas, 105, 560, "Balance Brought Forward")
    _money(canvas, balance, 560, "50.00")
    _text(canvas, 40, 540, "03/07/2026")
    _text(canvas, 105, 540, "SYNTHETIC USD PURCHASE")
    _money(canvas, withdrawal, 540, "2.00")
    _money(canvas, balance, 540, "48.00")
    _text(canvas, 105, 520, "Total Balance Carried Forward")
    _money(canvas, withdrawal, 520, "2.00")
    _money(canvas, deposit, 520, "0.00")
    _money(canvas, balance, 520, "48.00")

    canvas.saveState()
    canvas.translate(580, 300)
    canvas.rotate(90)
    _text(canvas, 0, 0, "SYNTHETIC ROTATED MARGINALIA")
    canvas.restoreState()
    canvas.save()


def generate_paylah() -> None:
    path = ROOT / "paylah_legacy.pdf"
    canvas = Canvas(str(path), pagesize=A4, pageCompression=0)
    canvas.setFont("Helvetica", 9)
    _text(canvas, 40, 805, "Statement of Account 08 Aug 2026")
    _text(canvas, 40, 780, "PREVIOUS BALANCE")
    _money(canvas, 550, 780, "0.00")
    _text(canvas, 40, 750, "01 Aug TOP UP WALLET FROM MY ACCOUNT 10.00 CR")
    _text(canvas, 110, 735, "REF NO: TEST-PAYLAH-0001")
    _text(canvas, 40, 710, "01 Aug SYNTHETIC WALLET PAYMENT 10.00 DB")
    _text(canvas, 110, 695, "REF NO: TEST-PAYLAH-0002")
    _text(canvas, 40, 665, "Total :")
    _money(canvas, 550, 665, "0.00")
    canvas.save()


def _mari_savings_body(canvas: Canvas, *, interest_header: str) -> None:
    canvas.setFont("Helvetica", 9)
    _text(canvas, 490, 800, "05 AUG 2026")
    _text(canvas, 40, 780, "STATEMENT PERIOD: 05 JUL 2026 to 31 JUL 2026")
    _text(canvas, 195, 760, "SAVINGS - ACCOUNT SUMMARY")
    _text(canvas, 244, 745, "ACCOUNT: 111 222 333")
    _text(
        canvas,
        48,
        728,
        "ACCOUNT STARTING BALANCE (SGD) TOTAL OUTGOING (SGD) "
        "TOTAL INCOMING (SGD) ENDING BALANCE (SGD)",
    )
    _text(canvas, 48, 712, "SAVINGS")
    _money(canvas, 222, 712, "0.00")
    _money(canvas, 331, 712, "30.00")
    _money(canvas, 439, 712, "100.55")
    _money(canvas, 545, 712, "70.55")
    _text(canvas, 490, 696, "TOTAL:")
    _money(canvas, 545, 696, "70.55")

    _text(canvas, 187, 668, "SAVINGS - TRANSACTION DETAILS")
    _text(canvas, 244, 653, "ACCOUNT: 111 222 333")
    _text(canvas, 48, 636, "DATE TRANSACTION OUTGOING (SGD) INCOMING (SGD)")
    # Credited-interest row: month-only date, printed with month precision.
    _text(canvas, 174, 616, "Interest")
    _text(canvas, 48, 604, "JUL")
    _money(canvas, 545, 604, "0.55")
    _text(canvas, 174, 592, "Interest")
    # Incoming PayNow transfer, description wraps above and below the amount line.
    _text(canvas, 174, 570, "SENDER NAME ONE")
    _text(canvas, 48, 558, "07 JUL")
    _text(canvas, 174, 558, "PayNow Transfer")
    _money(canvas, 545, 558, "100.00")
    _text(canvas, 174, 546, "REF ABC123")
    # Outgoing card payment.
    _text(canvas, 174, 524, "COFFEE MERCHANT SYNTHETIC")
    _text(canvas, 48, 512, "12 JUL")
    _money(canvas, 420, 512, "30.00")
    _text(canvas, 174, 500, "Card Payment")

    _text(canvas, 200, 470, "SAVINGS - INTEREST DETAILS*")
    _text(canvas, 244, 455, "ACCOUNT: 111 222 333")
    _text(canvas, 48, 438, interest_header)
    _text(canvas, 48, 421, "05 JUL")
    _money(canvas, 293, 421, "0.00")
    _money(canvas, 545, 421, "0.20")
    _text(canvas, 48, 404, "06 JUL")
    _money(canvas, 293, 404, "100.00")
    _money(canvas, 545, 404, "0.20")
    _text(canvas, 48, 387, "07 JUL")
    _money(canvas, 293, 387, "100.20")
    _money(canvas, 545, 387, "0.15")
    _text(canvas, 48, 368, "TOTAL INTEREST CREDITED:")
    _money(canvas, 545, 368, "0.55")

    _text(canvas, 177, 338, "INVESTMENTS - ACCOUNT SUMMARY")
    _text(canvas, 244, 320, "NO HOLDINGS")
    _text(canvas, 177, 300, "INVESTMENTS - TRANSACTION DETAILS")
    _text(canvas, 244, 282, "NO RECENT TRANSACTION")


def generate_maribank_savings() -> None:
    canvas = Canvas(str(ROOT / "maribank_savings.pdf"), pagesize=A4, pageCompression=0)
    _mari_savings_body(
        canvas, interest_header="DATE PREVIOUS DAY BALANCE (SGD) INTEREST (SGD)"
    )
    canvas.save()


def generate_maribank_savings_unknown_profile() -> None:
    canvas = Canvas(
        str(ROOT / "maribank_savings_unknown_profile.pdf"), pagesize=A4, pageCompression=0
    )
    _mari_savings_body(
        canvas,
        interest_header="DATE PREVIOUS DAY BALANCE (SGD) TIER INTEREST RATE INTEREST (SGD)",
    )
    canvas.save()


def _mari_card_body(canvas: Canvas, *, equation: str) -> None:
    canvas.setFont("Helvetica", 9)
    _text(canvas, 40, 780, "STATEMENT PERIOD: 21 JUN 2026 to 20 JUL 2026")
    _text(canvas, 230, 760, "ACCOUNT SUMMARY")
    _text(canvas, 48, 728, "ACCOUNT STATEMENT DATE CREDIT LIMIT STATEMENT DUE MINIMUM PAYMENT DUE DUE DATE")
    _text(canvas, 48, 712, "MARI CREDIT CARD 21 JUL 2026 500.00 94.00 30.00 10 AUG 2026")

    _text(canvas, 222, 686, "STATEMENT SUMMARY")
    letters = ["(A)", "(B)", "(C)", "(D)", "(E)", "(F)", "(G)", "(H)", "(I)"]
    values = ["100.00", "50.00", "0.00", "30.00", "0.00", "1.00", "0.00", "15.00", "0.00"]
    columns = [48, 110, 175, 235, 292, 350, 405, 458, 512]
    # Separate words at spread x; pdfplumber still joins them to "(A) (B) ...".
    for column, letter in zip(columns, letters, strict=True):
        _text(canvas, column, 664, letter)
    for column, value in zip(columns, values, strict=True):
        _money(canvas, column + 24, 648, value)
    _text(canvas, 48, 628, f"CURRENT OUTSTANDING ({equation}) 94.00")

    _text(canvas, 222, 598, "TRANSACTION DETAILS")
    _text(canvas, 48, 582, "POSTED DATE TRANSACTION DATE DESCRIPTION AMOUNT (SGD)")

    _text(canvas, 249, 560, "Repayment/Conversion")
    _text(canvas, 302, 544, "SENDER NAME SYNTHETIC")
    _text(canvas, 48, 532, "05 JUL")
    _text(canvas, 174, 532, "05 JUL")
    _money(canvas, 545, 532, "+50.00")
    _text(canvas, 302, 520, "Repayment")

    _text(canvas, 278, 496, "Purchase")
    _text(canvas, 302, 480, "MERCHANT ALPHA SYNTHETIC")
    _text(canvas, 48, 468, "06 JUL")
    _money(canvas, 545, 468, "-20.00")
    _text(canvas, 302, 456, "Card Payment")
    _text(canvas, 302, 432, "MERCHANT BETA SYNTHETIC")
    _text(canvas, 48, 420, "07 JUL")
    _text(canvas, 174, 420, "06 JUL")
    _money(canvas, 545, 420, "-10.00")
    _text(canvas, 302, 408, "USD 7.50")

    _text(canvas, 277, 384, "Cashback")
    _text(canvas, 302, 368, "Cashback Credit")
    _text(canvas, 48, 356, "21 JUL")
    _text(canvas, 174, 356, "21 JUL")
    _money(canvas, 545, 356, "+1.00")
    _text(canvas, 302, 344, "Cashback")

    _text(canvas, 268, 320, "Split Payment")
    _text(canvas, 302, 304, "Split Payment 6M")
    _text(canvas, 48, 292, "21 JUL")
    _text(canvas, 174, 292, "10 MAY")
    _money(canvas, 545, 292, "-15.00")
    _text(canvas, 302, 280, "Split Payment 3/6 Instalment")

    _text(canvas, 48, 250, "General")
    _text(canvas, 48, 234, "This is a system-generated e-Statement.")


def generate_mari_card() -> None:
    canvas = Canvas(str(ROOT / "mari_card.pdf"), pagesize=A4, pageCompression=0)
    _mari_card_body(canvas, equation="A-B-C+D+E-F+G+H-I")
    canvas.save()


def generate_mari_card_unknown_equation() -> None:
    canvas = Canvas(str(ROOT / "mari_card_unknown_equation.pdf"), pagesize=A4, pageCompression=0)
    _mari_card_body(canvas, equation="A+B+C+D")
    canvas.save()


def _trust_savings_body(canvas: Canvas, *, detail_closing: str) -> None:
    """One-page synthetic Trust Bank Singapore savings statement.

    ``detail_closing`` is the Main Account "Closing balance" figure printed in
    the transaction detail. The activity is opening 10.00 + 50.00 incoming
    - 8.20 outgoing (5.00 EUR at 1.6400) + 0.01 interest = 51.81. Passing
    anything other than ``"51.81"`` breaks the per-sub-account balance walk
    without touching the self-consistent activity summary, which is the
    profile's blocking refusal path.
    """
    canvas.setFont("Helvetica", 9)
    _text(canvas, 53, 800, "ALEX SYNTHETIC TAN")
    _text(canvas, 300, 800, "Account Statement 01-2345678-9")
    _text(canvas, 53, 782, "Statement period 1 Aug 2026 - 31 Aug 2026")
    _text(canvas, 53, 764, "Statement date 1 Sep 2026")

    _text(canvas, 53, 740, "ACTIVITY SUMMARY")

    # Activity summary header. Tokens are laid out left to right with a wide gap
    # so pdfplumber never merges adjacent words: the parser needs the literal
    # substrings "Opening balance", "Total incoming", and "Closing balance", and
    # derives each column's right edge from these word extents. Row values below
    # are right-aligned to the recorded edges, so column matching is exact.
    width = canvas.stringWidth
    header_y = 722
    pen = 145.0
    column_right: dict[str, float] = {}
    for token, key in (
        ("Opening", None),
        ("balance", "opening"),
        ("+", None),
        ("Total", None),
        ("incoming", "incoming"),
        ("-", None),
        ("Total", None),
        ("outgoing", "outgoing"),
        ("+", None),
        ("Interest*", "interest"),
        ("=", None),
        ("Closing", None),
        ("balance", "closing"),
    ):
        _text(canvas, pen, header_y, token)
        pen += width(token, "Helvetica", 9)
        if key is not None:
            column_right[key] = pen
        pen += 7.0
    _text(canvas, 53, header_y, "Savings Account")
    opening_r = column_right["opening"]
    incoming_r = column_right["incoming"]
    outgoing_r = column_right["outgoing"]
    interest_r = column_right["interest"]
    closing_r = column_right["closing"]

    def _summary_row(y: float, label: str, values: dict[float, str]) -> None:
        _text(canvas, 53, y, label)
        for right, value in values.items():
            _money(canvas, right, y, value)

    _summary_row(
        704,
        "Main Account",
        {
            opening_r: "S$10.00",
            incoming_r: "S$50.00",
            outgoing_r: "S$8.20",
            interest_r: "S$0.01",
            closing_r: "S$51.81",
        },
    )
    _summary_row(
        688,
        "Savings",
        {opening_r: "S$0.00", incoming_r: "S$0.00", outgoing_r: "S$0.00", closing_r: "S$0.00"},
    )
    _summary_row(
        672,
        "Total",
        {
            opening_r: "S$10.00",
            incoming_r: "S$50.00",
            outgoing_r: "S$8.20",
            interest_r: "S$0.01",
            closing_r: "S$51.81",
        },
    )
    _text(canvas, 53, 656, "*All interest earned will be credited to your Main Account.")

    _text(canvas, 53, 632, "IMPORTANT NOTICE")
    _text(canvas, 53, 618, "DEPOSIT INSURANCE SCHEME: insured deposits per the Singapore law.")

    sgd_r = 543
    fcy_amount_x = 367
    fcy_code_x = 405

    _text(canvas, 53, 596, "TRANSACTION DETAILS")
    _text(canvas, 53, 578, "Main Account")
    _text(canvas, 53, 562, "Posting date Description Amount in FCY Amount in SGD")
    _text(canvas, 53, 544, "1 Aug Previous balance")
    _money(canvas, sgd_r, 544, "10.00")
    _text(canvas, 53, 526, "1 Aug TAN SYNTHETIC NAME")
    _money(canvas, sgd_r, 526, "+50.00")
    _text(canvas, 53, 508, "9 Aug SYNTHETIC MERCHANT EU")
    _money(canvas, fcy_amount_x + width("5.00", "Helvetica", 9), 508, "5.00")
    _text(canvas, fcy_code_x, 508, "EUR")
    _money(canvas, sgd_r, 508, "8.20")
    _text(canvas, 152, 494, "1 EUR = 1.6400 SGD TRUST FX CHARGE = 0%")
    _text(canvas, 53, 476, "31 Aug Interest")
    _money(canvas, sgd_r, 476, "+0.01")
    _text(canvas, 53, 458, "31 Aug Closing balance")
    _money(canvas, sgd_r, 458, detail_closing)

    _text(canvas, 53, 434, "Savings")
    _text(canvas, 53, 418, "Posting date Description Amount in FCY Amount in SGD")
    _text(canvas, 53, 400, "1 Aug Previous balance")
    _money(canvas, sgd_r, 400, "0.00")
    _text(canvas, 53, 382, "31 Aug Closing balance")
    _money(canvas, sgd_r, 382, "0.00")


def generate_trust_savings() -> None:
    canvas = Canvas(str(ROOT / "trust_savings.pdf"), pagesize=A4, pageCompression=0)
    _trust_savings_body(canvas, detail_closing="51.81")
    canvas.save()


def generate_trust_savings_unbalanced() -> None:
    canvas = Canvas(str(ROOT / "trust_savings_unbalanced.pdf"), pagesize=A4, pageCompression=0)
    _trust_savings_body(canvas, detail_closing="99.99")
    canvas.save()


def generate_unclassified() -> None:
    path = ROOT / "unclassified.pdf"
    canvas = Canvas(str(path), pagesize=A4, pageCompression=0)
    canvas.setFont("Helvetica", 9)
    _text(canvas, 40, 805, "Synthetic page with no recognized statement regions")
    canvas.save()


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    generate_dbs()
    generate_paylah()
    generate_maribank_savings()
    generate_maribank_savings_unknown_profile()
    generate_mari_card()
    generate_mari_card_unknown_equation()
    generate_trust_savings()
    generate_trust_savings_unbalanced()
    generate_revolut()
    generate_revolut_unbalanced()
    generate_unclassified()
