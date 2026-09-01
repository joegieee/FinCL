"""Deterministic branded DBS<->Revolut automatic linking.

These tests seed the minimum ledger rows the linker reads and exercise
``refresh_automatic_transfer_links`` directly, so the branded description
predicates, global mutual uniqueness, and projection semantics are covered
without a PDF/CSV corpus.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fincl.db import connect_sqlite, initialize_schema
from fincl.reconciliation import (
    AMBIGUOUS_MUTUAL_MATCH,
    DBS_MARIBANK_PAYNOW_TOPUP_LINK_VERSION,
    DBS_PAYLAH_LINK_VERSION,
    DBS_REVOLUT_CARD_TOPUP_LINK_VERSION,
    DBS_REVOLUT_LEGACY_LINK_VERSION,
    DBS_REVOLUT_OUTBOUND_TRANSFER_LINK_VERSION,
    DBS_REVOLUT_PAYNOW_TOPUP_LINK_VERSION,
    MARI_SAVINGS_CARD_REPAYMENT_LINK_VERSION,
    refresh_automatic_transfer_links,
)

CARD_DESCRIPTION = "Debit Card Transaction\nREVOLUT**0000* SINGAPORE SGP 27JAN\n4000-0000-0000-0002"
PAYNOW_DESCRIPTION = (
    "Advice FAST Payment / Receipt\nPAYNOW TRANSFER 1000001\n"
    "TO: REVOLUT TECHNOLOGIES SINGAPORE\n000000000000001\nOTHER"
)
PERSONAL_DESCRIPTION = (
    "Advice FAST Payment / Receipt\nPAYNOW TRANSFER 1000002\nTO: SAM\nPAYNOW TRANSFER\nOTHER"
)
GENERIC_DESCRIPTION = (
    "Advice FAST Payment / Receipt\nPAYNOW TRANSFER 1000003\n"
    "TO: ACME TRAINING ACADEMY\nINV-0000000001\nOTHER"
)
PAYLAH_TOPUP_DBS_DESCRIPTION = (
    "Advice Funds Transfer\nTOP-UP TO PAYLAH! :\nPAT\nVALUE DATE : 03/05/2026"
)


def _insert(connection: sqlite3.Connection, table: str, **columns: object) -> None:
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({names}) VALUES ({placeholders})", tuple(columns.values())
    )


class _Ledger:
    """Small builder for the exact rows the transfer linker joins over."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._seq = 0
        self._rev_statements: dict[str, str] = {}
        _insert(
            connection,
            "accounts",
            id="acct-dbs",
            institution="DBS",
            source_type="DBS_CONSOLIDATED",
            display_name="DBS Multi",
            masked_identifier="****1111",
        )
        _insert(
            connection,
            "accounts",
            id="acct-rev",
            institution="Revolut",
            source_type="REVOLUT_PDF",
            display_name="Revolut",
            masked_identifier="****2222",
        )
        _insert(
            connection,
            "accounts",
            id="acct-other-csv",
            institution="Other",
            source_type="OTHER_CSV",
            display_name="Other CSV",
            masked_identifier="****3333",
        )
        for account_id, source_type, name in (
            ("acct-mari-sav", "MARI_SAVINGS", "MariBank Savings"),
            ("acct-mari-card", "MARI_CARD", "Mari Credit Card"),
            ("acct-paylah", "DBS_PAYLAH", "DBS PayLah!"),
        ):
            _insert(
                connection,
                "accounts",
                id=account_id,
                institution="MariBank",
                source_type=source_type,
                display_name=name,
                masked_identifier="****4444",
            )

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def _evidence(self) -> str:
        sha = f"{self._seq:064d}"
        _insert(
            self.connection,
            "source_content",
            sha256=sha,
            byte_size=10,
            media_type="application/octet-stream",
            evidence_key=self._next("key"),
            storage_kind="PLAINTEXT",
            original_path="/tmp/x",
            last_observed_path="/tmp/x",
            first_seen_at="2026-01-01T00:00:00Z",
            last_seen_at="2026-01-01T00:00:00Z",
            integrity_status="VERIFIED",
        )
        return sha

    def dbs_statement(self, *, selection_state: str = "SELECTED") -> tuple[str, str]:
        run_id = self._next("dbs-run")
        sha = self._evidence()
        _insert(
            self.connection,
            "extraction_runs",
            id=run_id,
            evidence_sha256=sha,
            source_profile_version="dbs-consolidated-2026-v1",
            parser_version="p1",
            extraction_policy_version="e1",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            status="ACCEPTED",
            selection_state="SELECTED",
        )
        statement_id = self._next("stmt")
        _insert(
            self.connection,
            "statements",
            id=statement_id,
            extraction_run_id=run_id,
            evidence_sha256=sha,
            account_id="acct-dbs",
            institution="DBS",
            source_type="DBS_CONSOLIDATED",
            statement_date="2026-01-31",
            statement_identity=self._next("ident"),
            selection_state=selection_state,
            conflict_state="NONE",
        )
        return run_id, statement_id

    def dbs_entry(
        self,
        run_id: str,
        statement_id: str,
        *,
        description: str,
        amount: str = "-50.00",
        posted_date: str = "2026-01-27",
        currency_pocket: str = "SGD",
        event_kind: str | None = None,
        economic_direction: str = "OUT",
        analytics_eligible: int = 1,
    ) -> str:
        coefficient = int(round(float(amount) * 100))
        record_id = self._next("dbs-rec")
        sha = self.connection.execute(
            "SELECT evidence_sha256 FROM extraction_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
        self._seq += 1
        anchor_seq = self._seq
        _insert(
            self.connection,
            "document_records",
            id=record_id,
            extraction_run_id=run_id,
            evidence_sha256=sha,
            statement_id=statement_id,
            source_profile_version="dbs-consolidated-2026-v1",
            parser_version="p1",
            source_anchor=f"anchor-{anchor_seq}",
            extraction_ordinal=anchor_seq,
            page_number=1,
            record_kind="TRANSACTION",
            raw_text=description,
            raw_fields_json="{}",
            description_raw=description,
            confidence="HIGH",
            required_fields_complete=1,
        )
        entry_id = self._next("dbs-entry")
        if event_kind is None:
            event_kind = (
                "CARD_PAYMENT"
                if description.upper().startswith("DEBIT CARD TRANSACTION")
                else "TOPUP"
                if "TOP-UP TO PAYLAH" in description.upper()
                else "TRANSFER"
            )
        _insert(
            self.connection,
            "ledger_entries",
            id=entry_id,
            source_family="DOCUMENT",
            extraction_run_id=run_id,
            evidence_sha256=sha,
            document_record_id=record_id,
            statement_id=statement_id,
            natural_anchor=f"{sha}:anchor-{anchor_seq}:{anchor_seq}",
            account_id="acct-dbs",
            currency_pocket=currency_pocket,
            effective_at=posted_date,
            effective_precision="DAY",
            amount_text=amount,
            amount_coefficient=coefficient,
            amount_scale=2,
            event_kind=event_kind,
            economic_direction=economic_direction,
            lifecycle_state="COMPLETED",
            analytics_eligible=analytics_eligible,
        )
        return entry_id

    def paylah_statement(self, *, selection_state: str = "SELECTED") -> tuple[str, str]:
        run_id = self._next("paylah-run")
        sha = self._evidence()
        _insert(
            self.connection,
            "extraction_runs",
            id=run_id,
            evidence_sha256=sha,
            source_profile_version="dbs-paylah-2026-v1",
            parser_version="p1",
            extraction_policy_version="e1",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            status="ACCEPTED",
            selection_state="SELECTED",
        )
        statement_id = self._next("paylah-stmt")
        _insert(
            self.connection,
            "statements",
            id=statement_id,
            extraction_run_id=run_id,
            evidence_sha256=sha,
            account_id="acct-paylah",
            institution="DBS",
            source_type="DBS_PAYLAH",
            statement_date="2026-01-31",
            statement_identity=self._next("paylah-ident"),
            selection_state=selection_state,
            conflict_state="NONE",
        )
        return run_id, statement_id

    def paylah_topup(
        self,
        run_id: str,
        statement_id: str,
        *,
        amount: str = "2.00",
        posted_date: str = "2026-01-27",
        description: str = "TOP UP WALLET FROM MY ACCOUNT",
        analytics_eligible: int = 1,
    ) -> str:
        coefficient = int(round(float(amount) * 100))
        sha = self.connection.execute(
            "SELECT evidence_sha256 FROM extraction_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
        self._seq += 1
        anchor_seq = self._seq
        record_id = self._next("paylah-rec")
        _insert(
            self.connection,
            "document_records",
            id=record_id,
            extraction_run_id=run_id,
            evidence_sha256=sha,
            statement_id=statement_id,
            source_profile_version="dbs-paylah-2026-v1",
            parser_version="p1",
            source_anchor=f"paylah-anchor-{anchor_seq}",
            extraction_ordinal=anchor_seq,
            page_number=1,
            record_kind="TRANSACTION",
            raw_text=description,
            raw_fields_json="{}",
            description_raw=description,
            confidence="HIGH",
            required_fields_complete=1,
        )
        entry_id = self._next("paylah-entry")
        _insert(
            self.connection,
            "ledger_entries",
            id=entry_id,
            source_family="DOCUMENT",
            extraction_run_id=run_id,
            evidence_sha256=sha,
            document_record_id=record_id,
            statement_id=statement_id,
            natural_anchor=f"{sha}:paylah-anchor-{anchor_seq}:{anchor_seq}",
            account_id="acct-paylah",
            currency_pocket="SGD",
            effective_at=posted_date,
            effective_precision="DAY",
            amount_text=amount,
            amount_coefficient=coefficient,
            amount_scale=2,
            event_kind="TOPUP",
            economic_direction="IN",
            lifecycle_state="COMPLETED",
            analytics_eligible=analytics_eligible,
        )
        return entry_id

    def mari_statement(
        self, source_type: str, *, selection_state: str = "SELECTED"
    ) -> tuple[str, str]:
        run_id = self._next("mari-run")
        sha = self._evidence()
        _insert(
            self.connection,
            "extraction_runs",
            id=run_id,
            evidence_sha256=sha,
            source_profile_version="mari-2026-v1",
            parser_version="p1",
            extraction_policy_version="e1",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            status="ACCEPTED",
            selection_state="SELECTED",
        )
        statement_id = self._next("mari-stmt")
        _insert(
            self.connection,
            "statements",
            id=statement_id,
            extraction_run_id=run_id,
            evidence_sha256=sha,
            account_id="acct-mari-sav" if source_type == "MARI_SAVINGS" else "acct-mari-card",
            institution="MariBank",
            source_type=source_type,
            statement_date="2026-01-31",
            statement_identity=self._next("mari-ident"),
            selection_state=selection_state,
            conflict_state="NONE",
        )
        return run_id, statement_id

    def mari_entry(
        self,
        run_id: str,
        statement_id: str,
        *,
        source_type: str,
        description: str,
        amount: str,
        economic_direction: str,
        posted_date: str = "2026-01-27",
        event_kind: str = "TRANSFER",
        currency_pocket: str = "SGD",
        analytics_eligible: int = 1,
    ) -> str:
        coefficient = int(round(float(amount) * 100))
        sha = self.connection.execute(
            "SELECT evidence_sha256 FROM extraction_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
        self._seq += 1
        anchor_seq = self._seq
        record_id = self._next("mari-rec")
        _insert(
            self.connection,
            "document_records",
            id=record_id,
            extraction_run_id=run_id,
            evidence_sha256=sha,
            statement_id=statement_id,
            source_profile_version="mari-2026-v1",
            parser_version="p1",
            source_anchor=f"mari-anchor-{anchor_seq}",
            extraction_ordinal=anchor_seq,
            page_number=1,
            record_kind="TRANSACTION",
            raw_text=description,
            raw_fields_json="{}",
            description_raw=description,
            confidence="HIGH",
            required_fields_complete=1,
        )
        entry_id = self._next("mari-entry")
        _insert(
            self.connection,
            "ledger_entries",
            id=entry_id,
            source_family="DOCUMENT",
            extraction_run_id=run_id,
            evidence_sha256=sha,
            document_record_id=record_id,
            statement_id=statement_id,
            natural_anchor=f"{sha}:mari-anchor-{anchor_seq}:{anchor_seq}",
            account_id="acct-mari-sav" if source_type == "MARI_SAVINGS" else "acct-mari-card",
            currency_pocket=currency_pocket,
            effective_at=posted_date,
            effective_precision="DAY",
            amount_text=amount,
            amount_coefficient=coefficient,
            amount_scale=2,
            event_kind=event_kind,
            economic_direction=economic_direction,
            lifecycle_state="COMPLETED",
            analytics_eligible=analytics_eligible,
        )
        return entry_id

    def revolut_run(self, *, status: str = "ACCEPTED") -> str:
        run_id = self._next("rev-run")
        sha = self._evidence()
        _insert(
            self.connection,
            "extraction_runs",
            id=run_id,
            evidence_sha256=sha,
            source_profile_version="revolut-pdf-en-sg-2026-v1",
            parser_version="p1",
            extraction_policy_version="e1",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            status=status,
            selection_state="SELECTED" if status == "ACCEPTED" else "QUARANTINED",
        )
        statement_id = self._next("rev-stmt")
        _insert(
            self.connection,
            "statements",
            id=statement_id,
            extraction_run_id=run_id,
            evidence_sha256=sha,
            account_id="acct-rev",
            institution="Revolut",
            source_type="REVOLUT_PDF",
            statement_date="2026-01-31",
            statement_identity=self._next("rev-ident"),
            selection_state="SELECTED" if status == "ACCEPTED" else "QUARANTINED",
            conflict_state="NONE",
        )
        self._rev_statements[run_id] = statement_id
        return run_id

    def revolut_entry(
        self,
        run_id: str,
        *,
        amount: str = "50.00",
        completed_at: str = "2026-01-28",
        event_kind: str = "TOPUP",
        currency_pocket: str = "SGD",
        account_id: str = "acct-rev",
        economic_direction: str = "IN",
        analytics_eligible: int = 1,
        raw_fields_json: str = "{}",
    ) -> str:
        coefficient = int(round(float(amount) * 100))
        record_id = self._next("rev-rec")
        statement_id = self._rev_statements[run_id]
        sha = self.connection.execute(
            "SELECT evidence_sha256 FROM extraction_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
        self._seq += 1
        anchor_seq = self._seq
        _insert(
            self.connection,
            "document_records",
            id=record_id,
            extraction_run_id=run_id,
            evidence_sha256=sha,
            statement_id=statement_id,
            source_profile_version="revolut-pdf-en-sg-2026-v1",
            parser_version="p1",
            source_anchor=f"rev-anchor-{anchor_seq}",
            extraction_ordinal=anchor_seq,
            page_number=1,
            record_kind="TRANSACTION",
            raw_text="row",
            raw_fields_json=raw_fields_json,
            description_raw="Balance top-up",
            confidence="HIGH",
            required_fields_complete=1,
        )
        entry_id = self._next("rev-entry")
        _insert(
            self.connection,
            "ledger_entries",
            id=entry_id,
            source_family="DOCUMENT",
            extraction_run_id=run_id,
            evidence_sha256=sha,
            document_record_id=record_id,
            statement_id=statement_id,
            natural_anchor=f"{sha}:rev-anchor-{anchor_seq}:{anchor_seq}",
            account_id=account_id,
            currency_pocket=currency_pocket,
            effective_at=completed_at,
            effective_precision="DAY",
            amount_text=amount,
            amount_coefficient=coefficient,
            amount_scale=2,
            fee_text="0.00",
            fee_coefficient=0,
            fee_scale=2,
            lifecycle_state="COMPLETED",
            event_kind=event_kind,
            economic_direction=economic_direction,
            analytics_eligible=analytics_eligible,
        )
        return entry_id


class BrandedTransferLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "ledger.sqlite3"
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.ledger = _Ledger(self.connection)

    def _links(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT method_version, link_kind, status, left_entry_id, right_entry_id, "
            "implied_rate_numerator, implied_rate_denominator, evidence_json "
            "FROM transfer_links ORDER BY method_version, status"
        ).fetchall()

    def test_card_topup_links_one_eligible_revolut_credit(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        dbs = self.ledger.dbs_entry(run_id, statement_id, description=CARD_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        rev = self.ledger.revolut_entry(rev_run)

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 1)
        links = self._links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["method_version"], DBS_REVOLUT_CARD_TOPUP_LINK_VERSION)
        self.assertEqual(links[0]["status"], "CONFIRMED")
        self.assertEqual((links[0]["left_entry_id"], links[0]["right_entry_id"]), (dbs, rev))

    def test_paynow_topup_links_one_eligible_revolut_credit(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(run_id, statement_id, description=PAYNOW_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run, event_kind="TRANSFER")

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 1)
        links = self._links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["method_version"], DBS_REVOLUT_PAYNOW_TOPUP_LINK_VERSION)
        self.assertEqual(links[0]["status"], "CONFIRMED")

    def test_personal_name_transfer_never_enters_a_candidate_set(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(run_id, statement_id, description=PERSONAL_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run)

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 0)
        self.assertEqual(self._links(), [])

    def test_generic_paynow_transfer_is_out_of_scope(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(run_id, statement_id, description=GENERIC_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run)

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(self._links(), [])

    def test_revolut_outbound_transfer_links_its_printed_dbs_settlement(self) -> None:
        dbs_run, dbs_statement = self.ledger.dbs_statement()
        dbs = self.ledger.dbs_entry(
            dbs_run,
            dbs_statement,
            description="Debit Card Transaction\n4000-0000-0000-0002",
            amount="60.00",
            posted_date="2026-06-06",
            event_kind="CARD_PAYMENT",
            economic_direction="IN",
        )
        revolut_run = self.ledger.revolut_run()
        revolut = self.ledger.revolut_entry(
            revolut_run,
            amount="-44.80",
            completed_at="2026-06-06",
            event_kind="TRANSFER",
            currency_pocket="USD",
            economic_direction="OUT",
            raw_fields_json='{"counterpart":"S$60.00"}',
        )

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 1)
        links = self._links()
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual(link["method_version"], DBS_REVOLUT_OUTBOUND_TRANSFER_LINK_VERSION)
        self.assertEqual(link["link_kind"], "CURRENCY_EXCHANGE")
        self.assertEqual((link["left_entry_id"], link["right_entry_id"]), (revolut, dbs))
        self.assertEqual((link["implied_rate_numerator"], link["implied_rate_denominator"]), (75, 56))

    def test_branded_linkers_refuse_non_sgd_pockets(self) -> None:
        """The declared branded pair is DBS SGD to Revolut SGD, never USD."""
        for description in (CARD_DESCRIPTION, PAYNOW_DESCRIPTION):
            with self.subTest(description=description.splitlines()[0]):
                run_id, statement_id = self.ledger.dbs_statement()
                self.ledger.dbs_entry(
                    run_id,
                    statement_id,
                    description=description,
                    amount="-50.00",
                    currency_pocket="USD",
                )
                rev_run = self.ledger.revolut_run()
                self.ledger.revolut_entry(
                    rev_run,
                    amount="50.00",
                    currency_pocket="USD",
                )

                self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
                self.assertEqual(self._links(), [])

    def test_exchange_linker_refuses_a_non_revolut_statement(self) -> None:
        """Exchange pairing is scoped to REVOLUT_PDF statements, not any EXCHANGE row."""
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(
            run_id,
            statement_id,
            description="FX CONVERSION OUT",
            amount="-100.00",
            posted_date="2026-01-27",
            event_kind="EXCHANGE",
        )
        self.ledger.dbs_entry(
            run_id,
            statement_id,
            description="FX CONVERSION IN",
            amount="75.00",
            posted_date="2026-01-27",
            currency_pocket="USD",
            event_kind="EXCHANGE",
        )

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(self._links(), [])

    def test_cross_matcher_collision_rejects_both_candidates(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        card = self.ledger.dbs_entry(
            run_id, statement_id, description=CARD_DESCRIPTION, posted_date="2026-01-27"
        )
        paynow = self.ledger.dbs_entry(
            run_id, statement_id, description=PAYNOW_DESCRIPTION, posted_date="2026-01-28"
        )
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run, completed_at="2026-01-28 09:00:00")

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 0)
        links = self._links()
        self.assertEqual({row["status"] for row in links}, {"PROPOSED"})
        self.assertEqual(
            {row["method_version"] for row in links},
            {DBS_REVOLUT_CARD_TOPUP_LINK_VERSION, DBS_REVOLUT_PAYNOW_TOPUP_LINK_VERSION},
        )
        self.assertEqual({row["left_entry_id"] for row in links}, {card, paynow})
        for row in links:
            self.assertEqual(
                json.loads(row["evidence_json"])["rejection"]["code"], AMBIGUOUS_MUTUAL_MATCH
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
            ).fetchone()[0],
            0,
        )

    def test_within_matcher_duplicate_candidates_reject(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(run_id, statement_id, description=CARD_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run, completed_at="2026-01-27 09:00:00")
        self.ledger.revolut_entry(rev_run, completed_at="2026-01-28 09:00:00")

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 0)
        links = self._links()
        self.assertEqual(len(links), 2)
        self.assertEqual({row["status"] for row in links}, {"PROPOSED"})

    def test_requalification_after_a_later_refresh(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(run_id, statement_id, description=CARD_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run, completed_at="2026-01-27 09:00:00")
        losing = self.ledger.revolut_entry(rev_run, completed_at="2026-01-28 09:00:00")

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
            ).fetchone()[0],
            0,
        )

        # The second Revolut credit loses eligibility; the pair is now unique.
        self.connection.execute(
            "UPDATE ledger_entries SET analytics_eligible = 0 WHERE id = ?", (losing,)
        )

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 1)
        links = self._links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["status"], "CONFIRMED")

    def test_refresh_is_idempotent_and_reports_zero_on_no_change(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(run_id, statement_id, description=CARD_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run)

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 1)
        first = self._links()
        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        second = self._links()
        self.assertEqual([tuple(row) for row in first], [tuple(row) for row in second])

    def test_unselected_dbs_statement_produces_no_link(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement(selection_state="CANDIDATE")
        self.ledger.dbs_entry(run_id, statement_id, description=CARD_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run)

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(self._links(), [])

    def test_out_of_window_credit_does_not_link(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(
            run_id, statement_id, description=CARD_DESCRIPTION, posted_date="2026-01-27"
        )
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run, completed_at="2026-02-05 09:00:00")

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(self._links(), [])

    def test_card_topup_window_is_measured_from_the_description_date(self) -> None:
        """DBS posts the debit days later; the merchant line's DDMMM is the truth."""
        run_id, statement_id = self.ledger.dbs_statement()
        dbs = self.ledger.dbs_entry(
            run_id,
            statement_id,
            description="Debit Card Transaction\nREVOLUT**0000* SINGAPORE SGP 16MAR\n4000-0000-0000-0002",
            amount="-20.00",
            posted_date="2026-03-18",
        )
        rev_run = self.ledger.revolut_run()
        on_txn_date = self.ledger.revolut_entry(
            rev_run, amount="20.00", completed_at="2026-03-16 12:00:00"
        )
        # Same amount, inside the old +/-2 posted-date window, but not the charge.
        self.ledger.revolut_entry(
            rev_run, amount="20.00", completed_at="2026-03-20 12:00:00"
        )

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 1)
        links = self._links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["status"], "CONFIRMED")
        self.assertEqual(
            (links[0]["left_entry_id"], links[0]["right_entry_id"]), (dbs, on_txn_date)
        )

    def test_two_same_amount_card_topups_split_by_their_transaction_dates(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        first = self.ledger.dbs_entry(
            run_id,
            statement_id,
            description="Debit Card Transaction\nREVOLUT**0000* SINGAPORE SGP 16MAR\n4000-0000-0000-0002",
            amount="-20.00",
            posted_date="2026-03-18",
        )
        second = self.ledger.dbs_entry(
            run_id,
            statement_id,
            description="Debit Card Transaction\nREVOLUT**0000* SINGAPORE SGP 20MAR\n4000-0000-0000-0002",
            amount="-20.00",
            posted_date="2026-03-24",
        )
        rev_run = self.ledger.revolut_run()
        rev_first = self.ledger.revolut_entry(
            rev_run, amount="20.00", completed_at="2026-03-16 12:00:00"
        )
        rev_second = self.ledger.revolut_entry(
            rev_run, amount="20.00", completed_at="2026-03-20 12:00:00"
        )

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 2)
        links = self._links()
        self.assertEqual({row["status"] for row in links}, {"CONFIRMED"})
        self.assertEqual(
            sorted((row["left_entry_id"], row["right_entry_id"]) for row in links),
            sorted([(first, rev_first), (second, rev_second)]),
        )

    def test_card_topup_without_a_date_token_falls_back_to_the_posted_date(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        dbs = self.ledger.dbs_entry(
            run_id,
            statement_id,
            description="Debit Card Transaction\nREVOLUT**0000* SINGAPORE SGP\n4000-0000-0000-0002",
            amount="-20.00",
            posted_date="2026-03-18",
        )
        rev_run = self.ledger.revolut_run()
        rev = self.ledger.revolut_entry(
            rev_run, amount="20.00", completed_at="2026-03-19 09:00:00"
        )

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 1)
        links = self._links()
        self.assertEqual((links[0]["left_entry_id"], links[0]["right_entry_id"]), (dbs, rev))

    def test_card_topup_ignores_an_implausible_description_date(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(
            run_id,
            statement_id,
            description="Debit Card Transaction\nREVOLUT**0000* SINGAPORE SGP 01JAN\n4000-0000-0000-0002",
            amount="-20.00",
            posted_date="2026-06-15",
        )
        rev_run = self.ledger.revolut_run()
        # A credit on the misread date must not be pulled in from six months away.
        self.ledger.revolut_entry(
            rev_run, amount="20.00", completed_at="2026-01-01 09:00:00"
        )
        near_posted = self.ledger.revolut_entry(
            rev_run, amount="20.00", completed_at="2026-06-14 09:00:00"
        )

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 1)
        links = self._links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["right_entry_id"], near_posted)

    def test_card_topup_transaction_date_crosses_the_year_boundary(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        dbs = self.ledger.dbs_entry(
            run_id,
            statement_id,
            description="Debit Card Transaction\nREVOLUT**0000* SINGAPORE SGP 31DEC\n4000-0000-0000-0002",
            amount="-20.00",
            posted_date="2026-01-02",
        )
        rev_run = self.ledger.revolut_run()
        rev = self.ledger.revolut_entry(
            rev_run, amount="20.00", completed_at="2025-12-31 22:00:00"
        )

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 1)
        links = self._links()
        self.assertEqual((links[0]["left_entry_id"], links[0]["right_entry_id"]), (dbs, rev))

    def test_legacy_broad_links_are_cleared_by_refresh(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        dbs = self.ledger.dbs_entry(run_id, statement_id, description=CARD_DESCRIPTION)
        rev_run = self.ledger.revolut_run()
        rev = self.ledger.revolut_entry(rev_run)
        _insert(
            self.connection,
            "transfer_links",
            id="legacy-link",
            left_entry_id=dbs,
            right_entry_id=rev,
            link_kind="SAME_CURRENCY_TRANSFER",
            left_currency="SGD",
            left_amount_text="50.00",
            left_amount_coefficient=5000,
            left_amount_scale=2,
            right_currency="SGD",
            right_amount_text="50.00",
            right_amount_coefficient=5000,
            right_amount_scale=2,
            method_version=DBS_REVOLUT_LEGACY_LINK_VERSION,
            status="CONFIRMED",
            evidence_json="{}",
            created_at="2026-01-01T00:00:00Z",
        )

        refresh_automatic_transfer_links(self.connection)

        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM transfer_links WHERE method_version = ?",
                (DBS_REVOLUT_LEGACY_LINK_VERSION,),
            ).fetchone()[0],
            0,
        )
        links = self._links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["method_version"], DBS_REVOLUT_CARD_TOPUP_LINK_VERSION)

    def test_confirmed_links_always_reference_two_eligible_legs(self) -> None:
        run_id, statement_id = self.ledger.dbs_statement()
        self.ledger.dbs_entry(run_id, statement_id, description=CARD_DESCRIPTION)
        self.ledger.dbs_entry(run_id, statement_id, description=PAYNOW_DESCRIPTION, posted_date="2026-02-10")
        rev_run = self.ledger.revolut_run()
        self.ledger.revolut_entry(rev_run, completed_at="2026-01-28 09:00:00")
        self.ledger.revolut_entry(rev_run, amount="50.00", completed_at="2026-02-10 09:00:00", event_kind="TRANSFER")

        refresh_automatic_transfer_links(self.connection)

        rows = self.connection.execute(
            """
            SELECT l.id
            FROM transfer_links l
            WHERE l.status = 'CONFIRMED' AND l.link_kind = 'SAME_CURRENCY_TRANSFER'
              AND NOT EXISTS (
                SELECT 1 FROM ledger_entries d WHERE d.id = l.left_entry_id AND d.analytics_eligible = 1
              )
            """
        ).fetchall()
        self.assertEqual(rows, [])


