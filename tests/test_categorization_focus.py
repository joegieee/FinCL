"""Grouped review's quick-discovery focuses and combined-completeness
measure (PLAN-unified-transaction-classification.md Step 5).

Reuses the synthetic ledger and command helpers already built for
``fincl.classification`` (Step 1) so these tests exercise the same evaluator
the shared editor uses, rather than a second reimplementation.
"""
from __future__ import annotations

from fincl.categorization_queries import (
    categorization_candidate_detail,
    categorization_candidates,
    categorization_summary,
)
from fincl.classification import ClassificationDraft, FieldEdit
from fincl.db import connect_readonly
from fincl.generation import current_generation
from fincl.refresh import refresh_ledger
from tests.test_classification import ClassificationTestBase


class FocusDiscoveryTests(ClassificationTestBase):
    def _generation(self) -> str:
        connection = connect_readonly(self.database)
        try:
            return current_generation(connection)["generation_token"]
        finally:
            connection.close()

    def test_category_focus_is_unchanged_from_before_step_5(self) -> None:
        generation = self._generation()
        page = categorization_candidates(self.database, generation=generation)
        self.assertEqual(page["focus"], "category")
        self.assertTrue(page["candidates"])
        for card in page["candidates"]:
            self.assertNotIn("spending_details", card["candidate_id"])

    def test_category_queue_ignores_reporting_dates_and_agrees_with_all_time_summary(self) -> None:
        """Milestone 0 contract: reporting dates must not hide selected history."""
        generation = self._generation()
        summary = categorization_summary(self.database, generation=generation)
        all_time = categorization_candidates(self.database, generation=generation, focus="category")
        queue_records = sum(
            len(categorization_candidate_detail(
                self.database, generation=generation, candidate_id=card["candidate_id"], focus="category",
            )["records"])
            for card in all_time["candidates"]
        )
        self.assertEqual(queue_records, summary["counts"].get("unclassified_active", 0) + summary["counts"].get("unclassified_inactive", 0))

    def test_candidate_detail_exposes_authoritative_per_row_applicability(self) -> None:
        """Milestone 0 contract for the Milestone 2 row shape."""
        generation = self._generation()
        card = categorization_candidates(self.database, generation=generation, focus="category")["candidates"][0]
        record = categorization_candidate_detail(
            self.database, generation=generation, candidate_id=card["candidate_id"], focus="category",
        )["records"][0]
        self.assertIn("detail_bucket", record)
        self.assertEqual(set(record["applicable_fields"]), {"category", "cost_behavior", "need", "incoming_type"})

    def test_alias_aware_regrouping_forms_one_candidate_after_refresh(self) -> None:
        """Current regrouping behavior to preserve when canonical merge is added."""
        self._decide(
            kind="MERCHANT_ALIAS_SET", anchors={"canonical": "Everyday shops"},
            payload={"patterns": [{"exact": "CORNER SHOP"}, {"exact": "BISTRO"}],
                     "display_name": "Everyday shops", "reason": "synthetic merge"},
        )
        refresh_ledger(self.database)
        generation = self._generation()
        page = categorization_candidates(self.database, generation=generation, focus="category")
        cards = [card for card in page["candidates"] if card["label"] == "Everyday shops"]
        self.assertEqual(len(cards), 1)
        detail = categorization_candidate_detail(
            self.database, generation=generation, candidate_id=cards[0]["candidate_id"], focus="category",
        )
        self.assertEqual({record["merchant_normalized"] for record in detail["records"]}, {"CORNER SHOP", "BISTRO"})

    def test_derived_focus_groups_show_the_display_name_not_the_raw_key(self) -> None:
        """F2.5 regression: `_derived_focus_groups` hardcoded `display_name:
        None`, so the spending-details/incoming/conflicts queues showed a raw
        merchant key even when an alias display name existed."""
        self._decide(
            kind="MERCHANT_ALIAS_SET", anchors={"canonical": "CORNER SHOP"},
            payload={"patterns": [{"exact": "CORNER SHOP"}], "display_name": "The Corner Shop", "reason": "rename"},
        )
        anchor = self._anchor("Corner Shop", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Cash / Cash Withdrawal — Purpose Unknown"),
            reason="categorize only",
        ))
        self._commit(result["commands"])
        refresh_ledger(self.database)
        generation = self._generation()
        details_page = categorization_candidates(self.database, generation=generation, focus="spending_details")
        labels = {card["label"] for card in details_page["candidates"]}
        self.assertIn("The Corner Shop", labels)
        self.assertNotIn("CORNER SHOP", labels)

    def test_spending_details_focus_finds_a_categorized_but_unresolved_row_across_the_full_result_set(self) -> None:
        # Give one Corner Shop entry a category outside the built-in
        # cost/need default map (so it drops out of the "category" focus,
        # while its spending pattern/need stay "unresolved" by design --
        # baseline/roles.py's own documented behavior for such categories).
        anchor = self._anchor("Corner Shop", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Cash / Cash Withdrawal — Purpose Unknown"),
            reason="categorize only",
        ))
        self._commit(result["commands"])
        refresh_ledger(self.database)
        generation = self._generation()

        category_page = categorization_candidates(self.database, generation=generation, focus="category")
        category_anchors = {
            record["anchor"]
            for card in category_page["candidates"]
            for record in categorization_candidate_detail(
                self.database, generation=generation, candidate_id=card["candidate_id"], focus="category",
            )["records"]
        }
        self.assertNotIn(anchor, category_anchors, "a categorized row should no longer need the category focus")

        details_page = categorization_candidates(self.database, generation=generation, focus="spending_details")
        self.assertEqual(details_page["focus"], "spending_details")
        details_anchors = {
            record["anchor"]
            for card in details_page["candidates"]
            for record in categorization_candidate_detail(
                self.database, generation=generation, candidate_id=card["candidate_id"], focus="spending_details",
            )["records"]
        }
        self.assertIn(anchor, details_anchors)

    def test_spending_details_focus_excludes_a_fully_resolved_transaction(self) -> None:
        anchor = self._anchor("Corner Shop", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), category=FieldEdit("set", "Food & Dining / Groceries"),
            cost_behavior=FieldEdit("set", "variable_routine"), need=FieldEdit("set", "essential"),
            reason="everyday groceries",
        ))
        self._commit(result["commands"])
        refresh_ledger(self.database)
        generation = self._generation()
        details_page = categorization_candidates(self.database, generation=generation, focus="spending_details")
        details_anchors = {
            record["anchor"]
            for card in details_page["candidates"]
            for record in categorization_candidate_detail(
                self.database, generation=generation, candidate_id=card["candidate_id"], focus="spending_details",
            )["records"]
        }
        self.assertNotIn(anchor, details_anchors)

    def test_incoming_focus_finds_an_unclassified_credit(self) -> None:
        anchor = self._anchor("Client Payment", nth=0)
        generation = self._generation()
        incoming_page = categorization_candidates(self.database, generation=generation, focus="incoming")
        self.assertEqual(incoming_page["focus"], "incoming")
        incoming_anchors = {
            record["anchor"]
            for card in incoming_page["candidates"]
            for record in categorization_candidate_detail(
                self.database, generation=generation, candidate_id=card["candidate_id"], focus="incoming",
            )["records"]
        }
        self.assertIn(anchor, incoming_anchors)

    def test_incoming_focus_excludes_a_transaction_once_classified(self) -> None:
        anchor = self._anchor("Client Payment", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(anchor,), incoming_type=FieldEdit("set", "income"), reason="salary",
        ))
        self._commit(result["commands"])
        refresh_ledger(self.database)
        generation = self._generation()
        incoming_page = categorization_candidates(self.database, generation=generation, focus="incoming")
        incoming_anchors = {
            record["anchor"]
            for card in incoming_page["candidates"]
            for record in categorization_candidate_detail(
                self.database, generation=generation, candidate_id=card["candidate_id"], focus="incoming",
            )["records"]
        }
        self.assertNotIn(anchor, incoming_anchors)

    def test_unknown_focus_is_refused(self) -> None:
        from fincl.errors import FinCLError

        generation = self._generation()
        with self.assertRaises(FinCLError):
            categorization_candidates(self.database, generation=generation, focus="not-a-real-focus")

    def test_combined_completeness_excludes_fields_that_do_not_apply(self) -> None:
        """An incoming receipt does not need Essential/Discretionary to be
        complete (PLAN §10); a fully classified spending trio also counts."""
        spending_anchor = self._anchor("Corner Shop", nth=0)
        income_anchor = self._anchor("Client Payment", nth=0)
        result = self._evaluate(ClassificationDraft(
            anchors=(spending_anchor,), category=FieldEdit("set", "Food & Dining / Groceries"),
            cost_behavior=FieldEdit("set", "variable_routine"), need=FieldEdit("set", "essential"),
            reason="everyday groceries",
        ))
        self._commit(result["commands"])
        result = self._evaluate(ClassificationDraft(
            anchors=(income_anchor,), incoming_type=FieldEdit("set", "income"), reason="salary",
        ))
        self._commit(result["commands"])
        refresh_ledger(self.database)
        generation = self._generation()
        summary = categorization_summary(self.database, generation=generation)
        combined = summary["combined_completeness"]
        self.assertGreaterEqual(combined["numerator"], 2)
        self.assertGreaterEqual(combined["denominator"], combined["numerator"])
        # Existing category-only measures are untouched by the new field.
        self.assertIn("active_categorization_denominator", summary)
        self.assertIn("progress_numerator", summary)
