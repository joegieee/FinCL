"""v0.4 slice 2: card facilities, dated limit observations, utilization views."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from typer.testing import CliRunner

from fincl.application import record_card_decision
from fincl.cli import app
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.refresh import refresh_ledger

_FIXTURE = Path(__file__).parent / "fixtures" / "mari_card.pdf"


def _intent(kind: str, anchors: dict, payload: dict) -> str:
    return hashlib.sha256(f"{kind}{anchors}{payload}".encode()).hexdigest()


class CardFacilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "fincl.sqlite3"
        self.evidence = self.root / "evidence"
        DocumentImportService.plaintext_phase2(
            self.database, self.evidence
        ).import_mari_card(_FIXTURE)
        refresh_ledger(self.database)
        connection = sqlite3.connect(self.database)
        try:
            self.account_id = connection.execute(
                "SELECT account_id FROM statements LIMIT 1"
            ).fetchone()[0]
            self.evidence_sha = connection.execute(
                "SELECT evidence_sha256 FROM statements LIMIT 1"
            ).fetchone()[0]
        finally:
            connection.close()

    def _record(self, kind: str, anchors: dict, payload: dict):
        return record_card_decision(
            self.database,
            request_id=str(uuid.uuid4()),
            intent_hash=_intent(kind, anchors, payload),
            decision_kind=kind,
            anchors=anchors,
            payload=payload,
        )

    def _rows(self, table: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            connection.close()

    def _facility(self, *, currency="SGD", accounts=None):
        facility_id = str(uuid.uuid4())
        self._record(
            "CARD_FACILITY_SET",
            {"facility": facility_id},
            {
                "label": "Mari shared limit",
                "account_ids": accounts or [self.account_id],
                "currency": currency,
                "reason": "Issuer states one limit across the card pockets",
            },
        )
        return facility_id

    # -- projection ------------------------------------------------------- #

    def test_facility_and_membership_project_and_bind(self) -> None:
        self._facility()
        facilities = self._rows("card_facilities")
        self.assertEqual(len(facilities), 1)
        self.assertEqual(facilities[0]["currency"], "SGD")
        self.assertEqual(facilities[0]["bound"], 1)
        members = self._rows("card_facility_accounts")
        self.assertEqual([m["account_id"] for m in members], [self.account_id])
        self.assertEqual(members[0]["bound"], 1)

    def test_facility_with_a_non_card_account_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._facility(accounts=["not-a-card-account"])

    def test_same_account_in_two_active_facilities_is_refused(self) -> None:
        self._facility()
        with self.assertRaises(FinCLError):
            self._facility()

    def test_limit_observation_inherits_currency_and_labels_source(self) -> None:
        facility_id = self._facility()
        self._record(
            "CARD_LIMIT_OBSERVATION_SET",
            {"facility": facility_id, "observation": str(uuid.uuid4())},
            {
                "observed_on": "2026-07-21",
                "limit": "500.00",
                "source_ref": self.evidence_sha,
                "reason": "Printed on the July statement",
            },
        )
        self._record(
            "CARD_LIMIT_OBSERVATION_SET",
            {"facility": facility_id, "observation": str(uuid.uuid4())},
            {
                "observed_on": "2026-09-01",
                "limit": "800.00",
                "source_ref": None,
                "reason": "Operator raised the limit via the app; no statement yet",
            },
        )
        observations = sorted(
            self._rows("card_limit_observations"), key=lambda r: r["observed_on"]
        )
        self.assertEqual(observations[0]["currency"], "SGD")
        self.assertEqual(observations[0]["source_kind"], "statement-evidence")
        self.assertEqual(observations[1]["source_kind"], "operator-declared")

    def test_retiring_a_facility_with_observations_is_refused(self) -> None:
        facility_id = self._facility()
        self._record(
            "CARD_LIMIT_OBSERVATION_SET",
            {"facility": facility_id, "observation": str(uuid.uuid4())},
            {
                "observed_on": "2026-07-21",
                "limit": "500.00",
                "source_ref": None,
                "reason": "declared",
            },
        )
        with self.assertRaises(FinCLError):
            self._record(
                "CARD_FACILITY_RETIRE",
                {"facility": facility_id},
                {"reason": "closing the account"},
            )

    # -- repayment plan ----------------------------------------------------- #

    def test_a_repayment_plan_is_declared_shown_and_removed(self) -> None:
        from fincl.cards.queries import card_detail

        self._record("CARD_REPAYMENT_PLAN_SET", {"account": self.account_id},
                     {"rule": "pay_in_full", "reason": "I clear the card each month now"})
        refresh_ledger(self.database)
        plan = card_detail(self.database, self.account_id, as_of="2026-08-01")["repayment_plan"]
        self.assertEqual((plan["rule"], plan["reason"]), ("pay_in_full", "I clear the card each month now"))

        self._record("CARD_REPAYMENT_PLAN_RETIRE", {"account": self.account_id}, {"reason": "Changed my mind"})
        refresh_ledger(self.database)
        self.assertIsNone(card_detail(self.database, self.account_id, as_of="2026-08-01")["repayment_plan"])
        with self.assertRaises(FinCLError):
            self._record("CARD_REPAYMENT_PLAN_RETIRE", {"account": self.account_id}, {"reason": "again"})

    def test_a_repayment_plan_is_refused_for_a_bad_rule_or_a_non_card_account(self) -> None:
        for anchors, payload in (
            ({"account": self.account_id}, {"rule": "minimum_only", "reason": "x"}),
            ({"account": self.account_id}, {"rule": "pay_in_full"}),
            ({"account": "not-a-card"}, {"rule": "pay_in_full", "reason": "x"}),
        ):
            with self.subTest(anchors=anchors, payload=payload), self.assertRaises(FinCLError):
                self._record("CARD_REPAYMENT_PLAN_SET", anchors, payload)

    # -- utilization view -------------------------------------------------- #

    def test_card_detail_reports_dated_limit_basis_and_utilization(self) -> None:
        from fincl.cards.queries import card_detail

        facility_id = self._facility()
        self._record(
            "CARD_LIMIT_OBSERVATION_SET",
            {"facility": facility_id, "observation": str(uuid.uuid4())},
            {
                "observed_on": "2026-07-01",
                "limit": "500.00",
                "source_ref": self.evidence_sha,
                "reason": "July statement print",
            },
        )
        refresh_ledger(self.database)
        data = card_detail(self.database, self.account_id, as_of="2026-08-01")
        self.assertEqual(data["facility"]["facility_id"], facility_id)
        self.assertEqual(data["facility"]["account_ids"], [self.account_id])
        self.assertEqual(data["limit_basis"]["value"], "500.00")
        self.assertEqual(data["limit_basis"]["observed_on"], "2026-07-01")
        self.assertEqual(data["limit_basis"]["source_kind"], "statement-evidence")
        util = data["utilization"]
        self.assertTrue(util["available"])
        self.assertFalse(util["over_limit"])
        # closing liability 94.00 / limit 500.00 = 0.1880
        self.assertEqual(util["ratio"], "0.1880")
        self.assertEqual(util["currency"], "SGD")

    def test_utilization_unavailable_without_a_facility_falls_back_to_statement(self) -> None:
        from fincl.cards.queries import card_detail

        data = card_detail(self.database, self.account_id, as_of="2026-08-01")
        self.assertIsNone(data["facility"])
        # Falls back to the printed statement limit, labelled as statement utilization.
        self.assertEqual(data["utilization"]["basis"], "statement utilization")
        self.assertEqual(data["utilization"]["ratio"], "0.1880")

    # -- API + CLI ------------------------------------------------------- #

    def test_cli_facility_and_limit_commands(self) -> None:
        runner = CliRunner()
        facility_id = str(uuid.uuid4())
        set_result = runner.invoke(
            app,
            [
                "cards", "facility-set", "--facility", facility_id,
                "--label", "Mari", "--currency", "SGD",
                "--account", self.account_id,
                "--reason", "issuer shared-limit scope",
                "--database", str(self.database), "--json",
            ],
        )
        self.assertEqual(set_result.exit_code, 0, set_result.output)
        observe = runner.invoke(
            app,
            [
                "cards", "limit-observe", "--facility", facility_id,
                "--observed-on", "2026-07-21", "--limit", "500.00",
                "--reason", "printed", "--database", str(self.database), "--json",
            ],
        )
        self.assertEqual(observe.exit_code, 0, observe.output)
        self.assertEqual(len(self._rows("card_limit_observations")), 1)


if __name__ == "__main__":
    unittest.main()
