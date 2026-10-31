"""Create an isolated, entirely synthetic ledger for development. Never touches var/.

Every merchant, person, amount and date below is invented. The point is the shape
of a lived-in ledger, not anyone's life: six accounts on different statement cycles
that started at different times, money moving between them so every transfer linker
has work, a card with installment plans, small frequent spending, and a job half
done, with most entries unclassified and some debts and plans missing their dates.
The answer's to-dos are left open on purpose, so the flows that fix them can be run.

Statements are rendered to PDF and imported through the real pipeline, and decisions
go through the journal, so reconciliation, evidence, replay and `rebuild` all behave
as they do on a real ledger. The same ``through`` always builds the same ledger.
"""
import random
import sys
import uuid
from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fincl import clock
from fincl.categorization import rule_id_for
from fincl.db import connect_sqlite
from fincl.decision_journal import append_decision, default_journal_path
from fincl.document_service import DocumentImportService
from fincl.fx import load_fx_rates
from fincl.refresh import refresh_ledger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from mari_card_pdf import (  # noqa: E402
    MariRow,
    MariStatement,
    mari_card_pdf_bytes,
    statement_outstanding,
)
from revolut_pdf import revolut_pdf_bytes  # noqa: E402
from statement_pdfs import (  # noqa: E402
    DbsPocket,
    Row,
    SavingsRow,
    TrustRow,
    dbs_pdf_bytes,
    mari_savings_pdf_bytes,
    paylah_pdf_bytes,
    trust_pdf_bytes,
)

SEED = 20260301
MONTHS = 8
POLICY = "demo-v2"
SYNTHETIC_MARKER = "SYNTHETIC"  # scripts/dev.py serves only a ledger that carries this
_ID_NAMESPACE = uuid.UUID("7f1c7e62-5b1e-4d7c-9a56-0d1f2b3c4e5f")

DBS_MERCHANTS = ("Corner Grocer", "Fresh Market", "Lunch Counter", "Neighbourhood Bakery",
                 "Pharmacy Plus", "Transit Top-up", "Book Nook", "Hardware Hut")
WALLET_MERCHANTS = ("HAWKER STALL 12", "KOPI CORNER", "NOODLE HOUSE", "BUBBLE TEA BAR", "CHICKEN RICE STALL")
SGD_CARD_MERCHANTS = ("Ride-hail", "Streaming Box", "Night Market Grill", "Campus Cafe")
ABROAD_MERCHANTS = ("Bistro du Port", "Metro Lisboa", "Museu Pass", "Tram 28 Kiosk", "Pastelaria Sol",
                    "Sushi Ten", "Konbini Mart", "City Hostel")
CARD_MERCHANTS = ("Electro World", "Home Goods Co", "Outdoor Supply", "Pet Pantry", "Garden Centre")
LONG_PLAN_AGE = timedelta(days=180)


def _months_back(end: date, count: int) -> list[date]:
    months, year, month = [], end.year, end.month
    for _ in range(count):
        months.append(date(year, month, 1))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return months[::-1]


def _month_end(first: date) -> date:
    return first.replace(day=monthrange(first.year, first.month)[1])


def _next_month(first: date) -> date:
    return _month_end(first) + timedelta(days=1)


def _money(rng: random.Random, low: float, high: float) -> Decimal:
    return Decimal(str(round(rng.uniform(low, high), 2))).quantize(Decimal("0.01"))


def _paynow_token(rng: random.Random) -> str:
    return "".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=24)) + "7A"


def default_through(today: date) -> date:
    """The last month-end old enough to leave the evidence behind, as a lived-in ledger's is."""
    last_complete = today.replace(day=1) - timedelta(days=1)
    return last_complete.replace(day=1) - timedelta(days=1)