class PaylahTopupBalancedGroupTests(unittest.TestCase):
    """Same-day, same-amount DBS->PayLah top-up groups resolve without review.

    The DBS ``TOP-UP TO PAYLAH!`` line has no reference and the PayLah
    ``TOP UP WALLET FROM MY ACCOUNT`` line's ``REF NO:. PLPE...`` is not on the
    DBS side, so identical same-day top-ups have no disambiguating key. Because
    both legs are the owner's own SGD accounts and every perfect matching nets
    identically, a balanced complete group is auto-confirmed on one deterministic
    bijection instead of being parked as ``AMBIGUOUS_MUTUAL_MATCH``.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "ledger.sqlite3"
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.ledger = _Ledger(self.connection)
        self.dbs_run, self.dbs_stmt = self.ledger.dbs_statement()
        self.paylah_run, self.paylah_stmt = self.ledger.paylah_statement()

    def _links(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT status, left_entry_id, right_entry_id, evidence_json "
            "FROM transfer_links WHERE method_version = ? "
            "ORDER BY left_entry_id, right_entry_id",
            (DBS_PAYLAH_LINK_VERSION,),
        ).fetchall()

    def _dbs_topup(self, amount: str, date: str = "2026-05-03") -> str:
        return self.ledger.dbs_entry(
            self.dbs_run,
            self.dbs_stmt,
            description=PAYLAH_TOPUP_DBS_DESCRIPTION,
            amount=f"-{amount}",
            posted_date=date,
        )

    def _paylah_topup(self, amount: str, date: str = "2026-05-03") -> str:
        return self.ledger.paylah_topup(
            self.paylah_run, self.paylah_stmt, amount=amount, posted_date=date
        )

    def test_two_identical_same_day_topups_auto_confirm_a_bijection(self) -> None:
        dbs = [self._dbs_topup("2.00"), self._dbs_topup("2.00")]
        paylah = [self._paylah_topup("2.00"), self._paylah_topup("2.00")]

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 2)
        links = self._links()
        self.assertEqual([row["status"] for row in links], ["CONFIRMED", "CONFIRMED"])
        expected = list(zip(sorted(dbs), sorted(paylah), strict=True))
        self.assertEqual(
            sorted((row["left_entry_id"], row["right_entry_id"]) for row in links),
            sorted(expected),
        )
        for row in links:
            evidence = json.loads(row["evidence_json"])
            self.assertFalse(evidence["unique_candidate"])
            self.assertTrue(evidence["balanced_group"]["arbitrary_pairing"])
            self.assertEqual(evidence["balanced_group"]["size"], 2)
            self.assertNotIn("rejection", evidence)

    def test_a_lone_topup_still_confirms_as_a_unique_candidate(self) -> None:
        dbs = self._dbs_topup("3.50")
        paylah = self._paylah_topup("3.50")

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 1)
        links = self._links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["status"], "CONFIRMED")
        self.assertEqual((links[0]["left_entry_id"], links[0]["right_entry_id"]), (dbs, paylah))
        evidence = json.loads(links[0]["evidence_json"])
        self.assertTrue(evidence["unique_candidate"])
        self.assertNotIn("balanced_group", evidence)

    def test_an_unbalanced_group_stays_proposed_for_review(self) -> None:
        self._dbs_topup("2.00")
        self._dbs_topup("2.00")
        self._paylah_topup("2.00")

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 0)
        links = self._links()
        self.assertEqual({row["status"] for row in links}, {"PROPOSED"})
        for row in links:
            evidence = json.loads(row["evidence_json"])
            self.assertEqual(evidence["rejection"]["code"], AMBIGUOUS_MUTUAL_MATCH)
            self.assertNotIn("balanced_group", evidence)

    def test_a_balanced_group_and_a_distinct_amount_both_resolve(self) -> None:
        two = [self._dbs_topup("2.00"), self._dbs_topup("2.00")]
        five = self._dbs_topup("5.00")
        p_two = [self._paylah_topup("2.00"), self._paylah_topup("2.00")]
        p_five = self._paylah_topup("5.00")

        created = refresh_automatic_transfer_links(self.connection)

        self.assertEqual(created, 3)
        links = self._links()
        self.assertEqual([row["status"] for row in links], ["CONFIRMED"] * 3)
        pairs = {(row["left_entry_id"], row["right_entry_id"]) for row in links}
        self.assertIn((five, p_five), pairs)
        self.assertEqual(
            {p for p in pairs if p != (five, p_five)},
            set(zip(sorted(two), sorted(p_two), strict=True)),
        )

    def test_a_bound_journal_decision_keeps_its_group_for_the_overlay(self) -> None:
        dbs = [self._dbs_topup("2.00"), self._dbs_topup("2.00")]
        paylah = [self._paylah_topup("2.00"), self._paylah_topup("2.00")]
        anchors = {
            entry_id: self.connection.execute(
                "SELECT natural_anchor FROM ledger_entries WHERE id = ?", (entry_id,)
            ).fetchone()[0]
            for entry_id in (*dbs, *paylah)
        }
        # The operator pinned the cross pairing dbs[0] <-> paylah[1] by hand.
        _insert(
            self.connection,
            "link_decisions",
            left_anchor=anchors[dbs[0]],
            right_anchor=anchors[paylah[1]],
            anchor_version="v1",
            link_kind="SAME_CURRENCY_TRANSFER",
            decision="CONFIRM",
            policy_version="operator",
            journal_seq=1,
            snapshot="{}",
            bound=1,
        )

        created = refresh_automatic_transfer_links(self.connection)

        links = self._links()
        for row in links:
            self.assertNotIn("balanced_group", json.loads(row["evidence_json"]))
        confirmed = [
            (row["left_entry_id"], row["right_entry_id"])
            for row in links
            if row["status"] == "CONFIRMED"
        ]
        self.assertEqual(confirmed, [(dbs[0], paylah[1])])
        self.assertEqual(created, 1)

    def test_balanced_group_resolution_is_idempotent(self) -> None:
        self._dbs_topup("2.00")
        self._dbs_topup("2.00")
        self._paylah_topup("2.00")
        self._paylah_topup("2.00")

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 2)
        first = self._links()
        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(self._links(), first)


_DBS_TO_MARIBANK = (
    "Advice FAST Payment / Receipt\nPAYNOW TRANSFER 1000004\n"
    "TO: MARIBANK SINGAPORE PRIVATE LIMI\n{token}\nOTHER"
)


class MariTransferLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "ledger.sqlite3"
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.ledger = _Ledger(self.connection)

    def _links(self, method_version: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT status, left_entry_id, right_entry_id FROM transfer_links "
            "WHERE method_version = ? ORDER BY left_entry_id",
            (method_version,),
        ).fetchall()

    def _dbs_to_maribank(self, token: str, *, amount: str, posted_date: str = "2026-01-27") -> str:
        run_id, statement_id = self.ledger.dbs_statement()
        return self.ledger.dbs_entry(
            run_id,
            statement_id,
            description=_DBS_TO_MARIBANK.format(token=token),
            amount=amount,
            posted_date=posted_date,
        )

    def _savings_inflow(self, token: str, *, amount: str, posted_date: str = "2026-01-27") -> str:
        run_id, statement_id = self.ledger.mari_statement("MARI_SAVINGS")
        return self.ledger.mari_entry(
            run_id,
            statement_id,
            source_type="MARI_SAVINGS",
            description=f"TAN ALEX SYNTHETIC PayNow Transfer {token}",
            amount=amount,
            economic_direction="IN",
            posted_date=posted_date,
        )

    def test_dbs_maribank_paynow_links_on_the_shared_reference_token(self) -> None:
        left = self._dbs_to_maribank("SYN0TEST1REF2TOKEN3ABCDEF", amount="-180.00")
        right = self._savings_inflow("SYN0TEST1REF2tOkEn3abcdeF", amount="180.00")

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 1)
        links = self._links(DBS_MARIBANK_PAYNOW_TOPUP_LINK_VERSION)
        self.assertEqual(
            [(row["status"], row["left_entry_id"], row["right_entry_id"]) for row in links],
            [("CONFIRMED", left, right)],
        )

    def test_one_shared_token_is_split_by_the_exact_amount(self) -> None:
        big_left = self._dbs_to_maribank("SYN0TEST1REF2TOKEN3ABCDEF", amount="-180.00")
        small_left = self._dbs_to_maribank("SYN0TEST1REF2TOKEN3ABCDEF", amount="-50.00")
        big_right = self._savings_inflow("SYN0TEST1REF2TOKEN3ABCDEF", amount="180.00")
        small_right = self._savings_inflow("SYN0TEST1REF2TOKEN3ABCDEF", amount="50.00")

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 2)
        links = {
            (row["left_entry_id"], row["right_entry_id"])
            for row in self._links(DBS_MARIBANK_PAYNOW_TOPUP_LINK_VERSION)
            if row["status"] == "CONFIRMED"
        }
        self.assertEqual(links, {(big_left, big_right), (small_left, small_right)})

    def test_a_truncated_reference_token_still_matches_on_its_prefix(self) -> None:
        self._dbs_to_maribank("ZZ9FAKE8PAYNOW7REF6XYZ5Q4", amount="-96.42")
        self._savings_inflow("ZZ9FAKE8PAYNOW7REF6XYZ5", amount="96.42")

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 1)
        self.assertEqual(
            self._links(DBS_MARIBANK_PAYNOW_TOPUP_LINK_VERSION)[0]["status"], "CONFIRMED"
        )

    def test_a_maribank_inflow_without_a_reference_token_never_links(self) -> None:
        self._dbs_to_maribank("SYN0TEST1REF2TOKEN3ABCDEF", amount="-20.00")
        run_id, statement_id = self.ledger.mari_statement("MARI_SAVINGS")
        self.ledger.mari_entry(
            run_id,
            statement_id,
            source_type="MARI_SAVINGS",
            description="ALEX S TAN MoneySend Payment",
            amount="20.00",
            economic_direction="IN",
        )

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(self._links(DBS_MARIBANK_PAYNOW_TOPUP_LINK_VERSION), [])

    def test_savings_debit_links_the_same_day_card_repayment_credit(self) -> None:
        sav_run, sav_stmt = self.ledger.mari_statement("MARI_SAVINGS")
        left = self.ledger.mari_entry(
            sav_run,
            sav_stmt,
            source_type="MARI_SAVINGS",
            description="Repayment Mari Credit Card",
            amount="-412.35",
            economic_direction="OUT",
            posted_date="2026-01-27",
        )
        card_run, card_stmt = self.ledger.mari_statement("MARI_CARD")
        right = self.ledger.mari_entry(
            card_run,
            card_stmt,
            source_type="MARI_CARD",
            description="ALEX SYNTHETIC TAN Repayment",
            amount="412.35",
            economic_direction="IN",
            posted_date="2026-01-27",
        )

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 1)
        links = self._links(MARI_SAVINGS_CARD_REPAYMENT_LINK_VERSION)
        self.assertEqual(
            [(row["status"], row["left_entry_id"], row["right_entry_id"]) for row in links],
            [("CONFIRMED", left, right)],
        )

    def test_a_card_moneysend_credit_is_not_a_repayment_leg(self) -> None:
        sav_run, sav_stmt = self.ledger.mari_statement("MARI_SAVINGS")
        self.ledger.mari_entry(
            sav_run,
            sav_stmt,
            source_type="MARI_SAVINGS",
            description="Repayment Mari Credit Card",
            amount="-30.00",
            economic_direction="OUT",
        )
        card_run, card_stmt = self.ledger.mari_statement("MARI_CARD")
        self.ledger.mari_entry(
            card_run,
            card_stmt,
            source_type="MARI_CARD",
            description="Alex S Tan MoneySend Payment",
            amount="30.00",
            economic_direction="IN",
        )

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(self._links(MARI_SAVINGS_CARD_REPAYMENT_LINK_VERSION), [])

    def test_savings_card_repayment_requires_the_same_posted_day(self) -> None:
        sav_run, sav_stmt = self.ledger.mari_statement("MARI_SAVINGS")
        self.ledger.mari_entry(
            sav_run,
            sav_stmt,
            source_type="MARI_SAVINGS",
            description="Repayment Mari Credit Card",
            amount="-57.19",
            economic_direction="OUT",
            posted_date="2026-01-27",
        )
        card_run, card_stmt = self.ledger.mari_statement("MARI_CARD")
        self.ledger.mari_entry(
            card_run,
            card_stmt,
            source_type="MARI_CARD",
            description="ALEX SYNTHETIC TAN Repayment",
            amount="57.19",
            economic_direction="IN",
            posted_date="2026-01-29",
        )

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        self.assertEqual(self._links(MARI_SAVINGS_CARD_REPAYMENT_LINK_VERSION), [])

    def test_both_mari_linkers_are_idempotent(self) -> None:
        self._dbs_to_maribank("SYN0TEST1REF2TOKEN3ABCDEF", amount="-180.00")
        self._savings_inflow("SYN0TEST1REF2TOKEN3ABCDEF", amount="180.00")
        sav_run, sav_stmt = self.ledger.mari_statement("MARI_SAVINGS")
        self.ledger.mari_entry(
            sav_run,
            sav_stmt,
            source_type="MARI_SAVINGS",
            description="Repayment Mari Credit Card",
            amount="-412.35",
            economic_direction="OUT",
        )
        card_run, card_stmt = self.ledger.mari_statement("MARI_CARD")
        self.ledger.mari_entry(
            card_run,
            card_stmt,
            source_type="MARI_CARD",
            description="ALEX SYNTHETIC TAN Repayment",
            amount="412.35",
            economic_direction="IN",
        )

        self.assertEqual(refresh_automatic_transfer_links(self.connection), 2)
        self.assertEqual(refresh_automatic_transfer_links(self.connection), 0)
        confirmed = self.connection.execute(
            "SELECT count(*) FROM transfer_links WHERE status = 'CONFIRMED'"
        ).fetchone()[0]
        self.assertEqual(confirmed, 2)


if __name__ == "__main__":
    unittest.main()
