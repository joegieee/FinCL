"""Recognising a statement's type from the parsers themselves."""
from __future__ import annotations

import unittest
from pathlib import Path

from fincl.intake import detect_adapter

_FIXTURES = Path(__file__).parent / "fixtures"


class DetectAdapterTests(unittest.TestCase):
    def test_each_supported_statement_is_recognised(self) -> None:
        for name, adapter in (
            ("dbs_multicurrency.pdf", "DBS_CONSOLIDATED"), ("paylah_legacy.pdf", "DBS_PAYLAH"),
            ("maribank_savings.pdf", "MARI_SAVINGS"), ("mari_card.pdf", "MARI_CARD"),
            ("trust_savings.pdf", "TRUST_SAVINGS"), ("revolut_statement.pdf", "REVOLUT_PDF"),
        ):
            with self.subTest(name=name):
                self.assertEqual(detect_adapter((_FIXTURES / name).read_bytes()), (adapter, "recognised"))

    def test_a_damaged_statement_still_goes_to_its_own_parser(self) -> None:
        """It is imported and quarantined exactly as a manual choice would be."""
        for name, adapter in (("revolut_statement_unbalanced.pdf", "REVOLUT_PDF"),
                              ("trust_savings_unbalanced.pdf", "TRUST_SAVINGS"),
                              ("mari_card_unknown_equation.pdf", "MARI_CARD")):
            with self.subTest(name=name):
                self.assertEqual(detect_adapter((_FIXTURES / name).read_bytes()), (adapter, "recognised, with problems"))

    def test_an_unrecognised_file_is_never_guessed(self) -> None:
        adapter, basis = detect_adapter((_FIXTURES / "unclassified.pdf").read_bytes())
        self.assertIsNone(adapter)
        self.assertIn("doesn't match", basis)
        self.assertEqual(detect_adapter(b"not a pdf")[0], None)


if __name__ == "__main__":
    unittest.main()