@dataclass(slots=True)
class Books:
    """Each account's rows for the whole window, before they are cut into statements."""

    through: date
    months: list[date]
    rng: random.Random
    revolut_opened: date
    savings_opened: date
    card_opened: date
    dbs: list[Row] = field(default_factory=list)
    wallet: list[Row] = field(default_factory=list)
    savings: list[SavingsRow] = field(default_factory=list)
    trust: list[TrustRow] = field(default_factory=list)
    revolut: list[tuple[date, str, str, Decimal, Decimal, str, str, str | None]] = field(default_factory=list)
    card: list[MariStatement] = field(default_factory=list)
    savings_closing: Decimal = Decimal("0.00")

    def in_window(self, on: date) -> bool:
        return on <= self.through


def _spending(books: Books) -> None:
    rng = books.rng
    for index, month in enumerate(books.months):
        last = _month_end(month).day
        books.dbs.append(Row(month.replace(day=2), "PayNow Transfer", Decimal("-1180.00"),
                             ("TO: LANDLORD SYNTHETIC",)))
        books.dbs.append(Row(month.replace(day=11), "Bill Payment", -_money(rng, 62, 118),
                             ("SYNTHETIC POWER AND WATER",)))
        for _ in range(9):
            charged = month.replace(day=rng.randint(1, last - 3))
            posted = charged + timedelta(days=rng.randint(1, 3))
            name = rng.choice(DBS_MERCHANTS)
            books.dbs.append(Row(posted, "Debit Card Transaction", -_money(rng, 3, 48),
                                 (f"{name.upper()} SINGAPORE SGP {charged:%d%b}".upper(),)))
        for _ in range(8):
            paid = month.replace(day=rng.randint(1, last))
            amount = _money(rng, 2, 9)
            books.dbs.append(Row(paid, "Top-up to PayLah!", -amount))
            books.wallet.append(Row(paid, "TOP UP WALLET FROM MY ACCOUNT", amount))
            books.wallet.append(Row(paid, rng.choice(WALLET_MERCHANTS), -amount))
        if index % 2:
            paid = month.replace(day=rng.randint(3, last))
            amount = _money(rng, 8, 24)
            books.dbs.append(Row(paid, "Top-up to PayLah!", -amount))
            books.wallet.append(Row(paid, "TOP UP WALLET FROM MY ACCOUNT", amount))
            books.wallet.append(Row(paid, "SEND MONEY TO JAMIE SYNTHETIC", -amount))
        if index % 3 == 1:
            books.wallet.append(Row(month.replace(day=rng.randint(10, last)), "REFUND FROM NOODLE HOUSE", _money(rng, 2, 6)))
        if index % 4 == 2:
            books.dbs.append(Row(month.replace(day=rng.randint(5, last)), "Debit Card Transaction",
                                 _money(rng, 6, 20), ("REFUND BOOK NOOK SINGAPORE SGP",)))


def _income(books: Books) -> None:
    """A monthly salary, with the odd side payment on no particular day."""
    rng = books.rng
    for month in books.months:
        books.dbs.append(Row(month.replace(day=25), "Funds Transfer", _money(rng, 2380, 2420), ("ACME STUDIO PTE LTD SALARY",)))
    for index in (2, 6):
        books.dbs.append(Row(books.months[index].replace(day=rng.randint(8, 16)), "Funds Transfer",
                             _money(rng, 280, 640), ("SIDE PROJECT CLIENT",)))
    books.dbs.append(Row(books.months[3].replace(day=14), "Funds Transfer", _money(rng, 40, 90),
                         ("JAMIE SYNTHETIC REIMBURSE",)))
    books.dbs.append(Row(books.months[5].replace(day=9), "Funds Transfer", _money(rng, 120, 180),
                         ("TAX OFFICE SYNTHETIC RELIEF",)))


