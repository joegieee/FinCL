"""Cash-flow coverage: certified quiet intervals, partial totals and remedies.

Three behaviors are proven here, against a ledger built through the real import
path:

* a journalled ``MOVEMENT_COVERAGE_CERTIFY`` bridges a quiet interval that a
  statement alone could not certify, and retiring it puts the gap back;
* when one liquid pocket cannot be proven, the strict combined total is still
  withheld, but a partial total over the pockets that *are* proven is published
  beside it with its own date and an explicit short list;
* each remaining gap is reported as a named action carrying the exact pocket and
  date range it would certify.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from datetime import date
from pathlib import Path

from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.position import (
    coverage_remedies,
    movement_certifications,
    position_page,
    validate_position_payload,
)
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
     "Seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
    ["Card Payment", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
     "Rent", "-500.00", "0.00", "SGD", "COMPLETED", "400.00"],
]


def _ledger(rows: list[list[str]]) -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "ledger.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(rows))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


def _account_id(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        return str(connection.execute("SELECT id FROM accounts").fetchone()[0])
    finally:
        connection.close()


def _certify(
    database: Path,
    *,
    account_id: str,
    period_start: str,
    period_end: str,
    certification: str | None = None,
) -> str:
    identifier = certification or str(uuid.uuid4())
    append_decision(
        default_journal_path(database),
        decision_kind="MOVEMENT_COVERAGE_CERTIFY",
        anchors={"certification": identifier},
        payload={
            "account_id": account_id,
            "currency_pocket": "SGD",
            "period_start": period_start,
            "period_end": period_end,
            "basis": "account-app-transaction-list",
            "reason": "checked the account's own transaction list; no activity",
        },
        policy_version="position-coverage-v1",
        snapshot="coverage test",
    )
    _replay(database)
    return identifier


def _replay(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        replay_journal(connection)
        connection.commit()
    finally:
        connection.close()


class CertifiedQuietIntervalTests(unittest.TestCase):
    def test_an_uncertified_quiet_interval_leaves_the_position_unsupported(self) -> None:
        database = _ledger(_ROWS)

        page = position_page(database, today=date(2026, 3, 20))

        self.assertFalse(page["current_position_supported"])
        # The complete read falls back to the statement close it can prove.
        self.assertEqual(page["supported_cutoff"], "2026-03-05")
        self.assertEqual(page["pockets"][0]["best_supported_cutoff"], "2026-03-05")

    def test_certifying_the_quiet_interval_supports_the_position_through_today(self) -> None:
        database = _ledger(_ROWS)
        _certify(
            database,
            account_id=_account_id(database),
            period_start="2026-03-06",
            period_end="2026-03-20",
        )

        page = position_page(database, today=date(2026, 3, 20))

        self.assertTrue(page["current_position_supported"])
        self.assertEqual(page["supported_cutoff"], "2026-03-20")
        # The certification proves silence, never an amount: the figure is still
        # the statement's own closing balance carried forward unchanged.
        self.assertEqual(page["liquid_total"], {"SGD": "400.00"})
        self.assertEqual(page["coverage_summary"]["status"], "complete")
        self.assertEqual(page["coverage_summary"]["ratio"], 1.0)

    def test_retiring_the_certification_restores_the_gap(self) -> None:
        database = _ledger(_ROWS)
        account_id = _account_id(database)
        identifier = _certify(
            database,
            account_id=account_id,
            period_start="2026-03-06",
            period_end="2026-03-20",
        )
        self.assertTrue(position_page(database, today=date(2026, 3, 20))["current_position_supported"])

        append_decision(
            default_journal_path(database),
            decision_kind="MOVEMENT_COVERAGE_RETIRE",
            anchors={"certification": identifier},
            payload={"reason": "found a transaction I had missed"},
            policy_version="position-coverage-v1",
            snapshot="coverage test",
        )
        _replay(database)

        page = position_page(database, today=date(2026, 3, 20))
        self.assertFalse(page["current_position_supported"])
        self.assertEqual(movement_certifications(database)["certifications"], [])

    def test_an_active_certification_is_listed_for_review(self) -> None:
        database = _ledger(_ROWS)
        account_id = _account_id(database)
        _certify(
            database,
            account_id=account_id,
            period_start="2026-03-06",
            period_end="2026-03-20",
        )

        listed = movement_certifications(database)["certifications"]

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["period_start"], "2026-03-06")
        self.assertEqual(listed[0]["period_end"], "2026-03-20")
        self.assertEqual(listed[0]["basis"], "account-app-transaction-list")


class PartialPositionTests(unittest.TestCase):
    """A pocket that cannot reach today must not hold the others back.

    Both pockets are proven through the statement close on 2026-03-05, but only
    the SGD side is certified onward. The complete total therefore stays dated
    2026-03-05, while the partial read moves to 2026-03-20 with SGD alone and
    says so.
    """

    def _two_pocket_ledger(self) -> Path:
        # The USD pocket's last movement is well before the SGD one, and only
        # the SGD side is certified onward, so no single date is supported by
        # both pockets.
        return _ledger([
            ["Topup", "Current", "2026-03-01 10:00:00", "2026-03-01 10:00:01",
             "SGD seed", "900.00", "0.00", "SGD", "COMPLETED", "900.00"],
            ["Topup", "Current", "2026-03-05 10:00:00", "2026-03-05 10:00:01",
             "USD seed", "50.00", "0.00", "USD", "COMPLETED", "50.00"],
        ])

    def test_the_complete_total_stays_at_the_older_shared_cutoff(self) -> None:
        """The complete read must not borrow the partial read's later date."""
        database = self._two_pocket_ledger()
        _certify(
            database,
            account_id=_account_id(database),
            period_start="2026-03-06",
            period_end="2026-03-20",
        )

        page = position_page(database, today=date(2026, 3, 20))

        self.assertEqual(page["supported_cutoff"], "2026-03-05")
        self.assertEqual(
            page["combined_liquid_position"], {"SGD": "900.00", "USD": "50.00"}
        )
        self.assertFalse(page["current_position_supported"])
        self.assertIn(
            "complete actual movements from the supported cutoff through today "
            "are unavailable",
            page["current_position_reasons"],
        )

    def test_the_partial_total_names_its_date_its_pockets_and_what_is_missing(self) -> None:
        database = self._two_pocket_ledger()
        _certify(
            database,
            account_id=_account_id(database),
            period_start="2026-03-06",
            period_end="2026-03-20",
        )

        partial = position_page(database, today=date(2026, 3, 20))["partial_position"]

        self.assertEqual(partial["as_of"], "2026-03-20")
        self.assertEqual(partial["by_currency"], {"SGD": "900.00"})
        self.assertFalse(partial["is_complete"])
        self.assertEqual(
            [entry["currency_pocket"] for entry in partial["contributing_pockets"]], ["SGD"]
        )
        self.assertEqual(
            [entry["currency_pocket"] for entry in partial["short_pockets"]], ["USD"]
        )
        self.assertIn("1 of 2 liquid pockets", str(partial["reason"]))

    def test_the_coverage_summary_reports_the_proven_share(self) -> None:
        database = self._two_pocket_ledger()
        _certify(
            database,
            account_id=_account_id(database),
            period_start="2026-03-06",
            period_end="2026-03-20",
        )

        summary = position_page(database, today=date(2026, 3, 20))["coverage_summary"]

        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["included_pockets"], 2)
        self.assertEqual(summary["covered_pockets"], 1)
        self.assertEqual(summary["short_pockets"], 1)
        self.assertEqual(summary["ratio"], 0.5)

    def test_each_pocket_reports_its_own_furthest_supported_date(self) -> None:
        database = self._two_pocket_ledger()
        _certify(
            database,
            account_id=_account_id(database),
            period_start="2026-03-06",
            period_end="2026-03-20",
        )

        pockets = {
            pocket["currency_pocket"]: pocket
            for pocket in position_page(database, today=date(2026, 3, 20))["included_pockets"]
        }

        self.assertEqual(pockets["SGD"]["best_supported_cutoff"], "2026-03-20")
        self.assertEqual(pockets["SGD"]["best_supported_balance"], "900.00")
        self.assertEqual(pockets["USD"]["best_supported_cutoff"], "2026-03-05")
        self.assertIsNotNone(pockets["USD"]["blocking_reason"])


