"""v0.6 slice 3 — recurring commitments, dismissals and occurrence associations."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from fincl import clock
from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import entry_anchor, replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.forecast.engine import _obligation_events
from fincl.obligations import obligations_page
from fincl.planning.queries import recurring_occurrences
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes

_ROWS = [
    ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01", "Seed", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
]


def _csv() -> bytes:
    return revolut_pdf_bytes(_ROWS)


class RecurringProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv())
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.commitment = str(uuid.uuid4())

    def _append(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> None:
        append_decision(
            default_journal_path(self.database), decision_kind=kind, anchors=anchors,
            payload=payload, policy_version="planning-v1", snapshot=f"{kind} {anchors}",
        )

    def _commitment(self, *, currency: str = "SGD", status: str = "active", ended_on: str | None = None) -> None:
        self._append(
            "RECURRING_COMMITMENT_SET", {"commitment": self.commitment},
            {
                "name": "Streaming", "currency": currency, "merchant_key": "streamco",
                "account_ids": ["acct-1"], "seed_entry_anchors": ["a1", "a2", "a3"],
                "schedule": {"kind": "monthly", "day": 15, "anchor_on": "2026-01-15"},
                "expected_amount": "14.99", "amount_tolerance": "1.00",
                "status": status, "ended_on": ended_on, "reason": "confirmed",
            },
        )

    def test_commitment_dismissal_and_occurrences_project_and_replay(self) -> None:
        self._commitment()
        self._append("RECURRING_CANDIDATE_DISMISS", {"candidate": "detv1:streamco:SGD:acct-1:monthly"}, {"reason": "not a subscription"})
        self._append(
            "RECURRING_OCCURRENCE_SET", {"commitment": self.commitment, "scheduled_on": "2026-02-15"},
            {"entry": "entry-anchor-x", "reason": "manual match"},
        )
        self._append(
            "RECURRING_OCCURRENCE_SET", {"commitment": self.commitment, "scheduled_on": "2026-03-15"},
            {"entry": None, "reason": "skipped this month"},
        )
        replay_journal(self.connection)

        row = self.connection.execute(
            "SELECT name, currency, expected_coefficient, expected_scale, tolerance_coefficient, "
            "status, schedule_json, account_ids_json FROM recurring_commitments WHERE commitment = ?",
            (self.commitment,),
        ).fetchone()
        self.assertEqual((row["currency"], row["expected_coefficient"], row["expected_scale"], row["tolerance_coefficient"]),
                         ("SGD", 1499, 2, 100))
        self.assertEqual(json.loads(row["schedule_json"])["day"], 15)
        self.assertEqual(json.loads(row["account_ids_json"]), ["acct-1"])
        self.assertEqual(row["status"], "active")

        dismissal = self.connection.execute("SELECT reason FROM recurring_candidate_dismissals").fetchone()
        self.assertEqual(dismissal["reason"], "not a subscription")

        occ = self.connection.execute(
            "SELECT scheduled_on, entry_anchor FROM recurring_occurrences WHERE commitment = ? ORDER BY scheduled_on",
            (self.commitment,),
        ).fetchall()
        self.assertEqual([(o["scheduled_on"], o["entry_anchor"]) for o in occ],
                         [("2026-02-15", "entry-anchor-x"), ("2026-03-15", None)])

        # Retire one occurrence, cold replay.
        self._append("RECURRING_OCCURRENCE_RETIRE", {"commitment": self.commitment, "scheduled_on": "2026-03-15"}, {"reason": "back to detection"})
        for table in ("recurring_commitments", "recurring_candidate_dismissals", "recurring_occurrences"):
            self.connection.execute(f"DELETE FROM {table}")
        self.connection.commit()
        replay_journal(self.connection)
        remaining = self.connection.execute(
            "SELECT scheduled_on FROM recurring_occurrences WHERE commitment = ?", (self.commitment,)
        ).fetchall()
        self.assertEqual([r["scheduled_on"] for r in remaining], ["2026-02-15"])

    def test_an_occurrence_amount_projects_retires_and_replays(self) -> None:
        self._commitment()
        anchors = {"commitment": self.commitment, "scheduled_on": "2026-04-15"}
        self._append("RECURRING_OCCURRENCE_AMOUNT_SET", anchors, {"amount": "19.99", "reason": "price rise notice"})
        self._append("RECURRING_OCCURRENCE_AMOUNT_SET", {**anchors, "scheduled_on": "2026-05-15"},
                     {"amount": "17.50", "reason": "prorated"})
        self._append("RECURRING_OCCURRENCE_AMOUNT_RETIRE", {**anchors, "scheduled_on": "2026-05-15"},
                     {"reason": "not prorated after all"})
        replay_journal(self.connection)
        replay_journal(self.connection)

        rows = self.connection.execute(
            "SELECT scheduled_on, amount_coefficient, amount_scale, reason FROM recurring_occurrence_amounts"
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("2026-04-15", 1999, 2, "price rise notice")])

    def test_an_occurrence_amount_must_fall_on_the_schedule(self) -> None:
        self._commitment()
        self._append("RECURRING_OCCURRENCE_AMOUNT_SET", {"commitment": self.commitment, "scheduled_on": "2026-04-16"},
                     {"amount": "19.99", "reason": "wrong day"})
        with self.assertRaisesRegex(FinCLError, "not a scheduled occurrence"):
            replay_journal(self.connection)

    def test_retiring_an_occurrence_amount_that_was_never_set_is_refused(self) -> None:
        self._commitment()
        self._append("RECURRING_OCCURRENCE_AMOUNT_RETIRE", {"commitment": self.commitment, "scheduled_on": "2026-04-15"},
                     {"reason": "nothing there"})
        with self.assertRaisesRegex(FinCLError, "No amount is set"):
            replay_journal(self.connection)

    def test_currency_is_immutable_for_a_commitment(self) -> None:
        self._commitment(currency="SGD")
        self._commitment(currency="USD")
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_ended_status_requires_a_date(self) -> None:
        self._commitment(status="ended", ended_on=None)
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_occurrence_for_an_unknown_commitment_is_refused(self) -> None:
        self._append(
            "RECURRING_OCCURRENCE_SET", {"commitment": str(uuid.uuid4()), "scheduled_on": "2026-02-15"},
            {"entry": None, "reason": "orphan"},
        )
        with self.assertRaises(FinCLError):
            replay_journal(self.connection)

    def test_real_entry_anchor_survives_as_a_commitment_seed(self) -> None:
        # A real document natural anchor is ~143 characters (evidence sha +
        # ":pdf:revolut:" + the record sha256 hex + the ordinal); the seed
        # anchor field must carry it verbatim into the projection.
        refresh_ledger(self.database)
        entry_id = self.connection.execute(
            "SELECT id FROM ledger_entries ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        anchor = entry_anchor(self.connection, entry_id)
        self.assertGreater(len(anchor), 120)

        self._append(
            "RECURRING_COMMITMENT_SET", {"commitment": self.commitment},
            {
                "name": "Streaming", "currency": "SGD", "merchant_key": "streamco",
                "account_ids": ["acct-1"], "seed_entry_anchors": [anchor],
                "schedule": {"kind": "monthly", "day": 15, "anchor_on": "2026-01-15"},
                "expected_amount": "14.99", "amount_tolerance": "1.00",
                "status": "active", "ended_on": None, "reason": "confirmed",
            },
        )
        replay_journal(self.connection)

        row = self.connection.execute(
            "SELECT seed_anchors_json FROM recurring_commitments WHERE commitment = ?",
            (self.commitment,),
        ).fetchone()
        self.assertEqual(json.loads(row["seed_anchors_json"]), [anchor])


class OccurrenceAmountReaderTests(unittest.TestCase):
    """A varied occurrence is read at its own amount; every other keeps the usual one."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        balance = Decimal("500.00")
        rows = [_ROWS[0]]
        for month, amount in (("01", "14.99"), ("02", "19.99"), ("03", "14.99")):
            balance -= Decimal(amount)
            rows.append(["Card Payment", "Current", f"2026-{month}-15 10:00:00", f"2026-{month}-15 10:00:01",
                         "StreamCo", f"-{amount}", "0.00", "SGD", "COMPLETED", str(balance)])
        rows.append(["Topup", "Current", "2026-03-31 10:00:00", "2026-03-31 10:00:01", "Seed", "10.00", "0.00",
                     "SGD", "COMPLETED", str(balance + 10)])
        source = root / "revolut.csv"
        source.write_bytes(revolut_pdf_bytes(rows))
        self.database = root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, root / "evidence").import_revolut_pdf(source)
        self.commitment = str(uuid.uuid4())
        self._append(
            "RECURRING_COMMITMENT_SET", {"commitment": self.commitment},
            {
                "name": "Streaming", "currency": "SGD", "merchant_key": "STREAMCO",
                "account_ids": [], "seed_entry_anchors": [],
                "schedule": {"kind": "monthly", "day": 15, "anchor_on": "2026-01-15"},
                "expected_amount": "14.99", "amount_tolerance": "0.00",
                "status": "active", "ended_on": None, "reason": "confirmed",
            },
        )
        for scheduled_on, amount in (("2026-02-15", "19.99"), ("2026-06-15", "30.00"), ("2026-09-15", "19.99")):
            self._append(
                "RECURRING_OCCURRENCE_AMOUNT_SET", {"commitment": self.commitment, "scheduled_on": scheduled_on},
                {"amount": amount, "reason": "bill notice"},
            )
        refresh_ledger(self.database)

    def _append(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> None:
        append_decision(
            default_journal_path(self.database), decision_kind=kind, anchors=anchors,
            payload=payload, policy_version="planning-v1", snapshot=f"{kind} {anchors}",
        )

    def test_a_varied_payment_matches_its_own_occurrence(self) -> None:
        page = recurring_occurrences(self.database, self.commitment, start="2026-01-01", end="2026-03-31")
        rows = {o["scheduled_on"]: o for o in page["occurrences"]}
        self.assertEqual({on: o["state"] for on, o in rows.items()},
                         {"2026-01-15": "observed", "2026-02-15": "observed", "2026-03-15": "observed"})
        self.assertEqual(rows["2026-02-15"]["expected_amount"], "19.99")
        self.assertEqual(rows["2026-02-15"]["amount_reason"], "bill notice")
        self.assertEqual(rows["2026-03-15"]["expected_amount"], "14.99")
        self.assertIsNone(rows["2026-03-15"]["amount_reason"])

    def test_a_pinned_clock_decides_which_occurrences_are_still_upcoming(self) -> None:
        clock.pin(date(2026, 2, 1))
        self.addCleanup(clock.pin, None)

        page = recurring_occurrences(self.database, self.commitment, start="2026-01-01", end="2026-03-31")

        states = {o["scheduled_on"]: o["state"] for o in page["occurrences"]}
        self.assertEqual(states["2026-01-15"], "observed")
        self.assertEqual(states["2026-02-15"], "upcoming_estimate")

    def test_the_obligation_and_forecast_use_each_occurrence_amount(self) -> None:
        page = obligations_page(self.database, as_of="2026-07-20", today=date(2026, 7, 20))
        streaming = next(o for o in page["obligations"] if o["obligation_id"] == f"recurring:{self.commitment}")
        # The earliest unmatched occurrence is the one due, at its own amount.
        self.assertEqual(streaming["date_basis"]["value"], "2026-06-15")
        self.assertEqual(streaming["remaining_amount"]["value"], "30.00")
        self.assertEqual(streaming["expected_amount"], "14.99")

        events, _ = _obligation_events(page, start="2026-07-20", end="2026-10-31")
        self.assertEqual(
            [(e.on, e.amount) for e in events if e.source_id.startswith(f"recurring:{self.commitment}")],
            [("2026-08-15", "14.99"), ("2026-09-15", "19.99"), ("2026-10-15", "14.99")],
        )