def _trip_spend(books: Books) -> None:
    """One trip abroad, paid from whichever account had money in it."""
    rng = books.rng
    trip = books.months[5]
    books.trust.append(TrustRow(trip.replace(day=4), "JAMIE SYNTHETIC", Decimal("320.00")))
    for day in (11, 12, 14):
        foreign = _money(rng, 12, 48)
        books.trust.append(TrustRow(trip.replace(day=day), rng.choice(ABROAD_MERCHANTS).upper(),
                                    -(foreign * Decimal("1.46")).quantize(Decimal("0.01")),
                                    (foreign, "EUR", "1.4600")))
    books.trust.append(TrustRow(_month_end(trip), "Interest", Decimal("0.04")))
    books.trust.append(TrustRow(_month_end(books.months[6]), "Interest", Decimal("0.03")))
    for day in (11, 12, 13, 14, 15):
        for _ in range(2):
            books.revolut.append((trip.replace(day=day), "Card Payment", "EUR", -_money(rng, 6, 32), Decimal("0.00"),
                                  "COMPLETED", rng.choice(ABROAD_MERCHANTS), None))


def _revolut(books: Books) -> None:
    rng = books.rng
    for month in books.months:
        if _month_end(month) < books.revolut_opened:
            continue
        first = max(month, books.revolut_opened)
        last = _month_end(month).day
        for _ in range(2):
            topped = first if first.day == last else first.replace(day=rng.randint(first.day, last - 1))
            amount = _money(rng, 180, 320)
            books.dbs.append(Row(topped + timedelta(days=2), "Debit Card Transaction", -amount,
                                 (f"REVOLUT**4821* SINGAPORE SGP {topped:%d%b}".upper(),)))
            books.revolut.append((topped, "Topup", "SGD", amount, _money(rng, 0.5, 1.5), "COMPLETED", "Card top-up", None))
        if first.day > 1:
            continue
        exchanged = month.replace(day=rng.randint(2, 8))
        sgd = _money(rng, 120, 220)
        books.revolut.append((exchanged, "Exchange", "SGD", -sgd, Decimal("0.00"), "COMPLETED", "", None))
        books.revolut.append((exchanged, "Exchange", "EUR", (sgd * Decimal("0.68")).quantize(Decimal("0.01")),
                              Decimal("0.00"), "COMPLETED", "", None))
        for _ in range(4):
            books.revolut.append((month.replace(day=rng.randint(9, last)), "Card Payment", "SGD", -_money(rng, 2, 16),
                                  Decimal("0.00"), "COMPLETED", rng.choice(SGD_CARD_MERCHANTS), None))
        for _ in range(3):
            code = rng.choice(("JPY", "GBP"))
            foreign = _money(rng, 400, 2400) if code == "JPY" else _money(rng, 3, 18)
            books.revolut.append((month.replace(day=rng.randint(9, last)), "Card Payment", "SGD", -_money(rng, 4, 24),
                                  _money(rng, 0.05, 0.3), "COMPLETED", rng.choice(ABROAD_MERCHANTS), f"{foreign:.2f} {code}"))
        for _ in range(2):
            books.revolut.append((month.replace(day=rng.randint(9, last)), "Card Payment", "EUR", -_money(rng, 3, 14),
                                  Decimal("0.00"), "COMPLETED", rng.choice(ABROAD_MERCHANTS), None))
        books.revolut.append((month.replace(day=rng.randint(9, last)), "Card Payment", "EUR", -_money(rng, 2, 8),
                              Decimal("0.00"), "REVERTED", "Tram 28 Kiosk", None))
    paid_back = books.months[-2].replace(day=19)
    amount = _money(rng, 60, 110)
    books.revolut.append((paid_back, "Transfer", "SGD", -amount, Decimal("0.00"), "COMPLETED", "Transfer to my DBS account", None))
    books.dbs.append(Row(paid_back, "Debit Card Transaction", amount, ("MONEY IN FROM REVOLUT",)))
    last_month = books.months[-1]
    books.revolut.append((last_month.replace(day=26), "Card Payment", "EUR", -_money(rng, 2, 8), Decimal("0.00"),
                          "PENDING", "Konbini Mart", None))
    funded = books.months[-3].replace(day=12)
    amount = _money(rng, 90, 150)
    books.dbs.append(Row(funded, "PayNow Transfer", -amount, ("TO: REVOLUT TECHNOLOGIES SINGAPORE",)))
    books.revolut.append((funded, "Topup", "SGD", amount, Decimal("0.00"), "COMPLETED", "My DBS account", None))


