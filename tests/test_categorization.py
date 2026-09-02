"""Phase 4 Track A — the frozen taxonomy and its migration invariants.

The taxonomy is a versioned code constant (Confirmed 28, DESIGN-categorization
§3). These tests hold the two invariants that keep ``CATEGORY_MIGRATIONS``
honest across a version bump (§3.1) and check the resolver's three outcomes:
current category, migrated category, unknown string.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fincl import common
from fincl.categorization import (
    canonical_amount_band,
    canonical_match,
    rule_id_for,
    validate_alias_patterns,
    validate_payload,
)
from fincl.common import (
    CATEGORY_MIGRATIONS,
    CATEGORY_TAXONOMY,
    TAXONOMY_VERSION,
    category_strings,
    format_category,
    resolve_category,
)
from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import entry_anchor, replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from tests.revolut_pdf import revolut_pdf_bytes


class TaxonomyShapeTests(unittest.TestCase):
    def test_version_string_is_stable(self) -> None:
        self.assertEqual(TAXONOMY_VERSION, "taxonomy-2026-v1")

    def test_every_group_has_at_least_one_category(self) -> None:
        for group, categories in CATEGORY_TAXONOMY.items():
            self.assertTrue(categories, f"group {group!r} has no categories")

    def test_category_strings_are_unique(self) -> None:
        pairs = [
            format_category(group, category)
            for group, categories in CATEGORY_TAXONOMY.items()
            for category in categories
        ]
        self.assertEqual(sorted(pairs), sorted(set(pairs)))

    def test_required_structural_categories_exist(self) -> None:
        strings = category_strings()
        self.assertIn("Unclassified / Unclassified", strings)
        for internal in (
            "Internal / Transfer",
            "Internal / Card Repayment",
            "Internal / Currency Exchange",
            "Internal / Wallet Top-up",
        ):
            self.assertIn(internal, strings)

    def test_no_group_or_category_name_contains_the_separator(self) -> None:
        for group, categories in CATEGORY_TAXONOMY.items():
            self.assertNotIn(" / ", group)
            for category in categories:
                self.assertNotIn(" / ", category)


class MigrationInvariantTests(unittest.TestCase):
    """The two invariants from DESIGN-categorization §3.1 — CI, not runtime."""

    def test_every_migration_target_is_a_current_category(self) -> None:
        current = category_strings()
        for retired, target in CATEGORY_MIGRATIONS.items():
            self.assertIn(
                target, current, f"migration {retired!r} -> {target!r} dangles"
            )
            self.assertNotIn(
                target,
                CATEGORY_MIGRATIONS,
                f"migration target {target!r} is itself migrated (chain)",
            )

    def test_every_removed_category_has_a_migration_entry(self) -> None:
        removed = common._PREVIOUS_TAXONOMY_STRINGS - category_strings()
        for gone in removed:
            self.assertIn(
                gone,
                CATEGORY_MIGRATIONS,
                f"category {gone!r} was removed without a CATEGORY_MIGRATIONS entry",
            )


class ResolveCategoryTests(unittest.TestCase):
    def test_current_category_resolves_unflagged(self) -> None:
        self.assertEqual(
            resolve_category("Food & Dining / Groceries"),
            ("Food & Dining", "Groceries", None),
        )

    def test_unknown_category_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            resolve_category("Nonsense / Not Real")

    def test_migrated_category_resolves_to_its_target_and_is_flagged(self) -> None:
        with patch.dict(
            CATEGORY_MIGRATIONS,
            {"Old Group / Old Category": "Food & Dining / Groceries"},
        ):
            result = resolve_category("Old Group / Old Category")
        self.assertEqual(
            result, ("Food & Dining", "Groceries", "Old Group / Old Category")
        )

    def test_migration_to_a_nonexistent_target_is_refused(self) -> None:
        with (
            patch.dict(CATEGORY_MIGRATIONS, {"Old / Thing": "Still Not / Real"}),
            self.assertRaises(FinCLError),
        ):
            resolve_category("Old / Thing")


class RuleIdAndBandTests(unittest.TestCase):
    def test_rule_id_is_16_hex_and_deterministic(self) -> None:
        match = {"merchant_canonical": "Cornermart"}
        rid = rule_id_for(match)
        self.assertEqual(len(rid), 16)
        self.assertEqual(rid, rule_id_for({"merchant_canonical": "Cornermart"}))

    def test_band_scale_variants_canonicalize_to_one_rule_id(self) -> None:
        a = {"paynow_to": "ACME INSTITUTE", "amount_band": {"currency": "SGD", "min": {"coefficient": 10, "scale": 0}}}
        b = {"paynow_to": "ACME INSTITUTE", "amount_band": {"currency": "SGD", "min": {"coefficient": 1000, "scale": 2}}}
        self.assertEqual(rule_id_for(a), rule_id_for(b))

    def test_band_requires_currency_and_a_bound(self) -> None:
        with self.assertRaises(FinCLError):
            canonical_amount_band({"min": {"coefficient": 1, "scale": 0}})
        with self.assertRaises(FinCLError):
            canonical_amount_band({"currency": "SGD"})

    def test_band_rejects_negative_and_inverted(self) -> None:
        with self.assertRaises(FinCLError):
            canonical_amount_band({"currency": "SGD", "min": {"coefficient": -1, "scale": 0}})
        with self.assertRaises(FinCLError):
            canonical_amount_band(
                {"currency": "SGD",
                 "min": {"coefficient": 200, "scale": 0},
                 "max": {"coefficient": 10, "scale": 0}}
            )


class MatchAndAliasValidationTests(unittest.TestCase):
    def test_match_needs_exactly_one_predicate(self) -> None:
        with self.assertRaises(FinCLError):
            canonical_match({"merchant_canonical": "X", "paynow_to": "Y"})
        with self.assertRaises(FinCLError):
            canonical_match({})

    def test_short_description_prefix_is_refused(self) -> None:
        with self.assertRaises(FinCLError):
            canonical_match({"description_prefix": "AB"})

    def test_account_only_allowed_with_description_contains(self) -> None:
        canonical_match({"description_contains": "ACME RIDES", "account": "acct_x"})
        with self.assertRaises(FinCLError):
            canonical_match({"merchant_canonical": "X", "account": "acct_x"})

    def test_alias_prefix_min_length_and_normal_form(self) -> None:
        with self.assertRaises(FinCLError):
            validate_alias_patterns([{"prefix": "CORNE"}])
        with self.assertRaises(FinCLError):
            validate_alias_patterns([{"exact": "acme supermarket"}])  # not uppercased
        with self.assertRaises(FinCLError):
            validate_alias_patterns([{"regex": "("}])  # unbalanced
        with self.assertRaises(FinCLError):
            validate_alias_patterns([{"exact": "A"}, {"exact": "A"}])  # duplicate

    def test_rule_payload_rejects_a_mismatched_rule_id(self) -> None:
        with self.assertRaises(FinCLError):
            validate_payload(
                "CATEGORY_RULE_SET",
                {"rule_id": "0" * 16},
                {"match": {"paynow_to": "ACME INSTITUTE"}, "category": "Education / Education Services", "reason": "r"},
            )


def _csv_bytes() -> bytes:
    return revolut_pdf_bytes(
        [
            ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01",
             "ACME SUPERMARKET", "-10.00", "0.00", "SGD", "COMPLETED", "90.00"],
        ]
    )


class RuleProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.journal = default_journal_path(self.database)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def _append(self, kind: str, anchors: dict[str, str], payload: dict[str, object]) -> None:
        append_decision(
            self.journal, decision_kind=kind, anchors=anchors, payload=payload,
            policy_version="category-engine-2026-v1", snapshot=f"{kind} {anchors}",
        )

    def test_rule_set_projects_then_reissue_supersedes_then_retire_removes(self) -> None:
        match = {"merchant_canonical": "ACME SUPERMARKET"}
        rid = rule_id_for(match)
        self._append("CATEGORY_RULE_SET", {"rule_id": rid},
                     {"match": match, "category": "Food & Dining / Groceries", "reason": "first"})
        replay_journal(self.connection)
        row = self.connection.execute(
            "SELECT group_name, category, reason, journal_seq FROM category_rules WHERE rule_id = ?", (rid,)
        ).fetchone()
        self.assertEqual((row["group_name"], row["category"]), ("Food & Dining", "Groceries"))

        self._append("CATEGORY_RULE_SET", {"rule_id": rid},
                     {"match": match, "category": "Shopping / General Shopping", "reason": "second"})
        replay_journal(self.connection)
        rows = self.connection.execute(
            "SELECT category, reason FROM category_rules WHERE rule_id = ?", (rid,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["category"], rows[0]["reason"]), ("General Shopping", "second"))

        self._append("CATEGORY_RULE_RETIRE", {"rule_id": rid}, {"reason": "gone"})
        replay_journal(self.connection)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM category_rules WHERE rule_id = ?", (rid,)
            ).fetchone()
        )

    def test_alias_set_and_retire(self) -> None:
        self._append("MERCHANT_ALIAS_SET", {"canonical": "Cornermart"},
                     {"patterns": [{"exact": "CORNERMART"}, {"prefix": "CORNERMART-"}],
                      "display_name": "Cornermart", "reason": "brand"})
        replay_journal(self.connection)
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM merchant_aliases WHERE canonical = 'Cornermart'"
            ).fetchone()
        )
        self._append("MERCHANT_ALIAS_RETIRE", {"canonical": "Cornermart"}, {"reason": "merged"})
        replay_journal(self.connection)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM merchant_aliases WHERE canonical = 'Cornermart'"
            ).fetchone()
        )

    def test_display_rename_keeps_canonical_identity_and_complete_patterns(self) -> None:
        patterns = [{"exact": "ACME SUPERMARKET"}, {"prefix": "ACME MARKET"}]
        self._append("MERCHANT_ALIAS_SET", {"canonical": "Acme"},
                     {"patterns": patterns, "display_name": "Acme", "reason": "initial"})
        self._append("MERCHANT_ALIAS_SET", {"canonical": "Acme"},
                     {"patterns": patterns, "display_name": "Acme Singapore", "reason": "rename"})
        replay_journal(self.connection)
        row = self.connection.execute(
            "SELECT canonical, patterns_json, display_name FROM merchant_aliases WHERE canonical = 'Acme'"
        ).fetchone()
        self.assertEqual((row["canonical"], row["display_name"]), ("Acme", "Acme Singapore"))
        self.assertEqual(row["patterns_json"], '[{"exact":"ACME SUPERMARKET"},{"prefix":"ACME MARKET"}]')

    def test_additive_alias_update_omitting_display_name_preserves_it(self) -> None:
        """F2.2 regression: an inbox save that only adds a statement variant
        must not silently wipe an established display name by omitting the
        key entirely. An explicit `display_name: null` still clears it."""
        self._append("MERCHANT_ALIAS_SET", {"canonical": "Acme"},
                     {"patterns": [{"exact": "ACME SUPERMARKET"}], "display_name": "Acme Singapore", "reason": "initial"})
        self._append("MERCHANT_ALIAS_SET", {"canonical": "Acme"},
                     {"patterns": [{"exact": "ACME MARKET"}], "reason": "add variant"})
        replay_journal(self.connection)
        row = self.connection.execute(
            "SELECT display_name, patterns_json FROM merchant_aliases WHERE canonical = 'Acme'"
        ).fetchone()
        self.assertEqual(row["display_name"], "Acme Singapore")
        self.assertIn("ACME MARKET", row["patterns_json"])
        self._append("MERCHANT_ALIAS_SET", {"canonical": "Acme"},
                     {"patterns": [{"exact": "ACME MARKET"}], "display_name": None, "reason": "clear display name"})
        replay_journal(self.connection)
        cleared = self.connection.execute(
            "SELECT display_name FROM merchant_aliases WHERE canonical = 'Acme'"
        ).fetchone()
        self.assertIsNone(cleared["display_name"])

    def test_alias_variant_update_unions_visible_existing_patterns(self) -> None:
        """A set command used as an edit must not silently discard an old variant."""
        self._append("MERCHANT_ALIAS_SET", {"canonical": "Acme"},
                     {"patterns": [{"exact": "ACME SUPERMARKET"}], "reason": "initial"})
        self._append("MERCHANT_ALIAS_SET", {"canonical": "Acme"},
                     {"patterns": [{"exact": "ACME MARKET"}], "reason": "add variant"})
        replay_journal(self.connection)
        patterns = self.connection.execute(
            "SELECT patterns_json FROM merchant_aliases WHERE canonical = 'Acme'"
        ).fetchone()[0]
        self.assertIn("ACME SUPERMARKET", patterns)

    def test_override_binds_to_a_live_entry_and_dangles_otherwise(self) -> None:
        entry_id = self.connection.execute(
            "SELECT id FROM ledger_entries LIMIT 1"
        ).fetchone()[0]
        anchor = entry_anchor(self.connection, entry_id)
        self._append("CATEGORY_OVERRIDE", {"entry": anchor},
                     {"category": "Food & Dining / Groceries", "reason": "receipt says so"})
        self._append("CATEGORY_OVERRIDE", {"entry": "missing-evidence:csv:404"},
                     {"category": "Shopping / Electronics", "reason": "dangling"})
        replay_journal(self.connection)
        bound_row = self.connection.execute(
            "SELECT bound, group_name FROM category_overrides WHERE entry_anchor = ?", (anchor,)
        ).fetchone()
        self.assertEqual((bound_row["bound"], bound_row["group_name"]), (1, "Food & Dining"))
        dangling_row = self.connection.execute(
            "SELECT bound, dangling_reason FROM category_overrides WHERE entry_anchor = ?",
            ("missing-evidence:csv:404",),
        ).fetchone()
        self.assertEqual(dangling_row["bound"], 0)
        self.assertIsNotNone(dangling_row["dangling_reason"])

    def test_replay_refuses_a_forged_rule_id(self) -> None:
        self._append("CATEGORY_RULE_SET", {"rule_id": "deadbeefdeadbeef"},
                     {"match": {"paynow_to": "ACME INSTITUTE"}, "category": "Education / Education Services", "reason": "r"})
        with self.assertRaises(FinCLError) as caught:
            replay_journal(self.connection)
        self.assertIn("record 1", str(caught.exception))

    def test_rule_naming_a_migrated_category_records_the_debt(self) -> None:
        match = {"merchant_canonical": "OLD MERCHANT"}
        rid = rule_id_for(match)
        with patch.dict(CATEGORY_MIGRATIONS, {"Old / Cat": "Food & Dining / Groceries"}):
            self._append("CATEGORY_RULE_SET", {"rule_id": rid},
                         {"match": match, "category": "Old / Cat", "reason": "legacy"})
            replay_journal(self.connection)
            row = self.connection.execute(
                "SELECT category, migrated_from FROM category_rules WHERE rule_id = ?", (rid,)
            ).fetchone()
        self.assertEqual((row["category"], row["migrated_from"]), ("Groceries", "Old / Cat"))


if __name__ == "__main__":
    unittest.main()
