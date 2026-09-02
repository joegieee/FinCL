"""Phase 4 Track A step 3 — field projection and merchant normalization.

``entry-projection-2026-v1`` derives ``description_text`` / ``merchant_raw`` /
``paynow_to`` from the evidence row; ``merchant-norm-2026-v1`` normalizes and
canonicalizes. NULLs propagate (never ``""``), and alias resolution fails closed
on regex overlap (§4.1–§4.4).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fincl.db import connect_sqlite, initialize_schema
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.document_service import DocumentImportService
from fincl.errors import FinCLError
from fincl.merchant import (
    detect_alias_collisions,
    normalize_merchant,
    project_fields,
    recompute_merchant_observations,
    resolve_canonical,
)
from tests.revolut_pdf import revolut_pdf_bytes

_DBS = "dbs-consolidated-2025-v1"
_REVOLUT = "revolut-pdf-en-sg-2026-v1"
_MARI_CARD = "mari-card-summary-A-B-C+D+E-F+G+H+I-v1"


class FieldProjectionTests(unittest.TestCase):
    def test_revolut_merchant_only_for_card_kinds(self) -> None:
        card = project_fields(source_profile_version=_REVOLUT, event_kind="CARD_PAYMENT",
                              description_raw="ACME SUPERMARKET")
        self.assertEqual(card.merchant_raw, "ACME SUPERMARKET")
        topup = project_fields(source_profile_version=_REVOLUT, event_kind="TOPUP",
                               description_raw="Apple Pay Top-Up")
        self.assertIsNone(topup.merchant_raw)
        self.assertEqual(topup.description_text, "Apple Pay Top-Up")

    def test_null_description_propagates_and_is_not_empty_string(self) -> None:
        p = project_fields(source_profile_version=_REVOLUT, event_kind="CARD_PAYMENT",
                           description_raw=None)
        self.assertEqual((p.description_text, p.merchant_raw, p.paynow_to), (None, None, None))
        blank = project_fields(source_profile_version=_REVOLUT, event_kind="CARD_PAYMENT",
                               description_raw="   \n  ")
        self.assertIsNone(blank.description_text)

    def test_doc_strips_the_leading_transaction_type_run(self) -> None:
        p = project_fields(source_profile_version=_DBS, event_kind="CARD_PAYMENT",
                           description_raw="Debit Card Transaction\nCORNER GROCER\n0002")
        self.assertEqual(p.merchant_raw, "CORNER GROCER 0002")

    def test_doc_paynow_form_has_no_merchant_and_yields_a_recipient(self) -> None:
        description = (
            "Advice FAST Payment / Receipt\nPAYNOW TRANSFER 7000001\n"
            "TO: ACME TRAINING ACADEMY\nINV-0000123456\nOTHER"
        )
        p = project_fields(source_profile_version=_DBS, event_kind="TRANSFER",
                           description_raw=description)
        self.assertIsNone(p.merchant_raw)
        self.assertEqual(p.paynow_to, "ACME TRAINING ACADEMY")

    def test_mari_card_strips_foreign_tail_and_plan_suffix(self) -> None:
        foreign = project_fields(source_profile_version=_MARI_CARD, event_kind="CARD_PAYMENT",
                                 description_raw="CAFE SOCIAL USD 12.34")
        self.assertEqual(foreign.merchant_raw, "CAFE SOCIAL")
        plan = project_fields(source_profile_version=_MARI_CARD, event_kind="CARD_PAYMENT",
                              description_raw="GADGET STORE 3/12 Instalment")
        self.assertEqual(plan.merchant_raw, "GADGET STORE")
        payment = project_fields(source_profile_version=_MARI_CARD, event_kind="PAYMENT",
                                 description_raw="Payment Received - Thank You")
        self.assertIsNone(payment.merchant_raw)


class NormalizationTests(unittest.TestCase):
    def test_legal_suffix_and_case_and_whitespace(self) -> None:
        self.assertEqual(normalize_merchant("Acme  Supermarket   Pte Ltd"), "ACME SUPERMARKET")

    def test_location_tail_after_dash(self) -> None:
        self.assertEqual(normalize_merchant("ACME LAUNDRY - WESTSIDE"), "ACME LAUNDRY")
        self.assertEqual(normalize_merchant("CAFE SOCIAL - DOWNTOWN"), "CAFE SOCIAL")

    def test_trailing_number_and_country_token(self) -> None:
        self.assertEqual(normalize_merchant("FOOD COURT 12345"), "FOOD COURT")
        self.assertEqual(normalize_merchant("BUBBLE TEA SINGAPORE"), "BUBBLE TEA")

    def test_none_in_none_out(self) -> None:
        self.assertIsNone(normalize_merchant(None))


class CanonicalizationTests(unittest.TestCase):
    def test_exact_beats_prefix_and_longest_prefix_wins(self) -> None:
        aliases = {
            "Cornermart": [{"prefix": "CORNERMART"}],
            "Cornermart Main St": [{"exact": "CORNERMART-MAIN ST"}],
        }
        exact = resolve_canonical("CORNERMART-MAIN ST", aliases)
        self.assertEqual((exact.merchant_canonical, exact.alias_applied), ("Cornermart Main St", True))
        aliases2 = {"Short": [{"prefix": "ACME "}], "Long": [{"prefix": "ACME SUPER"}]}
        pick = resolve_canonical("ACME SUPERMARKET", aliases2)
        self.assertEqual(pick.merchant_canonical, "Long")

    def test_no_match_returns_normalized_itself(self) -> None:
        got = resolve_canonical("UNSEEN MERCHANT", {})
        self.assertEqual(
            (got.merchant_canonical, got.alias_applied, got.alias_ambiguous),
            ("UNSEEN MERCHANT", False, False),
        )

    def test_regex_overlap_fails_closed(self) -> None:
        aliases = {
            "A": [{"regex": r"CORNER.*"}],
            "B": [{"regex": r".*MART"}],
        }
        got = resolve_canonical("CORNER MART", aliases)
        self.assertTrue(got.alias_ambiguous)
        self.assertFalse(got.alias_applied)
        self.assertEqual(got.merchant_canonical, "CORNER MART")

    def test_duplicate_pattern_across_canonicals_is_a_collision(self) -> None:
        with self.assertRaises(FinCLError):
            detect_alias_collisions({"A": [{"exact": "SHARED"}], "B": [{"exact": "SHARED"}]})
        # Same canonical, repeated pattern: not a collision.
        detect_alias_collisions({"A": [{"exact": "SHARED"}, {"exact": "SHARED"}]})


def _csv_bytes() -> bytes:
    return revolut_pdf_bytes(
        [
            ["Topup", "Current", "2026-01-01 10:00:00", "2026-01-01 10:00:01",
             "Bank Transfer", "100.00", "0.00", "SGD", "COMPLETED", "100.00"],
            ["Card Payment", "Current", "2026-01-02 11:00:00", "2026-01-02 11:00:01",
             "ACME SUPERMARKET PTE LTD", "-10.00", "0.00", "SGD", "COMPLETED", "90.00"],
        ]
    )


class ObservationRecomputeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / "revolut.csv"
        source.write_bytes(_csv_bytes())
        self.database = self.root / "fincl.sqlite3"
        DocumentImportService.plaintext_phase2(self.database, self.root / "evidence").import_revolut_pdf(source)
        self.connection = connect_sqlite(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)

    def test_recompute_projects_every_selected_entry_and_applies_an_alias(self) -> None:
        append_decision(
            default_journal_path(self.database),
            decision_kind="MERCHANT_ALIAS_SET",
            anchors={"canonical": "Acme Supermarket"},
            payload={"patterns": [{"exact": "ACME SUPERMARKET"}], "reason": "brand"},
            policy_version="category-engine-2026-v1",
            snapshot="alias",
        )
        replay_journal(self.connection)
        count = recompute_merchant_observations(self.connection)
        self.assertEqual(count, 2)
        card = self.connection.execute(
            "SELECT * FROM merchant_observations o JOIN ledger_entries e ON e.id = o.entry_id "
            "WHERE e.event_kind = 'CARD_PAYMENT'"
        ).fetchone()
        self.assertEqual(card["merchant_normalized"], "ACME SUPERMARKET")
        self.assertEqual(card["merchant_canonical"], "Acme Supermarket")
        self.assertEqual(card["alias_applied"], 1)
        topup = self.connection.execute(
            "SELECT * FROM merchant_observations o JOIN ledger_entries e ON e.id = o.entry_id "
            "WHERE e.event_kind = 'TOPUP'"
        ).fetchone()
        self.assertIsNone(topup["merchant_raw"])
        self.assertIsNone(topup["merchant_canonical"])


if __name__ == "__main__":
    unittest.main()