def _card(books: Books) -> None:
    """Cycles close on the 20th. Plans post the day after, outside the printed period."""
    rng = books.rng
    period_start = books.card_opened
    previous = Decimal("0.00")
    long_plan = {"amount": Decimal("38.20"), "number": 7, "term": 18, "opened": books.card_opened - LONG_PLAN_AGE}
    converted: dict[str, Any] | None = None
    short_plan: dict[str, Any] | None = None
    cycle = 0
    while True:
        period_end = (period_start if period_start.day <= 20 else _next_month(period_start.replace(day=1))).replace(day=20)
        if not books.in_window(period_end):
            break
        statement_date = period_end + timedelta(days=1)
        due = statement_date + timedelta(days=20)
        sections: dict[str, list[MariRow]] = {}
        purchases = []
        days = (period_end - period_start).days
        for _ in range(4 if cycle else 3):
            charged = period_start + timedelta(days=rng.randint(0, days - 2))
            posted = charged + timedelta(days=rng.randint(1, 2))
            purchases.append(MariRow(posted, -_money(rng, 8, 72), rng.choice(CARD_MERCHANTS).upper(), ("Card Payment",), charged))
        if cycle % 3 == 2:
            charged = period_start + timedelta(days=rng.randint(0, days - 2))
            foreign = _money(rng, 10, 40)
            purchases.append(MariRow(charged + timedelta(days=1), -(foreign * Decimal("1.46")).quantize(Decimal("0.01")),
                                     "MERCADO CENTRAL", ("EUR " + f"{foreign:.2f}",), charged))
        if cycle == 2:
            big = Decimal("324.00")
            charged = period_start + timedelta(days=3)
            purchases.append(MariRow(charged + timedelta(days=1), -big, "ELECTRO WORLD", ("Card Payment",), charged))
            converted = {"amount": Decimal("36.00"), "number": 1, "term": 9, "opened": charged + timedelta(days=4)}
            sections["Repayment/Conversion"] = [MariRow(charged + timedelta(days=4), big, "Split Payment", ("Conversion",), charged + timedelta(days=4))]
        if cycle == 3:
            short_plan = {"amount": Decimal("27.35"), "number": 1, "term": 4, "opened": period_start.replace(day=25)}
        sections["Purchase"] = sorted(purchases, key=lambda row: row.posted)
        if cycle % 4 == 1:
            sections["Cashback"] = [MariRow(period_end, _money(rng, 1, 4), "Cashback Credit", ("Cashback",), period_end)]
        if cycle % 4 == 3:
            sections["Waiver/Refund"] = [MariRow(period_end - timedelta(days=4), _money(rng, 6, 20), "OUTDOOR SUPPLY", ("Refund",), period_end - timedelta(days=5))]
        if previous > 0:
            repaid = period_end.replace(day=6)
            sections.setdefault("Repayment/Conversion", []).insert(
                0, MariRow(repaid, previous, "SYNTHETIC HOLDER", ("Repayment",), repaid))
            deposit = previous + _money(rng, 40, 160)
            token = _paynow_token(rng)
            books.dbs.append(Row(repaid - timedelta(days=2), "PayNow Transfer", -deposit,
                                 ("TO: MARIBANK SINGAPORE", token)))
            books.savings.append(SavingsRow(repaid - timedelta(days=2), "SYNTHETIC HOLDER", deposit, ("PayNow Transfer", f"REF {token}")))
            books.savings.append(SavingsRow(repaid, "Mari Credit Card", -previous, ("Repayment",)))
        plans = []
        for plan in (long_plan, converted, short_plan):
            if plan is None or plan["number"] > plan["term"]:
                continue
            plans.append(MariRow(statement_date, -plan["amount"], f"Split Payment {plan['term']}M",
                                 (f"Split Payment {plan['number']}/{plan['term']} Instalment",), plan["opened"]))
            plan["number"] += 1
        if plans:
            sections["Split Payment"] = plans
        statement = MariStatement(period_start, period_end, statement_date, due, previous, Decimal("6000.00"), sections)
        books.card.append(statement)
        previous = statement_outstanding(statement)
        period_start, cycle = statement_date, cycle + 1
    books.savings.append(SavingsRow(books.months[4].replace(day=23), "BOOK NOOK ONLINE", -_money(rng, 12, 40), ("Card Payment",)))


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _dbs_statements(books: Books, root: Path) -> Iterable[Path]:
    balance, usd = Decimal("10300.00"), Decimal("42.17")
    carried: list[Row] = []
    for month in books.months:
        end = _month_end(month)
        rows = sorted((row for row in books.dbs if month <= row.on <= end), key=lambda row: row.on)
        # A row booked in the last days of a month can land on the next statement,
        # dated outside the period it is printed in.
        late = [row for row in rows if row.on >= end - timedelta(days=1) and row.amount < 0][:1]
        printed = [*carried, *[row for row in rows if row not in late]]
        carried = late if end < books.through else []
        if end == books.through:
            printed += late
        pocket = DbsPocket("SINGAPORE DOLLAR", balance, tuple(printed))
        balance += sum((row.amount for row in printed), Decimal("0.00"))
        yield _write(root / f"dbs-{month:%Y-%m}.pdf", dbs_pdf_bytes(end, [
            pocket, DbsPocket("UNITED STATES DOLLAR", usd, ())]))


