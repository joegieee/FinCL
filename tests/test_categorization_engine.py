"""Phase 4 Track A steps 4-6 — the categorization engine, run manifest, and CLI.

Every selected entry gets exactly one reproducible category by the §13.4
precedence ladder; a direction conflict / ambiguity / NULL description falls
through to Unclassified and is flagged, never left without a category (§6).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from typer.testing import CliRunner

from fincl.application import record_batch
from fincl.categorization import rule_id_for
from fincl.categorization_queries import (
    _candidate_key,
    categorization_candidate_detail,
    categorization_candidates,
    derive_rule_options,
    merchant_merge_preview,
    merchant_rename_preview,
    merchant_search,
    merchant_similar,
    rule_suggestions,
)
from fincl.cli import app
from fincl.db import connect_readonly, connect_sqlite, initialize_schema
from fincl.document_service import DocumentImportService
from fincl.engine import categorize, evaluate_current_entries
from fincl.errors import FinCLError
from fincl.generation import current_generation
from fincl.money import ExactMoney, compare
from fincl.refresh import refresh_ledger
from tests.revolut_pdf import revolut_pdf_bytes

_RUNNER = CliRunner()


def _csv(rows) -> bytes:
    return revolut_pdf_bytes(rows)


# Topup (IN, balance 500) then three card payments and one refund.
_ROWS = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01", "Bank Transfer", "500.00", "0.00", "SGD", "COMPLETED", "500.00"],
    ["Card Payment", "Current", "2026-01-02 09:00:00", "2026-01-02 09:00:01", "ACME SUPERMARKET", "-20.00", "0.00", "SGD", "COMPLETED", "480.00"],
    ["Card Payment", "Current", "2026-01-03 09:00:00", "2026-01-03 09:00:01", "ACME SUPERMARKET", "-300.00", "0.00", "SGD", "COMPLETED", "180.00"],
    ["Card Refund", "Current", "2026-01-04 09:00:00", "2026-01-04 09:00:01", "ACME SUPERMARKET", "20.00", "0.00", "SGD", "COMPLETED", "200.00"],
    ["Card Payment", "Current", "2026-01-05 09:00:00", "2026-01-05 09:00:01", "MYSTERY CAFE", "-8.00", "0.00", "SGD", "COMPLETED", "192.00"],
]


class MoneyCompareTests(unittest.TestCase):
    def test_sign_and_cross_scale(self) -> None:
        self.assertEqual(compare(ExactMoney(10, 0, "10"), ExactMoney(1000, 2, "10.00")), 0)
        self.assertEqual(compare(ExactMoney(5, 0, "5"), ExactMoney(1000, 2, "10.00")), -1)
        self.assertEqual(compare(ExactMoney(2000, 2, "20.00"), ExactMoney(10, 0, "10")), 1)


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv(_ROWS))
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def _cli(self, *args: str) -> None:
        result = _RUNNER.invoke(app, [*args, "--database", str(self.database)])
        self.assertEqual(result.exit_code, 0, result.output)

    def _cat(self, description: str) -> sqlite3.Row:
        return self.connection.execute(
            """
            SELECT ec.* FROM entry_categories ec
            JOIN ledger_entries e ON e.id = ec.entry_id
            JOIN merchant_observations o ON o.entry_id = e.id
            WHERE o.merchant_normalized = ? ORDER BY e.effective_at
            """,
            (description,),
        ).fetchone()

    def _cats(self, description: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT ec.*, e.economic_direction, abs(e.amount_coefficient) AS mag
            FROM entry_categories ec
            JOIN ledger_entries e ON e.id = ec.entry_id
            JOIN merchant_observations o ON o.entry_id = e.id
            WHERE o.merchant_normalized = ? ORDER BY e.effective_at
            """,
            (description,),
        ).fetchall()

    def test_every_selected_entry_gets_exactly_one_category(self) -> None:
        categorize(self.connection)
        total = self.connection.execute("SELECT count(*) FROM entry_categories").fetchone()[0]
        eligible = self.connection.execute(
            "SELECT count(*) FROM ledger_entries e JOIN extraction_runs x "
            "ON x.id = e.extraction_run_id AND x.selection_state = 'SELECTED'"
        ).fetchone()[0]
        self.assertEqual(total, eligible)
        self.assertGreater(total, 0)

    def test_read_only_evaluator_matches_persisted_engine_output(self) -> None:
        categorize(self.connection)
        run_count = self.connection.execute("SELECT count(*) FROM categorization_runs").fetchone()[0]
        evaluated = evaluate_current_entries(self.connection)
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM categorization_runs").fetchone()[0], run_count
        )
        categorize(self.connection)
        persisted = self.connection.execute(
            "SELECT entry_id,group_name,category,precedence_tier,basis,ambiguous,direction_conflict "
            "FROM entry_categories ORDER BY entry_id"
        ).fetchall()
        self.assertEqual(len(evaluated), len(persisted))
        for result, row in zip(evaluated, persisted, strict=True):
            self.assertEqual(result.entry["id"], row["entry_id"])
            self.assertEqual(
                (result.group_name, result.category, result.precedence_tier, result.basis,
                 int(result.ambiguous), int(result.direction_conflict)),
                (row["group_name"], row["category"], row["precedence_tier"], row["basis"],
                 row["ambiguous"], row["direction_conflict"]),
            )

    def test_merchant_rule_applies_at_tier_3_including_the_refund(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        rows = self._cats("ACME SUPERMARKET")
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual((row["group_name"], row["category"]), ("Food & Dining", "Groceries"))
            self.assertEqual(row["precedence_tier"], 3)
            self.assertTrue(row["basis"].startswith("rule:"))
            self.assertEqual(row["direction_conflict"], 0)  # spend cat on the refund inflow is fine

    def test_refund_context_shows_the_usual_category_without_changing_anything(self) -> None:
        """Step 6e: read-only context only -- the refund stays unclassified
        incoming type, its bucket and category are untouched by this."""
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        refresh_ledger(self.database)
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        generation = current_generation(readonly)["generation_token"]
        page = categorization_candidates(self.database, generation=generation, focus="incoming")
        candidate = next(c for c in page["candidates"] if c["label"] == "ACME SUPERMARKET")
        detail = categorization_candidate_detail(
            self.database, generation=generation, candidate_id=candidate["candidate_id"], focus="incoming",
        )
        refund = next(r for r in detail["records"] if r["direction"] == "IN")
        self.assertEqual(refund["detail_bucket"], "refunds")
        self.assertEqual(refund["refund_context"], "Refund from ACME SUPERMARKET (usually Food & Dining / Groceries)")
        # Read-only: category, bucket and incoming type are unchanged.
        self.assertEqual(refund["effective"]["category"], "Food & Dining / Groceries")
        self.assertIsNone(refund["effective"]["incoming_type"])

    def test_no_refund_context_without_a_known_usual_category(self) -> None:
        categorize(self.connection)
        refresh_ledger(self.database)
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        generation = current_generation(readonly)["generation_token"]
        page = categorization_candidates(self.database, generation=generation, focus="incoming")
        candidate = next(c for c in page["candidates"] if c["label"] == "ACME SUPERMARKET")
        detail = categorization_candidate_detail(
            self.database, generation=generation, candidate_id=candidate["candidate_id"], focus="incoming",
        )
        refund = next(r for r in detail["records"] if r["direction"] == "IN")
        self.assertNotIn("refund_context", refund)

    def test_unmatched_entry_is_unclassified_tier_6(self) -> None:
        categorize(self.connection)
        row = self._cat("MYSTERY CAFE")
        self.assertEqual((row["group_name"], row["category"]), ("Unclassified", "Unclassified"))
        self.assertEqual(row["precedence_tier"], 6)

    def test_override_beats_the_rule_at_tier_1(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        anchor = self.connection.execute(
            "SELECT e.natural_anchor FROM ledger_entries e JOIN merchant_observations o "
            "ON o.entry_id = e.id WHERE o.merchant_normalized = 'ACME SUPERMARKET' "
            "ORDER BY e.effective_at LIMIT 1"
        ).fetchone()[0]
        self._cli("review", "categorize", anchor, "--as", "Shopping / General Shopping",
                  "--reason", "actually homeware")
        row = self.connection.execute(
            "SELECT ec.* FROM entry_categories ec JOIN ledger_entries e ON e.id = ec.entry_id "
            "WHERE e.natural_anchor = ?", (anchor,)
        ).fetchone()
        self.assertEqual((row["precedence_tier"], row["basis"]), (1, "override"))
        self.assertEqual((row["group_name"], row["category"]), ("Shopping", "General Shopping"))

    def test_income_rule_on_an_outflow_is_skipped_and_flagged(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Income & Inflow / Earned Income", "--reason", "wrong on purpose")
        rows = self._cats("ACME SUPERMARKET")
        outflows = [r for r in rows if r["economic_direction"] == "OUT"]
        inflow = [r for r in rows if r["economic_direction"] == "IN"][0]
        for row in outflows:
            self.assertEqual(row["precedence_tier"], 6)   # skipped -> unclassified
            self.assertEqual(row["direction_conflict"], 1)
        # The inflow (refund) is compatible with an Income category.
        self.assertEqual(inflow["direction_conflict"], 0)
        listing = _RUNNER.invoke(
            app, ["review", "list", "--kind", "uncategorized", "--database", str(self.database), "--json"]
        )
        self.assertEqual(listing.exit_code, 0, listing.output)
        anchors = {e["anchor"] for e in json.loads(listing.output)["entries"]}
        self.assertGreaterEqual(len(anchors), 2)

    def test_amount_band_narrower_wins_and_currency_must_match(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "small basket",
                  "--currency", "SGD", "--max-amount", "100.00")
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Shopping / General Shopping", "--reason", "big ticket",
                  "--currency", "SGD", "--min-amount", "100.00")
        rows = {r["mag"]: r for r in self._cats("ACME SUPERMARKET")}
        self.assertEqual(rows[2000]["category"], "Groceries")       # 20.00 -> small
        self.assertEqual(rows[30000]["category"], "General Shopping")  # 300.00 -> big
        # A USD-qualified band never touches the SGD entries.
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Travel / Activities", "--reason", "usd only",
                  "--currency", "USD", "--min-amount", "1.00")
        rows = {r["mag"]: r for r in self._cats("ACME SUPERMARKET")}
        self.assertNotEqual(rows[2000]["category"], "Activities")

    def test_run_id_is_reproducible_and_changes_with_a_rule(self) -> None:
        first = categorize(self.connection)
        again = categorize(self.connection)
        self.assertEqual(first, again)
        self._cli("rules", "add", "--merchant", "MYSTERY CAFE",
                  "--category", "Food & Dining / Cafes & Drinks", "--reason", "cafe")
        after = self.connection.execute(
            "SELECT run_id FROM entry_categories LIMIT 1"
        ).fetchone()[0]
        self.assertNotEqual(after, first)

    def test_only_the_latest_run_manifest_is_kept(self) -> None:
        categorize(self.connection)
        self._cli("rules", "add", "--merchant", "MYSTERY CAFE",
                  "--category", "Food & Dining / Cafes & Drinks", "--reason", "cafe")
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        rows = self.connection.execute("SELECT id FROM categorization_runs").fetchall()
        self.assertEqual(len(rows), 1)
        current = self.connection.execute(
            "SELECT DISTINCT run_id FROM entry_categories"
        ).fetchall()
        self.assertEqual([r[0] for r in current], [rows[0][0]])

    def test_alias_resolution_lands_at_tier_4(self) -> None:
        self._cli("rules", "alias", "Acme", "--pattern", "prefix:ACME S", "--reason", "brand")
        self._cli("rules", "add", "--merchant", "Acme",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        row = self._cat("ACME SUPERMARKET")
        self.assertEqual(row["precedence_tier"], 4)
        self.assertTrue(row["basis"].startswith("alias:Acme:"))

    def test_canonical_merge_migrates_dependent_category_rule(self) -> None:
        """Milestone 0 contract: an alias merge preserves dependent rule behavior."""
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        refresh_ledger(self.database)
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        generation = current_generation(readonly)["generation_token"]
        preview = merchant_merge_preview(
            self.database, generation=generation, sources=["ACME SUPERMARKET"],
            target="Combined Grocer", display_name="Combined Grocer", reason="same business",
        )
        record_batch(self.database, request_id=str(uuid.uuid4()), intent_hash=str(uuid.uuid4()),
                     items=preview["commands"], reason="same business")
        rows = self.connection.execute(
            "SELECT ec.category FROM entry_categories ec JOIN ledger_entries e ON e.id=ec.entry_id "
            "JOIN merchant_observations o ON o.entry_id=e.id WHERE o.merchant_canonical='Combined Grocer'"
        ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["category"] == "Groceries" for row in rows))

    def test_merge_rewrites_inbound_rules_and_the_source_resolves_to_the_target(self) -> None:
        match = {"merchant_canonical": "ACME SUPERMARKET"}
        rule_id = rule_id_for(match)
        record_batch(
            self.database, request_id=str(uuid.uuid4()), intent_hash=str(uuid.uuid4()),
            items=[{"kind": "INBOUND_RULE_SET", "anchors": {"rule_id": rule_id},
                    "payload": {"match": match, "classification": "reimbursement", "reason": "refunds"}}],
            reason="refunds",
        )
        refresh_ledger(self.database)
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        generation = current_generation(readonly)["generation_token"]
        preview = merchant_merge_preview(
            self.database, generation=generation, sources=["ACME SUPERMARKET"],
            target="Combined Grocer", display_name="Combined Grocer", reason="same source",
        )
        kinds = [command["kind"] for command in preview["commands"]]
        self.assertIn("INBOUND_RULE_SET", kinds)
        self.assertIn("INBOUND_RULE_RETIRE", kinds)
        record_batch(self.database, request_id=str(uuid.uuid4()), intent_hash=str(uuid.uuid4()),
                     items=preview["commands"], reason="same source")
        new_rule_id = rule_id_for({"merchant_canonical": "Combined Grocer"})
        row = self.connection.execute(
            "SELECT classification FROM inbound_rules WHERE rule_id = ?", (new_rule_id,),
        ).fetchone()
        self.assertEqual(row["classification"], "reimbursement")
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM inbound_rules WHERE rule_id = ?", (rule_id,),
        ).fetchone())

    def test_merge_conflict_blocks_the_rule_until_a_resolution_names_a_winner(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        self._cli("rules", "add", "--merchant", "MYSTERY CAFE",
                  "--category", "Transport / Taxi & Ride-hail", "--reason", "test")
        refresh_ledger(self.database)
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        generation = current_generation(readonly)["generation_token"]
        preview = merchant_merge_preview(
            self.database, generation=generation, sources=["ACME SUPERMARKET", "MYSTERY CAFE"],
            target="Combined", display_name="Combined", reason="same business",
        )
        self.assertEqual(len(preview["conflicts"]), 1)
        conflict = preview["conflicts"][0]
        self.assertEqual(conflict["kind"], "category")
        self.assertEqual(
            {candidate["from_merchant"] for candidate in conflict["candidates"]},
            {"ACME SUPERMARKET", "MYSTERY CAFE"},
        )
        kinds = [command["kind"] for command in preview["commands"]]
        self.assertNotIn("CATEGORY_RULE_SET", kinds)
        self.assertNotIn("CATEGORY_RULE_RETIRE", kinds)

        resolved = merchant_merge_preview(
            self.database, generation=generation, sources=["ACME SUPERMARKET", "MYSTERY CAFE"],
            target="Combined", display_name="Combined", reason="same business",
            resolutions={"category": "ACME SUPERMARKET"},
        )
        self.assertEqual(resolved["conflicts"], [])
        set_command = next(c for c in resolved["commands"] if c["kind"] == "CATEGORY_RULE_SET")
        self.assertEqual(set_command["payload"]["category"], "Food & Dining / Groceries")
        retire_kinds = [c["kind"] for c in resolved["commands"] if c["kind"] == "CATEGORY_RULE_RETIRE"]
        self.assertEqual(len(retire_kinds), 2)

    def test_merchant_search_and_similar_suggestions(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        refresh_ledger(self.database)
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        generation = current_generation(readonly)["generation_token"]

        page = merchant_search(self.database, generation=generation, query="acme", limit=20)
        self.assertEqual(len(page["merchants"]), 1)
        item = page["merchants"][0]
        self.assertEqual(item["merchant_key"], "ACME SUPERMARKET")
        self.assertTrue(item["has_rules"])
        self.assertEqual(item["transaction_count"], 3)

        empty = merchant_search(self.database, generation=generation, query="no-such-merchant", limit=20)
        self.assertEqual(empty["merchants"], [])

        similar = merchant_similar(self.database, generation=generation, merchant_key="ACME SUPERMARKET")
        self.assertEqual(similar["suggestions"], [])

        with self.assertRaises(FinCLError):
            merchant_similar(self.database, generation=generation, merchant_key="NO SUCH MERCHANT")

    def test_rename_an_unaliased_merchant_keeps_category_and_role_rules_and_sets_display_name(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        match = {"merchant_canonical": "ACME SUPERMARKET"}
        role_rule_id = rule_id_for(match)
        record_batch(
            self.database, request_id=str(uuid.uuid4()), intent_hash=str(uuid.uuid4()),
            items=[{"kind": "ROLE_RULE_SET", "anchors": {"rule_id": role_rule_id},
                    "payload": {"match": match, "cost_behavior": "variable_routine", "need": "essential", "reason": "role"}}],
            reason="role",
        )
        refresh_ledger(self.database)
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        generation = current_generation(readonly)["generation_token"]
        preview = merchant_rename_preview(
            self.database, generation=generation, merchant_key="ACME SUPERMARKET",
            display_name="Acme Singapore", reason="clearer name",
        )
        self.assertEqual(preview["affected_transactions"], 3)
        record_batch(self.database, request_id=str(uuid.uuid4()), intent_hash=str(uuid.uuid4()),
                     items=preview["commands"], reason="clearer name")
        rows = self._cats("ACME SUPERMARKET")
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual((row["group_name"], row["category"]), ("Food & Dining", "Groceries"))
            self.assertEqual(row["precedence_tier"], 4)
            self.assertTrue(row["basis"].startswith("alias:ACME SUPERMARKET:"))
        alias_row = self.connection.execute(
            "SELECT display_name FROM merchant_aliases WHERE canonical = 'ACME SUPERMARKET'"
        ).fetchone()
        self.assertEqual(alias_row["display_name"], "Acme Singapore")
        role_row = self.connection.execute(
            "SELECT cost_behavior, need FROM role_rules WHERE rule_id = ?", (role_rule_id,)
        ).fetchone()
        self.assertEqual((role_row["cost_behavior"], role_row["need"]), ("variable_routine", "essential"))

    def test_rename_preview_rejects_a_blank_or_unchanged_name(self) -> None:
        refresh_ledger(self.database)
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        generation = current_generation(readonly)["generation_token"]
        with self.assertRaises(FinCLError):
            merchant_rename_preview(
                self.database, generation=generation, merchant_key="ACME SUPERMARKET",
                display_name="   ", reason="blank",
            )
        with self.assertRaises(FinCLError):
            merchant_rename_preview(
                self.database, generation=generation, merchant_key="ACME SUPERMARKET",
                display_name="ACME SUPERMARKET", reason="unchanged",
            )

    def test_rules_list_reports_match_count(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        listing = _RUNNER.invoke(
            app, ["rules", "list", "--database", str(self.database), "--json"]
        )
        self.assertEqual(listing.exit_code, 0, listing.output)
        rule = json.loads(listing.output)["rules"][0]
        self.assertEqual(rule["match_count"], 3)

    def test_coverage_json_carries_a_category_block_and_warnings(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Income & Inflow / Earned Income", "--reason", "mismatch")
        result = _RUNNER.invoke(
            app, ["coverage", "--database", str(self.database), "--json"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        categories = json.loads(result.output)["categories"]
        self.assertIn("by_group", categories)
        self.assertGreaterEqual(categories["warnings"].get("direction_conflict", 0), 2)

    def test_report_plain_mentions_categories(self) -> None:
        categorize(self.connection)
        result = _RUNNER.invoke(app, ["report", "--database", str(self.database), "--plain"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("categories:", result.output)

    def test_rebuild_reproduces_the_categorization(self) -> None:
        self._cli("rules", "add", "--merchant", "ACME SUPERMARKET",
                  "--category", "Food & Dining / Groceries", "--reason", "grocer")
        anchor = self.connection.execute(
            "SELECT e.natural_anchor FROM ledger_entries e JOIN merchant_observations o "
            "ON o.entry_id = e.id WHERE o.merchant_normalized = 'MYSTERY CAFE'"
        ).fetchone()[0]
        self._cli("review", "categorize", anchor, "--as", "Food & Dining / Cafes & Drinks",
                  "--reason", "it is a cafe")
        before = {
            (row["natural_anchor"], row["group_name"], row["category"], row["precedence_tier"], row["basis"])
            for row in self.connection.execute(
                "SELECT e.natural_anchor, ec.* FROM entry_categories ec "
                "JOIN ledger_entries e ON e.id = ec.entry_id"
            )
        }
        run_before = self.connection.execute("SELECT id FROM categorization_runs").fetchone()[0]

        from fincl.rebuild import rebuild_database

        output = self.root / "rebuilt.sqlite3"
        rebuild_database(self.database, self.root / "evidence", output)
        rebuilt = connect_sqlite(output)
        self.addCleanup(rebuilt.close)
        initialize_schema(rebuilt)
        after = {
            (row["natural_anchor"], row["group_name"], row["category"], row["precedence_tier"], row["basis"])
            for row in rebuilt.execute(
                "SELECT e.natural_anchor, ec.* FROM entry_categories ec "
                "JOIN ledger_entries e ON e.id = ec.entry_id"
            )
        }
        run_after = rebuilt.execute("SELECT id FROM categorization_runs").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(run_before, run_after)


_RIDES_ROWS = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01", "Bank Transfer", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
    ["Card Payment", "Current", "2026-01-02 09:00:00", "2026-01-02 09:00:01", "ACME RIDES SG", "-8.00", "0.00", "SGD", "COMPLETED", "992.00"],
    ["Card Payment", "Current", "2026-01-03 09:00:00", "2026-01-03 09:00:01", "ACME RIDES SG", "-8.00", "0.00", "SGD", "COMPLETED", "984.00"],
    ["Card Payment", "Current", "2026-01-04 09:00:00", "2026-01-04 09:00:01", "ACME RIDES SG", "-9.50", "0.00", "SGD", "COMPLETED", "974.50"],
    ["Card Payment", "Current", "2026-01-05 09:00:00", "2026-01-05 09:00:01", "ACME RIDES SG", "-9.00", "0.00", "SGD", "COMPLETED", "965.50"],
    ["Card Payment", "Current", "2026-01-06 09:00:00", "2026-01-06 09:00:01", "ACME RIDES SG", "-25.00", "0.00", "SGD", "COMPLETED", "940.50"],
    ["Card Payment", "Current", "2026-01-07 09:00:00", "2026-01-07 09:00:01", "ACME RIDES SG", "-26.00", "0.00", "SGD", "COMPLETED", "914.50"],
    ["Card Payment", "Current", "2026-01-08 09:00:00", "2026-01-08 09:00:01", "ACME RIDES SG", "-30.00", "0.00", "SGD", "COMPLETED", "884.50"],
    ["Card Payment", "Current", "2026-01-09 09:00:00", "2026-01-09 09:00:01", "ACME RIDES SG", "-28.00", "0.00", "SGD", "COMPLETED", "856.50"],
    ["Card Payment", "Current", "2026-01-10 09:00:00", "2026-01-10 09:00:01", "MYSTERY CAFE", "-8.00", "0.00", "SGD", "COMPLETED", "848.50"],
    ["Card Payment", "Current", "2026-01-11 09:00:00", "2026-01-11 09:00:01", "MYSTERY CAFE", "-9.00", "0.00", "SGD", "COMPLETED", "839.50"],
]


class AmountBandRuleOptionTests(unittest.TestCase):
    """Step 6a: `derive_rule_options` offers a banded refinement when a merchant's amounts cluster."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv(_RIDES_ROWS))
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        categorize(self.connection)
        refresh_ledger(self.database)

    def _generation(self) -> str:
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        return current_generation(readonly)["generation_token"]

    def _anchor(self, merchant: str) -> str:
        return self.connection.execute(
            "SELECT e.natural_anchor FROM ledger_entries e JOIN merchant_observations o "
            "ON o.entry_id = e.id WHERE o.merchant_normalized = ? ORDER BY e.effective_at LIMIT 1",
            (merchant,),
        ).fetchone()[0]

    def test_clustered_merchant_amounts_offer_two_band_options(self) -> None:
        anchor = self._anchor("ACME RIDES")
        result = derive_rule_options(self.database, generation=self._generation(), anchors=[anchor])
        band_options = [option for option in result["options"] if "amount_band" in option["match"]]
        self.assertEqual(len(band_options), 2)
        for option in band_options:
            self.assertEqual(option["match"]["merchant_canonical"], "ACME RIDES")
            band = option["match"]["amount_band"]
            self.assertEqual(band["currency"], "SGD")
            self.assertIn("min", band)
            self.assertIn("max", band)
        rides, food = sorted(band_options, key=lambda option: option["match"]["amount_band"]["min"]["coefficient"])
        self.assertLessEqual(
            rides["match"]["amount_band"]["max"]["coefficient"],
            food["match"]["amount_band"]["min"]["coefficient"],
        )

    def test_too_few_transactions_offer_no_band_option(self) -> None:
        anchor = self._anchor("MYSTERY CAFE")
        result = derive_rule_options(self.database, generation=self._generation(), anchors=[anchor])
        self.assertFalse(any("amount_band" in option["match"] for option in result["options"]))


_DESCRIPTION_ROWS = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01", "Bank Transfer", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
    ["Card Payment", "Current", "2026-01-02 09:00:00", "2026-01-02 09:00:01", "COFFEE HOUSE JAN", "-5.00", "0.00", "SGD", "COMPLETED", "995.00"],
    ["Card Payment", "Current", "2026-01-03 09:00:00", "2026-01-03 09:00:01", "COFFEE HOUSE FEB", "-6.00", "0.00", "SGD", "COMPLETED", "989.00"],
    ["Card Payment", "Current", "2026-01-04 09:00:00", "2026-01-04 09:00:01", "PIZZA", "-7.00", "0.00", "SGD", "COMPLETED", "982.00"],
    ["Card Payment", "Current", "2026-01-05 09:00:00", "2026-01-05 09:00:01", "SUSHI", "-8.00", "0.00", "SGD", "COMPLETED", "974.00"],
]


class DescriptionPrefixRuleOptionTests(unittest.TestCase):
    """Step 6b: offer the longest common description prefix when descriptions differ."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv(_DESCRIPTION_ROWS))
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        categorize(self.connection)
        refresh_ledger(self.database)

    def _generation(self) -> str:
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        return current_generation(readonly)["generation_token"]

    def _anchor_by_description(self, description: str) -> str:
        return self.connection.execute(
            "SELECT e.natural_anchor FROM ledger_entries e JOIN merchant_observations o "
            "ON o.entry_id = e.id WHERE o.description_text = ?", (description,),
        ).fetchone()[0]

    def test_shared_prefix_is_offered_when_descriptions_differ(self) -> None:
        anchors = [self._anchor_by_description("COFFEE HOUSE JAN"), self._anchor_by_description("COFFEE HOUSE FEB")]
        result = derive_rule_options(self.database, generation=self._generation(), anchors=anchors)
        prefix_options = [option for option in result["options"] if "description_prefix" in option["match"]]
        self.assertEqual(len(prefix_options), 1)
        self.assertEqual(prefix_options[0]["match"], {"description_prefix": "COFFEE HOUSE "})

    def test_no_prefix_offered_below_the_minimum_literal_length(self) -> None:
        anchors = [self._anchor_by_description("PIZZA"), self._anchor_by_description("SUSHI")]
        result = derive_rule_options(self.database, generation=self._generation(), anchors=anchors)
        self.assertFalse(any("description_prefix" in option["match"] for option in result["options"]))


_REPEATED_OVERRIDE_ROWS = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01", "Bank Transfer", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
    ["Card Payment", "Current", "2026-01-02 09:00:00", "2026-01-02 09:00:01", "PIZZA PALACE", "-10.00", "0.00", "SGD", "COMPLETED", "990.00"],
    ["Card Payment", "Current", "2026-01-03 09:00:00", "2026-01-03 09:00:01", "PIZZA PALACE", "-12.00", "0.00", "SGD", "COMPLETED", "978.00"],
    ["Card Payment", "Current", "2026-01-04 09:00:00", "2026-01-04 09:00:01", "PIZZA PALACE", "-11.00", "0.00", "SGD", "COMPLETED", "967.00"],
    ["Card Payment", "Current", "2026-01-05 09:00:00", "2026-01-05 09:00:01", "PIZZA PALACE", "-13.00", "0.00", "SGD", "COMPLETED", "954.00"],
]


class RuleSuggestionTests(unittest.TestCase):
    """Step 6c: 3+ manual overrides agreeing on a category, with no rule yet, prompt a suggestion."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv(_REPEATED_OVERRIDE_ROWS))
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        categorize(self.connection)

    def _cli(self, *args: str) -> None:
        result = _RUNNER.invoke(app, [*args, "--database", str(self.database)])
        self.assertEqual(result.exit_code, 0, result.output)

    def _generation(self) -> str:
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        return current_generation(readonly)["generation_token"]

    def _anchors(self, merchant: str) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT e.natural_anchor FROM ledger_entries e JOIN merchant_observations o "
            "ON o.entry_id = e.id WHERE o.merchant_normalized = ? ORDER BY e.effective_at",
            (merchant,),
        ).fetchall()]

    def test_three_agreeing_overrides_with_no_rule_suggest_one(self) -> None:
        for anchor in self._anchors("PIZZA PALACE")[:3]:
            self._cli("review", "categorize", anchor, "--as", "Food & Dining / Dining Out", "--reason", "eaten there")
        refresh_ledger(self.database)
        result = rule_suggestions(self.database, generation=self._generation())
        self.assertEqual(len(result["suggestions"]), 1)
        suggestion = result["suggestions"][0]
        self.assertEqual(suggestion["merchant_key"], "PIZZA PALACE")
        self.assertEqual(suggestion["category"], "Food & Dining / Dining Out")
        self.assertEqual(suggestion["occurrences"], 3)
        self.assertEqual(suggestion["match"], {"merchant_canonical": "PIZZA PALACE"})

    def test_two_agreeing_overrides_do_not_suggest_yet(self) -> None:
        for anchor in self._anchors("PIZZA PALACE")[:2]:
            self._cli("review", "categorize", anchor, "--as", "Food & Dining / Dining Out", "--reason", "eaten there")
        refresh_ledger(self.database)
        result = rule_suggestions(self.database, generation=self._generation())
        self.assertEqual(result["suggestions"], [])

    def test_disagreeing_overrides_suggest_nothing(self) -> None:
        """Three overrides agreeing plus one disagreeing is not agreement: the
        merchant has no single answer, so it must produce no suggestion rather
        than one banner per category (both would share the merchant key)."""
        anchors = self._anchors("PIZZA PALACE")
        for anchor in anchors[:3]:
            self._cli("review", "categorize", anchor, "--as", "Food & Dining / Dining Out", "--reason", "eaten there")
        self._cli("review", "categorize", anchors[3], "--as", "Food & Dining / Groceries", "--reason", "takeaway run")
        refresh_ledger(self.database)
        result = rule_suggestions(self.database, generation=self._generation())
        self.assertEqual(result["suggestions"], [])

    def test_suggestion_names_the_merchant_by_its_display_name(self) -> None:
        self._cli("rules", "alias", "PIZZA PALACE", "--pattern", "exact:PIZZA PALACE",
                  "--display", "Pizza Palace", "--reason", "readable name")
        for anchor in self._anchors("PIZZA PALACE")[:3]:
            self._cli("review", "categorize", anchor, "--as", "Food & Dining / Dining Out", "--reason", "eaten there")
        refresh_ledger(self.database)
        suggestion = rule_suggestions(self.database, generation=self._generation())["suggestions"][0]
        self.assertEqual(suggestion["merchant_key"], "PIZZA PALACE")
        self.assertEqual(suggestion["display_label"], "Pizza Palace")
        self.assertIn("Pizza Palace", suggestion["label"])

    def test_no_suggestion_once_a_rule_already_exists(self) -> None:
        self._cli("rules", "add", "--merchant", "PIZZA PALACE",
                  "--category", "Food & Dining / Dining Out", "--reason", "already ruled")
        for anchor in self._anchors("PIZZA PALACE")[:3]:
            self._cli("review", "categorize", anchor, "--as", "Food & Dining / Dining Out", "--reason", "eaten there")
        refresh_ledger(self.database)
        result = rule_suggestions(self.database, generation=self._generation())
        self.assertEqual(result["suggestions"], [])


_SORT_BY_AMOUNT_ROWS = [
    ["Topup", "Current", "2026-01-01 09:00:00", "2026-01-01 09:00:01", "Bank Transfer", "1000.00", "0.00", "SGD", "COMPLETED", "1000.00"],
    ["Card Payment", "Current", "2026-01-02 09:00:00", "2026-01-02 09:00:01", "FREQUENT SMALL SHOP", "-5.00", "0.00", "SGD", "COMPLETED", "995.00"],
    ["Card Payment", "Current", "2026-01-03 09:00:00", "2026-01-03 09:00:01", "FREQUENT SMALL SHOP", "-5.00", "0.00", "SGD", "COMPLETED", "990.00"],
    ["Card Payment", "Current", "2026-01-04 09:00:00", "2026-01-04 09:00:01", "FREQUENT SMALL SHOP", "-5.00", "0.00", "SGD", "COMPLETED", "985.00"],
    ["Card Payment", "Current", "2026-01-05 09:00:00", "2026-01-05 09:00:01", "FREQUENT SMALL SHOP", "-5.00", "0.00", "SGD", "COMPLETED", "980.00"],
    ["Card Payment", "Current", "2026-01-06 09:00:00", "2026-01-06 09:00:01", "RARE BIG PURCHASE", "-500.00", "0.00", "SGD", "COMPLETED", "480.00"],
]


class CandidateSortTests(unittest.TestCase):
    """Step 6g: "Largest total first" ranks by observed amount, not active count."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv(_SORT_BY_AMOUNT_ROWS))
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        categorize(self.connection)
        refresh_ledger(self.database)

    def _generation(self) -> str:
        readonly = connect_readonly(self.database)
        self.addCleanup(readonly.close)
        return current_generation(readonly)["generation_token"]

    def test_default_sort_ranks_active_count_first(self) -> None:
        page = categorization_candidates(self.database, generation=self._generation(), kind="merchant")
        self.assertEqual(page["sort"], "active")
        self.assertEqual(page["candidates"][0]["label"], "FREQUENT SMALL SHOP")

    def test_amount_sort_ranks_the_largest_total_first(self) -> None:
        page = categorization_candidates(self.database, generation=self._generation(), kind="merchant", sort="amount")
        self.assertEqual(page["sort"], "amount")
        self.assertEqual(page["candidates"][0]["label"], "RARE BIG PURCHASE")
        self.assertEqual(page["candidates"][0]["total_abs_amount"], {"SGD": "500.00"})
        self.assertEqual(page["candidates"][1]["total_abs_amount"], {"SGD": "20.00"})

    def test_unknown_sort_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            categorization_candidates(self.database, generation=self._generation(), sort="nonsense")


class MissingMerchantLabelTests(unittest.TestCase):
    """Step 7 polish: a group with no merchant, PayNow recipient or description
    shows the account's display name, not its raw id."""

    def _row(self, **overrides: object) -> dict[str, object]:
        base = {
            "merchant_canonical": None, "merchant_normalized": None, "display_name": None,
            "paynow_to": None, "description_text": None, "account_id": "acct_123",
        }
        base.update(overrides)
        return base

    def test_uses_the_account_display_name_when_available(self) -> None:
        kind, key, label = _candidate_key(self._row(account_display_name="DBS Everyday"))
        self.assertEqual(kind, "missing_merchant")
        self.assertEqual(key, "acct_123")
        self.assertEqual(label, "Missing merchant — DBS Everyday")

    def test_falls_back_to_the_account_id_when_no_account_display_name_key_exists(self) -> None:
        _kind, _key, label = _candidate_key(self._row())
        self.assertEqual(label, "Missing merchant — acct_123")

    def test_falls_back_to_the_account_id_when_the_display_name_is_none(self) -> None:
        _kind, _key, label = _candidate_key(self._row(account_display_name=None))
        self.assertEqual(label, "Missing merchant — acct_123")


if __name__ == "__main__":
    unittest.main()
