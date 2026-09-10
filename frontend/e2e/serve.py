"""Launch a throwaway demo ledger under the real HTTP app for Playwright.

Playwright's ``webServer`` runs this. It builds an isolated synthetic ledger in a
temp directory, then serves ``fincl.api`` with a fixed bootstrap secret on
127.0.0.1:8799 so the browser test can open ``/#bootstrap=e2e-secret``. Nothing
here touches a real ledger.
"""
from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

import uvicorn

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from fincl.service import FinCLService  # noqa: E402

from fincl.api import create_app  # noqa: E402
from fincl.association import create_new_ledger_identity  # noqa: E402
from fincl.db import connect_sqlite, initialize_schema  # noqa: E402
from fincl.decision_journal import append_decision, default_journal_path  # noqa: E402
from fincl.decisions import entry_anchor  # noqa: E402
from fincl.refresh import refresh_ledger  # noqa: E402

workdir = Path(tempfile.mkdtemp(prefix="fincl-e2e-"))
database = workdir / "demo.sqlite3"

# Compute stable natural anchors from the two statement fixtures, then start the
# actual browser ledger empty. Salary classification can therefore resolve when
# the user imports the statements without ever asking them to type an income
# amount. Outgoings deliberately remain unclassified for grouped UI review.
anchor_root = workdir / "anchor-source"
anchor_root.mkdir()
anchor_db = anchor_root / "anchors.sqlite3"
service = FinCLService.plaintext_phase1(anchor_db, anchor_root / "evidence")
fixtures = REPO / "frontend" / "e2e" / "fixtures"
service.import_revolut(fixtures / "inadequate-one-month.csv")
service.import_revolut(fixtures / "reconciled-history.csv")
anchor_connection = connect_sqlite(anchor_db)
salary_anchors = [
    entry_anchor(anchor_connection, row["id"])
    for row in anchor_connection.execute(
        "SELECT e.id FROM ledger_entries e JOIN raw_records r ON r.id=e.raw_record_id "
        "WHERE r.description_raw='ACME PAYROLL' ORDER BY e.effective_at"
    )
]
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