def _wallet_statements(books: Books, root: Path) -> Iterable[Path]:
    balance = Decimal("7.35")
    carried: list[Row] = []
    close = books.months[1].replace(day=8)
    opens = books.months[0].replace(day=9)
    while close <= books.through + timedelta(days=8):
        rows = sorted((row for row in books.wallet if opens <= row.on <= close), key=lambda row: row.on)
        late = [row for row in rows if row.on == close][:2]
        printed = [*carried, *[row for row in rows if row not in late]]
        carried = late
        yield _write(root / f"wallet-{close:%Y-%m-%d}.pdf", paylah_pdf_bytes(close, balance, printed))
        balance += sum((row.amount for row in printed), Decimal("0.00"))
        opens = close + timedelta(days=1)
        close = _next_month(close.replace(day=1)).replace(day=8)


def _savings_statements(books: Books, root: Path) -> Iterable[Path]:
    balance = Decimal("0.00")
    for month in books.months:
        end = _month_end(month)
        if end < books.savings_opened:
            continue
        start = max(month, books.savings_opened)
        rows = sorted((row for row in books.savings if row.on and start <= row.on <= end), key=lambda row: row.on)
        interest = (balance * Decimal("0.00018")).quantize(Decimal("0.01"))
        yield _write(root / f"savings-{month:%Y-%m}.pdf", mari_savings_pdf_bytes(
            start, end, end + timedelta(days=5), balance, rows, interest))
        balance += interest + sum((row.amount for row in rows), Decimal("0.00"))
    books.savings_closing = balance


def _trust_statements(books: Books, root: Path) -> Iterable[Path]:
    balance = Decimal("0.00")
    for month in books.months[1:]:
        end = _month_end(month)
        rows = [row for row in sorted(books.trust, key=lambda row: row.on) if month <= row.on <= end]
        yield _write(root / f"trust-{month:%Y-%m}.pdf", trust_pdf_bytes(month, end, end + timedelta(days=1), balance, rows))
        balance += sum((row.amount for row in rows), Decimal("0.00"))


