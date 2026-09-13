"""Launch a throwaway demo ledger under the real HTTP app for Playwright.

Playwright's ``webServer`` runs this. It builds an isolated synthetic ledger in a
temp directory, then serves ``fincl.api`` with a fixed bootstrap secret on
127.0.0.1:8799 so the browser test can open ``/#bootstrap=e2e-secret``. Nothing
here touches a real ledger.
"""
from __future__ import annotations

import csv
import sys
import tempfile
import uuid
from pathlib import Path

import uvicorn

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from fincl.api import create_app  # noqa: E402
from fincl.association import create_new_ledger_identity  # noqa: E402
from fincl.db import connect_sqlite, initialize_schema  # noqa: E402
from fincl.decision_journal import append_decision, default_journal_path  # noqa: E402
from fincl.decisions import entry_anchor  # noqa: E402
from fincl.document_service import DocumentImportService  # noqa: E402
from fincl.refresh import refresh_ledger  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from revolut_pdf import revolut_pdf_bytes  # noqa: E402


def fixture_pdf(csv_path: Path) -> Path:
    """Render a tracked synthetic CSV fixture as the PDF the current importer accepts."""
    generated = Path("/tmp/fincl-e2e-fixtures")
    generated.mkdir(parents=True, exist_ok=True)
    output = generated / f"{csv_path.stem}.pdf"
    with csv_path.open(newline="") as source:
        rows = list(csv.reader(source))
    output.write_bytes(revolut_pdf_bytes(rows[1:]))
    return output


def generated_pdf(name: str, rows: list[list[str]]) -> Path:
    """Write an explicitly synthetic Revolut-like statement for UX acceptance."""
    generated = Path("/tmp/fincl-e2e-fixtures")
    generated.mkdir(parents=True, exist_ok=True)
    output = generated / f"{name}.pdf"
    output.write_bytes(revolut_pdf_bytes(rows))
    return output


