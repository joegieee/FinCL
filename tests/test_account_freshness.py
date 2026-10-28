"""Per-account evidence freshness, and which account holds the shared start back."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from fincl.decision_journal import append_decision, default_journal_path
from fincl.document_service import DocumentImportService
from fincl.position import account_freshness, coverage_remedies
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes
from tests.test_pocket_coverage import _month

_FIXTURES = Path(__file__).parent / "fixtures"
_TODAY = date(2026, 9, 20)
_REVOLUT = "acct_revolut_current"


class AccountFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.database = root / "ledger.sqlite3"
        service = DocumentImportService.plaintext_phase2(self.database, root / "evidence")
        balance = 20000.0
        for month in range(1, 8):
            rows, balance = _month(month, balance)
            source = root / f"revolut-{month}.pdf"
            source.write_bytes(revolut_pdf_bytes(rows))
            service.import_revolut_pdf(source)
        service.import_trust(_FIXTURES / "trust_savings.pdf")
        refresh_ledger(self.database)

    def by_account(self) -> dict[str, dict]:
        read = account_freshness(self.database, today=_TODAY)
        return {row["account"]: row for row in read["accounts"]}

    def record(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> None:
        append_decision(
            default_journal_path(self.database), decision_kind=kind, anchors=anchors,
            payload=payload, policy_version="position-v1", snapshot="freshness test",
        )
        refresh_ledger(self.database)

    def test_the_account_one_statement_behind_holds_the_start_back(self) -> None:
        accounts = self.by_account()

        self.assertEqual(
            accounts[_REVOLUT],
            {"account": _REVOLUT, "display_name": "Revolut Current", "currencies": ["SGD"],
             "evidence_through": "2026-07-31", "age_days": 51, "stale": True,
             "basis": "statement", "holds_back_start": True},
        )
        trust = accounts["trust-savings"]
        self.assertEqual((trust["evidence_through"], trust["holds_back_start"]), ("2026-08-31", False))
        self.assertEqual(
            account_freshness(self.database, today=_TODAY)["supported_cutoff"], "2026-07-31"
        )
        self.assertEqual(coverage_remedies(self.database, today=_TODAY)["freshness"],
                         list(self.by_account().values()))

    def test_an_observed_balance_and_a_certified_quiet_range_name_their_basis(self) -> None:
        self.record(
            "BALANCE_OBSERVATION_SET", {"observation": "11111111-1111-4111-8111-111111111111"},
            {"account_id": _REVOLUT, "currency_pocket": "SGD", "observed_on": "2026-09-20",
             "balance": "6840.00", "reason": "read off the app"},
        )
        self.record(
            "MOVEMENT_COVERAGE_CERTIFY", {"certification": "22222222-2222-4222-8222-222222222222"},
            {"account_id": "trust-savings", "currency_pocket": "SGD",
             "period_start": "2026-09-01", "period_end": "2026-09-20",
             "basis": "account-app-transaction-list", "reason": "no activity"},
        )

        accounts = self.by_account()

        self.assertEqual(
            {name: (row["evidence_through"], row["basis"], row["stale"], row["holds_back_start"])
             for name, row in accounts.items()},
            {_REVOLUT: ("2026-09-20", "observation", False, False),
             "trust-savings": ("2026-09-20", "certification", False, False)},
        )


if __name__ == "__main__":
    unittest.main()