def _revolut_statements(books: Books, root: Path) -> Iterable[Path]:
    balances = {"SGD": Decimal("0.00"), "EUR": Decimal("0.00")}
    ordered = sorted(books.revolut, key=lambda row: (row[0], row[1] != "Topup", row[1] != "Exchange" or row[3] < 0))
    for month in books.months:
        end = _month_end(month)
        if end < books.revolut_opened:
            continue
        start = max(month, books.revolut_opened)
        rows: list[list[str]] = []
        for on, kind, currency, amount, fee, state, label, foreign in ordered:
            if not start <= on <= end:
                continue
            stamp = f"{on.isoformat()} 10:00:00"
            balance = ""
            if state == "COMPLETED":
                balances[currency] += amount - fee
                balance = f"{balances[currency]:.2f}"
            row = [kind, "Current", stamp, stamp if state == "COMPLETED" else "", label, f"{amount:.2f}", f"{fee:.2f}",
                   currency, state, balance]
            if foreign is not None:
                row.append(foreign)
            rows.append(row)
        yield _write(root / f"revolut-{month:%Y-%m}.pdf", revolut_pdf_bytes(
            rows, period=(datetime.combine(start, time()), datetime.combine(end, time()))))


def _card_statements(books: Books, root: Path) -> Iterable[Path]:
    for statement in books.card:
        yield _write(root / f"card-{statement.period_end:%Y-%m}.pdf", mari_card_pdf_bytes(statement))


def _fx_rates(database: Path, months: list[date]) -> None:
    rates = []
    for index, month in enumerate(months):
        drift = Decimal(index) / 200
        for code, rate in (("USD", Decimal("1.34")), ("EUR", Decimal("1.46")), ("GBP", Decimal("1.71")), ("JPY", Decimal("0.0090"))):
            rates.append({"base_currency": code, "period": f"{month:%Y-%m}", "rate": f"{rate * (1 + drift):.4f}"})
    connection = connect_sqlite(database)
    try:
        load_fx_rates(connection, {"source": "Synthetic monthly rates", "source_version": "synthetic-demo-v2",
                                   "quote_currency": "SGD", "rates": rates})
    finally:
        connection.close()


def _stable_uuid(name: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, name))