def classification_rows(currency: str = "SGD") -> list[list[str]]:
    events: list[tuple[int, int, str, float]] = []

    def add(day: int, description: str, amount: float, *, month: int = 10) -> None:
        events.append((month, day, description, amount))

    add(1, "GROCERY SOLO LONG SYNTHETIC NAME", -41.25)
    add(2, "FILL COMPLETE", -18.00)
    add(3, "FILL MISSING", -19.00)
    add(4, "RULE SHOP EXCEPTION", -22.00)
    for index in range(55):
        add(5 + index % 24, f"BULK GROCERY {index + 1:02d}", -(10 + index / 10), month=10 + index // 48)
    add(5, "ONE OFF CONSULTING RECEIPT", 333.00, month=12)
    add(6, "FREELANCE PAYER", 700.00, month=10)
    add(6, "FREELANCE PAYER", 710.00, month=11)
    add(6, "FREELANCE PAYER", 705.00, month=12)
    add(7, "RULE SHOP COMMON", -31.00, month=12)
    add(8, "RULE SHOP COMMON", -32.00, month=12)
    add(9, "RULE SHOP COMMON", -33.00, month=12)
    rows: list[list[str]] = []
    balance = 9000.0
    for month, day, description, amount in sorted(events):
        balance += amount
        timestamp = f"2025-{month:02d}-{day:02d} 10:00:00"
        rows.append([
            "Topup" if amount > 0 else "Card Payment", "Current", timestamp,
            timestamp, description, f"{amount:.2f}", "0.00", currency,
            "COMPLETED", f"{balance:.2f}",
        ])
    return rows

workdir = Path(tempfile.mkdtemp(prefix="fincl-e2e-"))
database = workdir / "demo.sqlite3"

# Compute stable natural anchors from the two statement fixtures, then start the
# actual browser ledger empty. Salary classification can therefore resolve when
# the user imports the statements without ever asking them to type an income
# amount. Outgoings deliberately remain unclassified for grouped UI review.
anchor_root = workdir / "anchor-source"
anchor_root.mkdir()
anchor_db = anchor_root / "anchors.sqlite3"
fixtures = REPO / "frontend" / "e2e" / "fixtures"
generated_fixtures = {
    path.stem: fixture_pdf(path) for path in fixtures.glob("*.csv")
}
generated_fixtures["classification-history"] = generated_pdf(
    "classification-history", classification_rows()
)
generated_fixtures["classification-usd"] = generated_pdf(
    "classification-usd",
    [["Card Payment", "Current", "2025-12-20 10:00:00", "2025-12-20 10:00:00",
      "USD SYNTHETIC PURCHASE", "-12.50", "0.00", "USD", "COMPLETED", "987.50"]],
)
generated_fixtures["classification-future"] = generated_pdf(
    "classification-future",
    [["Topup", "Current", "2026-01-06 10:00:00", "2026-01-06 10:00:00",
      "FREELANCE PAYER", "715.00", "0.00", "SGD", "COMPLETED", "9715.00"]],
)
service = DocumentImportService.plaintext_phase2(anchor_db, anchor_root / "evidence")
service.import_revolut_pdf(generated_fixtures["inadequate-one-month"])
service.import_revolut_pdf(generated_fixtures["reconciled-history"])
service.import_revolut_pdf(generated_fixtures["classification-history"])
service.import_revolut_pdf(generated_fixtures["classification-usd"])
anchor_connection = connect_sqlite(anchor_db)
salary_anchors = [
    entry_anchor(anchor_connection, row["id"])
    for row in anchor_connection.execute(
        "SELECT e.id FROM ledger_entries e JOIN raw_records r ON r.id=e.raw_record_id "
        "WHERE r.description_raw='ACME PAYROLL' ORDER BY e.effective_at"
    )
]
seeded_anchors = {
    row["description_raw"]: entry_anchor(anchor_connection, row["id"])
    for row in anchor_connection.execute(
        "SELECT e.id, r.description_raw FROM ledger_entries e "
        "JOIN raw_records r ON r.id=e.raw_record_id "
        "WHERE r.description_raw IN ('FILL COMPLETE', 'FILL MISSING', 'RULE SHOP EXCEPTION')"
    )
}
rule_exception_row = anchor_connection.execute(
    "SELECT e.id FROM ledger_entries e JOIN raw_records r ON r.id=e.raw_record_id "
    "WHERE r.description_raw='RULE SHOP COMMON' ORDER BY e.effective_at LIMIT 1"
).fetchone()
if rule_exception_row is not None:
    seeded_anchors["RULE SHOP COMMON MANUAL EXCEPTION"] = entry_anchor(
        anchor_connection, rule_exception_row["id"]
    )
anchor_connection.close()

connection = connect_sqlite(database)
initialize_schema(connection)
create_new_ledger_identity(connection, database)
connection.close()
journal = default_journal_path(database)
for anchor in salary_anchors:
    append_decision(
        journal, decision_kind="INBOUND_CLASSIFICATION", anchors={"entry": anchor},
        payload={"classification": "income", "reason": "Synthetic payslip evidence"},
        policy_version="e2e-fixture-v1", snapshot="Synthetic salary classification",
    )
for description, anchor in seeded_anchors.items():
    append_decision(
        journal, decision_kind="CATEGORY_OVERRIDE", anchors={"entry": anchor},
        payload={"category": "Food & Dining / Groceries", "reason": "Synthetic acceptance setup"},
        policy_version="e2e-fixture-v1", snapshot=description,
    )
    append_decision(
        journal, decision_kind="ENTRY_ROLE_SET", anchors={"entry": anchor},
        payload={
            "cost_behavior": "variable_routine",
            "need": "essential" if description != "FILL MISSING" else "unresolved",
            "reason": "Synthetic acceptance setup",
        },
        policy_version="e2e-fixture-v1", snapshot=description,
    )

# A synthetic documented annual invoice is represented as a dated payable. It
# supplies both the amount and due date to the fund proposal; the browser only
# confirms those sourced values.
debt_id = str(uuid.uuid4())
append_decision(
    journal, decision_kind="PERSONAL_DEBT_OPEN", anchors={"debt": debt_id},
    payload={"direction": "PAYABLE", "currency": "SGD", "counterparty_label": "Annual insurance invoice", "opened_on": "2026-08-15", "due_on": "2027-02-15", "reason": "Synthetic invoice fixture"},
    policy_version="e2e-fixture-v1", snapshot="Annual insurance invoice",
)
append_decision(
    journal, decision_kind="PERSONAL_DEBT_EVENT_SET", anchors={"debt": debt_id, "event": str(uuid.uuid4())},
    payload={"event_kind": "BORROWED", "direction": "PAYABLE", "principal_amount": "3000.00", "effective_on": "2026-08-15", "entry_allocations": [], "opening_balance": True, "expense_category": None, "reason": "Amount printed on synthetic invoice"},
    policy_version="e2e-fixture-v1", snapshot="Annual invoice amount",
)
refresh_ledger(database)
app = create_app(
    database, port=8799, bootstrap_secret="e2e-secret",
    evidence_dir=workdir / "evidence", demo=True,
)
uvicorn.run(app, host="127.0.0.1", port=8799, log_level="warning")
