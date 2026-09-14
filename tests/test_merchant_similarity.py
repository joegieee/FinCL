"""Pure-function fixtures for the merchant duplicate-suggestion heuristic."""
from __future__ import annotations

import unittest

from fincl.merchant_similarity import levenshtein_ratio, merchants_similar


class MerchantsSimilarTests(unittest.TestCase):
    def test_shared_leading_token_across_pos_noise(self) -> None:
        self.assertTrue(merchants_similar("GRAB*RIDE", "GRAB FOOD SG"))

    def test_shared_leading_token_is_case_insensitive(self) -> None:
        self.assertTrue(merchants_similar("grab*ride123", "GRAB PAY"))

    def test_short_leading_token_does_not_match_on_its_own(self) -> None:
        # "GRAB" (4 chars) meets the threshold; a 3-char shared prefix like
        # "KOP" alone should not -- distinct merchants "KOP MART" and
        # "KOP EXPRESS" would otherwise collide.
        self.assertFalse(merchants_similar("KOP MART", "KOPI EXPRESS"))

    def test_a_short_key_does_not_spuriously_match_a_much_longer_one(self) -> None:
        self.assertFalse(merchants_similar("GRAB", "GRABBER HOLDINGS PTE LTD"))

    def test_near_duplicate_spelling_matches_via_levenshtein_ratio(self) -> None:
        self.assertTrue(merchants_similar("STARBUCKS COFFEE", "STARBUCKS COFEE"))

    def test_unrelated_merchants_do_not_match(self) -> None:
        self.assertFalse(merchants_similar("ACME GROCER", "SHELL SINGAPORE"))

    def test_identical_keys_are_not_considered_similar_to_themselves(self) -> None:
        self.assertFalse(merchants_similar("GRAB", "GRAB"))

    def test_levenshtein_ratio_is_one_for_identical_strings(self) -> None:
        self.assertEqual(levenshtein_ratio("GRAB", "GRAB"), 1.0)

    def test_levenshtein_ratio_is_zero_for_a_totally_different_pair_of_equal_length(self) -> None:
        self.assertEqual(levenshtein_ratio("AAAA", "BBBB"), 0.0)

    def test_levenshtein_ratio_handles_empty_strings(self) -> None:
        self.assertEqual(levenshtein_ratio("", ""), 1.0)
        self.assertEqual(levenshtein_ratio("ABC", ""), 0.0)


if __name__ == "__main__":
    unittest.main()