def _decisions(database: Path, books: Books) -> None:
    connection = connect_sqlite(database)
    entries = connection.execute(
        "SELECT e.natural_anchor, e.account_id, e.currency_pocket, e.effective_at, e.amount_text, "
        "d.description_raw FROM ledger_entries e JOIN document_records d ON d.id = e.document_record_id "
        "ORDER BY e.effective_at, e.natural_anchor").fetchall()
    connection.close()
    journal = default_journal_path(database)

    def decide(kind: str, anchors: dict[str, str], payload: dict[str, Any], snapshot: str) -> None:
        append_decision(journal, decision_kind=kind, anchors=anchors, payload=payload, policy_version=POLICY, snapshot=snapshot)

    def matching(text: str, account: str | None = None) -> list[Any]:
        return [row for row in entries if text in (row["description_raw"] or "")
                and (account is None or row["account_id"] == account)]

    for row in [*matching("ACME STUDIO"), *matching("SIDE PROJECT")]:
        decide("INBOUND_CLASSIFICATION", {"entry": row["natural_anchor"]},
               {"classification": "income", "reason": "Synthetic income"}, "Synthetic income")
    for text, classification in (("JAMIE SYNTHETIC REIMBURSE", "reimbursement"), ("TAX OFFICE", "other"),
                                 ("MONEY IN FROM REVOLUT", "self_transfer")):
        for row in matching(text):
            decide("INBOUND_CLASSIFICATION", {"entry": row["natural_anchor"]},
                   {"classification": classification, "reason": "Synthetic inflow"}, "Synthetic inflow")

    for match, category in (({"paynow_to": "LANDLORD SYNTHETIC"}, "Housing / Rent"),
                            ({"description_contains": "SYNTHETIC POWER"}, "Housing / Utilities"),
                            ({"description_contains": "CORNER GROCER"}, "Food & Dining / Groceries"),
                            ({"description_contains": "FRESH MARKET"}, "Food & Dining / Groceries"),
                            ({"description_contains": "LUNCH COUNTER"}, "Food & Dining / Dining Out"),
                            ({"description_contains": "TRANSIT TOP-UP"}, "Transport / Public Transit"),
                            ({"description_contains": "NOODLE HOUSE"}, "Food & Dining / Dining Out"),
                            ({"description_contains": "KOPI CORNER"}, "Food & Dining / Cafes & Drinks"),
                            ({"description_contains": "ELECTRO WORLD"}, "Shopping / Electronics")):
        decide("CATEGORY_RULE_SET", {"rule_id": rule_id_for(match)},
               {"match": match, "category": category, "reason": "Synthetic rule"}, "Synthetic rule")

    # A month-only interest row stops a statement proving its closing balance, so the
    # savings pocket needs a balance observation to reach the start of the forecast.
    decide("BALANCE_OBSERVATION_SET", {"observation": _stable_uuid("balance:savings")},
           {"account_id": "maribank-savings", "currency_pocket": "SGD", "observed_on": books.through.isoformat(),
            "balance": f"{books.savings_closing:.2f}", "reason": "Synthetic balance check"}, "Synthetic balance")

    rent = matching("LANDLORD SYNTHETIC")
    decide("RECURRING_COMMITMENT_SET", {"commitment": "rent"},
           {"name": "Rent", "currency": "SGD", "merchant_key": "LANDLORD SYNTHETIC", "account_ids": [],
            "seed_entry_anchors": [row["natural_anchor"] for row in rent[:3]],
            "schedule": {"kind": "monthly", "day": 2, "anchor_on": rent[0]["effective_at"][:10]},
            "expected_amount": "1180.00", "amount_tolerance": "0.00", "status": "active", "ended_on": None,
            "reason": "Synthetic lease"}, "Synthetic commitment")

    plans = {18: (books.card_opened - LONG_PLAN_AGE, "38.20", "Split Payment 18M"), 9: (None, "36.00", "Electro World")}
    for term, (opened, amount, label) in plans.items():
        rows = matching(f"/{term} Instalment", "mari-credit-card")
        decide("INSTALLMENT_PLAN_SET", {"plan": _stable_uuid(f"plan:{term}")}, {
            "card_account_id": "mari-credit-card", "currency": "SGD", "plan_reference": None,
            "merchant_label": label, "term_months": term, "known_principal": None, "financing_fee": None,
            "opened_on": (opened or date.fromisoformat(rows[0]["effective_at"][:10])).isoformat(), "status": "CONFIRMED",
            "occurrences": [{"occurrence_id": _stable_uuid(f"plan:{term}:{row['natural_anchor']}"), "occurrence_index": None,
                             "occurrence_count": None, "entry_anchor": row["natural_anchor"],
                             "effective_on": row["effective_at"][:10], "amount": amount, "date_precision": "DAY"} for row in rows],
            "schedule": [], "reason": "Synthetic plan"}, "Synthetic installment plan")

    trip_id = _stable_uuid("trip")
    trip_month = books.months[5]
    decide("TRIP_SET", {"trip": trip_id}, {"name": "Lisbon week", "start_on": trip_month.replace(day=10).isoformat(),
                                           "end_on": trip_month.replace(day=16).isoformat(), "countries": [],
                                           "reason": "Synthetic trip"}, "Synthetic trip")
    for row in entries:
        on = row["effective_at"][:10]
        if trip_month.replace(day=10).isoformat() <= on <= trip_month.replace(day=16).isoformat() and any(
                name.upper() in (row["description_raw"] or "").upper() for name in ABROAD_MERCHANTS):
            decide("ENTRY_TRIP_SET", {"entry": row["natural_anchor"]}, {"trip": trip_id, "reason": "On the trip"}, "Synthetic trip entry")

    start = _next_month(_next_month(books.through.replace(day=1)))
    for label, direction, opened, amount, due_on in (
        ("Jamie", "PAYABLE", 1, "640.00", None),
        ("Sam", "PAYABLE", 2, "1360.00", None),
        ("Robin", "RECEIVABLE", 3, "245.00", None),
        ("Priya", "RECEIVABLE", 4, "480.00", start.replace(day=20).isoformat()),
    ):
        debt = _stable_uuid(f"debt:{label}")
        on = books.months[opened].replace(day=6).isoformat()
        decide("PERSONAL_DEBT_OPEN", {"debt": debt}, {"direction": direction, "currency": "SGD", "counterparty_label": label,
                                                       "opened_on": on, "due_on": due_on, "reason": "Synthetic debt"}, "Synthetic debt")
        decide("PERSONAL_DEBT_EVENT_SET", {"debt": debt, "event": _stable_uuid(f"event:{label}")},
               {"event_kind": "BORROWED" if direction == "PAYABLE" else "LENT", "direction": direction, "principal_amount": amount,
                "effective_on": on, "entry_allocations": [], "opening_balance": True, "expense_category": None,
                "reason": "Synthetic opening balance"}, "Synthetic debt event")
    decide("PERSONAL_DEBT_SCHEDULE_SET", {"debt": _stable_uuid("debt:Sam")},
           {"effective_from": books.months[2].replace(day=6).isoformat(), "reason": "Agreed monthly",
            "plan": {"kind": "rows", "rows": [{"due_on": (start + timedelta(days=31 * step)).replace(day=10).isoformat(), "amount": "340.00"}
                                              for step in range(4)]}}, "Synthetic debt schedule")

    horizon = _next_month(_next_month(start))
    decide("PLANNED_FLOW_SET", {"flow": _stable_uuid("planned:dental")},
           {"name": "Dental work", "direction": "outflow", "currency": "SGD", "account_id": None, "category": None,
            "occurrences": [{"key": "a", "earliest": horizon.replace(day=14).isoformat(),
                             "latest": horizon.replace(day=14).isoformat(), "amount": "780.00"}],
            "certainty": "sure", "baseline_treatment": "extra", "link": None, "reason": "Quoted"}, "Synthetic planned item")

    for ident, payload in (("groceries", {"type": "budget", "name": "Groceries, with room to spare", "currency": "SGD", "target": "320.00",
                                          "saved": "0", "category": "Food & Dining / Groceries", "reason": "Synthetic budget"}),
                           ("emergency", {"type": "goal", "name": "A little peace of mind", "currency": "SGD", "target": "15000.00",
                                          "saved": "6200.00", "category": "", "reason": "Synthetic goal"})):
        decide("PLAN_SET", {"plan": ident}, payload, "Synthetic plan")