class RemedyTests(unittest.TestCase):
    def test_a_blocked_pocket_offers_the_exact_range_it_would_certify(self) -> None:
        database = _ledger(_ROWS)

        result = coverage_remedies(database, today=date(2026, 3, 20))

        certify = [
            remedy for remedy in result["remedies"]
            if remedy["action"] == "certify-quiet-interval"
        ]
        self.assertEqual(len(certify), 1)
        self.assertEqual(certify[0]["decision_kind"], "MOVEMENT_COVERAGE_CERTIFY")
        self.assertEqual(certify[0]["currency_pocket"], "SGD")
        self.assertEqual(
            certify[0]["parameters"], {"period_start": "2026-03-05", "period_end": "2026-03-20"}
        )

    def test_a_blocked_pocket_also_offers_a_dated_balance_observation(self) -> None:
        database = _ledger(_ROWS)

        result = coverage_remedies(database, today=date(2026, 3, 20))

        observe = [
            remedy for remedy in result["remedies"]
            if remedy["action"] == "record-balance-observation"
        ]
        self.assertEqual(len(observe), 1)
        self.assertEqual(observe[0]["decision_kind"], "BALANCE_OBSERVATION_SET")
        self.assertEqual(observe[0]["parameters"], {"suggested_observed_on": "2026-03-20"})

    def test_a_fully_covered_ledger_offers_nothing_to_fix(self) -> None:
        database = _ledger(_ROWS)
        _certify(
            database,
            account_id=_account_id(database),
            period_start="2026-03-06",
            period_end="2026-03-20",
        )

        result = coverage_remedies(database, today=date(2026, 3, 20))

        self.assertEqual(result["remedies"], [])
        self.assertTrue(result["current_position_supported"])
        self.assertEqual(result["coverage"]["status"], "complete")


class CertificationValidationTests(unittest.TestCase):
    def _payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "account_id": "acct-1",
            "currency_pocket": "SGD",
            "period_start": "2026-03-06",
            "period_end": "2026-03-20",
            "basis": "account-app-transaction-list",
            "reason": "no activity in the account's transaction list",
        }
        payload.update(overrides)
        return payload

    def _validate(self, payload: dict[str, object]) -> None:
        validate_position_payload(
            "MOVEMENT_COVERAGE_CERTIFY",
            {"certification": str(uuid.uuid4())},
            payload,
            today=date(2026, 3, 20),
        )

    def test_a_well_formed_certification_validates(self) -> None:
        self._validate(self._payload())

    def test_a_future_period_end_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._validate(self._payload(period_end="2026-04-01"))

    def test_a_reversed_range_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._validate(self._payload(period_start="2026-03-21", period_end="2026-03-20"))

    def test_an_unnamed_basis_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._validate(self._payload(basis="I am fairly sure"))

    def test_an_amount_cannot_be_smuggled_into_a_certification(self) -> None:
        """A certification proves completeness only; it has no amount field."""
        with self.assertRaises(FinCLError):
            self._validate(self._payload(balance="1000.00"))


if __name__ == "__main__":
    unittest.main()
