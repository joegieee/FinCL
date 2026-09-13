"""Unified transaction classification (PLAN-unified-transaction-classification.md
Step 1) — the shared typed draft, combined impact preview, and command
generation in ``fincl.classification``.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from fincl.application import record_batch, record_decision
from fincl.classification import ClassificationDraft, FieldEdit, evaluate_classification_draft
from fincl.db import connect_readonly
from fincl.decision_journal import default_journal_path, read_records
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.queries import all_entries
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes as csv_bytes


def _rows() -> list[list[str]]:
    rows = [
        # A Topup, not a Card Payment: stays a plain unresolved inbound credit
        # rather than tripping the refund heuristic below.
        ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01",
         "Seed", "5000.00", "0.00", "SGD", "COMPLETED", "5000.00"],
        ["Topup", "Current", "2026-01-10 09:00:00", "2026-01-10 09:00:01",
         "Client Payment", "300.00", "0.00", "SGD", "COMPLETED", "5300.00"],
    ]
    balance = Decimal("5300.00")
    for month, day, merchant, amount in (
        ("01", "05", "Corner Shop", "20.00"),
        ("01", "12", "Corner Shop", "20.00"),
        ("02", "05", "Bistro", "45.00"),
    ):
        balance -= Decimal(amount)
        rows.append([
            "Card Payment", "Current", f"2026-{month}-{day} 10:00:00",
            f"2026-{month}-{day} 10:00:01", merchant, f"-{amount}",
            "0.00", "SGD", "COMPLETED", f"{balance}",
        ])
    return rows


def _ledger() -> Path:
    root = Path(tempfile.mkdtemp())
    database = root / "fincl.sqlite3"
    source = root / "revolut.csv"
    source.write_bytes(csv_bytes(_rows()))
    DocumentImportService.plaintext_phase2(database, root / "evidence").import_revolut_pdf(source)
    refresh_ledger(database)
    return database


class ClassificationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.database = _ledger()

    def _entries(self) -> dict[str, dict]:
        connection = connect_readonly(self.database)
        try:
            return {e["anchor"]: e for e in all_entries(connection)}
        finally:
            connection.close()

    def _anchor(self, merchant: str, *, nth: int = 0) -> str:
        connection = connect_readonly(self.database)
        try:
            rows = connection.execute(
                "SELECT natural_anchor FROM ledger_entries e "
                "JOIN merchant_observations m ON m.entry_id = e.id "
                "WHERE m.merchant_normalized LIKE ? OR m.description_text LIKE ? "
                "ORDER BY e.effective_at", (f"%{merchant.upper()}%", f"%{merchant}%"),
            ).fetchall()
            return rows[nth][0]
        finally:
            connection.close()

    def _evaluate(self, draft: ClassificationDraft) -> dict:
        connection = connect_readonly(self.database)
        try:
            return evaluate_classification_draft(connection, draft)
        finally:
            connection.close()

    def _decide(self, *, kind: str, anchors: dict[str, str], payload: dict) -> None:
        record_decision(
            self.database, request_id=str(uuid.uuid4()), intent_hash=str(uuid.uuid4()),
            decision_kind=kind, anchors=anchors, payload=payload,
            policy_version="test-v1", snapshot=kind,
        )

    def _commit(self, commands: list[dict], *, reason: str = "test batch") -> None:
        record_batch(
            self.database, request_id=str(uuid.uuid4()), intent_hash=str(uuid.uuid4()),
            items=commands, reason=reason,
        )


class SpendingTrioTests(ClassificationTestBase):
    """The plan's flagship acceptance case: Groceries -> Variable routine ->
    Essential together, then an independent per-transaction exception."""

    def test_one_spending_transaction_generates_category_and_role_together(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,),
            category=FieldEdit("set", "Food & Dining / Groceries"),
            cost_behavior=FieldEdit("set", "variable_routine"),
            need=FieldEdit("set", "essential"),
            reason="everyday groceries",
        ))
        self.assertEqual(result["changed_transactions"], 1)
        self.assertEqual(result["command_count"], 2)
        kinds = sorted(c["kind"] for c in result["commands"])
        self.assertEqual(kinds, ["CATEGORY_OVERRIDE", "ENTRY_ROLE_SET"])
        role = next(c for c in result["commands"] if c["kind"] == "ENTRY_ROLE_SET")
        self.assertEqual(role["payload"]["cost_behavior"], "variable_routine")
        self.assertEqual(role["payload"]["need"], "essential")

        self._commit(result["commands"])
        refresh_ledger(self.database)
        entry = self._entries()[anchor]
        self.assertEqual(entry["category"], "Food & Dining / Groceries")
        self.assertEqual(entry["cost_behavior"], "variable_routine")
        self.assertEqual(entry["need"], "essential")

    def test_changing_only_need_preserves_manual_pattern_through_replay(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        self._decide(
            kind="ENTRY_ROLE_SET", anchors={"entry": anchor},
            payload={"cost_behavior": "fixed_recurring", "need": None, "reason": "looks recurring"},
        )
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), need=FieldEdit("set", "discretionary"), reason="actually a treat",
        ))
        self.assertEqual(result["command_count"], 1)
        command = result["commands"][0]
        self.assertEqual(command["kind"], "ENTRY_ROLE_SET")
        self.assertEqual(set(command["payload"]) - {"reason"}, {"need"})

        self._commit(result["commands"])
        refresh_ledger(self.database)
        entry = self._entries()[anchor]
        self.assertEqual(entry["cost_behavior"], "fixed_recurring")
        self.assertEqual(entry["need"], "discretionary")

    def test_a_transaction_can_diverge_from_a_prior_grocery_trio_independently(self) -> None:
        a, b = self._anchor("Corner Shop", nth=0), self._anchor("Corner Shop", nth=1)
        trio = ClassificationDraft(
            anchors=(a, b), category=FieldEdit("set", "Food & Dining / Groceries"),
            cost_behavior=FieldEdit("set", "variable_routine"), need=FieldEdit("set", "essential"),
            reason="everyday groceries",
        )
        self._commit(self._evaluate(trio)["commands"])
        refresh_ledger(self.database)

        exception = self._evaluate(ClassificationDraft(
            anchors=(a,), cost_behavior=FieldEdit("set", "irregular"),
            need=FieldEdit("set", "discretionary"), reason="one-off splurge",
        ))
        self.assertEqual(exception["command_count"], 1)
        self._commit(exception["commands"])
        refresh_ledger(self.database)

        entries = self._entries()
        self.assertEqual(entries[a]["category"], "Food & Dining / Groceries")
        self.assertEqual(entries[a]["cost_behavior"], "irregular")
        self.assertEqual(entries[a]["need"], "discretionary")
        self.assertEqual(entries[b]["category"], "Food & Dining / Groceries")
        self.assertEqual(entries[b]["cost_behavior"], "variable_routine")
        self.assertEqual(entries[b]["need"], "essential")


class NoOpAndPolicyTests(ClassificationTestBase):
    def test_no_op_edits_produce_no_commands_and_no_manual_replacements(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        current = self._entries()[anchor]
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", current["category"]),
            reason="reconfirm",
        ))
        self.assertEqual(result["command_count"], 0)
        self.assertEqual(result["changed_transactions"], 0)
        self.assertEqual(result["fields"]["category"]["manual_replacements"], 0)

    def test_fill_missing_only_touches_unresolved_fields(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        before = self._entries()[anchor]
        self.assertEqual(before["category"], "Unclassified / Unclassified")
        self.assertEqual(before["need"], "unresolved")

        first_fill = self._evaluate(ClassificationDraft(
            anchors=(anchor,), policy="fill_missing",
            category=FieldEdit("set", "Food & Dining / Groceries"),
            need=FieldEdit("set", "essential"),
            reason="fill only what is missing",
        ))
        self.assertEqual(first_fill["fields"]["category"]["changed"], 1)
        self.assertEqual(first_fill["fields"]["need"]["changed"], 1)
        self._commit(first_fill["commands"])
        refresh_ledger(self.database)

        # Category and (derived) need are now resolved; a second fill-missing
        # pass over the same fields must skip both, not overwrite them.
        second_fill = self._evaluate(ClassificationDraft(
            anchors=(anchor,), policy="fill_missing",
            category=FieldEdit("set", "Food & Dining / Dining Out"),
            need=FieldEdit("set", "discretionary"),
            reason="fill only what is missing",
        ))
        self.assertEqual(second_fill["fields"]["category"]["skipped_fill_missing"], 1)
        self.assertEqual(second_fill["fields"]["need"]["skipped_fill_missing"], 1)
        self.assertEqual(second_fill["command_count"], 0)

        replace = self._evaluate(ClassificationDraft(
            anchors=(anchor,), policy="replace",
            category=FieldEdit("set", "Food & Dining / Dining Out"),
            reason="replace the manual category",
        ))
        self.assertEqual(replace["fields"]["category"]["changed"], 1)
        self.assertEqual(replace["fields"]["category"]["manual_replacements"], 1)
        self.assertEqual(replace["commands"][0]["kind"], "CATEGORY_OVERRIDE")
        self.assertEqual(replace["commands"][0]["payload"]["category"], "Food & Dining / Dining Out")


class IncomeAndApplicabilityTests(ClassificationTestBase):
    def test_combined_income_and_category_preview_in_one_reviewed_save(self) -> None:
        anchor = self._anchor("Client Payment")
        entries = self._entries()
        self.assertEqual(entries[anchor]["bucket"], "pending_review")
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Income & Inflow / Earned Income"),
            incoming_type=FieldEdit("set", "income"), reason="confirmed salary",
        ))
        self.assertEqual(result["command_count"], 2)
        kinds = sorted(c["kind"] for c in result["commands"])
        self.assertEqual(kinds, ["CATEGORY_OVERRIDE", "INBOUND_CLASSIFICATION"])
        self.assertTrue(result["fields"]["cost_behavior"]["not_applicable"] >= 1)
        self.assertEqual(result["fields"]["cost_behavior"]["applicable"], 0)

    def test_inapplicable_fields_are_disclosed_and_produce_no_invalid_decisions(self) -> None:
        spending = self._anchor("Corner Shop", nth=0)
        incoming = self._anchor("Client Payment")
        result = self._evaluate(ClassificationDraft(
            anchors=(spending, incoming), cost_behavior=FieldEdit("set", "irregular"),
            reason="mixed selection",
        ))
        by_anchor = {row["anchor"]: row for row in result["rows"]}
        self.assertTrue(by_anchor[spending]["fields"]["cost_behavior"]["applicable"])
        self.assertFalse(by_anchor[incoming]["fields"]["cost_behavior"]["applicable"])
        self.assertIsNotNone(by_anchor[incoming]["fields"]["cost_behavior"]["reason"])
        self.assertEqual(result["command_count"], 1)
        self.assertEqual(result["commands"][0]["anchors"]["entry"], spending)

    def test_category_derived_role_change_appears_without_explicit_role_edit(self) -> None:
        anchor = self._anchor("Bistro")
        before = self._entries()[anchor]
        self.assertEqual(before["category"], "Unclassified / Unclassified")
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Food & Dining / Groceries"),
            reason="reclassify",
        ))
        self.assertEqual(result["command_count"], 1)
        row = result["rows"][0]
        self.assertTrue(row["fields"]["cost_behavior"]["derived"])
        self.assertEqual(row["fields"]["cost_behavior"]["after"], "variable_routine")
        self.assertTrue(row["fields"]["need"]["derived"])
        self.assertEqual(row["fields"]["need"]["after"], "essential")


class ValidationAndLimitTests(ClassificationTestBase):
    def test_preview_does_not_write(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        before = len(read_records(default_journal_path(self.database)))
        self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Food & Dining / Groceries"),
            reason="just looking",
        ))
        self.assertEqual(len(read_records(default_journal_path(self.database))), before)

    def test_a_reason_is_required_for_a_non_category_decision(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        with self.assertRaises(FinCLError):
            self._evaluate(ClassificationDraft(
                anchors=(anchor,), cost_behavior=FieldEdit("set", "irregular"), reason="",
            ))

    def test_a_bare_category_change_needs_no_reason(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Food & Dining / Groceries"), reason="",
        ))
        self.assertEqual(result["command_count"], 1)

    def test_choosing_no_field_to_change_is_refused(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        with self.assertRaises(FinCLError):
            self._evaluate(ClassificationDraft(anchors=(anchor,)))

    def test_an_inactive_anchor_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            self._evaluate(ClassificationDraft(
                anchors=("does-not-exist",), category=FieldEdit("set", "Food & Dining / Groceries"),
            ))

    def test_one_invalid_command_prevents_all_items_from_committing(self) -> None:
        a, b = self._anchor("Corner Shop", nth=0), self._anchor("Corner Shop", nth=1)
        result = self._evaluate(ClassificationDraft(
            anchors=(a, b), category=FieldEdit("set", "Food & Dining / Groceries"), reason="batch",
        ))
        commands = list(result["commands"])
        commands.append({
            "kind": "CATEGORY_OVERRIDE", "anchors": {"entry": b},
            "payload": {"category": "Not / Real", "reason": "bad"},
        })
        before = len(read_records(default_journal_path(self.database)))
        with self.assertRaises(FinCLError):
            self._commit(commands)
        self.assertEqual(len(read_records(default_journal_path(self.database))), before)

    def test_generated_command_counts_not_just_selected_transaction_counts(self) -> None:
        a, b = self._anchor("Corner Shop", nth=0), self._anchor("Corner Shop", nth=1)
        result = self._evaluate(ClassificationDraft(
            anchors=(a, b), category=FieldEdit("set", "Food & Dining / Groceries"),
            cost_behavior=FieldEdit("set", "variable_routine"), need=FieldEdit("set", "essential"),
            reason="two commands per transaction",
        ))
        # 2 anchors x (1 category + 1 combined role) = 4, not 2.
        self.assertEqual(result["command_count"], 4)
        self.assertEqual(result["changed_transactions"], 2)


class DuplicateSubmitAndRefreshTests(ClassificationTestBase):
    def test_duplicate_batch_submit_records_one_logical_batch(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Food & Dining / Groceries"),
            reason="dedupe check",
        ))
        request_id, intent_hash = str(uuid.uuid4()), "same-intent"
        first = record_batch(
            self.database, request_id=request_id, intent_hash=intent_hash,
            items=result["commands"], reason="dedupe check",
        )
        second = record_batch(
            self.database, request_id=request_id, intent_hash=intent_hash,
            items=result["commands"], reason="dedupe check",
        )
        self.assertTrue(first.appended)
        self.assertFalse(second.appended)
        self.assertEqual(first.journal_seq, second.journal_seq)
        self.assertEqual(len(read_records(default_journal_path(self.database))), 1)

    def test_a_refresh_pending_receipt_does_not_trigger_a_second_write(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Food & Dining / Groceries"),
            reason="refresh pending check",
        ))
        request_id, intent_hash = str(uuid.uuid4()), "same-intent"
        first = record_decision(
            self.database, request_id=request_id, intent_hash=intent_hash,
            decision_kind="DECISION_BATCH", anchors={"batch": request_id},
            payload={"items": result["commands"], "reason": "refresh pending check"},
            policy_version="relationship-batch-v1",
            snapshot=f"DECISION_BATCH: {len(result['commands'])} item(s)",
            refresh=False,
        )
        self.assertTrue(first.appended)
        # No projection refresh happened yet; retrying the same request id must
        # still resolve to the one durable append, not a second write, and the
        # retry itself reports the outcome as recorded-but-pending.
        second = record_decision(
            self.database, request_id=request_id, intent_hash=intent_hash,
            decision_kind="DECISION_BATCH", anchors={"batch": request_id},
            payload={"items": result["commands"], "reason": "refresh pending check"},
            policy_version="relationship-batch-v1",
            snapshot=f"DECISION_BATCH: {len(result['commands'])} item(s)",
        )
        self.assertFalse(second.appended)
        self.assertTrue(second.refresh_pending)
        self.assertEqual(first.journal_seq, second.journal_seq)
        self.assertEqual(len(read_records(default_journal_path(self.database))), 1)


class InternalBucketTests(ClassificationTestBase):
    """§5.1's applicability matrix distinguishes a confirmed-relationship
    internal movement (nothing editable) from an unlinked ``self_transfer``
    classification (incoming type stays editable)."""

    def _confirm_transfer_link(self, left: str, right: str) -> None:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            rows = {
                row["natural_anchor"]: row
                for row in connection.execute(
                    "SELECT id, natural_anchor, currency_pocket, amount_text, "
                    "amount_coefficient, amount_scale FROM ledger_entries "
                    "WHERE natural_anchor IN (?, ?)", (left, right),
                ).fetchall()
            }
            a, b = rows[left], rows[right]
            connection.execute(
                "INSERT INTO transfer_links(id, left_entry_id, right_entry_id, link_kind, "
                "left_currency, left_amount_text, left_amount_coefficient, left_amount_scale, "
                "right_currency, right_amount_text, right_amount_coefficient, right_amount_scale, "
                "method_version, status, evidence_json, created_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("test-link", a["id"], b["id"], "SAME_CURRENCY_TRANSFER",
                 a["currency_pocket"], a["amount_text"].lstrip("-"),
                 abs(a["amount_coefficient"]), a["amount_scale"],
                 b["currency_pocket"], b["amount_text"].lstrip("-"),
                 abs(b["amount_coefficient"]), b["amount_scale"],
                 "test-v1", "CONFIRMED", "{}", "2026-01-01T00:00:00+00:00"),
            )
            connection.commit()
        finally:
            connection.close()

    def test_a_confirmed_relationship_internal_row_has_no_editable_field(self) -> None:
        a, b = self._anchor("Corner Shop", nth=0), self._anchor("Corner Shop", nth=1)
        self._confirm_transfer_link(a, b)
        refresh_ledger(self.database)
        entries = self._entries()
        self.assertEqual(entries[a]["bucket"], "internal")
        # The frontend's shared editor (Step 2) has no server-side bucket-detail
        # call before rendering; it distinguishes this case from an unlinked
        # self_transfer row using exactly this `linked` flag on the Entry API
        # response (see frontend/src/features/classification/api.ts detailBucket).
        self.assertTrue(entries[a]["linked"])

        result = self._evaluate(ClassificationDraft(
            anchors=(a,), category=FieldEdit("set", "Food & Dining / Groceries"),
            incoming_type=FieldEdit("set", "income"), reason="should not apply",
        ))
        self.assertEqual(result["command_count"], 0)
        row = result["rows"][0]
        for field_name in ("category", "cost_behavior", "need", "incoming_type"):
            self.assertFalse(row["fields"][field_name]["applicable"])

    def test_an_unlinked_self_transfer_row_keeps_incoming_type_editable(self) -> None:
        anchor = self._anchor("Client Payment")
        self._decide(
            kind="INBOUND_CLASSIFICATION", anchors={"entry": anchor},
            payload={"classification": "self_transfer", "reason": "moved between my own accounts"},
        )
        refresh_ledger(self.database)
        entries = self._entries()
        self.assertEqual(entries[anchor]["bucket"], "internal")
        # Same distinction as above, the other way: unlinked, so the frontend
        # editor still offers incoming type.
        self.assertFalse(entries[anchor]["linked"])
        self.assertEqual(entries[anchor]["classification"], "self_transfer")

        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), incoming_type=FieldEdit("set", "other"),
            category=FieldEdit("set", "Food & Dining / Groceries"), reason="reclassify",
        ))
        row = result["rows"][0]
        self.assertTrue(row["fields"]["incoming_type"]["applicable"])
        self.assertFalse(row["fields"]["category"]["applicable"])
        self.assertEqual(result["command_count"], 1)
        self.assertEqual(result["commands"][0]["kind"], "INBOUND_CLASSIFICATION")


if __name__ == "__main__":
    unittest.main()