def create_demo(root: Path, *, through: date | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "demo.sqlite3"
    if database.exists():
        raise RuntimeError("Choose an empty directory for the demo ledger")
    end = through or default_through(clock.today())
    rng = random.Random(SEED)
    months = _months_back(end.replace(day=1), MONTHS)
    books = Books(end, months, rng, revolut_opened=months[0].replace(day=rng.randint(10, 19)),
                  savings_opened=months[2].replace(day=rng.randint(10, 19)), card_opened=months[3].replace(day=rng.randint(7, 16)))

    _income(books)
    _spending(books)
    _card(books)
    _revolut(books)
    _trip_spend(books)

    statements = root / "statements"
    statements.mkdir()
    service = DocumentImportService.plaintext_phase2(database, root / "evidence")
    for path in _dbs_statements(books, statements):
        service.import_dbs(path)
    for path in _wallet_statements(books, statements):
        service.import_paylah(path)
    for path in _savings_statements(books, statements):
        service.import_maribank(path)
    for path in _card_statements(books, statements):
        service.import_mari_card(path)
    for path in _revolut_statements(books, statements):
        service.import_revolut_pdf(path)
    for path in _trust_statements(books, statements):
        service.import_trust(path)

    _fx_rates(database, books.months)
    _decisions(database, books)
    refresh_ledger(database)
    (root / SYNTHETIC_MARKER).write_text("Built by scripts/create_demo.py. Nothing here is real.\n")
    return database


if __name__ == "__main__":
    print(create_demo(Path(sys.argv[1])))
